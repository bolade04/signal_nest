# Incident Response (Phase 3A.4b Batch 5)

Operator playbook for SignalNest production incidents. Each scenario lists the
**detection signal**, **triage**, **containment**, and **recovery** steps, grounded
in the actual runtime (typed errors in `apps/api/app/core/errors.py`, metrics in
`apps/api/app/core/metrics.py`, endpoints under `/api/v1/system` and
`/api/v1/internal/system`). See also [worker_operations.md](./worker_operations.md),
[dashboards.md](./dashboards.md), and [alerts.md](./alerts.md).

## General principles

- **Liveness vs readiness.** `GET /api/v1/system/health` is cheap, dependency-free
  liveness. `GET /api/v1/system/readiness` runs bounded probes and returns `503`
  when a required capability is not ready. Never wire liveness to a dependency.
- **The database is authoritative** for durable jobs; Redis is advisory-only. Losing
  Redis degrades notification latency, not correctness.
- **Secrets never surface.** Errors are typed and messages carry class names, not
  payloads/credentials. If you see a secret in a log, treat it as a separate P1
  logging-redaction incident.
- **Telemetry never breaks the request path.** Exporter failures fail closed to
  no-op and increment `telemetry_failures_total` / `trace_export_failures`.
- **Where queue depth / worker counts come from.** `jobs_queue_depth` and
  `worker_active` are defined but **not yet emitted** as metrics; read them live from
  the operator endpoints `GET /api/v1/internal/system/jobs` (`status_counts`) and
  `GET /api/v1/internal/system/workers` (`active_count`/`stale_count`). Where this
  runbook names those metrics, use the endpoints until instrumentation lands (see
  [dashboards.md](./dashboards.md)).

## Severity quick reference

| Sev | Meaning | Examples |
| --- | --- | --- |
| SEV1 | Customer-facing outage or data-integrity risk | PostgreSQL down, suspected cross-tenant exposure |
| SEV2 | Degraded but serving | Storage outage, worker-fleet loss, large backlog |
| SEV3 | Elevated risk, no customer impact yet | Dead-letter spike, lease-recovery spike, telemetry outage |

---

## 1. PostgreSQL outage (SEV1)

**Detection.** `/system/readiness` returns `503` with `database` not ready;
`PostgresUnavailableError` in logs; API 5xx on data paths; workers exit at startup
("schema is not initialized") or fail to claim.

**Triage.** Confirm scope: is it connectivity (network/DNS/credentials), the instance
itself, or pool exhaustion (see §8)? Check `/internal/system/readiness` for operator
detail. Confirm liveness (`/system/health`) still returns `200`.

**Containment.** Do not restart-loop replicas — they will fail readiness and be pulled
from rotation automatically. Halt the migration actor if a rollout is in progress.
Preserve in-flight worker leases; they expire and recover safely.

**Recovery.** Restore PostgreSQL connectivity/instance. Replicas recover on their own
once readiness passes; no manual queue repair is needed — jobs that lost their lease
are recovered by the single-winner path. Verify `jobs_queue_depth` drains and
`worker_active` returns to expected.

## 2. Redis outage (SEV2)

**Detection.** `RedisUnavailableError` / `RedisNotifyFailedError` in logs;
`redis_notify_total{outcome="failure"}` rising; cache-dependent latency up.

**Triage.** Redis is **advisory-only**: durable-job correctness is unaffected.
Workers still poll the database on `WORKER_POLL_INTERVAL_SECONDS`, so jobs still run —
just without the low-latency wake-up hint. Confirm the database is healthy.

**Containment.** None required for correctness. If notify failures are noisy, expect
slightly higher enqueue→claim latency bounded by the poll interval.

**Recovery.** Restore Redis. Notification latency returns to normal. No queue repair
needed. If rate limiting was relying on a future Redis backend, note this is **not
yet implemented** (the limiter is process-local — see the security review).

## 3. Object-storage (S3) outage (SEV2)

