#!/usr/bin/env bash
# Integration smoke: start the FastAPI server against an already migrated + seeded
# SQLite database, wait for readiness with a bounded loop, run the HTTP smoke
# checks (scripts/smoke_http.py), and always stop the server — including on failure.
#
# Prerequisites (the caller / CI job must do this first):
#   npm run demo:setup      # migrate + seed the SQLite DB the server will read
#
# The server is started WITHOUT --reload (deterministic, single process, easy to
# kill). Local mode / SQLite / mock LLM are the defaults, so no external services
# are required.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv

HOST="${API_HOST:-127.0.0.1}"
PORT="${API_PORT:-8000}"
BASE_URL="http://${HOST}:${PORT}"
LOG_FILE="${SMOKE_LOG_FILE:-$ROOT_DIR/api-smoke.log}"
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "==> stopping API server (pid $SERVER_PID)"
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
# Guarantee the server is stopped on any exit path (success, failure, signal).
trap cleanup EXIT INT TERM

cd "$API_DIR"
echo "==> starting uvicorn app.main:app on ${BASE_URL} (log: $LOG_FILE)"
"$VENV_PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
SERVER_PID=$!

# Bounded readiness loop — poll /health until it answers 200 or we exhaust tries.
# No arbitrary long sleep; ~30s worst case at 0.5s intervals.
echo "==> waiting for ${BASE_URL}/health"
ready=""
for _ in $(seq 1 60); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: API server exited before becoming ready" >&2
    echo "----- api-smoke.log -----" >&2
    cat "$LOG_FILE" >&2 || true
    exit 1
  fi
  if curl -sf "${BASE_URL}/health" >/dev/null 2>&1; then
    ready="yes"
    break
  fi
  sleep 0.5
done

if [ -z "$ready" ]; then
  echo "ERROR: API server did not become ready in time" >&2
  echo "----- api-smoke.log -----" >&2
  cat "$LOG_FILE" >&2 || true
  exit 1
fi
echo "==> server is ready"

cd "$ROOT_DIR"
SMOKE_BASE_URL="$BASE_URL" "$VENV_PY" "$ROOT_DIR/scripts/smoke_http.py"
