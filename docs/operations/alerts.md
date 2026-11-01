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

### 5a. Scheduled scouting tick failed (P6-PLAT-6)
- **Purpose:** a recurring schedule lost an occurrence. The chain normally survives
  (the tick's terminal-failure hook re-seeds the next occurrence), but a customer's
  scheduled run did not happen, and a chain that could not be re-seeded stops.
- **Signal:** `increase(jobs_dead_lettered_total{job_type="scout_schedule.tick"})` +
  `increase(jobs_failed_total{job_type="scout_schedule.tick"})` (both dead-letter paths
  — retries exhausted, and lease expired with no attempts left — are counted). Log
  alternative: the ERROR event `scout_schedule_tick_failed`; page-worthy when
  `chained=false` with `outcome` other than `disabled` / `schedule_missing`. Pollable
  fallback: `/api/v1/internal/system/overview` `schedules.state_counts.stalled > 0`.
- **Threshold / window:** any increase over 1h (ticks fire at most daily per schedule).
- **Severity:** SEV3; SEV2 when `stalled > 0` (recurrence stopped).
- **False positives:** a deliberate pause/delete racing an in-flight tick (logged with
  `outcome=disabled` / `schedule_missing`, `chained=false`).
- **Action:** [scout-scheduling-runbook.md](./scout-scheduling-runbook.md) "Health
  checks" — inspect the failed tick's `error_class`; resume any `stalled` schedule
  after fixing the cause.

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
- *(2026-10-20, `P6-PLAT-3`)* With `RATE_LIMIT_BACKEND=redis` the limit is shared
  across replicas, and the per-process note above applies only to the `memory`
  backend. While Redis cannot answer, watch
  `dependency_operation_total{dependency="redis",operation="rate_limit"}`
  (`outcome="degraded"`: limiting fell back to per process; `outcome="denied"`:
  `deny` mode answered `503`) and the `rate_limit.shared_store_unavailable` warning
  (see [deployment.md](./deployment.md), runtime contract).

## Scout execution

### 21. Blocked Scouts (needs attention) (FU-267-A1, PROPOSED)
- **Purpose:** a Scout is **blocked** (`needs_attention`): an external operation it
  admitted (a model call or a feed fetch) has no known end and is eligible for
  reconciliation. It admits nothing more — no Run now, no scheduled run — until an
  operator reconciles it, and nothing in SignalNest clears it on its own. Customers see
  "Needs attention" on the Scout; this alert is the **operator** signal and is separate
  from any customer notification.
- **Signal:** the gauge `scouts_blocked` (Scouts blocked now, across every tenant; no
  labels), with `scouts_blocked_oldest_age_seconds` (how long the longest-blocked one has
  been blocked; `0` when none is) for escalation. Every worker sets both from its registry
  heartbeat (every `WORKER_HEARTBEAT_SECONDS`, no customer traffic needed) to the same
  fleet-wide value, so read the **Maximum** across workers, never the sum. The count is
  exactly the Scouts `python -m app.jobs.reconcile blocked` lists. Pollable fallback (and
  the source while metrics are off): `GET /api/v1/internal/system/overview` →
  `scouts.blocked_count`, `scouts.oldest_blocked_age_seconds`. **PostgreSQL only:** on
  SQLite no admission ledger is kept, so the gauges are absent (never `0`) and the
  overview reports `scouts.ledger_kept: false` with `null` values.
- **Threshold / window:** `Maximum(scouts_blocked) >= 1` in 3 consecutive 5-minute
  periods (15m). Escalate when `Maximum(scouts_blocked_oldest_age_seconds) > 86400` (a
  Scout blocked for over a day) or `scouts_blocked >= 3` (several at once usually means a
  provider or feed incident).
- **Severity:** SEV3; SEV2 on either escalation.
- **Missing data:** treat missing data as **missing** and **notify on
  `INSUFFICIENT_DATA`** (or, where a backend cannot notify on that state, treat missing data
  as **breaching**). Never treat it as not breaching: an absent gauge means nobody is
  measuring — no worker is running, metrics are off on the workers, or every worker's read is
  failing — which is indistinguishable from "no blocked Scouts" if it reads as OK. Nothing
  else covers that gap today: alert 11's `worker_active` is defined but has no emit path,
  and alert 13 is endpoint-based, so neither can back an alarm. A gauge that stops while
  workers are healthy means metrics are off or the read is failing
  (`worker.blocked_scouts_sweep_failed` warnings, class name only).
- **Read bound:** on PostgreSQL each sweep's read sets a transaction-scoped `lock_timeout`
  and `statement_timeout` (`set_config(…, true)`, the function form of `SET LOCAL`): 2 s and
  5 s by default, and at most a quarter of
  `WORKER_STALE_AFTER_SECONDS − WORKER_HEARTBEAT_SECONDS` for any such margin of at least
  0.4 s (below that a 100 ms floor applies, because `0` would mean no timeout), because the
  read shares the registry heartbeat's loop. A read that hits the bound (for example while a migration holds
  a lock on `scout_admissions`, `scout_requests` or `jobs`) is logged as
  `worker.blocked_scouts_sweep_failed` and sets no gauge for that interval; the next beat is
  late by at most about the bound. Sustained, it shows as missing data.
- **False positives:** none by state — a healthy in-flight operation (its attempt still
  holds its lease, or its stored deadline has not passed by the database clock) is never
  counted, and every counted Scout really cannot run. But a blocked Scout does **not**
  mean the external call failed: its end is unknown, or it outlived its deadline. A burst
  during a provider incident (timeouts, 5xx/529) is real and is handled as one incident.
- **Action:** [scout-scheduling-runbook.md](./scout-scheduling-runbook.md) "Reconciliation
  (procedure)" and [incident_response.md](./incident_response.md) §13: list the Scouts with
  `python -m app.jobs.reconcile blocked`; for each admission, establish **outside
  SignalNest** (the provider's console or request log, the feed server) that the
  operation is over; then `python -m app.jobs.reconcile resolve --admission <id>
  --operator <name> --reason <code>`. The stored deadline establishes eligibility under
  the procedure, not proof that the external party stopped: reconciling is the
  operator's declaration. Reconciliation changes no accounting.
- **Backing metric/endpoint:** `scouts_blocked`, `scouts_blocked_oldest_age_seconds`
  (`app/jobs/blocked_signal.py`, set by `Worker._start_registry_heartbeat`);
  `/internal/system/overview` `scouts`. **Not wired in any environment yet:** the gauges
  reach CloudWatch only with `METRICS_ENABLED=true` and `METRICS_EXPORTER=emf` on the
  worker, and no alarm for them exists in IaC (the staging IaC is frozen).

## Enabling checklist

1. Wire your monitoring backend to the emitted metrics (respect `ALLOWED_LABELS`).
2. For every **(pending instrumentation)** alert, either land the emit path or point
   the alert at the named operator endpoint — do not enable it against an empty
   series.
3. Calibrate each threshold against 1–2 weeks of real baseline before paging on it.
4. Route SEV1 to page, SEV2 to on-call queue, SEV3 to a review channel.
