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

Set SERVICE_USER=name if the systemd instance user is not the current user.
USAGE
    exit 1
    ;;
esac
