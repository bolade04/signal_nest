#!/usr/bin/env bash
# Show the current revision and full migration history.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
echo "==> current revision"
"$VENV_PY" -m alembic current --verbose
echo ""
echo "==> history"
"$VENV_PY" -m alembic history --indicate-current
