#!/usr/bin/env bash
# Seed idempotent demo data (safe to run repeatedly). Passes through extra flags, e.g.
#   npm run seed
#   npm run seed -- --reset
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
echo "==> seeding demo data"
"$VENV_PY" -m app.db.seed "$@"
