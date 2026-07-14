# Phase 3A.4b — Acceptance Report

**Classification: IN PROGRESS — Batches 1–4 complete; final operations and acceptance remain.**

Branch: `feat/phase-3a-observability-deployment` · Draft PR: #31 (kept in **draft**;
not marked ready, not merged) · Base: `main` @ `3fefb36`.

Phase 3A.4b is delivered as reviewable batches. This report records what is **accepted
so far**; the phase is **not** complete until the deferred batch (below) lands.

## Delivered

### Batch 1 — concurrency and data-plane hardening (accepted)

- Single-winner expired-lease recovery (dialect-aware guarded CAS; exactly one winner,
  exactly one `lease_recovered` event).
- True PostgreSQL `FOR UPDATE SKIP LOCKED` lock-contention test in CI (`postgres:16`).
- Worker-registration generation fencing (opaque token rotated on register; a stale
  generation cannot heartbeat or transition its replacement).
- SQLite claim compare-and-set isolated in a per-candidate SAVEPOINT.
- Final S3 tenant-key validation; collision-resistant Redis cache-key encoding.
- Additive migration `df66ff0426d2` (nullable `worker_registrations.generation_token`).

### Batch 2 — structured logging, correlation, and safe metrics (accepted)

- **Structured logging + redaction** — stable-field JSON formatter, human-readable dev
  console formatter, safe fallback that never raises; one recursive secret-redaction
  layer applied to every emitted structured value (`app/core/logging.py`,
  `app/core/redaction.py`, `app/core/log_context.py`).
- **Request correlation** — strict, bounded request-id middleware: accepts a valid
  inbound id or mints one, echoes it in the `x-request-id` response header, resets
  request-local context after every request (`app/core/middleware.py`).
- **Durable-job correlation** — opaque `correlation_id` distinct from the row id /
  lease token / worker id / tenant ids, persisted via additive nullable migration
  `e7c2a9b4f1d3`, restored into the worker (and heartbeat thread) logging context for
  the duration of execution and cleared afterward.
- **Provider-neutral metrics** — fixed metric-name catalog + strict label allow-list
  (unknown names and identifying/high-cardinality labels rejected at dev/test time);
  no-op default, in-memory test backend, runtime failures isolated and counted
  (`app/core/metrics.py`).
- **Lifecycle instrumentation** — bounded metrics at authoritative commit points for
  the HTTP path, enqueue, claim, completion, retry/fail/dead-letter, execution
  duration, single-winner lease recovery, and Redis notify (counted separately from
  enqueue). Idle polls increment an aggregated counter rather than logging.
- **Operator-only telemetry status** — `GET /internal/system/telemetry` (401 anon /
  403 non-operator / 200 operator), bounded status only.
- Operator guide: `docs/operations/observability.md`.

### Batch 3 — distributed tracing and end-to-end telemetry (accepted)

- **Provider-neutral tracing seam** — pure-Python, OTel-compatible tracer
  (`app/core/tracing.py`): `NoOpTracer` default, `InMemoryTracer` test exporter,
  import-guarded OTLP builder that fails closed to no-op, bounded span-name catalog +
  attribute allow-list, strict W3C `traceparent` parse/format, deterministic
  parent-based ratio sampling (low-value roots reduced ×1/10). No hosted vendor SDK in
  core code.
- **HTTP request spans** — a bounded server span per request via `CorrelationMiddleware`:
  inbound `traceparent` becomes the remote parent, route template (never the raw path),
  method + status code, safe error class; `trace_id` bound into the log context only
  while a recording span is active, reset on exit.
- **Durable-job propagation** — enqueue persists the active traceparent on the `jobs` row
  via additive nullable migration `a1b2c3d4e5f6`; the worker restores it as the remote
  parent of `job.execute`. `worker.poll` scopes only recovery + claim; execution runs
  after it closes, so a claimed job's trace is never parented to the reduced-sampled
  poll (fresh root when nothing was persisted).
- **Lifecycle + dependency spans** — job claim/complete/fail/recover, a single
  transaction-level DB claim span, and bounded Redis cache and S3 upload/sign-url spans
  carrying only component/dependency/operation/outcome; never a key, value, object key,
  bucket, endpoint or signed URL.
- **Safe exception recording** — class + error status only, never the message or a
  stack trace.
- **Operator-safe trace diagnostics** — `GET /internal/system/telemetry` gains
  `tracing_enabled`, `tracing_exporter`, `tracing_status`, `tracing_sample_ratio` and
  `trace_export_failures` (bounded enums + counts only).
- **Collector-free tests** — 39-test tracing suite (`app/tests/test_tracing.py`).

### Batch 4 — production containers, graceful lifecycle, and migration strategy (accepted)

