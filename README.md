# Agent Collab

An authenticated file-spool bridge for interactive Claude Code and Codex sessions. It exposes stdio MCP tools, routes messages through a configurable directory, injects each message only into its target session, and can explicitly report a summary to a Feishu custom bot.

One host can use a private local directory. Multiple trusted hosts can use the same shared directory. There is no listening network service, central daemon, task scheduler, role system, or autonomous Agent cluster.

## Security boundary

- Registrations and messages are authenticated with HMAC-SHA256. A process without `AGENT_COLLAB_AUTH_KEY` cannot create an accepted member or message.
- The configuration file and state files must be mode `0600`; their protected directories must be owned by the current user and mode `0700`.
- Sender identity comes from the sidecar. `send` has no `from` argument, and messages target one concrete session rather than a reusable display name.
- Local sessions are checked with PID plus process start time. Remote sessions must refresh a signed heartbeat every 5 seconds and expire after 30 seconds.
- Invalid, stale, or duplicate records are never injected. Client injection credentials and Feishu credentials never enter the spool or logs.
- No TCP, HTTP, SSE, WebSocket, service-discovery, or custom Unix listening endpoint is opened. Feishu is outbound only.

All hosts and sessions with the shared authentication key are one trust domain. HMAC proves membership in that trust domain; it does not distinguish a malicious holder of the shared key from another enrolled member. The spool is authenticated but not encrypted, so anyone who can legitimately read the shared state can read its messages and registration metadata. This protects against unconfigured Agents, other normal Unix users, and forged files; it does not protect against root, storage administrators, a compromised trusted host, or a process that already controls the current Unix account and can read the key.

Authenticated peer messages are normal collaboration requests. Bug investigation and fixes, progress updates, assigned worker tasks, reviews, research, and similar work do not require the user to repeat the request. The receiving session still applies its repository instructions and existing safety boundaries.

## Install

Python 3.11 or newer is required.

```bash
python -m pip install .
```

For development directly from a checkout, `bin/agent-collab` loads the package from `src/` without installation.

## Configure

Create a protected configuration and state directory:

```bash
agent-collab init-env \
  --env-file ~/.config/agent-collab/config.env \
  --state-dir ~/.local/state/agent-collab
```

The generated file contains:

```dotenv
AGENT_COLLAB_AUTH_KEY=<random secret>
AGENT_COLLAB_STATE_DIR=/absolute/path/to/state
```

`FEISHU_WEBHOOK_URL` is optional. `init-env --source-env /secure/source.env` copies only that setting from an existing env file without printing it.

The MCP launcher accepts any explicit configuration path:

```bash
./bin/configure-mcp /secure/path/agent-collab.env
```

This replaces the exact `agent-collab` MCP entry for both installed clients, installs the `find-coagent` skill without overwriting an existing skill, and skips a missing client. The stored launcher command contains no host name: each sidecar resolves its Host label per machine when it starts, as `AGENT_COLLAB_HOST` env > machine hostname (`src/agent_collab/identity.py::detect_identity`). A client config directory can therefore be shared across machines without carrying one machine's label onto another. To configure either client manually:

```bash
codex mcp add agent-collab -- \
  agent-collab serve \
  --env-file /secure/path/agent-collab.env \
  --host-name dev-box

claude mcp add --scope user agent-collab -- \
  agent-collab serve \
  --env-file /secure/path/agent-collab.env \
  --host-name dev-box
```

A manual `--host-name` pins one fixed label; use it only in per-machine client configs, never in a config directory shared across machines.

Start a new client session after adding the MCP server. Run a non-secret configuration check with:

```bash
agent-collab doctor --env-file /secure/path/agent-collab.env
```

### Host-label pitfalls

The enrolled host label is resolved when each sidecar starts, as an explicit
`--host-name` launcher argument > `AGENT_COLLAB_HOST` env > machine hostname
(`src/agent_collab/identity.py::detect_identity`). `configure-mcp` passes no
`--host-name`, so the label always comes from the machine actually running the
session. Fixed issues and operational facts, newest first:

