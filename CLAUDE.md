# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`offlane` is a tiny, **stdlib-only** CLI that acts as an [MCP](https://modelcontextprotocol.io)
client for any server speaking streamable HTTP. Its job: move a server's tool
**read** results *off* the LLM context window. `offlane call` writes the tool
payload to a file and prints only a `bytes/records/keys` summary; the caller then
`jq`-projects just the fields it needs, so only an affirmatively-chosen excerpt
enters context. It exists to give agents that can't already do this — notably
Anthropic Managed Agents, whose managed MCP connection delivers every result
straight into context — the fetch-to-disk pattern a local shell agent gets for
free. See `README.md` for the full motivation.

This is a **public** repository.

## Commands

```bash
pip install -e ".[test]"     # editable install + test deps (src layout: install to import)
pytest                       # run the suite (network-free)

uv run --extra test pytest   # same, via uv (handles the venv + install)

python -m offlane --help     # run the CLI from a checkout
# verbs: offlane ls [prefix] | schema <tool> [<tool> ...] | call <tool> '<json>' --out FILE [--peek N]
```

## Layout

- `src/offlane/__init__.py` — the **entire** implementation (transport, result
  handling, verbs, arg parsing). `main` is the console-script entry point.
- `src/offlane/__main__.py` — `python -m offlane`.
- `tests/test_offlane.py` — unit tests; plain `import offlane` (no `sys.path`
  hacks — the package is installed).
- `FRONTMATTER.md` — the copy-paste **system-prompt preamble** for an agent that
  drives offlane (schema-first → `call --out` → `jq`, never `cat`).
- `README.md` — user-facing docs.

## Design invariants (don't "simplify" these away)

- **stdlib-only, zero runtime dependencies.** Deliberate — the installed command
  drops into a locked-down sandbox with nothing to resolve. Keep to
  `urllib`/`json`/`argparse`; do not add runtime deps.
- **The projection guardrail is the whole point.** `offlane call` writes the
  payload to `--out` and prints ONLY `summarize()` (bytes/records/keys) — never the
  body. `summarize` emits key names and counts but **never values** (a value leak
  would defeat the tool and could expose secrets). Don't add a default that dumps
  the payload to stdout; `--peek` is opt-in and bounded.
- **Stateless bare POST — no MCP handshake.** offlane assumes the server runs its
  streamable-HTTP transport statelessly, so one self-contained POST works with no
  `initialize` and no session id. The reply is SSE-framed (`_parse_sse`), with a
  bare-JSON fallback. It pins `MCP-Protocol-Version` and **fails loud** on a
  400/406/415/428 (which signal the server isn't stateless). Don't swallow those.
- **Transient retry lives at the `_http_post` seam.** `_post_with_retry` retries
  502/503/504 and connection resets / `IncompleteRead` (an upstream LB dropping a
  backend) with bounded backoff; `_is_transient` classifies, TLS errors fail fast.
- **`_extract_payload` caps `isError` text.** A tool error re-enters the caller's
  context via stderr, so it's bounded — keep it capped.
- **Config is env-only** (`OFFLANE_MCP_*`): `OFFLANE_MCP_URL` required, token
  optional (unauthenticated servers exist). See the README Configure table.

## Conventions

- Match the existing style: one module, small pure functions, `LaneError` for every
  user-facing failure (caught in `main`, printed as `offlane: …`, exit 1).
- Keep `FRONTMATTER.md`, the `--help` epilog (`_HELP_EPILOG`), and the README's
  Usage/protocol **in agreement** whenever you change the verbs or the workflow.
- Tests stay **network-free** — monkeypatch `_http_post` (or `_post`); never hit a
  real server.
- The version lives in **both** `pyproject.toml` and `__version__` in
  `src/offlane/__init__.py` — bump them together on release.
