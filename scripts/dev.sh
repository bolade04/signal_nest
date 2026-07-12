#!/usr/bin/env bash
# Start the full local stack: FastAPI (SQLite) + Vite web dev server.
# Both run in the foreground; Ctrl-C stops both.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv

HOST="${API_HOST:-127.0.0.1}"
PORT="${API_PORT:-8000}"

cd "$API_DIR"
echo "==> starting API  (http://$HOST:$PORT)"
"$VENV_PY" -m uvicorn app.main:app --reload --host "$HOST" --port "$PORT" &
API_PID=$!

# Ensure the API is stopped whenever this script exits.
cleanup() {
  echo ""
  echo "==> stopping API"
  kill "$API_PID" 2>/dev/null || true
  wait "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"
echo "==> starting web  (http://localhost:3000)"
npm run dev --workspace apps/web