- **Config reuse across machines mislabels the host (fixed 2026-09-12).**
  Earlier versions stored a fixed `--host-name` launcher argument detected at
  configuration time. If a client config directory was shared with or migrated
  to another machine (e.g. a symlinked shared `.codex`), every session there
  enrolled under the old machine's name while its process ran locally — a
  session on one host was observed enrolling under a previous host's label
  carried over by the inherited client config. `configure-mcp` now bakes no
  host name; each machine resolves `AGENT_COLLAB_HOST` > its own hostname
  when the sidecar starts. Client entries written by older versions still
  carry the old argument — re-run `configure-mcp` on each machine to replace
  them.
- **Reception is an envelope plus an inbox read (unified 2026-09-14).** Every
  receiver — Claude and codex alike — gets a one-line envelope
  (`[Agent Collab] ← <from_name> -> <to_name> <status> <id8> ·<title>`, with
  `#<room>` appended for room fan-out) plus the message UUID in its
  `inbox(message_id=...)` pointer, and must call `inbox` for the text; the
  peer text itself is never pushed into a session. Messages carry a required
  ≤10-char `title` that every envelope and the `send` result show in place of
  any body preview. Before this change a Claude receiver got the full text
  injected through its messaging socket. A
  stopped codex has no sidecar at all. Persisted messages survive both: a
  session that resumes (`codex resume`) inherits its pending backlog on
  sidecar start, while a brand-new session does not. A sender whose `→
  <peer> queued` notice never reaches a final status line is looking at a
  peer that is not receiving; `report_to_feishu` can wake the human operator.
- **A never-used codex session swallows queue notices (fixed 2026-09-14).**
  codex 0.153 deletes a queued item for a thread that has never run a turn —
  no injection, no transcript, no error; the notice is gone. `CodexDelivery`
  now refuses to queue into such a thread — detected via the rollout
  transcript, which codex writes only at the thread's first turn — and fails
  the delivery instead, so the message stays pending in the spool and the
  sidecar retries every few seconds; once the session runs its first turn the
  notice flows normally. To make a freshly opened codex reachable without
  waiting, launch it with a one-word positional prompt (for example
  `codex 'standby; handle Agent Collab notices'`) or wrap the launcher so the
  primer is always sent.
- **Codex launch-environment facts (recorded 2026-09-14).** The configured
  provider `env_key` (for example `OPENAI_API_KEY`) must be present in the
  shell that starts codex; a codex spawned from a bare environment (cron,
  non-login tmux) fails every model turn with a missing-variable error. And
  `--ask-for-approval never` combined with the default sandbox rejects MCP
  tool calls outright (`MCP tool call requires approval, but approval policy
  is never`), which breaks the notice → `inbox` read path; run the
  sandbox/approval combination your operator actually trusts.

## Chat rooms

Three or more agents can coordinate in a chat room instead of repeating point-to-point sends:

- `create_room(members?)` creates a room, registers the caller as the first member, and invites the listed Agent IDs (from `find_coagents`) with a peer message carrying the room id. It returns the 4-char room id plus the room charter.
- `join_room(room_id)` enters a room by its 4-char id; `leave_room()` leaves and notifies the remaining members. One agent is in at most one room at a time; peer-to-peer `send` keeps working regardless. Both results carry the room transcript's jsonl path — joining members read prior history from that file (each line's `payload` field is plain JSON) or ask existing members directly.
- `send(to="#a7x9", ...)` fans the message out to every online member except the sender. All copies share one `msg_id` and reuse the existing delivery, notification, and archive machinery; the sender's sidecar reports one aggregated final line (`#a7x9 delivered 2 queued 1 <id8>`, with `offline` counts for members that went down).

Rooms carry no resident resources, so they have no lifecycle management. A room is two passive files under `<state dir>/rooms/`: `<long-id>.jsonl` (append-only sealed transcript of `created`/`join`/`leave`/`chat` events) and `<long-id>.members.json` (the sealed member list). The 4-char id is the first 4 characters of the 16-char long id and is the join credential; there is deliberately no further access control — the shared state directory is one trust domain. Membership binds to Agent IDs: offline members are pruned lazily on the next room access, a restarted session must rejoin with the room id, and an empty room stays on disk and can be rejoined at any time.

Each session's display name defaults to `<repo-short-name>-<MODEL>-<index>` (e.g. `agent-collab-GLM53-1`, `task-factory-OPUS5-2`): the repo short name, the UPPERCASE model abbreviation, and the smallest numeric index not held by another online agent on the same host — assigned at registration by scanning the registry, re-scanned on collision, and reused once freed. The repo short name is the lowercased repo-directory slug (capped at 24 characters) unless overridden by `AGENT_COLLAB_REPO_SHORT_NAMES=task-factory=TF,...` in the env file (or the same process env var). The name can be changed with `set_identity(name=...)`; names are unique among online agents. Room messages show `[room a7x9]` in the header and the speaker's display name in notice lines.

