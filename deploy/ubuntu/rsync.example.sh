#!/usr/bin/env bash
set -euo pipefail

# Deployment variables are loaded from the ignored .env file when present.
# You can still override them in the shell for one-off runs.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.env"
  set +a
fi

REMOTE_USER="${REMOTE_USER:-ubuntu}"
REMOTE_HOST="${REMOTE_HOST:?Set REMOTE_HOST in .env or the shell}"
REMOTE_DIR="${REMOTE_DIR:-/home/$REMOTE_USER/twag}"
SSH_PORT="${SSH_PORT:-22}"
RUN_REMOTE_INSTALL="${RUN_REMOTE_INSTALL:-false}"
DEPLOY_TERMINAL_STATIC="${DEPLOY_TERMINAL_STATIC:-false}"
SYNC_ENV_FILE="${SYNC_ENV_FILE:-true}"
LOCAL_ENV_FILE="${LOCAL_ENV_FILE:-}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-/etc/twag/twag.env}"
LOCAL_ENV_FILE="${LOCAL_ENV_FILE:-$ROOT_DIR/.env}"

rsync -az --delete \
  -e "ssh -p $SSH_PORT" \
  --exclude ".git/" \
  --exclude ".env" \
  --exclude ".venv/" \
  --exclude ".pytest_cache/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude "*.pyo" \
  --exclude "*.egg-info/" \
  --exclude ".telegram-agent.lock" \
  --exclude "deploy/ubuntu/rsync.privileged.sh" \
  "$ROOT_DIR/" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"

echo "Synced $ROOT_DIR to $REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR"

if [[ "$SYNC_ENV_FILE" == "true" ]]; then
  if [[ -f "$LOCAL_ENV_FILE" ]]; then
    remote_tmp="$REMOTE_DIR/.twag.env.$RANDOM.$$"
    remote_env_dir="$(dirname "$REMOTE_ENV_FILE")"

    rsync -az \
      -e "ssh -p $SSH_PORT" \
      "$LOCAL_ENV_FILE" "$REMOTE_USER@$REMOTE_HOST:$remote_tmp"

    ssh -p "$SSH_PORT" "$REMOTE_USER@$REMOTE_HOST" \
      "set -eu; service_group=\$(id -gn '$REMOTE_USER'); sudo install -d -m 0750 -o '$REMOTE_USER' -g \"\$service_group\" '$remote_env_dir'; sudo install -m 0640 -o '$REMOTE_USER' -g \"\$service_group\" '$remote_tmp' '$REMOTE_ENV_FILE'; rm -f '$remote_tmp'"

    echo "Synced $LOCAL_ENV_FILE to $REMOTE_USER@$REMOTE_HOST:$REMOTE_ENV_FILE"
  else
    echo "No local env file at $LOCAL_ENV_FILE; skipped remote env sync."
  fi
fi

if [[ "$DEPLOY_TERMINAL_STATIC" == "true" ]]; then
  "$ROOT_DIR/deploy/terminal-static.sh"
fi

if [[ "$RUN_REMOTE_INSTALL" == "true" ]]; then
  ssh -p "$SSH_PORT" "$REMOTE_USER@$REMOTE_HOST" \
    "cd '$REMOTE_DIR' && SERVICE_USER='$REMOTE_USER' deploy/ubuntu/install-after-rsync.sh"
  cat <<EOF

Remote install completed on $REMOTE_USER@$REMOTE_HOST.

Already done:
  - synced repository to $REMOTE_DIR
  - synced env file to $REMOTE_ENV_FILE when SYNC_ENV_FILE=true and the local env existed
  - installed/updated the remote venv and Python package
  - installed systemd unit templates and reloaded systemd

Still required:
  - verify $REMOTE_ENV_FILE has the expected secrets
  - enable/start new services, or restart already-enabled services

Run on the Ubuntu host:
  ssh -p $SSH_PORT $REMOTE_USER@$REMOTE_HOST
  sudo systemctl enable --now twag-telegram-agent@$REMOTE_USER.service
  sudo systemctl enable --now twag-telegram-agent-boston@$REMOTE_USER.service
  sudo systemctl enable --now twag-sync-agent@$REMOTE_USER.service
  sudo systemctl enable --now twag-terminal@$REMOTE_USER.service

For an existing deployment, restart instead:
  sudo systemctl restart twag-telegram-agent@$REMOTE_USER.service twag-telegram-agent-boston@$REMOTE_USER.service twag-sync-agent@$REMOTE_USER.service twag-terminal@$REMOTE_USER.service

Then check:
  systemctl status twag-telegram-agent@$REMOTE_USER.service twag-telegram-agent-boston@$REMOTE_USER.service twag-sync-agent@$REMOTE_USER.service twag-terminal@$REMOTE_USER.service --no-pager
EOF
else
  cat <<EOF
Repository sync completed on $REMOTE_USER@$REMOTE_HOST.

Already done:
  - synced repository to $REMOTE_DIR
  - synced env file to $REMOTE_ENV_FILE when SYNC_ENV_FILE=true and the local env existed

Still required:
  - install/update the remote venv and systemd units
  - verify $REMOTE_ENV_FILE has the expected secrets
  - enable/start new services, or restart already-enabled services

Run on the Ubuntu host:
  ssh -p $SSH_PORT $REMOTE_USER@$REMOTE_HOST
  cd $REMOTE_DIR
  SERVICE_USER=$REMOTE_USER deploy/ubuntu/install-after-rsync.sh

Then enable/start services:
  sudo systemctl enable --now twag-telegram-agent@$REMOTE_USER.service twag-telegram-agent-boston@$REMOTE_USER.service twag-sync-agent@$REMOTE_USER.service twag-terminal@$REMOTE_USER.service
EOF
fi
