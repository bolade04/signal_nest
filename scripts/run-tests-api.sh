#!/usr/bin/env bash
# Run the API test suite (pytest). Extra flags pass through, e.g.
#   npm run test:api
#   npm run test:api -- -k scoring -q
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
echo "==> pytest"
exec "$VENV_PY" -m pytest "$@"