**Detection.** `ObjectStorageUnavailableError` in logs; storage-dependent job types
failing/retrying; `/system/readiness` may flag `object_storage` if configured
required.

**Triage.** Determine whether uploads or signed-URL generation are affected. Jobs
that touch storage will fail as retryable and back off; they are not lost.

**Containment.** Expect retries within policy (`JOB_RETRY_BASE_SECONDS` →
`JOB_RETRY_MAX_SECONDS`). Watch `jobs_retried_total` and `jobs_dead_lettered_total`;
if backoff is exhausting budgets, consider pausing affected job producers.

**Recovery.** Restore storage. Retrying jobs succeed on the next attempt. Requeue any
dead-lettered jobs after confirming the root cause is resolved.

## 4. Worker-fleet loss (SEV2)

**Detection.** `worker_active` at/near zero; `/internal/system/workers` shows no
`READY` workers or all `STALE`; `jobs_queue_depth` climbing while
`jobs_claimed_total` flatlines.

**Triage.** With `REQUIRE_WORKER_FLEET=false` (default), **API liveness/readiness do
not depend on workers** — the API keeps serving and the queue simply accumulates.
Determine why workers died (deploy gone wrong, node loss, crash loop, DB registration
failure).

**Containment.** No data loss occurs — enqueued jobs wait durably in the database.

**Recovery.** Start workers (`python -m app.jobs.worker` / scale the `worker`
deployment). They register, mark `READY`, and drain the backlog. Confirm
`jobs_queue_depth` falls and terminal counters advance.

## 5. Durable-job queue backlog (SEV2)

**Detection.** `jobs_queue_depth` rising steadily; enqueue→terminal latency growing.

**Triage.** Compare arrival rate vs completion rate (`jobs_completed_total` +
terminal-failure counters). Backlog is either under-provisioning or a stuck
dependency (Redis/storage/downstream) causing retries.

**Containment.** Scale workers horizontally (more processes) and/or raise
`WORKER_CONCURRENCY` for IO-bound handlers. If a dependency is the cause, treat that
incident first (§2/§3).

**Recovery.** Confirm depth drains below alert threshold; return the fleet to baseline
size once caught up.

## 6. Dead-letter spike (SEV3)

**Detection.** `jobs_dead_lettered_total` climbing; alert fires.

**Triage.** Inspect recent jobs via `/internal/system/jobs` (status counts + recent).
Identify the failing `job_type` and `error_class` (from `jobs_failed_total` /
`jobs_dead_lettered_total` labels). Messages carry class names only — reproduce from
the job type and inputs, not from logged payloads.

**Containment.** If a bad deploy introduced the failure, roll back (see
[deployment.md](./deployment.md)). Pause the producer if it is generating poison jobs.

**Recovery.** Fix the handler/root cause, deploy, then requeue dead-lettered jobs per
policy. Confirm the counter stops rising.

## 7. Lease-recovery spike (SEV3)

**Detection.** `worker_lease_recovered_total` rising; frequent `worker.lease_lost` /
`worker.heartbeat.lease_lost` warnings.

**Triage.** Recovery is the safety net for crashed/slow workers. A spike means
workers are dying mid-job (OOM/crash) or handlers exceed `WORKER_LEASE_SECONDS`.
Cross-check `job_execution_duration_ms` and worker restart counts.

**Containment.** If handlers legitimately run long, raise `WORKER_LEASE_SECONDS`
(keeping `WORKER_HEARTBEAT_SECONDS` strictly below it). If workers are crashing,
treat as a stability incident.

**Recovery.** Confirm recovery rate returns to baseline. Recovery never double-runs a
job — lease fencing guarantees a single winner.

## 8. Connection-pool exhaustion (SEV2)

**Detection.** Requests/claims stalling then failing with pool-timeout errors;
latency spikes; `PostgresUnavailableError`-style timeouts under load.

