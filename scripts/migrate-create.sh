#!/usr/bin/env bash
# Autogenerate a new migration from model changes.
#   npm run migrate:create -- --message "add widget table"
#   npm run migrate:create -- -m "add widget table"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"

MESSAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message)
      MESSAGE="${2:-}"; shift 2 ;;
    --message=*)
      MESSAGE="${1#*=}"; shift ;;
    *)
      # Allow bare message: npm run migrate:create -- "add widget table"
      MESSAGE="$1"; shift ;;
  esac
done

if [ -z "$MESSAGE" ]; then
  echo "ERROR: a migration message is required." >&2
  echo "Usage: npm run migrate:create -- --message \"describe the change\"" >&2
  exit 1
fi

echo "==> alembic revision --autogenerate -m \"$MESSAGE\""
"$VENV_PY" -m alembic revision --autogenerate -m "$MESSAGE"
echo ""
echo "Review the generated file in apps/api/alembic/versions/ before committing."
