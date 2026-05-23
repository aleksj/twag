#!/usr/bin/env bash
set -euo pipefail

command="${TWAG_NIMBLE_COMMAND:-.venv/bin/twag-nytw-tool-server}"

exec /bin/bash -lc "exec $command"
