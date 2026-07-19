#!/usr/bin/env python3
"""offlane — move an MCP server's read results OFF your LLM context window.

`offlane` is a tiny JSON-RPC client for any MCP server that speaks streamable
HTTP. Instead of a tool result flowing back through the model's context window
(where it rides forward, re-billed, on every subsequent turn), `offlane call`
writes the payload to a file on disk and prints ONLY a byte/record/keys summary.
You then `jq`-project just the fields you need — so only an affirmatively-chosen
excerpt ever enters context. Big reads stop blowing the token budget.

It's a general MCP client: it can call any tool your bearer is authorized for.
The projection-summary default is what makes it a "lane" for reads; writes work
the same way (you just rarely need the guardrail for them).

Design notes (why a bare POST works against most streamable-HTTP servers):
  * Many MCP servers run their StreamableHTTP transport in *stateless* mode
    (a fresh server per request, no session id), so a single self-contained POST
    of a tools/list / tools/call body succeeds with NO `initialize` handshake and
    NO Mcp-Session-Id. We skip the handshake deliberately — in stateless mode it
    is functionally inert (clientInfo from an initialize can't survive to a later
    call, since a different server instance handles it).
  * The reply is usually SSE-framed (JSON responses disabled): the JSON-RPC
    message rides on `data:` line(s), so we parse the SSE frame, not a bare JSON
    body. A bare-JSON reply is also accepted as a fallback.
  * We pin `MCP-Protocol-Version` explicitly (default 2025-03-26, a widely
    supported spec revision) rather than rely on the server default, and fail
    LOUD on a 400/406/415/428 that would signal the server needs a handshake,
    a different protocol version, or a session id (i.e. it isn't stateless).

stdlib-only (urllib/json/argparse) so the installed command has zero runtime deps.

Verbs:
  offlane ls [prefix]                    list tool names + one-line descriptions
  offlane schema <tool> [<tool> ...]     print one (or several, batched) tools' input
                                         JSON schema(s)
  offlane call <tool> <args.json|-> --out PATH [--peek N]
                                         run a tool; write the payload to PATH;
                                         print ONLY a byte/record/keys summary

Env:
  OFFLANE_MCP_URL              (required) the MCP server endpoint, e.g.
                               https://your-host/mcp
  OFFLANE_MCP_TOKEN            bearer token, sent as `Authorization: Bearer …`
                               (omit for an unauthenticated server)
  OFFLANE_MCP_PROTOCOL_VERSION default 2025-03-26
  OFFLANE_SESSION_ID           optional; sent as `X-Offlane-Session` — a
                               per-request attribution seam a server can key
                               telemetry on (inert unless the server reads it)
  OFFLANE_MCP_TIMEOUT          per-request timeout seconds (default 60)
  OFFLANE_MCP_RETRIES          attempts on 502/503/504/reset (default 3)
  OFFLANE_MCP_BACKOFF          base backoff seconds, doubled per attempt (0.5)
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

__version__ = "0.1.0"

# A widely-supported MCP spec revision. We pin it explicitly rather than rely on
# the server's implicit default so a bare POST can't be rejected as "unsupported";
# see the handshake note in the module docstring.
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
_RETRY_STATUS = frozenset({502, 503, 504})

# Shown in `offlane --help`. When driving an LLM agent, point its prompt here for
# the full protocol so the prompt stays thin — keep this the single source of
# usage truth.
_HELP_EPILOG = """\
protocol (reads run off-context — only what you jq-project ever enters context):
  1. offlane schema <tool> [<tool> ...]      ALWAYS before first use of any tool — read the
                                             WHOLE schema (format / filters / projections /
                                             pagination); a skipped lever is silent quality loss.
                                             Batch several names when you know you'll need
                                             them next — one call, catalog fetched once; the
                                             batch prints a JSON object keyed by tool name
  2. offlane call <tool> '<json>' --out FILE writes the payload to FILE; prints ONLY a
                                             bytes/records/keys summary, never the body
  3. jq '<projection>' FILE                  pull ONLY the fields you need; never `cat` FILE

