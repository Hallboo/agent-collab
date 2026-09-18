# DISPATCH gateway configuration template.
#
# Copy to dispatch/config.sh (git-ignored) and fill in real values:
#   cp dispatch/config.example.sh dispatch/config.sh
#
# Values use conditional assignment, so an environment variable set before
# the scripts run wins over what config.sh would set. Empty a line to fall
# back to the script's built-in default.

# Feishu/Lark chat id of the controlling conversation. Required by
# launch.sh (its first argument overrides this).
: "${DISPATCH_CHAT_ID:=oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx}"

# cc profile env file sourced before starting claude (API credentials,
# model, launch arguments).
: "${DISPATCH_PROFILE:=$HOME/.config/agent-collab/dispatch.env}"

# agent-collab state directory of this deployment (the one holding
# agents/, rooms/ and archive/; same value as AGENT_COLLAB_STATE_DIR).
: "${DISPATCH_STATE_DIR:=$HOME/.local/state/agent-collab}"

# tmux socket and session name hosting the resident agent pane.
: "${DISPATCH_TMUX_SOCKET:=dispatcher}"
: "${DISPATCH_SESSION:=dispatch}"