- **Production images** — one multi-stage `apps/api/Dockerfile` builds two runtime
  targets (`api`, `worker`) from a single locked `.[full]` install. Pinned
  `python:3.12-slim`, non-root user `app` (UID/GID `10001`), read-only-root
  compatible (`PYTHONDONTWRITEBYTECODE=1`, only `/tmp` writable), no build toolchain
  or dev/test deps in the runtime stage, exec-form commands so the app is PID 1 and
  receives `SIGTERM` directly. The `worker` image exposes no port; the `api` image
  exposes `8000` with a liveness `HEALTHCHECK` on `GET /health`.
- **Secret-free build** — `apps/api/.dockerignore` excludes `.env*`, local databases,
  private keys, VCS metadata, caches, the venv and the test suite;
  `scripts/docker-security-check.sh` (run in CI on both images) fails the build on a
  root UID or any baked secret/local database.
- **Single-actor migration strategy** — `python -m app.db.migrate`
  (`upgrade`/`downgrade`/`check`); replicas never migrate. A read-only startup
  **schema-compatibility gate** (`app/db/schema.py`) classifies the database as
  `compatible` / `ahead` / `pending` / `uninitialized`; additive-first policy makes
  `ahead` startup-safe for rolling deploys, while `pending`/`uninitialized` fail fast
  with an instruction to run the migration actor. Verify only — the gate never mutates.
- **Graceful lifecycle** — ordered API startup (install tracer → schema gate → startup
  metric); shared bounded, idempotent shutdown (`app/core/lifecycle.py`): flush
  metrics, flush traces under budget, close Redis cache/notifier, dispose the DB pool —
  every step best-effort so a slow exporter can never block exit. Worker draining: first
  `SIGTERM` stops claiming and lets in-flight jobs finish within
  `WORKER_SHUTDOWN_GRACE_SECONDS`; a second signal shortens the budget to
  `WORKER_FORCE_SHUTDOWN_GRACE_SECONDS`, abandoning still-running work so its lease
  expires and the next worker recovers it, then runs the same bounded flush/close.
- **Lifecycle metrics** — `service_startups_total`, `service_shutdowns_total` and
  `migration_runs_total` reuse the existing label allow-list (no new label keys).
- **Container CI** — a `container-build` job builds both targets, runs the security
  check on each, imports `app.main` in the API image, exercises the migration actor +
  schema check in the worker image, and proves the schema gate rejects an un-migrated
  database.
- **Optional local full-mode stack** — `infra/docker-compose.yml` runs the production
  images against real PostgreSQL + Redis with a one-shot migration actor gating `api`
  and `worker` startup (developer convenience, explicitly **not** a production manifest).
- Operator guides: `docs/operations/deployment.md`, `docs/operations/migrations.md`,
  and a process-lifecycle section added to `docs/operations/observability.md`.

## Deferred (phase not complete)

- **Batch 5** — deployment/migration/worker/incident runbooks, dashboards + alert
  recommendations, broad failure-injection suite, security review, final acceptance,
  plus orchestrator manifests (Kubernetes/Nomad) and cloud infrastructure.

## Gate results (this update)

Run locally unless noted; PostgreSQL-gated tests run in CI (no local PostgreSQL — see
note).

| Gate | Result |
| --- | --- |
| Backend `pytest` | **362 passed, 2 skipped** (PG-gated; run in CI) |
| Backend `ruff check app/` | clean |
| Alembic drift (`alembic check`) | no new operations; head `a1b2c3d4e5f6` |
| Migration upgrade/downgrade/re-upgrade | green |
| Startup schema gate (`python -m app.db.migrate check`) | reports `compatible`, exit 0 |
| Frontend lint | pass |
| Frontend type-check | pass |
| Frontend tests | **20/20** |
| Generated contract (`gen:types` + `git diff --exit-code`) | clean (no new diff) |
| Smoke | **13/13**, four-market isolation, no cross-market contamination |
| `npm audit` | **0 vulnerabilities** |

**PostgreSQL / Docker note.** This environment has no local PostgreSQL or Docker, so
the opt-in gated cross-worker PostgreSQL claim/recovery test and the new
`container-build` job (production image build + `docker-security-check.sh` +
migration-actor / schema-gate checks) were **not** executed locally. They run in CI:
the PG-gated test against the `postgres:16` service via `TEST_POSTGRES_URL`, and the
container job on the GitHub runner. Batch 4 adds no new external-service dependency to
the default local test path.

## Contract impact

Additive only: the operator-only `GET /internal/system/telemetry` schema
(`TelemetryStatusOut`) gains five coarse tracing fields (`tracing_enabled`,
`tracing_exporter`, `tracing_status`, `tracing_sample_ratio`, `trace_export_failures`).
No customer-facing route or schema changed; the durable-job `correlation_id` and
`trace_context` are internal and are not exposed by any customer API.

Batch 4 makes **no** contract change: it adds container images, the single-actor
migration command, the startup schema gate and the graceful lifecycle — none of which
touch any HTTP route or schema. `gen:types` produced no diff.
