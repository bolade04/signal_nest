# Testing

Two independent suites, each runnable on its own.

| Suite | Command | Count | Stack |
| --- | --- | --- | --- |
| Frontend | `npm test` | 20 | Vitest + React Testing Library + MSW v2 |
| Backend | `npm run test:api` | 86 | pytest |

## Backend (`apps/api`)

Pure-engine unit tests need no FastAPI or database and run in milliseconds.

- **`test_scoring_engines.py`**
  - Relevance: strong match clears the action floor; off-topic falls below it;
    exclusion terms hard-kill relevance.
  - Opportunity/confidence: weighted totals stay within 0–100; strong signals score
    high, weak ones low; classification and confidence bands; confidence rewards
    evidence quantity, diversity, and score consistency.
  - Noise gate: spam and bot authors are hard noise; a quality signal clears the
    full-analysis threshold; short content is penalized for low context.
  - Validation: cross-source agreement dominates; total caps at 100.
- **`test_decision_and_geo.py`**
  - Decision engine: blocked risk blocks; noise stays silent; out-of-area monitors;
    **relevance < 40 never recommends action**; unclear audience fit monitors;
    high-priority + high confidence acts now; validated acts soon.
  - Geography: haversine sanity (Dallas→London ≈ 4.9k mi); radius inside/outside;
    unevaluable radius returns `None`; explicit exclusions win; online-global covers
    everything; geo-relevance resolves the strongest-evidence market with confidence.
  - Claim safety: health claims flagged high; financial claims blocked; explicit
    blocked-claim list; strict industries escalate medium→high; clean text is low
    risk; competitor-weakness translation stays safe.
- **`test_api_isolation.py`** (integration — drives the real FastAPI app via
  `TestClient` against the seeded SQLite DB)
  - Unauthenticated requests are rejected (401/403).
  - A valid demo login yields a usable bearer token.
  - **Per-location isolation:** querying opportunities by `location_id` only ever
    returns that location's rows, and no opportunity id appears under two locations.
  - The unfiltered feed is a superset of every location's filtered feed.

  These tests require a migrated + seeded database (`npm run demo:setup`). If the demo
  data is absent, the module skips itself rather than failing spuriously.

  The same module also covers the Phase 3A **system** endpoints against the seeded
  instance. After the Phase 3A.2 hardening the surface is split by audience:
  `/system/health` is live (anonymous); `/system/readiness` reports ready (schema
  migrated + all probes healthy, anonymous, secret-free); `/system/capabilities`
  requires authentication and returns only a **coarse summary** (mode + readiness, no
  per-capability topology); and the detailed topology moved to operator-only
  `/internal/system/capabilities` and `/internal/system/readiness` — anonymous callers
  get `401`, an authenticated non-operator customer gets `403`, and only an operator sees
  the detailed (still secret-free) report.
- **`test_runtime_foundation.py`** (Phase 3A runtime foundation — pure unit, no DB)
  - Configuration rejects invalid production combinations (full mode + SQLite, mock LLM
    in production, missing secret key, real provider without an API key, dev fallback in
    production) — proving there is **no silent production fallback**.
  - Phase 3A.2 adds environment-driven enforcement: `environment=production` requires
    `app_mode=full` and rejects each local-only backend by name (SQLite, in-process
    queue, in-memory cache, local storage, brute-force vector); the default local stack
    is rejected wholesale; `storage_backend=s3` requires a bucket; readiness timeout
    bounds are validated (positive, per-probe ≤ total); and a configuration error
    **never echoes secret values**.
  - The capability registry classifies each backend (local vs external), flags
    unconfigured production backends instead of reporting them healthy, and **never
    exposes secrets** in its public view.
  - Job envelopes are versioned and deterministic (stable `envelope_hash`), reject
    unknown contract versions, and remain backward compatible with the legacy bare
    payload; the tenant execution context makes org/workspace/location isolation
    explicit.
- **`test_readiness_probes.py`** (Phase 3A.2 bounded readiness probes — pure unit)
  - The seeded local stack probes all report healthy; the **public** probe view excludes
    hosts/ports/URLs/adapter detail while the operator view carries safe diagnostics.
  - `storage_backend=s3` without a bucket is `not_configured` (never healthy); the local
    storage probe **rejects path traversal**; a real LLM provider is `degraded` (config
    only) without a live paid call.
  - Probe execution is **bounded and non-blocking**: a probe that hangs for seconds is
    cut off well under its own timeout; a failed required probe blocks overall readiness.
  - A probe that raises exposes only the exception **class name** in its operator detail —
    never the raw message — so a driver error's host/port/URL never reaches a response.

## Frontend (`apps/web`)

Component/integration tests render the real `App` with a small in-memory MSW backend
(`src/test/handlers.ts`) modeling one org → one workspace → one brand → four
independent cities.

- **`auth.test.tsx`** — sign-in flow and protected-route redirects.
- **`onboarding.test.tsx`** — the wizard offers every presence path, gates continue
  on a business name, and autosaves the draft to `localStorage`.