## Shared-directory hosts

To collaborate across hosts:

1. Provision the same `AGENT_COLLAB_AUTH_KEY` to each trusted host through protected configuration files.
2. Point each host's `AGENT_COLLAB_STATE_DIR` at the same underlying shared directory. The local mount paths may differ.
3. Ensure the filesystem preserves the same numeric owner, mode `0700` directories, mode `0600` files, atomic rename, and cross-host advisory file locking.
4. Keep host names unique and clocks synchronized within the 30-second remote heartbeat window.

Each sidecar refreshes its own registration and polls the shared spool. The target host's sidecar performs local Claude/Codex injection, so client sockets and tokens never cross hosts. Stale registration files are ignored rather than automatically deleted.

If the shared filesystem cannot provide those semantics, use a host-local state directory instead.

## MCP tools

- `set_identity(role, name?)`
- `list_agents()`
- `find_coagents()` — read-only Markdown table of online agents (copyable Agent ID, display name, repo, client, model, PID) plus, when rooms exist, a `Room ID / Members / Created` table; `#<room id>` is directly usable as `send(to=...)`
- `send(to, title, text, reply_to?)` — `to` is a peer target or `#<room id>`; `title` is a required ≤10-char headline carried in every envelope, `text` is the body
- `inbox(since?, limit?, message_id?)`
- `report_to_feishu(text, title?)`
- `create_room(members?)`
- `join_room(room_id)`
- `leave_room()`

Use `name@host` for an explicit cross-host target. A bare name works only when it identifies exactly one online session.

The optional [`find-coagent`](skills/find-coagent/SKILL.md) skill routes natural-language requests such as “find coagent” to `find_coagents()` instead of Claude's built-in team/subagent listing. Discovery only reads signed registrations; it never sends a message or changes another Agent's context. The caller's row is marked `（本机·自己）`; other sessions on that Host are marked `（本机）`. `Agent ID` is the table's first column and follows one observer-independent rule: `<repo-directory>_<claude-or-codex>_<host-label>_<PID>`. A configured Host name of at most 12 characters is kept intact; longer machine names use their final segment, limited to 12 characters. Non-variable characters become underscores and PID stays full decimal. For example, `agent_collab_codex_dev_box_4242` can be double-clicked and passed directly to `send(to=...)`. The `Name` column shows the display name (`<repo-short-name>-<MODEL>-<index>` by default); it is for humans referring to each other — address messages by Agent ID.

`send` durably persists the message before the receiving sidecar attempts delivery, and its result settles the transport status within a bounded wait (`send_wait_seconds`, default 3 s) instead of returning an unconditional "queued": `status` is `delivered` (injected into a Claude session), `queued` (accepted into a codex queue), an aggregate like `delivered 2 queued 1` for rooms, or `pending` when the receiver sidecar has not confirmed yet. The result carries a one-line notice `[Agent Collab] → <peer> <status> <id8> ·<title>` and never echoes the full body. A transient delivery failure remains pending and is retried while the target session stays online; an instance that resumes the same client session inherits the pending backlog, so `codex resume` picks up messages that arrived while the process was down. Delivery is at least once, so a crash at the delivery/archive boundary can produce a duplicate notification rather than lose a message. Messages are never redirected to an unrelated new session or by display name. A send that returned `pending` gets one asynchronous final status line injected into the sending session when the receiver completes (or the copy gives up); a `pending` that never reaches a final status line means the peer is not currently receiving.

Claude socket success is recorded as `delivered`. Codex delivery uses `codex queue` and is recorded as `queued`; this does not prove the current turn was interrupted or that the model consumed the message. Every envelope — Claude and codex alike — leads with `[Agent Collab] ← <from_name> -> <to_name> <status> <id8> ·<title>` and carries the message UUID in its `inbox(message_id=...)` pointer, so batching limits and sender clock skew cannot hide that message. Only this authenticated lookup envelope is ever pushed, never the peer text itself.

## Development checks

```bash
PYTHONPATH=src python -m pytest
python -m ruff check src tests
```
