#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="${SERVICE_USER:-$(id -un)}"
ACTION="${1:-status}"

case "$ACTION" in
  start|stop|restart|status)
    sudo systemctl "$ACTION" "twag-telegram-agent@$SERVICE_USER.service"
    sudo systemctl "$ACTION" "twag-nimble@$SERVICE_USER.service"
    ;;
  logs)
    journalctl -u "twag-telegram-agent@$SERVICE_USER.service" \
      -u "twag-nimble@$SERVICE_USER.service" -f
    ;;
  telegram-logs)
    journalctl -u "twag-telegram-agent@$SERVICE_USER.service" -f
    ;;
  nimble-logs)
    journalctl -u "twag-nimble@$SERVICE_USER.service" -f
    ;;
  diagnose)
    echo "systemd unit:"
    systemctl cat "twag-telegram-agent@$SERVICE_USER.service"
    echo
    echo "running processes:"
    ps -eo pid,ppid,user,lstart,command | grep -E 'twag-telegram-agent|twag telegram-agent' | grep -v grep || true
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

logger = logging.getLogger(client.CLICKHOUSE_HTTP_LOGGER)

print("python:", sys.executable)
print("client module:", client.__file__)
print("telegram module:", telegram_agent.__file__)
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
  deploy/ubuntu/control.sh nimble-logs
  deploy/ubuntu/control.sh diagnose

Set SERVICE_USER=name if the systemd instance user is not the current user.
USAGE
    exit 1
    ;;
esac
