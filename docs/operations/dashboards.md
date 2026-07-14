# Dashboard Recommendations (Phase 3A.4b Batch 5)

Recommended operator dashboards for SignalNest, built **only** from metrics the code
actually emits. SignalNest's metrics layer is provider-neutral
(`apps/api/app/core/metrics.py`): a bounded name catalog (`METRIC_NAMES`) and a
bounded label allow-list (`ALLOWED_LABELS`). These panels describe *what to graph*;
wiring a specific backend (Prometheus/OTLP collector/hosted vendor) is deployment
work, intentionally not prescribed here.

> **Honesty note.** Some names exist in `METRIC_NAMES` (so they are *validatable*)
> but are **not yet emitted** by production code. Those are called out explicitly as
> *defined, not yet emitted*. Panels that depend on them are marked **(pending
> instrumentation)** and should not be treated as live until the emit path lands.

## Metric inventory — emitted vs. pending

### Emitted today (safe to graph)

| Metric | Kind | Emitted from | Key labels |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `core/middleware.py` | `outcome`, `status_class` |
| `http_request_duration_ms` | histogram | `core/middleware.py` | `outcome` |
| `jobs_enqueued_total` | counter | `jobs/service.py` | `outcome`, `job_type`, `job_status` |
| `redis_notify_total` | counter | `jobs/service.py` | `dependency`, `outcome` |
| `jobs_claimed_total` | counter | `jobs/worker.py` | `worker_type`, `job_type` |
| `jobs_completed_total` | counter | `jobs/worker.py` | `outcome`, `job_type` |
| `jobs_failed_total` | counter | `jobs/worker.py` | `job_type`, `error_class` |
| `jobs_retried_total` | counter | `jobs/worker.py` | `job_type`, `error_class` |
| `jobs_dead_lettered_total` | counter | `jobs/worker.py` | `job_type`, `error_class` |
| `job_execution_duration_ms` | histogram | `jobs/worker.py` | `outcome`, `job_type` |
| `worker_poll_total` | counter | `jobs/worker.py` | `worker_type`, `outcome` |
| `worker_lease_recovered_total` | counter | `jobs/worker.py` | `worker_type` |
| `service_startups_total` | counter | `main.py` | `service`, `environment` |
| `service_shutdowns_total` | counter | `main.py` | `service`, `environment` |
| `migration_runs_total` | counter | `db/migrate.py` | `operation`, `outcome` |

### Defined but not yet emitted (do not graph as live)

| Metric | Kind | Status |
| --- | --- | --- |
| `jobs_queue_depth` | gauge | In `METRIC_NAMES`; **no production emit path**. Queue depth is currently observable via the operator endpoint `GET /api/v1/internal/system/jobs` (`status_counts`), not via metrics. |
| `worker_active` | gauge | In `METRIC_NAMES`; **no production emit path**. Active/stale worker counts are observable via `GET /api/v1/internal/system/workers` (`active_count`, `stale_count`), not via metrics. |
| `dependency_operation_total` | counter | In `METRIC_NAMES`; **no production emit path** yet. |
| `dependency_operation_duration_ms` | histogram | In `METRIC_NAMES`; **no production emit path** yet. |
| `telemetry_failures_total` | counter | Swallowed-telemetry-failure count is surfaced via `GET /api/v1/internal/system/telemetry` (`telemetry_failures`), not currently emitted as a labeled metric series. |

### Recommended signals with no metric at all (future instrumentation)

These are *not* in `METRIC_NAMES`; adding them is a follow-up (would extend the
catalog). Until then, use the operator endpoints or logs.

- **Oldest-pending-job age** — best backlog-health signal; today only inferable from
  `/internal/system/jobs` recent-job timestamps.
- **Lease/generation fencing-rejection counters** — `worker.lease_lost` and registry
  fencing are logged but not counted as metrics.
- **Polling fallback rate** (jobs claimed via poll vs Redis wake-up) — not
  distinguished in metrics.

---

## Dashboard 1 — API health

- **Request rate** — `rate(http_requests_total)` split by `status_class`
  (`2xx`/`3xx`/`4xx`/`5xx`).
- **Error ratio** — `http_requests_total{status_class="5xx"}` / total.
- **Latency** — `http_request_duration_ms` p50/p95/p99 (histogram quantiles).
- **Availability marker** — `service_startups_total` / `service_shutdowns_total`
  step lines to correlate deploys/restarts with error/latency changes.

## Dashboard 2 — Durable jobs

- **Throughput** — `rate(jobs_enqueued_total)` vs `rate(jobs_completed_total)` (the
  arrival-vs-drain view).
- **Failure mix** — stacked `jobs_failed_total`, `jobs_retried_total`,
  `jobs_dead_lettered_total` by `job_type` / `error_class`.
- **Execution latency** — `job_execution_duration_ms` p50/p95 by `job_type`.
- **Backlog (pending instrumentation)** — `jobs_queue_depth` by `job_status`. Until
  emitted, use `/internal/system/jobs` `status_counts` as the live source.

## Dashboard 3 — Worker fleet

- **Claim activity** — `rate(jobs_claimed_total)` by `worker_type`.
- **Poll outcome mix** — `worker_poll_total` split `idle` vs `claimed` (fleet
  saturation: high `claimed` share ⇒ under-provisioned).
- **Recovery rate** — `rate(worker_lease_recovered_total)` (spikes ⇒ workers
  crashing or handlers exceeding the lease).
- **Fleet size (pending instrumentation)** — `worker_active`. Until emitted, use
  `/internal/system/workers` `active_count` / `stale_count`.

## Dashboard 4 — Redis (advisory path)

- **Notify success/failure** — `redis_notify_total` by `outcome`. Sustained
  `failure` ⇒ Redis degraded; correctness unaffected (DB is authoritative), but
  enqueue→claim latency rises toward the poll interval.

## Dashboard 5 — Storage & dependencies

- **Dependency health (pending instrumentation)** — `dependency_operation_total` /
  `dependency_operation_duration_ms` by `dependency` / `operation` / `outcome`. Until
  emitted, rely on typed-error logs (`ObjectStorageUnavailableError`,
  `RedisUnavailableError`, `PostgresUnavailableError`) and `/system/readiness`.

## Dashboard 6 — Deployment & migrations

- **Migration runs** — `migration_runs_total` by `operation`
  (`upgrade`/`downgrade`/`check`) and `outcome`. A `failure` here should correlate
  with a halted rollout (see [incident_response.md](./incident_response.md) §9).
- **Restart/shutdown cadence** — `service_startups_total` /
  `service_shutdowns_total` by `service`.

## Notes for whoever wires the backend

- Respect the label allow-list (`ALLOWED_LABELS`): `service`, `environment`,
  `operation`, `outcome`, `status_class`, `dependency`, `worker_type`, `job_type`,
  `job_status`, `error_class`. No ids/keys/URLs/messages are ever labels — do not add
  high-cardinality dimensions in the dashboard queries either.
- Histograms (`*_duration_ms`) should be graphed as quantiles, not averages.
- Before publishing a panel for a **pending-instrumentation** metric, confirm the
  emit path has actually landed; otherwise the panel will read empty and mislead.