- **`campaign-context.test.tsx`** — context tabs, empty state, add-product dialog.
- **`scout-requests.test.tsx`** — lists all requests with status, runs a scout to
  completion, and opens a detail page with its configuration and simulated-source
  disclosure.
- **`opportunities.test.tsx`** — renders scored cards with human-readable score labels
  and simulated badges; **proves strict per-location isolation** (Dallas shows only
  Dallas; London only London); filters by classification.
- **`opportunity-detail.test.tsx`** — separates observed evidence from AI inference,
  exposes a traceable source link, surfaces known-limitation/simulated disclosures,
  and lets the user change status.
- **`locations.test.tsx`** — opening the edit dialog seeds the form from the selected
  location (regression guard for the render-phase form-reset in `LocationDialog`).
- **`settings-runtime.test.tsx`** — the Settings page surfaces the coarse runtime
  summary from `/system/capabilities` (local zero-dependency mode) for every
  authenticated user; an **operator** additionally sees the per-capability
  infrastructure detail from `/internal/system/capabilities` (database, queue, cache,
  vector, storage, llm), while a **non-operator** customer never triggers that internal
  endpoint and never sees the operator-only section.

## Also verified

- `npm run type-check` — strict `tsc -b --noEmit`, clean.
- `npm run lint` — ESLint with `--max-warnings 0`, clean.
- `npm run build` — production web build succeeds (single large-chunk warning only).

## Continuous integration

GitHub Actions runs the Phase 1–2 quality gates on every change:
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

**Triggers**

- Pull requests targeting `main`
- Pushes to `main`
- Pushes to `feature/**`
- Manual `workflow_dispatch`

Concurrency is grouped per workflow + branch/PR with `cancel-in-progress: true`, so a
newer commit cancels older in-flight runs. Workflow permissions are `contents: read`.

**Reliable failure propagation.** Every job step runs under a strict shell
(`shell: bash --noprofile --norc -euo pipefail {0}`) so a failing command in a
`cmd | tee log` pipeline is no longer masked by `tee`'s success. `npm run test:ci-pipefail`
is a maintained regression guard for this behavior; diagnostic log uploads remain
conditional on failure only.

**Jobs** (each name is stable and suitable as a required status check):

| Check name | What it runs |
| --- | --- |
| **Frontend quality** | `npm ci`, `npm run lint` (zero warnings), `npm run type-check`, `npm run test`, `npm run build` |
| **Backend quality** | fresh venv + `pip install -e apps/api[dev]`, `ruff check`, `demo-setup.sh` (migrate + seed), `pytest` |
| **Migrations and API contract** | empty-DB `alembic upgrade head`, `alembic check` (drift), seed, seed again (idempotency), `downgrade base`, `upgrade head`, `seed --reset`, then `npm run gen:types` + `git diff --exit-code` (contract drift) |
| **Integration smoke** | after the three gates pass: migrate + seed, start the API, wait on `/health`, run the HTTP isolation smoke, always stop the server |

**Environment.** CI runs entirely in the default zero-dependency **local** mode:
SQLite (`DATABASE_URL=sqlite:///./signalnest.db`), in-process queue
(`QUEUE_BACKEND=inprocess`), in-memory cache (`CACHE_BACKEND=memory`), brute-force
vector index (`VECTOR_BACKEND=bruteforce`), local-file storage
(`STORAGE_BACKEND=local`), the fixture connectors, and the deterministic mock LLM
(`LLM_PROVIDER=mock`, `LLM_MOCK_SEED=signalnest-ci`), with `ENVIRONMENT=test`. These
are the actual `Settings` field names (`apps/api/app/core/config.py`). **Docker,
PostgreSQL, Redis and paid OpenAI/Anthropic API keys are not required and no secrets
are used.** Runtimes: Node 20 (repo engines) and Python 3.12. On failure, each job
uploads its logs (`ci-logs/`, `api-smoke.log`) as artifacts.

**Recommended required status checks for `main`:** Frontend quality, Backend quality,
Migrations and API contract, Integration smoke.

### Running each CI gate locally

```bash
npm ci                       # clean install (matches CI)
npm run bootstrap            # create apps/api/.venv (or: python -m venv apps/api/.venv && .venv/bin/pip install -e 'apps/api[dev]')

# Frontend quality
npm run lint
npm run type-check
npm test
npm run build

# Backend quality
cd apps/api && .venv/bin/python -m ruff check . && cd ../..
npm run demo:setup
npm run test:api

# Migrations and API contract
cd apps/api
rm -f signalnest.db
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic check
.venv/bin/python -m app.db.seed
.venv/bin/python -m app.db.seed          # idempotent re-run
.venv/bin/python -m alembic downgrade base
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m app.db.seed --reset
cd ../..
npm run gen:types && git diff --exit-code -- apps/api/openapi.json apps/web/src/api/schema.d.ts

# Integration smoke (starts + stops the API for you)
npm run demo:setup
npm run smoke
```
