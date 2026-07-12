#!/usr/bin/env bash
# One-shot developer bootstrap:
#   1. Install JS workspace dependencies (npm).
#   2. Create the API Python virtualenv (apps/api/.venv) if missing.
#   3. Install the API in editable mode with dev extras.
# Safe to re-run; each step is idempotent.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
API_DIR="$ROOT_DIR/apps/api"
VENV_DIR="$API_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"

cd "$ROOT_DIR"
echo "==> installing JS workspace dependencies"
npm install

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ ! -x "$VENV_PY" ]; then
  echo "==> creating API virtualenv ($VENV_DIR)"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "==> installing API (editable, with dev extras)"
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -e "$API_DIR[dev]"

echo ""
echo "Bootstrap complete. Next:"
echo "  npm run migrate   # create the SQLite schema"
echo "  npm run seed      # load demo data"
echo "  npm run dev       # start API + web"
