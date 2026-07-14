# Phase 3A.4a — Production Data-Plane Adapters & Worker-Fleet Foundation

Fourth vertical slice of Phase 3. It hardens the **full-mode** data plane
(PostgreSQL, Redis, S3-compatible object storage) behind the existing adapter
seams and adds a **worker-fleet registry** so operators can see which worker
processes are alive, busy, draining or stale. Everything is additive: default
local mode still runs with **zero external services and no paid providers**, and
every Phase 1–2 / 3A.1–3A.3 behaviour — including the four-market isolation
guarantee and the durable-job lease invariant — is unchanged.

This is the **3A.4a** split of the larger 3A.4 effort. Observability
(metrics/tracing/OpenTelemetry), readiness caching, container/compose packaging,
deploy docs and the operator frontend panel are deliberately deferred to
**3A.4b** (see *Out of scope* below).

## What shipped

| Area | File(s) | Summary |
| --- | --- | --- |
| Config + validation | `app/core/config.py` | Dialect resolved via SQLAlchemy `make_url` (never string matching); bounded PostgreSQL pool settings, Redis tuning, S3 limits/credentials and worker-fleet settings — each range-validated at construction. A selected-but-unconfigured production backend stays a **soft** (environment-gated) unconfigured state locally, a hard failure in full/production. |
| DB engine/session | `app/db/session.py` | `build_engine(settings)` builds a single process engine with **dialect-isolated** pools: PostgreSQL gets a bounded `QueuePool` (`pool_size`/`max_overflow`/`pool_timeout`/`pool_recycle`), `pool_pre_ping`, bounded `connect_timeout` + non-secret `application_name`; SQLite keeps `check_same_thread=False` + FK/busy-timeout pragmas. The DB URL is never logged. |
| PostgreSQL claim | `app/jobs/store.py` | `claim_one` dispatches on the live dialect: PostgreSQL locks one due row with `SELECT … FOR UPDATE SKIP LOCKED` then updates it; SQLite keeps the compare-and-set scan. **One shared lifecycle** — the priority/FIFO ordering, fresh `lease_token`, `claimed` audit event and commit are identical; attempt counting stays solely in `mark_running` for both dialects. |
| Redis cache | `app/infra/cache.py` | Hardened `RedisCache` (injected client): namespaced + tenant-scoped keys, **JSON only (never pickle)**, wrapped values so cached falsey is distinguishable from a miss, TTL validation, bounded pool/timeout, sanitized `RedisUnavailableError`, safe `close`. `InMemoryCache` mirrors the same contract. |
| Redis coordination | `app/jobs/coordination.py` | Redis is a **wake-up optimization only**; the DB stays authoritative. `RedisJobNotifier` publishes/receives job-available signals and degrades to bounded polling on any failure; a lost/duplicate signal can never lose or double-run a job. Optional `RedisAdvisoryLock` uses an opaque token + bounded TTL and **releases only what it still owns** (WATCH/MULTI compare-and-delete). |
| Object storage | `app/infra/storage.py` | Hardened `S3Storage` (injected client): `validate_object_key` rejects empty/absolute/backslash/null-byte/`..`/non-normalized keys before any I/O; tenant-scoped prefixes; size validated pre-upload (`ObjectTooLargeError`); private by default (no ACL); bounded signed-URL TTL; custom endpoint/region/credentials; sanitized `ObjectStorageUnavailableError`. `LocalStorage` mirrors the contract. |
| Worker status | `app/jobs/worker_status.py` | `WorkerStatus` state machine (`starting/ready/busy/draining/stopped/stale/failed`) with an **explicit** transition map; `stopped` is the only terminal state; pure and unit-testable. |
| Worker registry | `app/jobs/worker_models.py`, `app/jobs/worker_registry.py`, `alembic/versions/…d4f6a8c0b2e1_*.py` | Additive `worker_registrations` table (indexes on `worker_id`/`status`/`last_heartbeat`/`worker_type`). Registration is **idempotent/self-replacing** (a restart re-initializes its own row to `starting`); heartbeats are cheap (no audit rows); stale is **derived** (`not stopped` AND heartbeat age > threshold) and never touches job ownership. Stores no credentials/env/IPs/lease tokens/payloads. |
| Worker lifecycle | `app/jobs/worker.py` | The worker registers on startup, marks `ready` only after validation, runs a bounded heartbeat+sweep thread, drains and marks `stopped` on shutdown. Registration failures are retried per config then raise `WorkerRegistrationFailedError`. Job-lease recovery is **independent** of registry status. |
| Errors | `app/core/errors.py` | An `AdapterError` taxonomy: `postgres_unavailable`, `redis_unavailable`, `redis_notify_failed`, `object_storage_unavailable`, `invalid_object_key`, `object_too_large`, `worker_registration_failed`, `worker_heartbeat_failed`, `worker_already_active`, `worker_stale`, `adapter_initialization_failed`, `production_adapter_not_configured`. Each carries a stable `code`, a **static safe message**, a `retryable` hint, a `log_severity`, an `internal_category` and an HTTP status — a raw driver/SDK message is never surfaced. |
| Capabilities | `app/core/runtime.py` | A `worker_registry` capability (backend = the queue wake-up transport) so operators see the fleet-coordination configuration alongside the other backends. Pure, no I/O, secret-free. |
| Readiness | `app/system/probes.py` | A bounded `worker_registry` probe: **informational by default** (schema present ⇒ healthy) and **blocking only when `require_worker_fleet` is enabled** (then requires ≥1 active worker). Never names a worker id or build metadata. |
| Operator diagnostics | `app/system/internal_routes.py`, `app/jobs/worker_schemas.py` | Operator-only `GET /internal/system/workers`: coarse fleet health — status counts, live active/stale totals and per-worker lifecycle summaries — that **never** exposes a worker id, build revision, host fingerprint, application version, URL or raw error. |

