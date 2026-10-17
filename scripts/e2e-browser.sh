#!/usr/bin/env bash
# Browser customer-flow proof (P6-CI-2): run the Playwright suite in the web workspace
# against the BUILT production bundle (served by `vite preview` with the same /api
# proxy as development), a real local API and the real durable job worker, all three
# sharing ONE disposable SQLite database that this script migrates from empty in a
# temporary directory. Nothing is seeded: the browser test registers its own synthetic
# customer and creates every resource through the UI. Every process is stopped on
# every exit path (trap on EXIT) and the temporary directory is removed; API and worker
# logs stay in the diagnostics directory for the CI artifact upload.
#
# Prerequisites (the caller / CI job must do this first):
#   npm ci && npm run build                    # the dist/ bundle the browser will load
#   the API virtualenv at apps/api/.venv       # scripts/bootstrap.sh, or the CI install
#   npm run e2e:install --workspace apps/web   # the Playwright-pinned Chromium build
#
# Modes: the zero-dependency local ones only — fixture connectors (the default source;
# the RSS connector stays disabled), the deterministic mock LLM, in-process queue,
# memory cache, brute-force vectors, local-file storage. The script REFUSES to run
# with a real LLM provider or a live connector enabled: no provider, mail service or
# cloud access is ever exercised by this proof.
#
# Fail-closed: the step fails when the bundle is missing, the database cannot be
# migrated, the API never becomes ready, the worker is not running, Playwright fails,
# or the JSON report shows anything other than every collected test executed once and
# passed (apps/web/scripts/assert-e2e-report.mjs — no skip, no retry, no empty run).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

API_HOST="${E2E_API_HOST:-127.0.0.1}"
API_PORT="${E2E_API_PORT:-8765}"
WEB_PORT="${E2E_WEB_PORT:-4173}"
API_URL="http://${API_HOST}:${API_PORT}"
LOG_DIR="${E2E_LOG_DIR:-$ROOT_DIR/ci-logs}"
RESULTS_DIR="$LOG_DIR/e2e-results"
API_LOG="$LOG_DIR/e2e-api.log"
WORKER_LOG="$LOG_DIR/e2e-worker.log"

# Deterministic modes. Each is overridable ONLY to another deterministic value; the
# guards below refuse the live ones outright.
export APP_MODE="${APP_MODE:-local}"
export ENVIRONMENT="${ENVIRONMENT:-test}"
export QUEUE_BACKEND="${QUEUE_BACKEND:-inprocess}"
export CACHE_BACKEND="${CACHE_BACKEND:-memory}"
export VECTOR_BACKEND="${VECTOR_BACKEND:-bruteforce}"
export STORAGE_BACKEND="${STORAGE_BACKEND:-local}"
export LLM_PROVIDER="${LLM_PROVIDER:-mock}"
export LLM_MOCK_SEED="${LLM_MOCK_SEED:-signalnest-e2e}"
export CONNECTOR_RSS_ENABLED="${CONNECTOR_RSS_ENABLED:-false}"
export WORKER_POLL_INTERVAL_SECONDS="${WORKER_POLL_INTERVAL_SECONDS:-0.5}"
if [ "$LLM_PROVIDER" != "mock" ]; then
  echo "ERROR: the browser proof runs only with LLM_PROVIDER=mock (got '$LLM_PROVIDER'); no real provider is exercised here" >&2
  exit 1
fi
if [ "$CONNECTOR_RSS_ENABLED" != "false" ]; then
  echo "ERROR: the browser proof runs only with the fixture connectors (CONNECTOR_RSS_ENABLED='$CONNECTOR_RSS_ENABLED')" >&2
  exit 1
fi

if [ ! -f apps/web/dist/index.html ]; then
  echo "ERROR: apps/web/dist/index.html is missing — build the production bundle first (npm run build)" >&2
  exit 1
fi
if [ ! -x apps/api/.venv/bin/python ]; then
  echo "ERROR: Python virtualenv not found at apps/api/.venv — install the API first" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
rm -rf "$RESULTS_DIR"
E2E_TMP="$(mktemp -d "${TMPDIR:-/tmp}/signalnest-e2e.XXXXXX")"
export DATABASE_URL="sqlite:///${E2E_TMP}/e2e.db"
export LOCAL_STORAGE_DIR="${E2E_TMP}/storage"
API_PID=""
WORKER_PID=""

cleanup() {
  for pid in "$WORKER_PID" "$API_PID"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "==> stopping pid $pid"
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  rm -rf "$E2E_TMP"
}
# Guarantee the API, the worker and the disposable database are gone on any exit path.
trap cleanup EXIT INT TERM

echo "==> disposable database: $DATABASE_URL (migrating from empty)"
cd apps/api
.venv/bin/python -m alembic upgrade head

echo "==> starting uvicorn app.main:app on ${API_URL} (log: $API_LOG)"
.venv/bin/python -m uvicorn app.main:app --host "$API_HOST" --port "$API_PORT" >"$API_LOG" 2>&1 &
API_PID=$!

echo "==> starting the durable job worker (log: $WORKER_LOG)"
.venv/bin/python -m app.jobs.worker >"$WORKER_LOG" 2>&1 &
WORKER_PID=$!
cd "$ROOT_DIR"

# Bounded readiness loop — poll /health until it answers 200 or we exhaust tries
# (~30 s worst case at 0.5 s intervals). No arbitrary long sleep.
echo "==> waiting for ${API_URL}/health"
ready=""
for _ in $(seq 1 60); do
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "ERROR: API server exited before becoming ready" >&2
    cat "$API_LOG" >&2 || true
    exit 1
  fi
  if curl -sf "${API_URL}/health" >/dev/null 2>&1; then
    ready="yes"
    break
  fi
  sleep 0.5
done
if [ -z "$ready" ]; then
  echo "ERROR: API server did not become ready in time" >&2
  cat "$API_LOG" >&2 || true
  exit 1
fi
echo "==> API is ready"

# The worker has no port: it is ready once it has logged its start and is still alive.
started=""
for _ in $(seq 1 60); do
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "ERROR: the job worker exited before the browser test started" >&2
    cat "$WORKER_LOG" >&2 || true
    exit 1
  fi
  if grep -q "worker.start" "$WORKER_LOG" 2>/dev/null; then
    started="yes"
    break
  fi
  sleep 0.5
done
if [ -z "$started" ]; then
  echo "ERROR: the job worker did not report its start in time" >&2
  cat "$WORKER_LOG" >&2 || true
  exit 1
fi
echo "==> worker is running (pid $WORKER_PID)"

echo "==> running the Playwright customer flow (chromium) against the built bundle on port ${WEB_PORT}"
VITE_API_PROXY_TARGET="$API_URL" E2E_WEB_PORT="$WEB_PORT" E2E_RESULTS_DIR="$RESULTS_DIR" \
  npm run e2e --workspace apps/web

echo "==> gating on the JSON report"
node apps/web/scripts/assert-e2e-report.mjs "$RESULTS_DIR/report.json"

if ! kill -0 "$WORKER_PID" 2>/dev/null; then
  echo "ERROR: the job worker is no longer running after the test" >&2
  cat "$WORKER_LOG" >&2 || true
  exit 1
fi
echo "==> browser customer flow PASSED"
