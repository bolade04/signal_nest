#!/usr/bin/env bash
# Apply all pending migrations (upgrade to head).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
echo "==> alembic upgrade head"
"$VENV_PY" -m alembic upgrade head