## Security & isolation properties (enforced, and covered by tests)

- **One winner per job, on both dialects.** PostgreSQL `FOR UPDATE SKIP LOCKED`
  and SQLite compare-and-set each guarantee exactly one claimer per row; a fresh
  `lease_token` on every claim fences any prior owner. The two paths share one
  lifecycle, so the attempt budget can never diverge.
- **Correctness never depends on Redis.** A job is committed to the DB before any
  wake-up is published; a publish failure is swallowed (a warning) and the job is
  still found by the next bounded poll. A duplicate wake only triggers an atomic,
  lease-fenced claim that finds nothing.
- **Advisory locks are safe.** Release deletes the key only when the caller still
  holds its exact token, so a caller can never release a lock a new owner has since
  taken. Locks are advisory — they never gate job ownership.
- **Keys can't escape their scope.** Object keys are validated before any I/O and
  tenant prefixes are derived server-side, so a crafted key cannot traverse out of
  a tenant's prefix or the storage root.
- **No secret leakage.** DB/Redis URLs, passwords, bucket names, endpoints and raw
  driver/SDK exception text are never logged or returned; adapter faults surface a
  static message + stable code only. Credentials fields use `repr=False`.
- **Fleet state is operational, not sensitive.** The registry stores no
  credentials, environment, IPs, lease tokens or payloads; the operator endpoint is
  coarse and omits worker ids and build metadata; stale detection changes only
  registry status and never affects job ownership (governed solely by lease expiry).
- **Operator gate.** `/internal/system/workers` is `401` anonymous, `403` for an
  authenticated non-operator, and coarse-but-secret-free for an operator.

## Worker-fleet lifecycle

```
                 register (startup, self-replacing)
                          │
                          ▼
   ┌────────► starting ──► ready ⇄ busy ──► draining ──► stopped (terminal)
   │            │            │      │           │
   │            └── failed ◄─┴──────┴───────────┘
   │                          │
   └──── stale ◄──────────────┘   (heartbeat age > threshold; recovers on next beat)
```

