#!/usr/bin/env bash
# Start the durable job worker (SQLite / local mode by default).
# The worker is a SEPARATE process from the API; it claims and executes queued
# jobs (e.g. scout-request runs). Extra flags/env are honoured, e.g.
#   npm run worker
#   WORKER_CONCURRENCY=2 npm run worker
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
echo "==> durable job worker  (python -m app.jobs.worker)"
exec "$VENV_PY" -m app.jobs.worker "$@"
