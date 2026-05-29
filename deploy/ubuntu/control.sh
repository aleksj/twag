#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="${SERVICE_USER:-$(id -un)}"
ACTION="${1:-status}"
JOURNAL_ARGS=()
if [[ -n "${LOG_SINCE:-}" ]]; then
  JOURNAL_ARGS+=(--since "$LOG_SINCE")
fi
if [[ "${LOG_FOLLOW:-true}" == "true" ]]; then
  JOURNAL_ARGS+=(-f)
fi

case "$ACTION" in
  start|stop|restart|status)
    sudo systemctl "$ACTION" "twag-telegram-agent@$SERVICE_USER.service"
    sudo systemctl "$ACTION" "twag-telegram-agent-boston@$SERVICE_USER.service"
    sudo systemctl "$ACTION" "twag-sync-agent@$SERVICE_USER.service"
    sudo systemctl "$ACTION" "twag-terminal@$SERVICE_USER.service"
    ;;
  logs)
    journalctl -u "twag-telegram-agent@$SERVICE_USER.service" \
      -u "twag-telegram-agent-boston@$SERVICE_USER.service" \
      -u "twag-sync-agent@$SERVICE_USER.service" \
      -u "twag-terminal@$SERVICE_USER.service" "${JOURNAL_ARGS[@]}"
    ;;
  telegram-logs)
    journalctl -u "twag-telegram-agent@$SERVICE_USER.service" \
      -u "twag-telegram-agent-boston@$SERVICE_USER.service" "${JOURNAL_ARGS[@]}"
    ;;
  ny-telegram-logs)
    journalctl -u "twag-telegram-agent@$SERVICE_USER.service" "${JOURNAL_ARGS[@]}"
    ;;
  boston-telegram-logs)
    journalctl -u "twag-telegram-agent-boston@$SERVICE_USER.service" "${JOURNAL_ARGS[@]}"
    ;;
  sync-agent-logs|nimble-logs)
    journalctl -u "twag-sync-agent@$SERVICE_USER.service" "${JOURNAL_ARGS[@]}"
    ;;
  terminal-logs)
    journalctl -u "twag-terminal@$SERVICE_USER.service" "${JOURNAL_ARGS[@]}"
    ;;
  nginx-diagnose)
    if ! command -v nginx >/dev/null 2>&1; then
      echo "nginx is not installed on this host." >&2
      exit 1
    fi
    sudo nginx -t
    echo
    echo "Enabled nginx site links:"
    find /etc/nginx/sites-enabled -maxdepth 1 -type l -print -exec readlink -f {} \; 2>/dev/null || true
    echo
    echo "Server-name conflicts from nginx -T:"
    sudo nginx -T 2>&1 | grep -F "conflicting server name" || echo "No conflicting server_name warnings found."
    ;;
  diagnose)
    echo "NY systemd unit:"
    systemctl cat "twag-telegram-agent@$SERVICE_USER.service"
    echo
    echo "Boston systemd unit:"
    systemctl cat "twag-telegram-agent-boston@$SERVICE_USER.service"
    echo
    echo "Terminal systemd unit:"
    systemctl cat "twag-terminal@$SERVICE_USER.service"
    echo
    echo "Sync-agent systemd unit:"
    systemctl cat "twag-sync-agent@$SERVICE_USER.service"
    echo
    echo "running processes:"
    ps -eo pid,ppid,user,lstart,command | grep -E 'twag-telegram-agent|twag telegram-agent|twag-terminal-server|twag-sync-agent' | grep -v grep || true
    echo
    echo "import diagnostics:"
    python_bin="$(pwd)/.venv/bin/python"
    if [[ ! -x "$python_bin" ]]; then
      python_bin=".venv/bin/python"
    fi
    "$python_bin" - <<'PY'
import logging
import sys

import twag_clickhouse.client as client
import twag_clickhouse.telegram_agent as telegram_agent
import twag_clickhouse.terminal_server as terminal_server

logger = logging.getLogger(client.CLICKHOUSE_HTTP_LOGGER)

print("python:", sys.executable)
print("client module:", client.__file__)
print("telegram module:", telegram_agent.__file__)
print("terminal module:", terminal_server.__file__)
print("clickhouse logger:", client.CLICKHOUSE_HTTP_LOGGER)
print("noise filters:", [type(filter_).__name__ for filter_ in logger.filters])
print("noise warning:", client.CLICKHOUSE_NOISY_WARNING)
PY
    ;;
  *)
    cat >&2 <<'USAGE'
Usage:
  deploy/ubuntu/control.sh start
  deploy/ubuntu/control.sh stop
  deploy/ubuntu/control.sh restart
  deploy/ubuntu/control.sh status
  deploy/ubuntu/control.sh logs
  deploy/ubuntu/control.sh telegram-logs
  deploy/ubuntu/control.sh ny-telegram-logs
  deploy/ubuntu/control.sh boston-telegram-logs
  deploy/ubuntu/control.sh sync-agent-logs
  deploy/ubuntu/control.sh terminal-logs
  deploy/ubuntu/control.sh nginx-diagnose
  deploy/ubuntu/control.sh diagnose

Set LOG_SINCE="2 hours ago" and/or LOG_FOLLOW=false for bounded journal output.

Set SERVICE_USER=name if the systemd instance user is not the current user.
USAGE
    exit 1
    ;;
esac
