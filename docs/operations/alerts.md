# Alert Definitions (Phase 3A.4b Batch 5)

Recommended alerts for SignalNest, each backed by a metric SignalNest actually emits
or an operator endpoint it exposes. Every alert lists its **signal**, a **recommended
starting threshold**, **window**, **severity**, **false-positive** notes, **operator
action**, and the **backing metric/endpoint**.

> **Thresholds are starting points, not validated production values.** They have not
> been calibrated against real traffic. Tune them against your own baselines before
> treating them as authoritative. Alerts marked **(pending instrumentation)** depend
> on a metric that is defined but **not yet emitted** — wire the emit path (or point
> the alert at the named operator endpoint) before enabling them.

Metric provenance is in [dashboards.md](./dashboards.md); response steps are in
[incident_response.md](./incident_response.md).

## API availability

### 1. API 5xx error ratio high
- **Purpose:** detect API-level failures affecting customers.
- **Signal:** `http_requests_total{status_class="5xx"}` / all `http_requests_total`.
- **Threshold / window:** > 2% over 5m (page at > 5%).
- **Severity:** SEV1 at page level, SEV2 at warn.
- **False positives:** a single bad client route; deploy blips. Require sustained 5m.
- **Action:** [incident_response.md](./incident_response.md) — check dependencies,
  recent deploy; consider rollback.

### 2. API latency p95 high
- **Purpose:** detect degraded responsiveness.
- **Signal:** `http_request_duration_ms` p95.
- **Threshold / window:** p95 > 1000ms over 10m.
- **Severity:** SEV2.
- **False positives:** cold start after deploy; batch traffic. Exclude the first
  minute post-restart (`service_startups_total`).
- **Action:** check DB pool (§8), slow queries, dependency latency.

### 3. Readiness failing
- **Purpose:** a required capability is not ready (serving degraded/removed from LB).
- **Signal:** `GET /api/v1/system/readiness` returns `503` (probe from your monitor).
- **Threshold / window:** any `503` sustained > 2m.
- **Severity:** SEV1.
- **False positives:** transient dependency reconnect; brief during rollout.
- **Action:** identify the unready capability via `/internal/system/readiness`;
  follow the matching incident section.

### 4. Unexpected restart churn
- **Purpose:** detect crash-looping API/worker.
- **Signal:** `rate(service_startups_total)`.
- **Threshold / window:** > 3 starts per service in 10m (outside a known deploy).
- **Severity:** SEV2.
- **False positives:** intentional rollout/scale event.
- **Action:** inspect logs for the crash cause; halt rollout if mid-deploy.

## Durable jobs

### 5. Dead-letter spike
- **Purpose:** jobs exhausting their retry budget.
- **Signal:** `rate(jobs_dead_lettered_total)`.
- **Threshold / window:** any increase > 0 over 15m (tune per baseline).
- **Severity:** SEV3 (SEV2 if a single `job_type` dominates).
- **False positives:** a known-bad batch already being handled.
- **Action:** [incident_response.md](./incident_response.md) §6 — identify
  `job_type`/`error_class`, fix, requeue.

### 6. Job failure ratio high
- **Purpose:** elevated handler failures.
- **Signal:** `rate(jobs_failed_total + jobs_retried_total)` / `rate(jobs_claimed_total)`.
- **Threshold / window:** > 20% over 10m.
- **Severity:** SEV2.
- **False positives:** a dependency incident already flagged (§8/§9 below).
- **Action:** correlate with dependency alerts; treat root dependency first.

### 7. Retry storm
- **Purpose:** a dependency causing mass retries/backoff.
- **Signal:** `rate(jobs_retried_total)`.
- **Threshold / window:** sustained > 3× baseline over 10m.
- **Severity:** SEV2.
- **False positives:** transient dependency blip.
- **Action:** check Redis/storage/downstream health; pause producers if needed.

### 8. Job execution latency high
- **Purpose:** handlers slowing down (risking lease loss).
- **Signal:** `job_execution_duration_ms` p95 by `job_type`.
- **Threshold / window:** p95 approaching `WORKER_LEASE_SECONDS` (e.g. > 20s with a
  30s lease) over 10m.
- **Severity:** SEV3.
- **False positives:** legitimately long handlers — if so, raise the lease.
- **Action:** optimize the handler or raise `WORKER_LEASE_SECONDS` (keep heartbeat <
  lease).

### 9. Queue backlog growing **(pending instrumentation)**
- **Purpose:** arrival outpacing completion.
- **Signal:** `jobs_queue_depth` (pending) — until emitted, poll
  `/api/v1/internal/system/jobs` `status_counts` pending count.
- **Threshold / window:** pending depth rising for > 15m or above an absolute cap.
- **Severity:** SEV2.
- **False positives:** expected burst that drains shortly.
- **Action:** [incident_response.md](./incident_response.md) §5 — scale workers /
  fix stuck dependency.

### 10. Enqueue failures
- **Purpose:** producers unable to enqueue.
- **Signal:** `jobs_enqueued_total{outcome="failure"}` (or non-`enqueued` outcomes).
- **Threshold / window:** any over 5m.
- **Severity:** SEV2.
- **False positives:** duplicate-suppression counted as a non-enqueue outcome — check
  the exact `outcome` label.
