# Testing

Two independent suites, each runnable on its own.

| Suite | Command | Count | Stack |
| --- | --- | --- | --- |
| Frontend | `npm test` | 20 | Vitest + React Testing Library + MSW v2 |
| Backend | `npm run test:api` | 121 | pytest |

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
- **`test_durable_jobs.py`** (Phase 3A.3 durable job foundation — pure contracts + store +
  worker against a throwaway SQLite DB, no external service)
  - **Contracts:** backoff is deterministic and bounded (capped, no overflow, jitter only
    reduces); terminal states have no outgoing transitions and illegal transitions raise;
    the error taxonomy classifies retryability; `JobError.safe_summary` is bounded/normalized.
  - **Store:** enqueue creates a `pending` (or future-dated `scheduled`) job with an audit
    event; a repeated idempotency key returns the same job while a **different** payload for
    that key is a conflict; the atomic claim marks/audits the job and prefers higher priority;
    the running→success/fail-fast/retry-with-backoff/dead-letter/cancel lifecycle is exact;
    an expired lease is recovered to `pending`; listing/reads are tenant-scoped.
  - **Concurrency:** four threads racing to claim a single job yield **exactly one winner**.
  - **Worker runner:** a claimed job runs end-to-end through a registered handler into every
    outcome — success (with result), retryable → `retry_wait`, non-retryable → `failed`,
    unknown type → `failed (unsupported_type)`, cooperative `cancelled`, and an unclassified
    exception recorded as `transient` carrying only the exception **class name** (never its
    message).
  - **Enqueue guards:** an unknown job type and an oversized payload are rejected before
    anything is enqueued.

  `test_api_isolation.py` additionally covers the customer job endpoints against the seeded
  instance: `…/jobs` requires authentication and returns the customer-safe envelope with **no**
  worker/lease/payload/idempotency fields, and operator-only `/internal/system/jobs` is `401`
  anonymous / `403` for a non-operator / secret-free diagnostics for an operator.

### PostgreSQL proofs

Production runs on PostgreSQL; the suite above runs on SQLite. These modules prove the
PostgreSQL-only paths on a **real** PostgreSQL, each test in a database of its own
(created and dropped by `app.tests._auth2_support.pg_database`), on schemas produced
by the real Alembic chain (never `metadata.create_all`):

