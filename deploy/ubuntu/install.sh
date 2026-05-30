#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible entrypoint. The canonical remote installer is
# install-remote.sh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/install-remote.sh"
