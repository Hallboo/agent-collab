# Agent Collab Messaging Protocol and Notice Formats

> Two confirmed usage rules. They apply to every Claude/Codex session
> enrolled in agent-collab.

## 1. Acknowledge receipt by age, reply in batches

On receiving a peer message, compare its `time` against the current time
before choosing how to reply:

- **Fresh message** (a few minutes old): reply normally as needed;
- **Older message or a backlog** (a clearly stale timestamp, or several
  unanswered messages from the same session at once): **merge by topic
  into a single reply** covering every open item; no ping-pong round
  trips message by message.

Purpose: **reduce message count**. Coordination messages are not free
(they interrupt the peer, inject context, and produce new notice lines);
one complete message beats several fragments.

A merged reply must still answer point by point (quote `msg_id` prefixes
or number the items) — merging must not drop answers. A pure status
acknowledgment (e.g. confirming a convention) may be omitted once the
other side has no further questions.

## 2. Notice-line format: arrow marks direction and peer

The queued-notice line once carried only a Message ID — no direction, no
peer:

```text
[Agent Collab] An authenticated peer message is queued. Message ID: f4cc7d17-f0e6-43d5-bcdf-1f01ffcc76ea
```

Expected format: **an arrow states send vs receive and the peer**; the
first 8 characters of the message ID are enough to locate it (the full ID
is available in the MCP result and the inbox):

```text
Outbound (queued, retrying):
[Agent Collab] → codex-01a08bdb@host-a queued f4cc7d17

Outbound (delivered):
[Agent Collab] → codex-01a08bdb@host-a delivered f4cc7d17

Inbound (landed in this session's inbox):
[Agent Collab] ← codex-01a08bc5@host-a queued 118cb7e5
```

- Arrow `→` = sent by this session; `←` = received from a peer;
- The peer appears as its session name (`codex-01a08bdb@host-a` form);
- Status keeps only the two levels `queued` / `delivered`.

> Implemented as specified: the inbound queue notice first line, the
> outbound initial line of the `send` result (`queued`), and the outbound
> terminal-state line the sender's sidecar injects once the peer takes
> delivery (delivered for a Claude receiver, queued for a Codex receiver)
> all follow this format; a terminal line that never comes = the peer is
> not receiving.

Room-message notice lines append the room short id and the display name:

```text
Inbound (room message queued):
[Agent Collab] ← glm-k7 #a7x9 queued 118cb7e5

Outbound initial (send tool result):
[Agent Collab] → #a7x9 (2) queued f4cc7d17

Outbound terminal (sender-side aggregate; counts when multiple members):
[Agent Collab] → #a7x9 delivered 2 f4cc7d17
```
