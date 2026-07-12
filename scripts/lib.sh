#!/usr/bin/env bash
# Shared helpers for SignalNest dev scripts.
#
# Locates the repo root + api virtualenv and exposes:
#   $ROOT_DIR   repository root
#   $API_DIR    apps/api
#   $VENV_PY    absolute path to the api venv python
# plus require_venv(), which errors clearly when the venv is missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
API_DIR="$ROOT_DIR/apps/api"
VENV_DIR="$API_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"

require_venv() {
  if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: Python virtualenv not found at apps/api/.venv" >&2
    echo "" >&2
    echo "Create it and install the API before running this command:" >&2
    echo "  npm run bootstrap" >&2
    echo "  # or manually:" >&2
    echo "  cd apps/api && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
    exit 1
  fi
}
