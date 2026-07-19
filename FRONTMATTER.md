# Agent system-prompt preamble

`offlane` only saves context if the agent driving it follows the discipline:
**schema-first, `call --out`, `jq`-project, never `cat`.** The block below is the
recommended preamble to paste **verbatim** into the system prompt of any agent
that will use `offlane`. It's written for the model, not the operator — keep the
imperative voice.

It assumes the environment has already put `offlane` on the agent's `PATH` and set
`OFFLANE_MCP_URL` (+ `OFFLANE_MCP_TOKEN` if the server authenticates) — see the
[README](README.md#configure). The agent should *use* `offlane`, never configure it.

Copy everything between the rules:

---

You have an `offlane` command on your `PATH`: a client for this environment's MCP
server, already configured with its endpoint and credentials. Use it for **every
read** from that server. `offlane` writes the full tool result to a file and
prints only a size summary — so a large result never enters your context, and you
pull in only what you need with `jq`.

**The read loop — follow it every time:**

1. `offlane ls [prefix]` — list available tools (names + one-line descriptions only).
2. `offlane schema <tool>` — **before the first time you call any tool.** Read the
   whole schema: its filters, projection / field parameters, and pagination.
   Narrowing the request server-side is how results stay small; a lever you skip
   is quality you silently lose.
3. `offlane call <tool> '<json args>' --out /tmp/<tool>.json` — runs the tool and
   writes the payload to the file. It prints only `bytes / records / keys`, never
   the data itself.
4. `jq '<projection>' /tmp/<tool>.json` — read only the fields you actually need.

**Rules — these are what make it work:**

- **Never `cat`, `head`, or `tail` an `--out` file.** That pours the whole payload
  into your context and defeats the entire purpose. Always read it through a
  narrow `jq` projection.
- Push filters and limits into the `call` arguments (learned from the schema)
  rather than fetching everything and filtering after the fact.
- If `offlane call` exits non-zero it printed the server's validation error — read
  it and fix your arguments; do not retry blindly.
- `--peek N` is for a quick shape check only, never for reading data.

---

## Optional lines to append

Add any of these to the block above and fill in the `<…>`, or delete them:

- **Writes** (if writes go elsewhere — e.g. an Anthropic Managed Agent whose
  writes stay on its managed MCP connection):
  > Use `offlane` for reads only. Send writes through `<your write tool / managed
  > MCP connection>`, not `offlane`.
- **Server hint** (steer the agent toward the tools it needs, avoiding a broad `ls`):
  > The tools you'll use most on this server are `<tool_a>`, `<tool_b>`, `<tool_c>`.
- **Scratch dir** (if `/tmp` isn't the right place):
  > Write `--out` files under `<dir>`.
