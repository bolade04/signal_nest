# Phase 3A.4b — Architecture Audit

Base `main` SHA: `3fefb36d432c7c9c46118e29bf631c32120f5e65`
Audit performed by direct inspection of the repository (no assumptions).

## Current-state inventory

### Logging
- `apps/api/app/core/logging.py`: a `JsonFormatter` emitting JSON to stdout with
  `level`, `logger`, `message`, `request_id`, `trace_id`, `exc_info`, plus any
  `record.extra_fields`. `request_id_ctx` / `trace_id_ctx` are `ContextVar`s.
- **No redaction utility exists.** Structured logs today rely on callers to pass
  only safe fields; there is no central scrubber for tokens/URLs/keys.
- Log call sites already use `extra={"extra_fields": {...}}` (see `worker.py`).

### Request correlation
- `request_id_ctx` and `trace_id_ctx` exist as context vars, but there is **no
  middleware** that populates them from inbound headers or generates them, and no
  propagation onto durable jobs. Correlation across the job boundary is absent.

### Error handling
- `app/core/errors.py` holds sanitized domain exceptions
  (`ObjectStorageUnavailableError`, `RedisUnavailableError`,
  `RedisNotifyFailedError`, `InvalidObjectKeyError`, `WorkerRegistrationFailedError`,
  …). Driver errors are converted to static-message exceptions at each adapter seam.

### Metrics / telemetry libraries
- **None.** No Prometheus client, no OpenTelemetry, no StatsD. No counters or
  histograms are emitted anywhere. This is a greenfield area.

### Sentry
- **Not integrated.** No `sentry_sdk` dependency or init.

### API lifecycle
- FastAPI app in `app/main.py` (mounts routers incl. `internal_routes`).
- Readiness probes: `app/system/probes.py` + operator `/internal/system/readiness`.
  Liveness vs readiness separation needs confirmation/adjustment in Batch 5.

### Worker lifecycle
- `app/jobs/worker.py`: separate process (`python -m app.jobs.worker`). Validates
  config + schema, registers (STARTING→READY), spawns N daemon loop threads, runs a
  bounded registry heartbeat + stale sweep, and drains on SIGINT/SIGTERM
  (READY→DRAINING→STOPPED) within `worker_shutdown_grace_seconds`.
- Observations for Batch 1/5: the worker never sets `BUSY` while executing;
  `_set_registry_status` reaches into the registry's private `_transition`.

### Database engine & pool
- `app/db/session.py::build_engine` — SQLite (local) vs bounded PostgreSQL
  `QueuePool` (size/overflow/timeout from settings). Dialect resolved via SQLAlchemy
  URL parsing, not string matching.
- No pool-utilization visibility is exported yet.

### Redis lifecycle
- `app/infra/cache.py` (cache) and `app/jobs/coordination.py` (wake-up + advisory
  lock). Bounded pool + socket timeouts; lazy driver import; sanitized errors.
  Redis is strictly advisory — the DB is the only authoritative queue.
- **Key construction risk (Batch 1):** `tenant_cache_key` joins parts with `:` and
  `_redis_key` interpolates `{prefix}:cache:{key}`. Colon-joining is ambiguous —
  `("a:b")` and `("a","b")` can collide. Needs collision-resistant encoding.

### S3 client lifecycle
- `app/infra/storage.py`: `LocalStorage` (default) and `S3Storage` (full mode),
  built via `build_storage`. Bounded timeouts/retries; private-by-default puts;
  sanitized errors; `validate_object_key` rejects hostile relative keys.
- **Composed-key gap (Batch 1):** `tenant_object_key` validates only the *relative*
  part, then prefixes `{org}/{ws}/`. The org/workspace segments and the final
  composed key are **not** re-validated, so a hostile tenant identifier could inject
  separators/traversal into the physical key.

### Existing Dockerfiles / containers
- **None.** No `Dockerfile`, `docker-compose`, or `.dockerignore` in the repo.
  Batch 5 is greenfield container work.

### Deployment documentation
- **None.** No `docs/operations/` directory exists yet.

### Health / readiness endpoints
- `/internal/system/readiness`, `/capabilities`, `/jobs`, `/workers` — all operator
  gated (`require_operator`). Coarse public liveness/readiness split to be confirmed
  in Batch 5.

### CI services
- `.github/workflows/ci.yml`: four protected jobs (Frontend quality, Backend
  quality, Migrations and API contract, Integration smoke). postgres:16 service with
  `TEST_POSTGRES_URL`; Redis/S3 exercised via fakeredis/injected fakes. Strict
  `bash --noprofile --norc -euo pipefail`; `git diff --exit-code` contract gate.

