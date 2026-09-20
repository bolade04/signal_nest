#!/usr/bin/env bash
# Start the full local stack: FastAPI (SQLite) + durable job worker + Vite web.
#
# All three run as children of this script; Ctrl-C (or any exit) stops all three.
# The worker is not optional: "Run now" enqueues a durable job that only a worker
# can execute, so a stack without one accepts work it will never perform.
#
# Prerequisite: the schema must exist (npm run demo:setup, or npm run migrate).
# Both Python processes fail fast and loudly on an un-migrated database.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv

HOST="${API_HOST:-127.0.0.1}"
PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-5173}"

# Bounded drain budget, generous enough to cover the worker's whole shutdown:
# WORKER_SHUTDOWN_GRACE_SECONDS (default 10s) for in-flight jobs, plus the
# fleet-heartbeat join (<=1s) and graceful_shutdown's bounded telemetry flush
# (TRACING_SHUTDOWN_FLUSH_SECONDS, default 5s) — ~16s worst case. Escalating to
# SIGKILL before that would abort a worker that was draining correctly.
STOP_TIMEOUT="${DEV_STOP_TIMEOUT:-20}"

API_PID=""
WORKER_PID=""
WEB_PID=""
STOPPING=""

# Declared and trapped BEFORE anything is spawned, so a signal landing mid-startup
# can never orphan a child that the trap did not yet know about.
cleanup() {
  [ -n "$STOPPING" ] && return 0 # idempotent: the INT handler runs, then EXIT
  STOPPING=1
  echo ""
  echo "==> stopping web / worker / API"
  for pid in $WEB_PID $WORKER_PID $API_PID; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  # Bounded wait for a graceful drain, then escalate. Never hangs the terminal.
  deadline=$((SECONDS + STOP_TIMEOUT))
  for pid in $WEB_PID $WORKER_PID $API_PID; do
    while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do
      sleep 0.2
    done
  done
  for pid in $WEB_PID $WORKER_PID $API_PID; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "==> forcing pid $pid" >&2
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true # reap; never leave zombies behind
}
on_signal() {
  cleanup
  exit 130
}
trap on_signal INT
trap on_signal TERM
trap cleanup EXIT

# Both Python processes must run from apps/api: DATABASE_URL defaults to the
# CWD-relative sqlite:///./signalnest.db and Settings reads a relative .env, so a
# different CWD would silently attach the worker to a different, empty database.
cd "$API_DIR"

echo "==> starting API     (http://$HOST:$PORT)"
"$VENV_PY" -m uvicorn app.main:app --reload --host "$HOST" --port "$PORT" &
API_PID=$!

echo "==> starting worker  (python -m app.jobs.worker)"
"$VENV_PY" -m app.jobs.worker &
WORKER_PID=$!

cd "$ROOT_DIR"
echo "==> starting web     (http://localhost:$WEB_PORT)"
npm run dev --workspace apps/web &
WEB_PID=$!

echo "==> stack up: API $API_PID / worker $WORKER_PID / web $WEB_PID — Ctrl-C stops all three"

# Supervise. macOS ships bash 3.2, which has no `wait -n`, so poll liveness the
# way scripts/ci-smoke.sh already does. A child that exits takes the whole stack
# down with a named, visible error instead of leaving a half-running stack that
# looks healthy in the browser.
while :; do
  for entry in "API:$API_PID" "worker:$WORKER_PID" "web:$WEB_PID"; do
    name="${entry%%:*}"
    pid="${entry##*:}"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "" >&2
      echo "ERROR: $name (pid $pid) exited — stopping the rest of the stack." >&2
      case "$name" in
        worker)
          echo "  The worker refuses to start if the durable-job schema is missing" >&2
          echo "  or a built-in job handler is unregistered." >&2
          echo "  Try: npm run demo:setup   (or npm run migrate)" >&2
          ;;
        API)
          echo "  Common causes: port $PORT already in use, or an un-migrated database." >&2
          ;;
        web)
          echo "  Common causes: port $WEB_PORT already in use, or missing JS deps." >&2
          echo "  Try: npm run bootstrap" >&2
          ;;
      esac
      exit 1 # the EXIT trap stops the other two
    fi
  done
  sleep 1
done
