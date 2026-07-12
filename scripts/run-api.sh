#!/usr/bin/env bash
# Start the FastAPI backend (SQLite / local mode by default) with autoreload.
# Extra flags are passed through to uvicorn, e.g.
#   npm run dev:api
#   npm run dev:api -- --port 9000
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
HOST="${API_HOST:-127.0.0.1}"
PORT="${API_PORT:-8000}"
echo "==> uvicorn app.main:app  (http://$HOST:$PORT)"
exec "$VENV_PY" -m uvicorn app.main:app --reload --host "$HOST" --port "$PORT" "$@"