### Environment validation
- `app/core/config.py`: Pydantic Settings with extensive bounded validation;
  production mode rejects local backends by name; secret fields `repr=False`.

### Secret-redaction utilities
- **None** beyond per-adapter static-message exceptions. Batch 2 must add a reusable
  scrubber and prove known secret patterns never reach logs.

### Job event / audit models
- `app/jobs/models.py`: `Job`, `JobEvent` (append-only, safe metadata only).
- `app/jobs/worker_models.py`: `WorkerRegistration` (no credentials/IP/token; unique
  `worker_id`; indexes on status/heartbeat/type). **No generation token today.**

### Operator endpoints
- `app/system/internal_routes.py`: `/capabilities`, `/readiness`, `/jobs`,
  `/workers`. Coarse, secret-free, operator-gated.

### Migration execution strategy
- Alembic; single head `d4f6a8c0b2e1`. `npm run migrate` / `migrate:down` /
  `migrate:status` wrap `scripts/*.sh`. **No documented single-actor production
  migration policy** — Batch 5.

## Gaps (by batch)

| Area | Gap | Batch |
|---|---|---|
| Lease recovery | `recover_expired_leases` selects-then-mutates in Python; two loops can double-recover / double-event | 1 |
| PG contention test | Existing gated test is sequential, not true concurrent lock contention | 1 |
| Worker identity | No generation fencing; an old process with the same `worker_id` can still heartbeat the replacement row | 1 |
| SQLite CAS | Lost-race path calls full `db.rollback()`, which can discard unrelated caller work | 1 |
| S3 keys | Composed `{org}/{ws}/{rel}` key not revalidated | 1 |
| Redis keys | Ambiguous colon joining; delimiter collisions possible | 1 |
| Logging | No redaction; no correlation middleware | 2 |
| Metrics | None | 3 |
| Tracing | None | 4 |
| Containers | None | 5 |
| Lifecycle | Liveness/readiness split, worker BUSY/drain hardening | 5 |
| Runbooks/alerts | None | 6 |

## Proposed implementation order (Batch 1)

1. Single-winner expired-lease recovery (`store.py`) + tests.
2. True PG `SKIP LOCKED` contention test (gated) — proves the existing claim path.
3. Worker-registration generation fencing (`worker_models.py`, `worker_registry.py`,
   `worker.py`, additive migration) + tests.
4. SQLite CAS transaction contract hardening (`store.py`) + tests.
5. Composed S3 tenant-key revalidation (`storage.py`) + hostile-key tests.
6. Collision-resistant Redis key encoding (`cache.py`, and structured components in
   `coordination.py` where applicable) + tests.

## Files likely to change (Batch 1)

- `apps/api/app/jobs/store.py`
- `apps/api/app/jobs/worker_registry.py`
- `apps/api/app/jobs/worker_models.py`
- `apps/api/app/jobs/worker.py`
- `apps/api/app/infra/storage.py`
- `apps/api/app/infra/cache.py`
- `apps/api/alembic/versions/*` (one additive migration for the generation column)
- `apps/api/app/tests/test_durable_jobs.py`, `test_production_adapters.py`,
  `test_worker_fleet.py` (+ possibly a new focused test module)

## Migration requirements

One **additive** migration: add a nullable `generation_token` (and, if needed, a
coarse `generation` integer) column to `worker_registrations`. No existing column is
altered; downgrade drops only the new column(s). ORM-only inserts, consistent with
the existing table. No existing migration is edited in place.

## Contract-impact assessment

Batch 1 changes are internal to the data plane and worker registry. Operator
diagnostics may expose a coarse generation indicator but **never** the token.
No customer-facing API schema changes are expected; the `git diff --exit-code`
generated-contract gate must stay green.

## Security risks addressed / considered

- Path traversal via tenant identifiers in composed S3 keys (closed in Batch 1).
- Redis key collision / tenant-boundary ambiguity (closed in Batch 1).
- Stale-process takeover of a replacement worker registration (closed in Batch 1).
- Generation tokens must never be logged or returned to customer APIs.

## Rollback strategy

Every Batch 1 change is behavior-preserving for the happy path and covered by the
additive migration's clean downgrade. If a regression appears, revert the offending
focused commit; the additive `worker_registrations` column can be dropped by the
migration downgrade without touching business or job data. The draft PR is never
merged until the full Phase 3A.4b acceptance gates (or a deliberate batch split) are
satisfied.
