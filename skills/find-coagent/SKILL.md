---
name: find-coagent
description: Find and display Claude Code and Codex sessions enrolled in Agent Collab. Use for requests such as "find coagent", "list collaborating agents", or checking which cross-host peers are online; do not use it for client-native teams or subagents.
---

# Find Coagent

## Happy path

Call the `find_coagents` tool from the `agent-collab` MCP server. Do not substitute Claude's built-in `ListAgents` or another client-native team/subagent listing.

Return the tool's Markdown table as-is. Its columns are `Agent ID`, `Repo`, shortened `Host Name`, `Client`, `Model`, and `PID`; it intentionally has no `Name` column. The caller's own row is marked `（本机·自己）`, while other rows on the same Host are marked `（本机）`. Every observer derives the ID with the same fixed rule: `<repo-directory>_<claude-or-codex>_<host-label>_<PID>`. A configured Host name of at most 12 characters stays intact; longer machine names use their final segment, limited to 12 characters. Non-variable characters become underscores and PID remains full decimal. The ID can be selected with a double-click and used directly as the `send(to=...)` target.

When chat rooms exist, the tool appends a second table with columns `Room ID`, `Members`, and `Created`. `Room ID` is shown in `#<4-char id>` form — the unique interaction id for rooms: it can be passed directly to `send(to=...)` for room fan-out and to `join_room(room_id=...)`. An empty room lists its members as `（空房,可凭 ID 重进）` and can be rejoined with the same id.

This is a read-only discovery operation. Do not call `send`, inject or queue messages, or otherwise affect another Agent's context while finding Agents.

## When the `find_coagents` tool is unavailable

The session has not loaded the agent-collab MCP. Do not stop at a bare report — first report the situation to the user, then attempt to repair the local MCP registration:

1. Inspect the client's MCP configuration for the server `agent-collab` (for example, `claude mcp get agent-collab`, `codex mcp get agent-collab`, or read the MCP config file directly). Record the registered launcher path and its `--env-file` argument, if any.
2. If a registration exists and its launcher file exists on disk, the configuration is already fine — the session simply predates it or the MCP failed to start. Skip to step 5.
3. If the launcher path is missing or stale, locate the agent-collab repo on this host: try the sibling directory named `agent-collab` under the parent of the stale registered repo root, or other likely workspace locations. Do not scan the whole filesystem, and do not guess deeper than one level. If the repo cannot be found, report the gap and ask the user for its location instead of inventing a registration.
4. Once the repo is found, execute its one-shot configurator, passing the env file path captured in step 1 (or the host's known env file):

   ```
   <agent-collab-repo>/bin/configure-mcp /absolute/path/to/agent-collab.env
   ```

   The script is idempotent: it registers `agent-collab` for Codex and Claude and installs this skill where missing. Do not run it with a fabricated env file — the env file must come from an existing registration or from the user. Report exactly what it changed.
5. MCP servers are loaded at session start, so the current session still cannot call `find_coagents` even after a successful repair. Tell the user to restart the session (this is expected and fine) and then invoke this skill again.

Never infer membership from process lists or unverified spool files, and never fabricate the table. Discovery only becomes possible once the MCP is actually loaded.
