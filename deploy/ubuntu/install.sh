#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible entrypoint. The canonical post-rsync installer is
# install-after-rsync.sh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/install-after-rsync.sh"
