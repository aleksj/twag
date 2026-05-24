#!/usr/bin/env bash
set -euo pipefail

# Copy this file to deploy/ubuntu/rsync.privileged.sh and edit the values.
# The privileged copy is ignored by Git.

REMOTE_USER="${REMOTE_USER:-ubuntu}"
REMOTE_HOST="${REMOTE_HOST:-203.0.113.10}"
REMOTE_DIR="${REMOTE_DIR:-/home/$REMOTE_USER/twag}"
SSH_PORT="${SSH_PORT:-22}"
RUN_REMOTE_INSTALL="${RUN_REMOTE_INSTALL:-false}"
SYNC_ENV_FILE="${SYNC_ENV_FILE:-true}"
LOCAL_ENV_FILE="${LOCAL_ENV_FILE:-}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-/etc/twag/twag.env}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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

if [[ "$RUN_REMOTE_INSTALL" == "true" ]]; then
  ssh -p "$SSH_PORT" "$REMOTE_USER@$REMOTE_HOST" \
    "cd '$REMOTE_DIR' && SERVICE_USER='$REMOTE_USER' deploy/ubuntu/install-after-rsync.sh"
else
  cat <<EOF
Next on the Ubuntu host:
  ssh -p $SSH_PORT $REMOTE_USER@$REMOTE_HOST
  cd $REMOTE_DIR
  SERVICE_USER=$REMOTE_USER deploy/ubuntu/install-after-rsync.sh
EOF
fi