| Module | What it proves (plan rows `P6-CI-4` / `P6-CI-5`, §14) |
| --- | --- |
| `test_postgres_migration_proof.py` | the full chain empty → head (every revision), `alembic check`, a guarded step down and the downgrade to **base** under the target-bound confirmation (the token that authorised one step is refused for the next; the round trip re-arms the original token — the guard's documented residual, shown by recomputation only), the re-upgrade, the migration actor (`python -m app.db.migrate`, `check`), an **interrupted migration** in two forms — a tracked revision failing partway (an induced catalog conflict: no partial object, `alembic_version` unmoved, recovery by removing the cause and re-running) and a controlled failure fixture in a disposable copy of the script tree (a crash inside a revision body is atomic; the repaired retry applies once) — and the seed guard's refusal of PostgreSQL (seed idempotency is SQLite-only by design) |
| `test_postgres_job_store_concurrency.py` | the production `DurableJobStore` methods through independent connections and genuinely overlapping transactions: claim exclusivity under barrier-synchronised claimers (every job claimed exactly once) and progress past a row another transaction holds locked (`FOR UPDATE SKIP LOCKED`); concurrent lease recovery with one effect and one `lease_recovered` event per job, and a loop skipping a locked expired row; retryable recovery → `pending` versus exhausted attempts → `dead_lettered`; stale-owner fencing after recovery/reclaim on independent connections — including a reclaim by the **same** `worker_id`, which only the rotated lease token can fence — and a race of stale heartbeats against the owner's; unexpired, future-scheduled and terminal jobs untouched. Every connection carries `statement_timeout`/`lock_timeout`, every thread join is bounded, so a regression that blocks on a lock fails fast instead of hanging |
| `test_postgres_llm_accounting.py` | the per-workspace LLM budget as a hard ceiling on PostgreSQL (D-LLM-1 option B): eight separate OS processes reserving against one workspace never exceed the budget together; a crashed process or attempt leaves a reservation that recovery charges at its worst case; a retried attempt reserves anew and releases the lost one; the same attempt settled twice, by racing processes, or late after `released`/`expired` recovery yields exactly one usage row (usage above the reservation is charged at the bound and counted once); recovery defers to a live lease; workspaces are independent, the month rolls over, and the run endpoint's read-only gate admits only what a reservation still fits. Offline (fixed-cost fake provider); every connection carries `statement_timeout`/`lock_timeout` and every join is bounded |
| `test_postgres_production_boot.py` | the P6-PLAT-1 production boot contract (strategy B, bounded in-process similarity, N_max = 100) in clean subprocesses that read their configuration from the environment only: the migration actor (`python -m app.db.migrate`, `ENVIRONMENT=production SN_MIGRATION_MODE=1`, no `REDIS_URL`) leaves the database at the code head; the API then boots the production full profile through its lifespan, serves `/health`, and the readiness probes report `ready` with the `vector` probe healthy and stating the bound. **Redis is doubled by fakeredis** in CI (the job has no Redis service; `TEST_REDIS_URL` selects a real Redis) - the proof asserts the mode it ran in and the CI step writes that mode to the job summary; no probe is weakened. No PostgreSQL DSN reaches any log |

Gate: `TEST_POSTGRES_URL`. Unset, these tests **fail** in CI (`CI=true`) and skip only on a
developer machine; the CI step also fails if the summary reports any skipped test, so
the proof can never pass silently. An unreachable database fails them. Locally:

```bash
# a disposable PostgreSQL 16 (Homebrew: brew install postgresql@16), for example
initdb -D /tmp/sn-pg16 -U signalnest --auth=trust && pg_ctl -D /tmp/sn-pg16 -o "-p 54329 -k /tmp" start
createdb -h 127.0.0.1 -p 54329 -U signalnest signalnest_test
cd apps/api && TEST_POSTGRES_URL=postgresql+psycopg://signalnest@127.0.0.1:54329/signalnest_test \
  .venv/bin/python -m pytest app/tests/test_postgres_migration_proof.py app/tests/test_postgres_job_store_concurrency.py -q
```

The older `TEST_POSTGRES_URL`-gated tests elsewhere in the suite (`skipif` when unset)
are unchanged and still run in CI, where the variable is set; the modules above are
the ones executed as a required, non-skippable step. Neither module touches the shared
`signalnest_test` database.

## Frontend (`apps/web`)

Component/integration tests render the real `App` with a small in-memory MSW backend
(`src/test/handlers.ts`) modeling one org → one workspace → one brand → four
independent cities.

- **`auth.test.tsx`** — sign-in flow and protected-route redirects.
- **`onboarding.test.tsx`** — the wizard offers every presence path, gates continue
  on a business name, and autosaves the draft to `localStorage`.
- **`campaign-context.test.tsx`** — context tabs, empty state, add-product dialog.
- **`scout-requests.test.tsx`** — lists all requests with status, runs a scout and
  confirms it reports back as **queued** (the run is now an async durable job), and opens
  a detail page with its configuration and simulated-source disclosure.
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

### Browser / E2E (`apps/web/e2e`, P6-CI-2)

The jsdom tests above never execute the built bundle. `apps/web/e2e/customer-flow.spec.ts`
does: a real **Chromium** (the build pinned by `@playwright/test` in the lockfile) drives
the **production bundle** (`npm run build` output, CSP meta included), served by
`vite preview` with the same `/api` + `/health` proxy as development, against a real local
API and the real durable worker sharing one disposable SQLite database that
`scripts/e2e-browser.sh` migrates from empty in a temporary directory. Nothing is seeded and
nothing is mocked in the browser: a synthetic customer registers through the UI and creates
every resource through the UI.

- **The flow** (the §14 minimum of the Phase-6 plan): sign-up → create the workspace the
  product asks for (Settings → Workspaces → New; registration creates an organization and
  no workspace) → onboarding → create location → create scout → run → see opportunity →
  sign out.
- **What it asserts, beyond the screens:** the stored session is the registered account's
  (one OWNER membership of the typed organization); onboarding is persisted on the server
  and counted on the overview; the created location and scout are the ones used (ids read
  back as the customer); the run is a durable job a worker **claimed** and **succeeded**
  for that scout and that location, every opportunity in the workspace carries that scout
  and location, the count shown equals the run's and the job's; the opportunity opened is
  one of them (market line, Simulated callout, Context link back to the scout); sign-out
  reaches `/sign-in`, clears the stored token, the session's tokens are refused with 401
  afterwards (`POST /auth/logout`), and protected routes bounce without rendering the
  customer's data.
- **Fixture boundary:** fixture connectors (the only source today) and the mock LLM. The
  location is placed in Dallas, TX because the pipeline derives the market from the
  location's city and state and the fixture library carries signals for that market; a
  location outside the four fixture markets completes with zero signals. This proves the
  product's own flow on deterministic inputs — not live ingestion, real LLM behaviour,
  real mail, deployment or production readiness.
- **Fail-closed:** `retries: 0`, one worker, `forbidOnly` always; `scripts/e2e-browser.sh`
  refuses a non-mock LLM provider or a live connector, fails when the bundle or venv is
  missing, the API never becomes ready or the worker is not running, and
  `apps/web/scripts/assert-e2e-report.mjs` refuses the JSON report unless it is a report
  of this flow — the test titled as in `apps/web/e2e/flow.json`, the one source the spec
  and the gate share, with every step listed there executed — run exactly once and passed
  (no skip, no retry, no empty or substituted test; `assert-e2e-report.test.mjs` covers
  each refusal).
- **Diagnostics:** on failure Playwright keeps the trace and a screenshot under
  `ci-logs/e2e-results/`, next to the API and worker logs, which the job's failure-only
  artifact upload carries (7-day retention). A trace records the browser's requests, so it
  carries the synthetic customer's bearer tokens — tokens of a database that no longer
  exists, signed with the test secret; never a real credential. Every process is stopped
  on every exit path.
- **Coverage boundary:** Chromium only, one desktop viewport, this one flow. Other routes
  are not browser-tested here.

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
| **Frontend quality** | `npm ci`, `npm run lint` (zero warnings), `npm run type-check`, `npm run test`, `npm run build`, then the browser customer flow against that build: API venv install, `npm run e2e:install --workspace apps/web` (Chromium), `scripts/e2e-browser.sh` (P6-CI-2) |
| **Backend quality** | fresh venv + `pip install -e apps/api[dev]`, `ruff check`, `demo-setup.sh` (migrate + seed), `pytest`; then the **PostgreSQL proofs** (`test_postgres_migration_proof.py`, `test_postgres_job_store_concurrency.py`, `test_postgres_llm_accounting.py`, `test_postgres_production_boot.py`) run again on their own against the job's disposable PostgreSQL 16 service as a required result — they fail if the database is unavailable and the step fails if any of them is skipped |
| **Migrations and API contract** | SQLite: empty-DB `alembic upgrade head`, `alembic check` (drift), seed, seed again (idempotency), `downgrade base`, `upgrade head`, `seed --reset`; **PostgreSQL 16** (disposable service): empty-DB `alembic upgrade head`, `alembic check`, `downgrade base` under the confirmation guard, `upgrade head`, with the revision read back after each phase (no seed — `app.db.seed` refuses PostgreSQL by design); then `npm run gen:types` + `git diff --exit-code` (contract drift) |
| **Integration smoke** | after the three gates pass: migrate + seed, start the API, wait on `/health`, run the HTTP isolation smoke, always stop the server |

**Code coverage (P6-CI-8, report only).** Both test jobs measure code coverage and
report it; neither gates on it, and no coverage threshold exists anywhere in the
repository (`--cov-fail-under` and vitest `thresholds` are deliberately absent; adding one
is a separate decision). **Backend quality** runs the API suite through `pytest-cov`
(`--cov=app --cov-branch`, configured by `[tool.coverage]` in `apps/api/pyproject.toml`),
writes the `TOTAL` line to the job step summary and uploads the Cobertura XML as the
`coverage-api` artifact (30 days). **Frontend quality** runs `npm run test` unchanged:
`apps/web/vite.config.ts` switches `@vitest/coverage-v8` on when `CI=true`
(`text-summary`, `json-summary`, `lcov` into `ci-logs/coverage-web/`), the summary lines go
to the step summary and the directory is uploaded as the `coverage-web` artifact (30 days).
First `main` baseline (2026-10-28, merge commit `b0b53951` of #270, CI run `40637670580`; report only, not a gate): API — coverage.py `TOTAL` 94% (combined statement and branch coverage, 14,567 of 15,487), line rate 95.37% (12,165 of 12,755 statements), branch rate 87.92% (2,402 of 2,732 branches); web — statements 91.86% (2,880 of 3,135), branches 82.33% (1,985 of 2,411), functions 87.84% (1,005 of 1,144), lines 94.54% (2,566 of 2,714). Later runs publish their own figures in the job summaries and artifacts; this line records the baseline only. Locally:
`cd apps/api && .venv/bin/python -m pytest --cov=app --cov-branch --cov-report=term`, and
`cd apps/web && CI=true npx vitest run --coverage --maxWorkers=2`.

**Environment.** CI runs entirely in the default zero-dependency **local** mode:
SQLite (`DATABASE_URL=sqlite:///./signalnest.db`), in-process queue
(`QUEUE_BACKEND=inprocess`), in-memory cache (`CACHE_BACKEND=memory`), brute-force
vector index (`VECTOR_BACKEND=bruteforce`), local-file storage
(`STORAGE_BACKEND=local`), the fixture connectors, and the deterministic mock LLM
(`LLM_PROVIDER=mock`, `LLM_MOCK_SEED=signalnest-ci`), with `ENVIRONMENT=test`. These
are the actual `Settings` field names (`apps/api/app/core/config.py`). The
application under test runs on SQLite throughout; the **Backend quality** and
**Migrations and API contract** jobs additionally provision a **disposable PostgreSQL 16
service** (`postgres:16`, a throwaway user/password, dropped with the runner) for the
PostgreSQL proofs described under [PostgreSQL proofs](#postgresql-proofs). **Docker,
Redis and paid OpenAI/Anthropic API keys are not required and no secrets are used.**
Runtimes: Node 20 (repo engines) and Python 3.12. On failure, each job uploads its
logs (`ci-logs/`, `api-smoke.log`) as artifacts.

**Recommended required status checks for `main`:** Frontend quality, Backend quality,
Migrations and API contract, Integration smoke.

### Merge-path security scanning and SBOMs (P6-CI-6)

The `Container build and security` and `Revision reader` contexts also run Trivy (locked
dependencies, the three built images, IaC misconfiguration, CycloneDX SBOMs) and Semgrep
(Python and TypeScript SAST), each evaluated by `security/gate.py` against the ~~PROPOSED~~
thresholds in `security/policy.json` *(ADOPTED for merge-path enforcement by the operator order
of 2026-10-19: a Trivy-selected CRITICAL fails in every covered population with or without a fix; ~~the jobs' installed tooling
environments are scanned as installed~~ *(corrected 2026-10-23: these two jobs scan as installed the Python tooling venvs
they create, `.security-venv` and `.reader-venv`, and Revision reader also its interpreter prefix's installed
distributions; the interpreter prefixes of the five jobs other than Revision reader are not scanned, see "Populations
after this tranche" in `security/README.md`)*)*. *(Since 2026-10-20, P6-CI-6 coverage gaps: Frontend quality, Backend
quality, Migrations and API contract and Integration smoke also scan ~~the environments they install~~ the Python
virtualenvs they install *(corrected 2026-10-21: their other installs, `node_modules` and the Playwright browser, are
not scanned as installed)* (`apps/api/.venv`, and `.locktools` in Backend quality) as installed, with the packages their pip vendors,
every context analyses the Trivy binary on its PATH, and Revision reader also the OpenTofu binary.)* Reports, SBOMs, identity records and gate summaries
are uploaded as the `security-reports-*` artifacts (30 days). Coverage, declared exclusions,
the baseline findings on `main` and the failure behaviour are documented in
`security/README.md`; the gate's own tests run with
`python3 -m unittest discover -s security -p "test_*.py"`.

### Running each CI gate locally

```bash
npm ci                       # clean install (matches CI)
npm run bootstrap            # create apps/api/.venv (or: python -m venv apps/api/.venv && .venv/bin/pip install -e 'apps/api[dev]')

# Frontend quality
npm run lint
npm run type-check
npm test
npm run build
npm run e2e:install --workspace apps/web   # once: the Playwright-pinned Chromium build
npm run e2e                                 # browser customer flow against the built bundle (needs apps/api/.venv)

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
