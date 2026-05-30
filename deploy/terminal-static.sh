#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.env"
  set +a
fi

BUILD_DIR="${TWAG_TERMINAL_STATIC_BUILD_DIR:-$ROOT_DIR/build/terminal-static}"
STATIC_REMOTE="${TWAG_TERMINAL_STATIC_REMOTE:?Set TWAG_TERMINAL_STATIC_REMOTE, for example root@data.flowers}"
STATIC_REMOTE_DIR="${TWAG_TERMINAL_STATIC_REMOTE_DIR:-/var/www/html/tw}"
STATIC_REMOTE_SSH_PORT="${TWAG_TERMINAL_STATIC_SSH_PORT:-22}"
STATIC_REMOTE_OWNER="${TWAG_TERMINAL_STATIC_OWNER:-root:root}"

uv run python scripts/build_terminal_static.py \
  --output "$BUILD_DIR" \
  --asset-base "." \
  --clean

rsync -az --delete \
  -e "ssh -p $STATIC_REMOTE_SSH_PORT" \
  "$BUILD_DIR/" "$STATIC_REMOTE:$STATIC_REMOTE_DIR/"

ssh -p "$STATIC_REMOTE_SSH_PORT" "$STATIC_REMOTE" \
  "chown -R '$STATIC_REMOTE_OWNER' '$STATIC_REMOTE_DIR' && find '$STATIC_REMOTE_DIR' -type d -exec chmod 755 {} + && find '$STATIC_REMOTE_DIR' -type f -exec chmod 644 {} +"

echo "Published TWAG terminal static assets to $STATIC_REMOTE:$STATIC_REMOTE_DIR"
