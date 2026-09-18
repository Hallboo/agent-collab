#!/usr/bin/env bash
# DISPATCH lifecycle management: an external supervisor (the agent cannot
# /compact or /clear itself; this script does it on the agent's behalf).
# Usage: supervisor.sh {status|ensure|compact|restart|daemon}
# Listener = the resident python gateway dispatch/gateway.py (flock
# single-instance, restarts lark-cli itself with backoff), decoupled from
# the DISPATCH session: while the session restarts, listening continues and
# event files are not lost.
# Suggested crontab (replace <repo> with this repository's absolute path):
#   */10 * * * *  <repo>/dispatch/supervisor.sh ensure
#   13 */6 * * *  <repo>/dispatch/supervisor.sh compact
#   41 4 * * *    <repo>/dispatch/supervisor.sh restart
set -euo pipefail
dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck disable=SC1091
[ -f "$dir/config.sh" ] && . "$dir/config.sh"
socket="${DISPATCH_TMUX_SOCKET:-dispatcher}"
session="${DISPATCH_SESSION:-dispatch}"
cmd="${1:-status}"

pane_tail() { tmux -L "$socket" capture-pane -t "$session" -p -S -15 2>/dev/null; }

gateway_armed() { pgrep -f 'dispatch/gateway\.py' >/dev/null 2>&1; }

ensure_gateway() {
  if gateway_armed; then return 0; fi
  echo "$(date '+%F %T') gateway missing; starting" >>"$dir/supervisor.log"
  mkdir -p "$dir/events"
  nohup python3 "$dir/gateway.py" >>"$dir/events/gateway.log" 2>&1 &
}

case "$cmd" in
  status)
    if tmux -L "$socket" has-session -t "$session" 2>/dev/null; then
      echo "session: running"
    else
      echo "session: DOWN"
    fi
    if gateway_armed; then echo "gateway: armed"; else echo "gateway: NOT armed"; fi
    ;;
  ensure)
    ensure_gateway
    if ! tmux -L "$socket" has-session -t "$session" 2>/dev/null; then
      "$dir/launch.sh"
      exit 0
    fi
    ;;
  compact)
    tmux -L "$socket" has-session -t "$session" 2>/dev/null || exit 0
    echo "$(date '+%F %T') injecting /compact" >>"$dir/supervisor.log"
    tmux -L "$socket" send-keys -t "$session" "/compact" Enter
    ;;
  restart)
    if ! tmux -L "$socket" has-session -t "$session" 2>/dev/null; then
      "$dir/launch.sh"
      exit 0
    fi
    tmux -L "$socket" send-keys -t "$session" \
      "Prepare for restart: per the CLAUDE.md discipline, write pending items to dispatch/STATE.md, then reply DONE and nothing else." Enter
    for _ in $(seq 1 24); do
      sleep 5
      pane_tail | grep -q 'DONE' && break
    done
    echo "$(date '+%F %T') graceful restart" >>"$dir/supervisor.log"
    tmux -L "$socket" kill-server 2>/dev/null || true
    sleep 2
    "$dir/launch.sh"
    # The gateway does not restart with the session; listening is uninterrupted.
    ;;
  daemon)
    # Resident supervision loop (for hosts without crond/systemd --user):
    # 60s watchdog, 6h compact, daily restart around 04:4x.
    last_compact=0
    last_restart_day=""
    while true; do
      bash "$dir/supervisor.sh" ensure
      now=$(date +%s)
      if (( now - last_compact >= 21600 )); then
        bash "$dir/supervisor.sh" compact && last_compact=$now
      fi
      today=$(date +%F)
      hour=$(date +%H)
      if [[ "$today" != "$last_restart_day" && "$hour" == "04" ]]; then
        bash "$dir/supervisor.sh" restart && last_restart_day=$today
      fi
      sleep 60
    done
    ;;
  *)
    echo "usage: $0 {status|ensure|compact|restart|daemon}" >&2
    exit 2
    ;;
esac
