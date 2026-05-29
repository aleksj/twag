#!/usr/bin/env bash
set -euo pipefail

command="${TWAG_SYNC_AGENT_COMMAND:-.venv/bin/twag-sync-agent}"

exec /bin/bash -lc "exec $command"
