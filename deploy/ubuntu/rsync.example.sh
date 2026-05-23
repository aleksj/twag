#!/usr/bin/env bash
set -euo pipefail

# Copy this file to deploy/ubuntu/rsync.privileged.sh and edit the values.
# The privileged copy is ignored by Git.

REMOTE_USER="${REMOTE_USER:-ubuntu}"
REMOTE_HOST="${REMOTE_HOST:-203.0.113.10}"
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/twag}"
SSH_PORT="${SSH_PORT:-22}"
RUN_REMOTE_INSTALL="${RUN_REMOTE_INSTALL:-false}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

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
