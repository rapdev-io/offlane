# offlane

**Move an MCP server's read results *off* your LLM context window — for agents
that can't already do it themselves.**

## Why this exists (and whether you need it)

`offlane` was built to work around a specific limitation of **[Anthropic Managed
Agents](https://platform.claude.com/docs/en/managed-agents)**. On a Managed
Agent's managed MCP connection:

- **Every tool result is delivered straight into the model's context** as a
  `tool_result` block — and because the agent loop is *append-only* (a fetched
  result can't be dropped), it's re-sent and re-billed on **every subsequent
  turn**. One 400 KB read becomes a permanent resident of the context window.
- **The entire tool catalog is injected up front** at session start — tens of
  thousands of tokens of tool schemas, whether or not the agent ever calls them.

There is no built-in way to say "put this result on a file instead." The sandbox
*has* a shell and a filesystem, but the managed connection is the only built-in
route to the MCP tools, and it always lands in context.

**This is not a universal problem** — it's specific to that platform's managed
connection:

- **A locally-running agent with a shell (e.g. Claude Code) mostly doesn't have
  it.** It loads tool schemas lazily via on-demand tool search,
  it can route a big read through a file and read back only a projection, and it
  can compact context to reclaim space. **If your agent already has a shell and
  lazy tools, you probably don't need offlane.**
- **Other platforms may or may not have it** — check how yours delivers MCP
  results and tool schemas before reaching for this.

`offlane` brings the local-agent pattern to a place that lacks it: it's an MCP
client you run **from the sandbox's own shell**, so results land on disk and only
your `jq` projection enters context — and tools are discovered on demand
(`offlane ls` / `schema`) instead of dumped up front.

### Side by side: a giant MCP read

| | Claude Code (local, has a shell) | Anthropic MA — managed MCP, **no offlane** |
|---|---|---|
| **Tool schemas** | loaded on demand (tool search / deferred) — you don't pay for tools you never call | whole catalog injected at session start, re-sent every turn |
| **A 400 KB result** | escape hatches: shell the read to a file and `jq`/`grep` a projection; the bulk never has to enter context | returned into the model's context as a `tool_result`, **in full** — no file-sink on that path |
| **On the next turn** | only the slice you pulled is still in context; compaction can reclaim the rest | the whole payload rides forward — re-sent and re-billed — for the rest of the session (append-only, can't be dropped) |
| **Who decides what's in context** | the agent (it chooses what to read from the file) | nobody — the payload is there whether it's needed or not |

*(Honest caveat: even a local agent's raw MCP result initially lands in context.
The difference is that it **has** the shell, files, lazy tools, and compaction to
keep the bulk out — the MA's managed connection gives you none of those. offlane
supplies the missing shell-side MCP client.)*

## What it does

`offlane call` writes the tool payload to a **file on disk** and prints only a
**byte / record / keys summary**. You then `jq`-project *just the
fields you need*, so only an affirmatively-chosen excerpt enters context.

```console
$ offlane call search_tool '{"limit":200}' --out /tmp/d.json
bytes:   412933
records: 200  (from key 'results')
keys:    query, results, total
out:     /tmp/d.json

$ jq '.results[].id' /tmp/d.json      # only these ids enter context
```

It's a general [MCP](https://modelcontextprotocol.io) client — it can call any
tool your bearer is authorized for. The projection-summary default is what makes
it a *lane* for reads; writes work the same way, you just rarely need the
guardrail for them.

## Install

### In an Anthropic Managed Agent

You don't run an install command — you **declare** offlane on the environment so
it's on the agent's `PATH` before its first turn, then teach the agent to use it:

1. **Add it to the environment's package manifest** (installed pre-execution):

   ```jsonc
   // on the cloud environment's config
   "packages": { "type": "packages", "pip": ["offlane==0.1.0"] }
   ```

   This requires `networking.allow_package_managers: true` on the environment.
2. **Provide the endpoint + bearer** to the sandbox as the environment variables
   `OFFLANE_MCP_URL` and `OFFLANE_MCP_TOKEN`, via the environment's credential /
   vault mechanism. (more auth modes coming soon)
3. **Prepend every implementing agent's system prompt** with the preamble in
   **[FRONTMATTER.md](FRONTMATTER.md)** — that discipline (schema-first, `--out`,
   `jq`, never `cat`) is what actually keeps results out of context.

`offlane` is stdlib-only (zero runtime deps), so it resolves cleanly in a
locked-down sandbox with nothing else to pull in.

### Locally, or on another platform

```console
pip install offlane          # once published
# or, from a checkout:
pip install -e .
```

Set `OFFLANE_MCP_URL` (+ `OFFLANE_MCP_TOKEN`) — see [Configure](#configure) — and
run `offlane`. Wiring it into a non-MA agent? The same
[FRONTMATTER.md](FRONTMATTER.md) preamble applies.

## Configure

```bash
export OFFLANE_MCP_URL=https://your-host/mcp   # required
export OFFLANE_MCP_TOKEN=…                      # bearer, if the server authenticates
```

| Env var | Default | Purpose |
|---|---|---|
| `OFFLANE_MCP_URL` | — (required) | MCP server endpoint |
| `OFFLANE_MCP_TOKEN` | — | `Authorization: Bearer …` (omit for an unauthenticated server) |
| `OFFLANE_MCP_PROTOCOL_VERSION` | `2025-03-26` | pinned `MCP-Protocol-Version` header |
| `OFFLANE_SESSION_ID` | — | sent as `X-Offlane-Session`, a per-request attribution seam a server can key telemetry on |
| `OFFLANE_MCP_TIMEOUT` | `60` | per-request timeout (seconds) |
| `OFFLANE_MCP_RETRIES` | `3` | attempts on 502/503/504/reset |
| `OFFLANE_MCP_BACKOFF` | `0.5` | base backoff (seconds), doubled per attempt |

## Usage

```
offlane ls [prefix]                    list tool names + one-line descriptions
offlane schema <tool> [<tool> ...]     print one (or several, batched) tools' schema(s)
offlane call <tool> <args> --out PATH  run a tool; write payload to PATH; print a summary
                                       [--peek N]   also print first N records / N chars
```

`args` is inline JSON, a path to a `.json` file, or `-` for stdin.

**The protocol (why this saves context):**

1. `offlane schema <tool>` — **always** before first use. Read the whole schema
   (filters, projections, pagination); a skipped lever is silent quality loss.
   Already know several tools you'll need next? **Batch them** —
   `offlane schema <tool_a> <tool_b> <tool_c>` — to fetch the catalog once and get
   a JSON object keyed by tool name back (see [Batching schema lookups](#batching-schema-lookups)).
2. `offlane call <tool> '<json>' --out FILE` — writes the payload to `FILE`,
   prints only a `bytes/records/keys` summary.
3. `jq '<projection>' FILE` — pull only the fields you need. Never `cat` the file.

### Batching schema lookups

`offlane schema` accepts more than one tool name. Passing several **traverses the
tool catalog a single time** (instead of once per tool) and prints a JSON object
keyed by tool name — do this whenever you already know the handful of tools your
next steps will touch:

```console
$ offlane schema search_tool get_record list_records
{
  "search_tool": { "type": "object", "properties": { "query": … } },
  "get_record":  { "type": "object", "properties": { "id": … } },
  "list_records": { "type": "object", "properties": { "limit": … } }
}

$ offlane schema search_tool get_record | jq '.get_record.properties'   # still jq-able
```

A single tool still prints its bare schema, unchanged. If any requested name isn't
found, `offlane` fails loud and lists **all** the missing names at once, so you fix
them in one pass.

## How it works

Most MCP servers run their Streamable HTTP transport **statelessly** (a fresh
server per request, no session id), so a single self-contained POST of a
`tools/list` / `tools/call` body succeeds with **no `initialize` handshake** and
**no `Mcp-Session-Id`**. `offlane` skips the handshake deliberately — in stateless
mode it's functionally inert. The reply is SSE-framed (JSON responses disabled),
so offlane parses the `data:` frame; a bare-JSON reply is accepted too. It pins
`MCP-Protocol-Version` explicitly and **fails loud** on a 400/406/415/428 that
would signal the server isn't stateless (needs a handshake / session / different
version) — in which case that server wants a full MCP client, not offlane.

Transient upstream failures (502/503/504, or a connection reset / incomplete read
mid-stream — e.g. a load balancer dropping a backend) are retried with bounded
backoff, so a flaky upstream never surfaces to the caller.

## Driving an agent with offlane

offlane is meant to be run by the *agent itself*, from its shell/bash tool —
that's exactly where a fat tool result would otherwise land in context. It only
pays off if the agent follows the discipline (schema-first, `call --out`,
`jq`-project, never `cat`), so **[FRONTMATTER.md](FRONTMATTER.md) is a
ready-to-paste system-prompt preamble** that enforces it. Prepend it to the
agent's system prompt, and point the prompt at `offlane --help` for the terse
reference.

## Development

`offlane` uses a standard src layout — install it (editable) and run the tests:

```console
pip install -e ".[test]"
pytest
```

Or with [uv](https://docs.astral.sh/uv/), which handles the venv + install for you:

```console
uv run --extra test pytest
```

## License

MIT — see [LICENSE](LICENSE).
