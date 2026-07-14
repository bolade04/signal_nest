# Phase 3A.4b — Acceptance Report

**Classification: IN PROGRESS — Batches 1 and 2 complete; tracing and deployment remain.**

Branch: `feat/phase-3a-observability-deployment` · Draft PR: #31 (kept in **draft**;
not marked ready, not merged) · Base: `main` @ `3fefb36`.

Phase 3A.4b is delivered as reviewable batches. This report records what is **accepted
so far**; the phase is **not** complete until the deferred batches (below) land.

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

## Deferred (phase not complete)

- **Batch 3** — distributed tracing (API-to-worker), export-failure isolation.
- **Batch 4** — production API + worker containers, graceful SIGTERM lifecycle,
  single-actor migration strategy.
- **Batch 5** — deployment/migration/worker/incident runbooks, dashboards + alert
  recommendations, broad failure-injection suite, security review, final acceptance.

## Gate results (this update)

Run locally unless noted; PostgreSQL-gated tests run in CI (no local PostgreSQL — see
note).

| Gate | Result |
| --- | --- |
| Backend `pytest` | **309 passed, 2 skipped** (PG-gated; run in CI) |
| Backend `ruff check app/` | clean |
| Alembic drift (`alembic check`) | no new operations; head `e7c2a9b4f1d3` |
| Migration upgrade/downgrade/re-upgrade | green (`test_worker_migration.py`) |
| Frontend lint | pass |
| Frontend type-check | pass |
| Frontend tests | **20/20** |
| Frontend build | pass |
| Generated contract (`gen:types` + `git diff --exit-code`) | clean (telemetry additions committed) |
| Smoke | **13/13**, four-market isolation, no cross-market contamination |
| `npm audit` | **0 vulnerabilities** |

**PostgreSQL note.** This environment has no local PostgreSQL, so the opt-in gated
cross-worker PostgreSQL claim/recovery test was **not** executed locally. It runs in CI
against the `postgres:16` service via `TEST_POSTGRES_URL`. Batch 2 introduces no new
external-service dependencies, so CI required no changes for this batch.

## Contract impact

Additive only: the operator-only `GET /internal/system/telemetry` route and its
`TelemetryStatusOut` schema. No customer-facing route or schema changed; the durable-job
`correlation_id` is internal and is not exposed by any customer API.
