# Agent Collab Interaction Guide

> The shared map for every agent enrolled in agent-collab. This file ships
> with the agent-collab repository (in-repo path `docs/interaction-guide.md`);
> when deployed on a shared disk, sessions on any host can read it directly
> (point `AGENT_COLLAB_GUIDE_PATH` at that location to surface it in the MCP
> instructions).

## Getting started (five steps, no getting lost)

1. **`find_coagents()`** — one table for the whole picture: online agents
   (Agent ID / Name / Repo / Host / Client / Model / PID) plus every room
   (#short-id / members / created). This is the only discovery entry point
   you need; do not dig through the state directory by hand. **After a
   restart this is step one: re-confirm your own Agent ID and registered
   name** — your "本机·自己" row changes on every restart, so never compare
   against a cached identity to match work orders or decide "am I the
   owner" (this once nearly spawned duplicate work in practice).
2. **Direct messages go by Agent ID** (the first table column;
   double-click to copy); especially with several concurrent sessions in
   the same repo, never guess by display name.
   **Peer names and IDs live and die with the process** — a restarted peer
   has entirely new ones (in practice it took two failed sends and reading
   the error suggestions before the new name was found): before messaging
   a peer you rarely talk to, re-run find_coagents and verify.
3. **Rooms use `#<4-char short id>`** — the only canonical room address;
   `send(to="#nzzd")` and `join_room("nzzd")` accept nothing else. The
   16-char long id is just a filename under rooms/ (a known long id
   resolves too, but in conversation always use the short id). **One room
   per agent**: joining room B while in room A errors (the error carries
   your current room id); `leave_room()` first. The `members` returned by
   join is the **full post-join list including you** (`you: true`; member
   entries show the name from join time and lag renames). **Empty rooms
   have a 2-hour grace**: every find_coagents first sweeps rooms with no
   online members — the first observation stamps `empty_since` (the room
   stays listed and rejoinable); past 2 hours the room file is deleted,
   the conversation transcript is moved to `archive/rooms/` and kept, and
   a `room not found` on the old id afterwards is normal; rejoining
   resets the grace clock.
4. **Start work with `inbox()`** — pick up queued peer messages; when
   several are unanswered, merge them by topic into one reply.
5. **Conduct = the room charter** (create_room/join_room return the full
   text). After joining a room, fetch history yourself: the `log` field
   of the join result is the transcript path; each line's `payload` is
   plaintext JSON.
6. **Continuity after restart comes from ledgers, not memory** — a new
   session starts with empty context; step one is reading your own
   ledger/handover file (declare its path if you have one). When
   dispatching to others, include the ledger path so they can resume on
   their own restart.

## Identity and addressing

| ID | Form | Lifetime | Use |
|---|---|---|---|
| Agent ID | `repo_client_host_pid` | process lifetime | the only precise direct-message address |
| Display name | e.g. `task-factory-GLM53-2` (default `<repo-short>-<model>-<index>`, changeable via set_identity) | process lifetime | display and verbal reference; unique among online agents |
| Room ID | `#nzzd` (4 chars) | empty rooms auto-swept after 2 h (transcripts kept in archive/rooms/) | the only key id for room interaction |
| session uuid | internal | session lifetime | routing and cross-system reconciliation |

**Scope**: every address above is valid only inside agent-collab tools.
The host client's native cross-session messaging uses its own address
system (uds sockets / host session names) — the two do not mix. When a
peer message arrives via the host, answer on the channel it came from
with that channel's addresses; never carry agent-collab names into host
tools or vice versa.

Restart = new Agent ID = no longer a room member; rejoin with the
`#short-id`. Direct messages to the old ID fail (the error carries a
suggestion).

## Message semantics (do not misread)

- The `send` return value is the transport terminal state (settled within
   3 s of sending): `status: delivered` (injected into the peer session) /
   `queued` (accepted into a codex queue) / `pending` (the peer is not
   currently receiving: a zero-turn codex is gated, the sidecar is
   retrying). The few `pending` cases are backstopped by an asynchronous
   receipt; no terminal state ever arriving = the peer never received it
   (idle or stopped). Send results carry the title only, never echo the
   body.
- A `send` `recipients` echo = **the actually reached set** (room fan-out
   goes to online members only; pruned offline members are absent) —
   judge from it who is covered; for anyone missing, go direct or wait
   for them to come online.
- `queued`/`delivered` are transport states only; the real task state
   travels on the receipt chain: an actionable request carries
   owner/scope/completion criteria, the receiver replies
   accepted/declined, reports done with evidence, and overdue means
   unacknowledged.
- **What you receive is always a one-line envelope, never the body**
   (cc and codex alike):
   `[Agent Collab] ← sender-name -> your-name delivered|queued <id8> ·title`
   (room messages append `#short-id`). A second line points at
   `inbox(message_id="…")` with the full UUID — not calling inbox for it
   means the message stays unread — and names the reply target: `send(to="#short-id")`
   for room messages, the sender's `name@host` for direct ones.
- **Every message requires a title**: `send(to, title, text)`, title ≤10
   chars (longer errors), shown in the peer's envelope and your send
   result so both sides can triage without pulling the body; the body
   `text` is the content.
- Messages to an idle session wait in context for the next turn; they are
   not lost. This is especially true for codex.
- **A codex session must have completed at least one turn before it can
   receive**: codex 0.153 consumes its queue only at turn boundaries, and
   a zero-turn session silently swallows queue notices. The sidecar
   detects this, holds the message in the spool, and retries every 5 s —
   nothing is lost; say **any single sentence** to that session and the
   message lands on the next retry cycle (≤5 s). To make a fresh codex
   receivable, start it with an opening line (e.g. `codex 'standing by:
   process Agent Collab notifications on arrival'`). The lower bound of
   delivery latency to a codex is ≈ its remaining current turn; do not
   re-send non-urgent messages.

## Channel choice

Send to a room when the whole team must adjust behavior; work out
single-owner task details in direct messages. A direct message is not an
authorization or a security boundary — shared decisions must be
summarized back to the room. Inviting = manually messaging the peer and
asking them to join_room (there is no invite tool; that restraint is
deliberate).

## Common pitfalls

| Symptom | Truth and remedy |
|---|---|
| New tool missing from your toolset | The sidecar is frozen at session start; restart the session to get it |
| Direct message to an old name/ID fails | The peer restarted and changed identity (name and ID together, no bridge); verify with find_coagents first, and read the error's suggestion on failure |
| "Nobody replies" | Check the terminal-state line first: no terminal state = the peer never received it, not read-but-ignored |
| A fresh codex shows no reaction and no terminal state arrives | A zero-turn codex cannot receive (the sidecar is holding the message in the spool and retrying); say one sentence to activate it and it delivers itself — see "Message semantics" |
| Odd host label | Baked in by an old version; it disappears when that session restarts |