- Registration is **idempotent**: registering a known `worker_id` overwrites that
  row and resets it to `starting` (a process restart re-initializes its own
  registration rather than colliding or accumulating stale rows).
- `worker_stale_after_seconds` must be strictly greater than
  `worker_heartbeat_seconds`, so a healthy worker is never flagged stale between
  beats. Stale rows recover to `ready` on the next heartbeat.
- Job-lease recovery is driven purely by lease expiry in the durable store; a
  worker's registry status has no bearing on job correctness.

## Configuration (new settings, all bounded)

| Group | Keys |
| --- | --- |
| PostgreSQL pool | `db_pool_size`, `db_max_overflow`, `db_pool_timeout_seconds`, `db_pool_recycle_seconds`, `db_connect_timeout_seconds`, `db_application_name` |
| Redis | `redis_pool_size`, `redis_operation_timeout_seconds`, `redis_key_prefix`, `redis_notify_channel`, `redis_lock_ttl_seconds` |
| S3 | `s3_region`, `s3_use_ssl`, `s3_access_key_id`, `s3_secret_access_key`, `s3_max_object_bytes`, `s3_signed_url_ttl_seconds`, `s3_operation_timeout_seconds`, `s3_max_retries` |
| Worker fleet | `worker_type`, `worker_stale_after_seconds`, `worker_registration_retry_limit`, `worker_registration_retry_delay_seconds`, `worker_id_max_length`, `require_worker_fleet`, `application_version`, `build_revision` |

Redis and S3 tuning bounds are enforced only when that backend is selected. The
*presence* of `redis_url` / `s3_bucket` stays environment-gated: missing locally
is a soft "unconfigured capability", missing in full/production fails fast.

## Testing

- `app/tests/test_production_adapters.py` — config-validation bounds and soft/hard
  gating; dialect resolution via URL parsing; dialect-isolated PostgreSQL pool
  wiring; an **always-run** proof the claim compiles to `FOR UPDATE SKIP LOCKED`;
  a **gated** live cross-worker claim test (`TEST_POSTGRES_URL`); Redis cache
  (namespacing, JSON-only, falsey-vs-miss, sanitized errors) and coordination
  (wake-up pub/sub, advisory-lock release-only-owned) via `fakeredis`; S3 key
  validation and put/get/head/delete/signed-url/private-by-default/size-limit via
  an injected fake client.
- `app/tests/test_worker_fleet.py` — pure transition legality; registry
  registration/self-replacement, lifecycle marks, bounded heartbeats, stale
  detection/sweep and counts against a throwaway SQLite DB with an injected clock;
  the readiness probe's policy gate (informational vs required).
- `app/tests/test_worker_migration.py` — real Alembic `upgrade`/`check`/
  `downgrade`/re-`upgrade` against a temp SQLite DB, proving the migration is
  additive, drift-free, surgical on downgrade and preserves business data.
- `app/tests/test_api_isolation.py` — the operator `/internal/system/workers`
  endpoint's operator gate and coarse, secret-free shape.

Local/test suites require **no external infrastructure**: PostgreSQL is exercised
by SQL compilation (and an opt-in gated integration test), Redis by `fakeredis`,
and S3 by an injected fake client.

## Rollback plan

- **Migration.** `alembic downgrade -1` drops only `worker_registrations` and its
  indexes; it carries no business or job data, so nothing else is affected. Workers
  re-register on the next startup after a re-upgrade.
- **Adapters.** All new behaviour is additive behind the existing seams and is
  inert in default local mode. Reverting the branch restores the prior engine/cache/
  storage construction with no schema change required beyond the migration
  downgrade.
- **Policy.** `require_worker_fleet` defaults to `false`, so API readiness never
  depends on a worker being up unless an operator explicitly opts in.

## Out of scope (deferred to 3A.4b)

Metrics, tracing and OpenTelemetry; a structured operational-event framework;
readiness-result caching; Docker/Compose packaging and a production `.env`
template; deploy documentation; the operator-facing frontend fleet panel; and
operational dashboards/checklists.
