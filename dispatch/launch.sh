#!/usr/bin/env bash
# Launch the resident scheduler agent: a dedicated tmux server (-L) avoids
# inheriting the old server's environment; gateway credentials arrive via
# the profile sourced by run.sh.
# Usage: launch.sh [feishu chat_id]
# The chat id comes from the first argument, else DISPATCH_CHAT_ID from
# dispatch/config.sh (see config.example.sh) or the environment.
set -euo pipefail
dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck disable=SC1091
[ -f "$dir/config.sh" ] && . "$dir/config.sh"
socket="${DISPATCH_TMUX_SOCKET:-dispatcher}"
session="${DISPATCH_SESSION:-dispatch}"
chat_id="${1:-${DISPATCH_CHAT_ID:-}}"
if [[ -z "$chat_id" ]]; then
  echo "chat_id required: pass it as the first argument or set DISPATCH_CHAT_ID in dispatch/config.sh" >&2
  exit 1
fi

if tmux -L "$socket" has-session -t "$session" 2>/dev/null; then
  echo "scheduler agent already running: tmux -L $socket attach -t $session" >&2
  exit 1
fi
tmux -L "$socket" new -s "$session" -d -c "$dir" "bash '$dir/run.sh'"
# Wait for the input prompt (first-run dialog / loading finished) before
# sending the startup instruction, at most 60 seconds.
for _ in $(seq 1 60); do
  sleep 1
  tmux -L "$socket" capture-pane -t "$session" -p 2>/dev/null | grep -q '❯' && break
done
tmux -L "$socket" send-keys -t "$session" -l \
  "Follow CLAUDE.md to run the startup self-check, then stand by. Feishu chat: $chat_id"
sleep 1
tmux -L "$socket" send-keys -t "$session" Enter
echo "DISPATCH started: tmux -L $socket attach -t $session"
