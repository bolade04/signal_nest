#!/usr/bin/env bash
# Delete all demo/seed data and reseed from scratch (local development only).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
echo "==> reset + reseed demo data"
"$VENV_PY" -m app.db.seed --reset "$@"
