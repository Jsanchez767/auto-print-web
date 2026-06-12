#!/bin/bash
# Launcher for the Auto-Print agent, used by the launchd service.
# Reads RELAY_URL and AGENT_TOKEN from a private env file (kept out of git).
set -euo pipefail

ENV_FILE="${AUTO_PRINT_ENV:-$HOME/.config/auto-print/agent.env}"
AGENT="${AUTO_PRINT_AGENT:-$HOME/auto-print-web/agent.py}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${RELAY_URL:-}" || -z "${AGENT_TOKEN:-}" ]]; then
  echo "Missing RELAY_URL or AGENT_TOKEN. Set them in $ENV_FILE" >&2
  exit 78  # EX_CONFIG
fi

# Ensure CUPS tools (lp/lpstat) are on PATH for the launchd environment.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Flush stdout/stderr immediately so the log files update in real time.
export PYTHONUNBUFFERED=1

exec /usr/bin/python3 "$AGENT" --relay "$RELAY_URL" --token "$AGENT_TOKEN"
