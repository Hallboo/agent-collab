# DISPATCH — resident scheduler deployment example

An optional deployment example for agent-collab: a resident scheduling
agent ("DISPATCH") that bridges a Feishu/Lark chat to the agent-collab
fleet — dispatching work to online agents, querying state, and reporting
back. It is not part of the agent-collab core, and the core stays what it
is: a stdio MCP plus a file spool. Nothing here is required to use
agent-collab.

## Architecture

- **`gateway.py`** — the resident daemon. It supervises a
  `lark-cli event +subscribe` child (restarted with exponential backoff
  when it dies), watches `dispatch/events/` for new `.json` event files
  the subscriber lands on disk, and pokes the agent's tmux panel when any
  arrive. One heartbeat poke per hour keeps the agent patrolling even in
  silence. A flock lock makes it single-instance and idempotent.
- **The agent session** — a claude session inside a dedicated tmux server,
  driven by `CLAUDE.md` in this directory (its standing order: startup
  self-check, event handling, dispatching discipline).
- **`supervisor.sh`** — lifecycle management: `status`, `ensure` (start
  missing pieces), `compact` (scheduled `/compact` injection — the agent
  cannot compact itself), `restart` (graceful: asks the agent to persist
  pending state, waits for `DONE`, relaunches), and `daemon` (a resident
  loop for hosts without cron: 60 s watchdog, 6 h compact, daily
  restart).
- **`launch.sh` / `run.sh`** — start the tmux session with a narrowly
  scoped allowed-tools list (lark-cli, supervisor, the agent-collab MCP
  tools, read-only state inspection).

## Code vs runtime data

Only code and the configuration template are tracked. Runtime data — real
chat events, ops logs, pending state, local credentials — is git-ignored
by design and never leaves the deployment host:

| Tracked (code & template) | Ignored (runtime data) |
| --- | --- |
| `gateway.py`, `supervisor.sh`, `launch.sh`, `run.sh` | `events/` — raw Feishu event files |
| `config.example.sh` — configuration template | `LOG.md` — append-only ops log |
| `CLAUDE.md` — the agent's standing order | `STATE.md` — pending items across restarts |
| `README.md` | `supervisor.log`, `events/gateway.log` |
| | `config.sh` — real configuration values |

## Configuration

```
cp dispatch/config.example.sh dispatch/config.sh   # then edit
```

`config.sh` is sourced by every script and is git-ignored. Required
values: `DISPATCH_CHAT_ID` (the controlling Feishu chat),
`DISPATCH_PROFILE` (env file with API credentials/model for the agent
session), `DISPATCH_STATE_DIR` (this deployment's agent-collab state
directory). Environment variables set before the scripts run override
`config.sh`.

## Running

```
bash dispatch/supervisor.sh ensure    # start gateway + session if missing
bash dispatch/supervisor.sh status    # session: running / gateway: armed
```

For unattended hosts either use the suggested crontab entries in
`supervisor.sh`'s header comment or run `bash dispatch/supervisor.sh
daemon` under your process supervisor of choice.

## Design notes: why event files, not pipes

The gateway's shape is dictated by four recorded failure classes of the
earlier bash `watch.sh` chain it replaced: stdout pipe buffering held
events until process exit (latency up to an hour); killing the listener
on every event left deaf windows while it re-armed; `pgrep`/`pkill` string
matching killed wrong processes while leftover subscriptions silently
diverted events; and bash quoting/cwd/path-expansion traps broke paths.
Hence: event files land on disk (durable, batch-visible), the gateway is
resident and never touches the listener on events, a flock lock owns
process identity, and all paths derive from `__file__` with argument
arrays instead of shell strings.

## Requirements

`lark-cli` (authenticated as the bot), `tmux`, `python3`; `cron` or the
bundled `daemon` loop for supervision.
