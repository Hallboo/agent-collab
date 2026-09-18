#!/usr/bin/env bash
# DISPATCH session launch parameters: source the cc profile (gateway
# credentials/model/launch arguments) first, then start claude.
# The allowed-tools list is scoped to exactly what dispatching needs.
# Values come from dispatch/config.sh (see config.example.sh); environment
# variables override them, DISPATCH_PROFILE selects the profile file.
set -euo pipefail
dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd "$dir"
# shellcheck disable=SC1091
[ -f "$dir/config.sh" ] && . "$dir/config.sh"
# Drop host cc session variables inherited from the launching shell or the
# tmux server, so this session generates its own.
unset CLAUDE_PID CLAUDE_CODE_SESSION_ID CLAUDE_CODE_MESSAGING_SOCKET \
      CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_EXECPATH CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true
: "${DISPATCH_PROFILE:?DISPATCH_PROFILE is required (dispatch/config.sh or environment)}"
: "${DISPATCH_STATE_DIR:?DISPATCH_STATE_DIR is required (dispatch/config.sh or environment)}"
set -a
# shellcheck disable=SC1090
. "$DISPATCH_PROFILE"
set +a

extra=()
if [[ -n "${DISPATCH_MODEL:-}" ]]; then
  extra+=(--model "$DISPATCH_MODEL")
fi
# ${CLAUDE_LAUNCH_ARGS} comes from the profile, word-split into arguments.
exec claude "${extra[@]}" \
  --allowedTools \
    'Bash(lark-cli *)' \
    'Bash(bash dispatch/supervisor.sh *)' \
    'mcp__agent-collab__find_coagents' \
    'mcp__agent-collab__list_agents' \
    'mcp__agent-collab__set_identity' \
    'mcp__agent-collab__send' \
    'mcp__agent-collab__inbox' \
    'mcp__agent-collab__create_room' \
    'mcp__agent-collab__join_room' \
    'mcp__agent-collab__leave_room' \
    "Bash(ls $DISPATCH_STATE_DIR/*)" \
    "Bash(cat $DISPATCH_STATE_DIR/*)" \
    'Bash(grep *)' \
    'Bash(head *)' \
    'Bash(tail *)' \
    'Read' \
  ${CLAUDE_LAUNCH_ARGS:-} \
  "$@"
