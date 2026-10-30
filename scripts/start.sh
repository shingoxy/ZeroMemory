#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
export ZERO_HOST=127.0.0.1
exec .venv/bin/python -m zero_memory "$@"
