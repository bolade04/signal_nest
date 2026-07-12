#!/usr/bin/env bash
# Regenerate the typed frontend API contract from the FastAPI OpenAPI schema.
# Dumps a fresh apps/api/openapi.json from the app object (no running server
# required) then runs openapi-typescript to update apps/web/src/api/schema.d.ts.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_venv
cd "$API_DIR"
echo "==> exporting apps/api/openapi.json"
"$VENV_PY" - <<'PY'
import json
from app.main import app

with open("openapi.json", "w", encoding="utf-8") as fh:
    json.dump(app.openapi(), fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
echo "==> openapi-typescript -> apps/web/src/api/schema.d.ts"
cd "$ROOT_DIR"
npm run api:generate --workspace apps/web