- **Action:** check DB write path and validation errors.

## Worker fleet

### 11. No active workers **(pending instrumentation)**
- **Purpose:** the fleet is gone while jobs wait.
- **Signal:** `worker_active` (pending) — until emitted, `/internal/system/workers`
  `active_count`.
- **Threshold / window:** `active_count == 0` for > 3m while pending jobs exist.
- **Severity:** SEV2 (API keeps serving; queue accumulates).
- **False positives:** intentional worker maintenance window.
- **Action:** [incident_response.md](./incident_response.md) §4 — start/scale workers.

### 12. Lease-recovery spike
- **Purpose:** workers crashing mid-job or exceeding the lease.
- **Signal:** `rate(worker_lease_recovered_total)`.
- **Threshold / window:** sustained > baseline over 10m.
- **Severity:** SEV3.
- **False positives:** a one-off worker restart.
- **Action:** [incident_response.md](./incident_response.md) §7 — investigate worker
  stability / handler duration.

### 13. Stale workers present **(endpoint-based)**
- **Purpose:** workers registered but not heart-beating.
- **Signal:** `/internal/system/workers` `stale_count`.
- **Threshold / window:** `stale_count > 0` for > 5m.
- **Severity:** SEV3.
- **False positives:** a worker mid-restart (clears on re-register or sweep).
- **Action:** confirm the process is alive; restart if needed (jobs auto-recover).

### 14. Worker poll saturation
- **Purpose:** fleet consistently busy (under-provisioned).
- **Signal:** `worker_poll_total{outcome="claimed"}` share of total polls.
- **Threshold / window:** claimed share > 80% over 15m.
- **Severity:** SEV3.
- **False positives:** a healthy busy period.
- **Action:** scale workers / raise `WORKER_CONCURRENCY` for IO-bound handlers.

## Dependencies

### 15. Redis notify failures
- **Purpose:** the advisory notify path is degraded.
- **Signal:** `rate(redis_notify_total{outcome="failure"})`.
- **Threshold / window:** sustained > 0 over 10m.
- **Severity:** SEV3 (correctness unaffected; latency rises to poll interval).
- **False positives:** brief Redis reconnect.
- **Action:** [incident_response.md](./incident_response.md) §2 — restore Redis; no
  queue repair needed.

### 16. Dependency errors **(pending instrumentation)**
- **Purpose:** PostgreSQL/Redis/storage operation failures.
- **Signal:** `dependency_operation_total{outcome="failure"}` (pending) — until
  emitted, alert on typed-error log volume (`*UnavailableError`).
- **Threshold / window:** sustained failures over 10m.
- **Severity:** SEV2.
- **False positives:** a single transient failure.
- **Action:** follow the matching dependency incident section.

## Deployment, migrations & telemetry

### 17. Migration failure
- **Purpose:** a migration-actor run failed during rollout.
- **Signal:** `migration_runs_total{outcome="failure"}`.
- **Threshold / window:** any occurrence.
- **Severity:** SEV1 during a rollout.
- **False positives:** none — treat every failure as real.
- **Action:** [incident_response.md](./incident_response.md) §9 — halt rollout, fix,
  re-run to head, `check`.

### 18. Schema-compatibility gate rejecting startup
- **Purpose:** replicas refusing to start against an incompatible schema.
- **Signal:** repeated startup failures / readiness `503` citing schema; correlate
  with `service_startups_total` not advancing after a deploy.
- **Threshold / window:** any sustained > 3m post-deploy.
- **Severity:** SEV1.
- **False positives:** the brief window between deploy and migration-actor completion.
- **Action:** run the migration actor to head before the code rollout completes.

### 19. Telemetry exporter degraded
- **Purpose:** observability loss (not availability loss).
- **Signal:** `/internal/system/telemetry` `exporter_status="degraded"` or
  `telemetry_failures` rising; `tracing_status="degraded"`.
- **Threshold / window:** degraded > 10m.
- **Severity:** SEV3.
- **False positives:** collector restart.
- **Action:** [incident_response.md](./incident_response.md) §10 — restore the
  collector; serving is unaffected.

### 20. Rate-limit rejections elevated
- **Purpose:** clients hitting the per-process fixed-window limiter (or abuse).
- **Signal:** `http_requests_total{status_class="4xx"}` filtered to `429` responses.
- **Threshold / window:** > baseline over 5m.
- **Severity:** SEV3.
- **False positives:** a legitimate traffic spike; **note** the limiter is
  process-local and not coordinated across replicas (see the security review), so the
  effective global limit scales with replica count — interpret this alert
  accordingly.
- **Action:** distinguish abuse from organic load; if distributed enforcement is
  required, see the rate-limit decision in the security review.

## Enabling checklist

1. Wire your monitoring backend to the emitted metrics (respect `ALLOWED_LABELS`).
2. For every **(pending instrumentation)** alert, either land the emit path or point
   the alert at the named operator endpoint — do not enable it against an empty
   series.
3. Calibrate each threshold against 1–2 weeks of real baseline before paging on it.
4. Route SEV1 to page, SEV2 to on-call queue, SEV3 to a review channel.
