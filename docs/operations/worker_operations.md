# Worker Operations (Phase 3A.4b Batch 5)

Operating the SignalNest **durable-job worker** — the separate process that claims
and executes durable jobs. This runbook covers starting, stopping, scaling, health
inspection, and troubleshooting. Deployment images are in
[deployment.md](./deployment.md); migrations in [migrations.md](./migrations.md);
telemetry in [observability.md](./observability.md); dashboards and alerts in
[dashboards.md](./dashboards.md) and [alerts.md](./alerts.md).

> The database is the **authoritative** durable-job queue. Redis is advisory-only
> (a wake-up hint), never the source of truth. A worker only needs the database to
> claim, execute, and complete work.

## What the worker is

The worker is a **separate process**, never auto-started inside the API. It:

- validates configuration and that the durable + registry schema is present,
- registers itself in the worker-fleet registry (`STARTING` → `READY`),
- polls the durable store on a bounded interval (never a busy-spin),
- recovers expired leases (an at-least-once safety net for crashed workers),
- atomically claims one due job (compare-and-set), executes its handler while
  heart-beating to hold the lease, then drives it to a terminal state, and
- on `SIGINT`/`SIGTERM` stops claiming, finishes in-flight work within a bounded
  grace period, then exits.

Source: `apps/api/app/jobs/worker.py`.

## Starting a worker

```bash
# From apps/api (virtualenv active), or inside the worker container:
python -m app.jobs.worker
# Repo dev wrapper:
npm run worker
```

Container form (the `worker` build target — no ports, non-root UID `10001`):

```bash
docker build -f apps/api/Dockerfile --target worker -t signalnest-worker apps/api
docker run --rm --read-only --tmpfs /tmp \
  --env-file worker.env signalnest-worker      # runs: python -m app.jobs.worker
```

Startup ordering is a hard gate: the worker **validates schema**, then **registers**
(retrying `worker_registration_retry_limit` + 1 times), and only then marks itself
`READY` and begins claiming. If registration exhausts its retry budget the process
**exits** rather than run unregistered. If the schema is missing it exits with
`Durable job schema is not initialized. Run migrations first`.

## Stopping and draining a worker

Draining is signal-driven and bounded:

| Signal | Behavior |
| --- | --- |
| First `SIGTERM`/`SIGINT` | Stop claiming new jobs; finish in-flight work within `worker_shutdown_grace_seconds` (default 10s); transition registry `DRAINING` → `STOPPED`; flush telemetry; close DB/Redis. |
| Second `SIGTERM`/`SIGINT` | Escalate: shorten the grace to `worker_force_shutdown_grace_seconds` (default 1s). Anything still running is abandoned — its lease expires and the next worker recovers it. |

Because in-flight work is fenced by a lease token, an abandoned job is **never lost
and never double-run**: the losing worker's mutations match zero rows, and a single
winner recovers it after the lease expires. Drain uses a **shared deadline**, so `N`
concurrency slots never sum to `N × grace`.

To stop a worker, send `SIGTERM` (e.g. `docker stop`, orchestrator termination, or
`kill -TERM <pid>`) and allow the grace period. Send a second `SIGTERM` only if you
need an immediate exit and accept lease-recovery of in-flight jobs.

## Scaling workers

- **Vertical (in-process):** `WORKER_CONCURRENCY` (default 1) runs that many
  identical worker threads in one process, each with its own DB session. The
  store's compare-and-set claim guarantees no two ever run the same job.
- **Horizontal (more processes):** run additional worker processes/containers.
  Every worker derives a unique id (`hostname-pid-<random>` unless `WORKER_ID` is
  set) and competes safely for jobs via `FOR UPDATE SKIP LOCKED` (PostgreSQL) or the
  per-candidate compare-and-set path (SQLite).

Guidance: prefer more processes for isolation/blast-radius; use concurrency for
lightweight IO-bound handlers. Keep the fleet sized so `jobs_queue_depth` drains
faster than it fills under normal load (see [dashboards.md](./dashboards.md)).

> Do **not** set the same fixed `WORKER_ID` on multiple concurrent processes.
> Registration generation-fencing will fence the older process out of the registry,
> but a shared identity defeats fleet observability. Leave `WORKER_ID` unset in
> normal operation.

## Health and status inspection

The worker exposes no HTTP surface of its own. Observe it through:

- **Fleet diagnostics (operator-only):**
  `GET /api/v1/internal/system/workers` — status counts, active/stale totals, and a
  per-worker lifecycle summary (never exposes worker id, host, or raw errors).
- **Queue diagnostics (operator-only):**
  `GET /api/v1/internal/system/jobs` — status counts and recent jobs.
