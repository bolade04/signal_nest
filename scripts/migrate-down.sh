#!/usr/bin/env bash
# Roll back migrations. Defaults to a single step; pass a target to override, e.g.
#   npm run migrate:down            # downgrade -1
#   npm run migrate:down -- base    # downgrade to empty
#   npm run migrate:down -- <rev>   # downgrade to a specific revision
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
TARGET="${1:--1}"
echo "==> alembic downgrade $TARGET"
"$VENV_PY" -m alembic downgrade "$TARGET"
