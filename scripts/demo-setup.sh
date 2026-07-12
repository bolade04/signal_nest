#!/usr/bin/env bash
# One-shot local setup: apply migrations, then seed demo data. Leaves the app ready to
# run via `npm run dev:api`.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
echo "==> [1/2] alembic upgrade head"
"$VENV_PY" -m alembic upgrade head
echo "==> [2/2] seed demo data"
"$VENV_PY" -m app.db.seed "$@"
echo ""
echo "Demo environment ready. Start the API with:  npm run dev:api"