- **Telemetry posture (operator-only):**
  `GET /api/v1/internal/system/telemetry` — metrics/exporter health and swallowed
  telemetry-failure count.
- **Metrics (emitted):** `worker_poll_total`, `worker_lease_recovered_total`,
  `jobs_claimed_total`, `jobs_completed_total`, `jobs_failed_total`,
  `jobs_retried_total`, `jobs_dead_lettered_total`, `job_execution_duration_ms`
  (see `apps/api/app/core/metrics.py`). Note: `worker_active` and `jobs_queue_depth`
  are defined in the catalog but **not yet emitted** by production code — read active
  worker and queue-depth counts live from the operator endpoints above instead (see
  [dashboards.md](./dashboards.md)).
- **Structured logs:** `worker.start`, `worker.signal`, `worker.signal.force`,
  `worker.draining`, `worker.stopped`, `worker.poll_error`, `worker.lease_lost`,
  `worker.heartbeat.lease_lost`, `worker.registry_heartbeat_failed`.

The registry state machine is `STARTING → READY → BUSY → DRAINING → STOPPED`, with
`STALE`/`FAILED` for unhealthy peers. A worker that stops heart-beating for longer
than `worker_stale_after_seconds` (default 60s) is swept to `STALE` by peers.

## Key configuration

All keys live in `apps/api/app/core/config.py` (env-var names are the upper-cased
field names). Defaults shown.

| Setting | Default | Meaning |
| --- | --- | --- |
| `WORKER_CONCURRENCY` | `1` | Worker threads per process. |
| `WORKER_POLL_INTERVAL_SECONDS` | `1.0` | Idle poll cadence. |
| `WORKER_LEASE_SECONDS` | `30.0` | Claim lease duration. |
| `WORKER_HEARTBEAT_SECONDS` | `10.0` | Lease/fleet heartbeat cadence (must be `<` lease). |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `10.0` | Normal drain budget. |
| `WORKER_FORCE_SHUTDOWN_GRACE_SECONDS` | `1.0` | Shortened grace after a second signal (must be `≤` normal grace). |
| `WORKER_STALE_AFTER_SECONDS` | `60.0` | Peer marked `STALE` after this silence. |
| `WORKER_REGISTRATION_RETRY_LIMIT` | `3` | Extra registration attempts before exit. |
| `WORKER_TYPE` | `durable-jobs` | Fleet label. |
| `WORKER_ID` | unset | Fixed identity (leave unset for a unique id). |
| `REQUIRE_WORKER_FLEET` | `false` | If false, API liveness never depends on worker presence. |
| `JOB_RETRY_BASE_SECONDS` / `JOB_RETRY_MAX_SECONDS` | `2.0` / `300.0` | Exponential backoff bounds. |
| `JOB_QUEUE_BACKEND` | `local` | Only `local` is implemented in this build. |

## Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Worker exits immediately with "schema is not initialized" | Migrations not applied to the target database. | Run the migration actor (`python -m app.db.migrate upgrade`); see [migrations.md](./migrations.md). |
| Worker exits with `WorkerRegistrationFailedError` | Registry table unreachable/contended for all retries. | Check DB connectivity and pool; confirm the `worker_registrations` schema; inspect DB errors. |
| `worker.lease_lost` warnings | Another worker reclaimed an expired lease (usually a slow handler exceeding the lease). | Expected under recovery; if frequent, raise `WORKER_LEASE_SECONDS` or lower handler latency. Confirms no double-run occurred. |
| `worker_lease_recovered_total` rising | Workers crashing/OOM mid-job, or handlers exceeding the lease. | Investigate worker stability and handler duration (`job_execution_duration_ms`). |
| Queue depth climbing (`/internal/system/jobs` `status_counts`) | Under-provisioned fleet or a stuck dependency. | Scale workers; check dependency health (Redis/storage/downstream). |
| Jobs land in dead-letter (`jobs_dead_lettered_total`) | A job exhausted its retry budget. | Inspect via `/internal/system/jobs`; fix the root cause; requeue per policy. |
| Worker shows `STALE` in fleet view | Heartbeat stalled (blocked handler, lost DB, or killed process). | Confirm the process is alive; restart if needed — its jobs are recovered automatically. |

## Safety invariants (do not break)

- The database remains the authoritative queue; Redis stays advisory-only.
- Every job mutation is fenced by `worker_id` + `lease_token`; a losing worker
  stops rather than reviving another worker's job.
- Registration generation-fencing prevents a stale same-id process from mutating a
  newer registration.
- Telemetry flush and resource cleanup on shutdown are best-effort and bounded, and
  never delay or fail the fenced terminal transition.