**Triage.** Pool config: `DB_POOL_SIZE` (5), `DB_MAX_OVERFLOW` (10),
`DB_POOL_TIMEOUT_SECONDS` (30). A checkout that exceeds the timeout raises a **bounded**
error rather than hanging forever. Look for a slow query, a leaked session, or too
many concurrent workers for the pool.

**Containment.** Reduce concurrency (`WORKER_CONCURRENCY`, replica count) or raise pool
size to match. Identify and kill long-running queries.

**Recovery.** Confirm checkout latency normalizes; right-size the pool for steady-state
concurrency.

## 9. Migration failure (SEV1 during rollout)

**Detection.** The migration actor (`python -m app.db.migrate`) fails; or replicas
refuse to start because the startup schema-compatibility gate reports the schema is
behind the code (`app/db/schema.py`).

**Triage.** Replicas **verify, never mutate** — a `503`/refuse-to-start here is the
gate working as designed, preventing code from running against an incompatible
schema. Determine whether the migration partially applied.

**Containment.** Do not let replicas auto-migrate (they never do). Do not edit an
applied migration in place. Halt the rollout.

**Recovery.** Fix the migration, re-run the single actor to reach head, verify with
`python -m app.db.migrate check`, then continue the rollout. If a downgrade is
required, use the migration actor's downgrade path per [migrations.md](./migrations.md)
— never hand-edit the schema.

## 10. Telemetry outage (SEV3)

**Detection.** `telemetry_failures_total` / `trace_export_failures` rising;
`/internal/system/telemetry` shows `exporter_status="degraded"` or
`tracing_status="degraded"`.

**Triage.** Telemetry is **fail-closed to no-op**: metrics/trace export failures never
break requests, commits, job execution, or shutdown. Customer impact is
**observability loss only**, not availability loss.

**Containment.** None required for serving. Prioritize restoring visibility if an
unrelated incident is in progress and you are flying blind.

**Recovery.** Restore the collector/exporter. Counts stop rising; status returns to
`healthy`. No application restart is required.

## 11. Credential rotation (planned or emergency)

**Detection.** Planned; or triggered by suspected credential compromise.

**Triage.** Identify which secret (database URL, Redis URL, object-storage keys, JWT
signing key). Secrets are injected via environment/secret store — never baked into
images (CI-enforced) and never logged (`repr=False`, `hide_input_in_errors=True`,
recursive log redaction).

**Containment.** For suspected compromise, rotate immediately and revoke the old
credential at the provider.

**Recovery.** Update the secret store, then roll replicas so new processes pick up the
new value. The migration actor and workers read the same config. Verify
`/system/readiness` returns to `200` and no `*UnavailableError` remains.

## 12. Suspected cross-tenant incident (SEV1)

**Detection.** A report or anomaly suggesting one tenant saw another tenant's data;
unexpected results in tenant-scoped endpoints.

**Triage.** Tenant isolation is enforced by deriving job scope from **persisted job
columns**, not the transported message (`_context_from_job`), and by tenant-scoped
storage/cache keys. The four-market isolation tests (Dallas/London/Lagos/Nairobi)
guard this in CI. Confirm whether the exposure is real or a client-side artifact.

**Containment.** If confirmed, this is the highest-priority incident: preserve logs
and DB state for forensics, and consider disabling the affected code path. Do **not**
purge audit rows.

**Recovery.** Root-cause the isolation break, add a regression test in the four-market
isolation suite, patch, and file a security review finding. Notify per your data-
incident policy.

---

## After any incident

1. Confirm `/system/readiness` is `200` and `worker_active` / `jobs_queue_depth` are
   at baseline.
2. Capture the timeline, signals, and fix for the postmortem.
3. If the incident revealed a monitoring gap, tune the relevant alert in
   [alerts.md](./alerts.md) and, if a signal was missing, file it as a metrics
   follow-up (several useful signals are recommended-but-not-yet-emitted — see
   [dashboards.md](./dashboards.md)).
