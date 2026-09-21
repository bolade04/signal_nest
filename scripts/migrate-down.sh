#!/usr/bin/env bash
# Roll back migrations. Needs a confirmation bound to this exact database and
# transition: (cd apps/api && .venv/bin/python -m app.db.migrate downgrade-confirmation <target>)
#   npm run migrate:down -- -1 <TOKEN>     # one step down
#   npm run migrate:down -- base <TOKEN>   # downgrade to empty
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
TARGET="${1:--1}"; CONFIRM="${2:-}"   # pass the confirmation as the second argument
echo "==> alembic downgrade $TARGET"
"$VENV_PY" -m alembic ${CONFIRM:+-x confirm="$CONFIRM"} downgrade "$TARGET"