offlane retries transient upstream 502/503/504 + connection resets itself, and prints
server-side validation errors on a non-zero exit (read + fix your args).

env: OFFLANE_MCP_URL (required), OFFLANE_MCP_TOKEN (bearer, if the server authenticates),
OFFLANE_SESSION_ID (optional; see the module header).

examples:
  offlane ls
  offlane schema search_tool
  offlane schema search_tool get_record list_records     # batch: catalog fetched once
  offlane call search_tool '{"limit":3}' --out /tmp/d.json && jq '.[].id' /tmp/d.json
"""


class LaneError(Exception):
    """A user-facing error (bad args, tool error, transport failure)."""


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.url = env.get("OFFLANE_MCP_URL", "")
        self.token = env.get("OFFLANE_MCP_TOKEN", "")
        # `or DEFAULT` (not a get-default) so a present-but-EMPTY value still pins —
        # a blank export must not silently revert to implicit server-default negotiation.
        self.protocol_version = env.get("OFFLANE_MCP_PROTOCOL_VERSION") or DEFAULT_PROTOCOL_VERSION
        self.session_id = env.get("OFFLANE_SESSION_ID", "")
        self.timeout = float(env.get("OFFLANE_MCP_TIMEOUT", "60"))
        self.attempts = int(env.get("OFFLANE_MCP_RETRIES", "3"))
        self.backoff = float(env.get("OFFLANE_MCP_BACKOFF", "0.5"))


# --- transport -------------------------------------------------------------
def _build_headers(config):
    headers = {
        "Content-Type": "application/json",
        # streamable-HTTP requires BOTH be acceptable, else 406/415.
        "Accept": "application/json, text/event-stream",
    }
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    if config.protocol_version:
        headers["MCP-Protocol-Version"] = config.protocol_version
    if config.session_id:
        # The per-request attribution seam (stateless mode can't carry clientInfo).
        headers["X-Offlane-Session"] = config.session_id
    return headers


def _http_post(url, data, headers, timeout):
    """Low-level POST. Returns (status, body_text). Raises HTTPError/URLError.

    This is the single seam tests monkeypatch to simulate 502s, resets, etc.
    """
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        body = resp.read().decode("utf-8", "replace")
    return status, body


def _is_transient(err):
    """Is this transport error worth retrying? Covers connect- AND read-phase."""
    if isinstance(err, urllib.error.HTTPError):
        return err.code in _RETRY_STATUS  # status-classified
    if isinstance(err, ssl.SSLError):
        return False  # TLS/cert errors fail fast (e.g. an untrusted proxy CA)
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", None)
        if isinstance(reason, ssl.SSLError):
            return False
        return isinstance(reason, (ConnectionError, TimeoutError, OSError))
    # Bare read-phase failures: ConnectionResetError/TimeoutError (OSError) or
    # http.client.IncompleteRead (HTTPException) — an upstream load balancer
    # dropping the backend mid-SSE-stream, which urllib does NOT wrap in URLError.
    return isinstance(err, (ConnectionError, TimeoutError, OSError, http.client.HTTPException))


def _post_with_retry(url, data, headers, timeout, attempts, backoff):
    """Retry transient transport failures (a clean 502/503/504 status OR a
    reset/IncompleteRead mid-SSE-read) so a flaky upstream never surfaces."""
    for attempt in range(max(1, attempts)):
        try:
            return _http_post(url, data, headers, timeout)
        except (OSError, http.client.HTTPException) as err:
            # URLError/HTTPError both subclass OSError; this also catches bare
            # connect- and read-phase resets/timeouts/IncompleteRead.
            if _is_transient(err) and attempt < attempts - 1:
                time.sleep(backoff * (2 ** attempt))
                continue
            raise
    raise LaneError("exhausted retries")  # pragma: no cover - loop returns/raises


def _http_error_to_lane(err):
    try:
        detail = err.read().decode("utf-8", "replace")[:400]
    except Exception:  # pragma: no cover - best-effort body read
        detail = ""
    hint = ""
    if err.code in (400, 406, 415, 428):
        hint = (
            " -- a 400/406/415/428 here can mean the server requires an MCP "
            "handshake, a specific MCP-Protocol-Version, or a session id (i.e. it "
            "is not running in stateless mode). offlane assumes a stateless bare "
            "POST; that server may need a full MCP client."
        )
    msg = f"HTTP {err.code} {err.reason} from {getattr(err, 'url', None) or 'MCP endpoint'}{hint}"
    if detail:
        msg += f"\n  body: {detail}"
    return LaneError(msg)


def _parse_sse(body):
    """Parse a streamable-HTTP reply into its JSON-RPC message.

    With JSON responses disabled the reply is text/event-stream and the JSON-RPC
    message rides on `data:` line(s) (joined with newlines within an event). We
    also accept a bare JSON body as a fallback, in case the server responds with
    JSON directly.
    """
    events = []
    cur = []
    for raw in body.splitlines():
        line = raw.rstrip("\r")
        if line == "":  # blank line dispatches the event
            if cur:
                events.append("\n".join(cur))
                cur = []
            continue
        if line.startswith(":"):
            continue  # SSE comment / keep-alive
        if line.startswith("data:"):
            val = line[len("data:"):]
            if val.startswith(" "):  # strip the single optional leading space
                val = val[1:]
            cur.append(val)
        # event:/id:/retry: fields are irrelevant here
    if cur:
        events.append("\n".join(cur))
    for chunk in events:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    stripped = body.strip()
    if stripped:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    raise LaneError("could not parse a JSON-RPC message from the response body")


def _post(method, params, config):
    """The single JSON-RPC chokepoint. Returns the JSON-RPC `result` object."""
    if not config.url:
        raise LaneError(
            "no MCP endpoint set -- export OFFLANE_MCP_URL to your server's "
            "endpoint (e.g. https://your-host/mcp) before calling offlane"
        )
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    headers = _build_headers(config)
    try:
        _status, text = _post_with_retry(
            config.url, body, headers, config.timeout, config.attempts, config.backoff
        )
    except urllib.error.HTTPError as err:
        raise _http_error_to_lane(err)
    except (OSError, http.client.HTTPException) as err:
        # URLError is an OSError subclass; this also covers bare connect-/read-phase
        # transport errors (reset/timeout/IncompleteRead) that escaped retry.
        reason = getattr(err, "reason", None) or err
        raise LaneError(f"network error contacting {config.url}: {reason}")
    msg = _parse_sse(text)
    if isinstance(msg, dict) and msg.get("error"):
        err = msg["error"]
        raise LaneError(f"JSON-RPC error {err.get('code')}: {err.get('message')}")
    if not isinstance(msg, dict) or "result" not in msg:
        raise LaneError(f"unexpected JSON-RPC response (no result): {str(msg)[:200]}")
    return msg["result"]


# --- result handling -------------------------------------------------------
def _content_text(result):
    parts = result.get("content") or []
    texts = [
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    return "\n".join(texts) if texts else None


def _extract_payload(result):
    """Pull the tool payload out of an MCP tools/call result.

    Surfaces a tool-level `isError` (e.g. server-side schema validation) as a
    LaneError so it exits non-zero with a crisp message.
    """
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        # CAP the text — this re-enters the caller's context via stderr, so an
        # unbounded tool error (e.g. one echoing a large received value) would
        # defeat the whole "only projected excerpts enter context" guarantee.
        raise LaneError("tool returned an error:\n" + (_content_text(result) or "(no message)")[:400])
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    text = _content_text(result)
    if text is None:
        return result
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text  # non-JSON text tool output kept verbatim


def summarize(payload, nbytes):
    """The load-bearing guardrail: byte/record/keys only, NEVER values."""
    lines = [f"bytes:   {nbytes}"]
    if isinstance(payload, list):
        lines.append(f"records: {len(payload)}")
        lines.append(f"keys:    [list of {len(payload)} items]")
    elif isinstance(payload, dict):
        arrays = {k: v for k, v in payload.items() if isinstance(v, list)}
        if len(arrays) == 1:
            k, v = next(iter(arrays.items()))
            lines.append(f"records: {len(v)}  (from key '{k}')")
        elif len(arrays) > 1:
            counts = ", ".join(f"{k}={len(v)}" for k, v in sorted(arrays.items()))
            lines.append(f"records: n/a (multiple arrays: {counts})")
        else:
            lines.append(f"records: n/a (object, {len(payload)} keys)")
        lines.append("keys:    " + ", ".join(sorted(payload.keys())))
    else:
        lines.append("records: n/a (scalar)")
        lines.append(f"keys:    n/a ({type(payload).__name__})")
    return "\n".join(lines)


def _peek(payload, data, n):
    """First N items (list), else first N chars of the ALREADY-serialized payload.

    Reuses `data` (the string cmd_call already wrote to --out) instead of
    re-serializing the whole payload a second time.
    """
    if isinstance(payload, list):
        return json.dumps(payload[:n], indent=2, ensure_ascii=False)
    return data[:n]


def _resolve_args(arg):
    """Args from inline JSON, a file path, or - for stdin. Must be a JSON object."""
    if not arg:
        return {}
    if arg == "-":
        raw = sys.stdin.read()
    elif os.path.exists(arg):
        with open(arg, encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = arg
    raw = raw.strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LaneError(f"could not parse args as JSON: {e}")
    if not isinstance(val, dict):
        raise LaneError("tool arguments must be a JSON object")
    return val


# --- verbs -----------------------------------------------------------------
def _list_tools(config, match_names=None):
    """tools/list, following MCP cursor pagination.

    Raises on a malformed (non-object) result rather than silently returning a
    short list. When `match_names` is given (a collection of names), short-circuits
    as soon as EVERY requested name has been seen — the lazy-schema lookup path, so
    schema-ing one tool (or a batch of them) never drains pages it doesn't need.
    """
    want = set(match_names) if match_names else None
    tools = []
    cursor = None
    seen = set()
    while True:
        params = {} if cursor is None else {"cursor": cursor}
        result = _post("tools/list", params, config)
        if not isinstance(result, dict):
            raise LaneError(
                f"malformed tools/list response (expected object, got {type(result).__name__})"
            )
        page = result.get("tools") or []
        tools.extend(page)
        if want is not None:
            want -= {t.get("name") for t in page}
            if not want:  # every requested name found — no need to drain the rest
                break
        cursor = result.get("nextCursor")
        # A non-str/empty/repeat cursor ends pagination (and guards an unhashable
        # cursor from raising TypeError on the `in seen` check).
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            break
        seen.add(cursor)
    return tools


def cmd_ls(config, prefix=None):
    tools = _list_tools(config)
    if prefix:
        tools = [t for t in tools if str(t.get("name", "")).startswith(prefix)]
    for t in sorted(tools, key=lambda t: str(t.get("name", ""))):
        name = t.get("name", "")
        desc = (t.get("description") or "").strip()
        first = desc.splitlines()[0] if desc else ""
        print(f"{name}  --  {first}" if first else name)
    print(f"({len(tools)} tools)", file=sys.stderr)
    return 0


def cmd_schema(config, tools):
    """Print a tool's input schema — or, for several tools, a name->schema map.

    Batch every tool you already know you'll need next into ONE `schema` call: it
    drains `tools/list` a single time instead of once per tool. One tool prints its
    bare schema (unchanged); two or more print a JSON object keyed by tool name, so
    the result is unambiguous and still `jq`-projectable (`... | jq '.some_tool'`).
    """
    catalog = {t.get("name"): t for t in _list_tools(config, match_names=tools)}
    # Fail loud, naming EVERY missing tool at once (so the caller fixes them in one
    # pass) — a silently-dropped tool is the same silent quality loss we warn about.
    missing = [name for name in tools if name not in catalog]
    if missing:
        raise LaneError(
            f"tool(s) not found: {', '.join(repr(m) for m in missing)} (try `offlane ls`)"
        )
    if len(tools) == 1:
        print(json.dumps(catalog[tools[0]].get("inputSchema", {}), indent=2, ensure_ascii=False))
    else:
        schemas = {name: catalog[name].get("inputSchema", {}) for name in tools}
        print(json.dumps(schemas, indent=2, ensure_ascii=False))
    return 0


def cmd_call(config, tool, args_arg, out, peek=None):
    arguments = _resolve_args(args_arg)
    result = _post("tools/call", {"name": tool, "arguments": arguments}, config)
    payload = _extract_payload(result)
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    nbytes = len(data.encode("utf-8"))
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(data)
    except OSError as e:
        # The (possibly costly) tool call already succeeded — surface the write
        # failure in offlane's own summary/exit-1 contract, not a raw traceback.
        raise LaneError(f"could not write --out {out}: {e}")
    print(summarize(payload, nbytes))
    print(f"out:     {out}")
    if peek is not None:  # explicit --peek 0 is honored (shows the shape, no records)
        print("\n--- peek ---")
        print(_peek(payload, data, peek))
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="offlane",
        description="Move an MCP server's read results off your context window: "
        "payloads land on disk, only a summary prints, you jq-project what you need.",
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"offlane {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ls = sub.add_parser("ls", help="list tool names + one-line descriptions")
    p_ls.add_argument("prefix", nargs="?", help="filter to names starting with this prefix (e.g. hubspot)")

    p_schema = sub.add_parser(
        "schema",
        help="print one or more tools' input JSON schema(s)",
        description="Print a tool's input schema. Pass several tool names to batch them "
        "into one call (drains the catalog once); the batch prints a JSON object keyed "
        "by tool name. Do this whenever you already know several tools you'll need next.",
    )
    p_schema.add_argument(
        "tool", nargs="+", metavar="tool",
        help="one or more tool names; batching several fetches the catalog a single time",
    )

    p_call = sub.add_parser(
        "call",
        help="run a tool; write payload to --out; print only a bytes/records/keys summary",
        description="Run a tool and write its payload to --out. Prints ONLY a summary — "
        "jq-project the --out file to bring fields into context; never cat the whole file.",
    )
    p_call.add_argument("tool")
    p_call.add_argument(
        "args", nargs="?", default="",
        help="tool arguments: inline JSON, a path to a .json file, or - for stdin",
    )
    p_call.add_argument("--out", required=True, help="write the tool payload here (only a summary is printed)")
    p_call.add_argument("--peek", type=int, metavar="N", help="also print first N records (list) / N chars (other)")
    return p


def _install_sigpipe():
    """Behave like a normal Unix filter when stdout closes early (e.g. `| head`):
    die on SIGPIPE instead of raising BrokenPipeError at interpreter shutdown."""
    try:
        import signal

        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError, ValueError):  # pragma: no cover - non-POSIX
        pass


def main(argv=None):
    _install_sigpipe()
    args = build_parser().parse_args(argv)
    config = Config()
    try:
        if args.cmd == "ls":
            return cmd_ls(config, args.prefix)
        if args.cmd == "schema":
            return cmd_schema(config, args.tool)
        if args.cmd == "call":
            return cmd_call(config, args.tool, args.args, args.out, args.peek)
    except LaneError as e:
        print(f"offlane: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
