# Observability (Phase 3A.4b Batch 2)

SignalNest's runtime observability is built from four cooperating, provider-neutral
pieces: **structured logging**, **secret redaction**, **request/job correlation**, and
**bounded metrics**. All are safe by construction — they never emit secrets or
high-cardinality identifiers, and a telemetry failure can never break a request, a
database commit, job claiming, worker execution, or shutdown.

Distributed tracing, production containers, deployment lifecycle, dashboards/alerts,
and the broad failure-injection suite are **deferred** to later batches.

## Structured logging

`app/core/logging.py` provides a JSON formatter for production sinks and a
human-readable console formatter for local development. The output format is
config-driven:

- `LOG_FORMAT=auto` (default) → `console` in development, `json` everywhere else.
- `LOG_FORMAT=json` / `LOG_FORMAT=console` force a specific formatter.
- `SERVICE_NAME` (default `signalnest-api`) stamps a stable, non-secret service id.

Every record carries a stable base field set (UTC `timestamp`, `severity`, `service`,
`environment`, `event`, and — when bound — `component`, `outcome`, `duration_ms`,
`request_id`, `trace_id`, `job_correlation_id`, `worker_type`). Emit events with
`log_event(logger, "event.name", component=..., outcome=..., **fields)`; extra fields
are redacted by the formatter before serialization. The formatter is defensive: if
formatting a record ever fails it falls back to a minimal safe payload rather than
raising into the caller.

**Never logged in production structured logs:** user/org/workspace/location ids, the
raw database job id, worker id, object keys, full URLs, emails, IPs, request/response
bodies, payloads, or any secret. Correlation ids (safe, opaque) are used instead.

## Secret redaction

`app/core/redaction.py` is a single reusable, recursive layer applied to every
structured value before it is emitted. It:

- redacts values whose **key** matches a sensitive substring (case-insensitive):
  passwords, secrets, tokens, api keys, authorization/cookie headers, credentials,
  private keys, client secrets, access keys, database/Redis URLs, DSNs, JWTs, signed
  URLs, lease/generation tokens;
- scrubs credentials and sensitive query parameters out of URL/DSN **strings**
  (`scheme://user:pass@host` → `scheme://[REDACTED]@host`);
- bounds depth, width and string length (oversized values are truncated);
- is cycle-safe and **never raises** (returns a safe marker on any internal error).

`sanitize_exception(exc)` returns `{error_class, error_message}` with the message
redacted, so an exception can be logged without leaking a secret-bearing message.

## Correlation

### HTTP requests

`CorrelationMiddleware` (`app/core/middleware.py`) accepts an inbound `x-request-id`
(or `x-trace-id`) **only** when it matches a strict, bounded opaque format; anything
missing, malformed, oversized, or newline-bearing is discarded and a fresh id is
minted. The id is bound to request-local context for the duration of the request,
echoed back in the `x-request-id` response header, and **reset on exit** (even on
error) so ids can never bleed between requests.

### Durable jobs

Enqueue mints an opaque `correlation_id` (`app/core/log_context.py`,
`app/jobs/service.py`) — distinct from the database job id, lease token, worker id and
every tenant id — persisted on the `jobs` row via additive nullable migration
`e7c2a9b4f1d3`. The worker restores it into the logging context for the whole
execution (`poll_once`) and clears it afterward; the heartbeat thread re-binds it
explicitly because contextvars do not cross threads. It is never used as a metric
label and is never exposed by a customer API.

## Metrics

`app/core/metrics.py` is a vendor-neutral seam. Core code emits through it and never
imports a hosted SDK.

- **Off by default.** The process backend is a `NoOpMetrics` unless one is installed
  via `configure_metrics(...)`; tests never emit to a real sink. `METRICS_ENABLED`
  gates emission.
- **Bounded names + labels.** Every metric name must be in `METRIC_NAMES` and every
  label key in `ALLOWED_LABELS` (`service`, `environment`, `operation`, `outcome`,
  `status_class`, `dependency`, `worker_type`, `job_type`, `job_status`,
  `error_class`). Unknown names or forbidden labels (any id, URL, message, tenant or
  business identifier) raise `MetricError` at dev/test time.
- **Failure isolation.** Runtime recording failures are swallowed and counted
  (`telemetry_failure_count()`), never propagated. Only validation errors (coding
  bugs) are raised.

### Catalog

| Family | Metrics |
| --- | --- |
| HTTP | `http_requests_total`, `http_request_duration_ms` |
| Jobs | `jobs_enqueued_total`, `jobs_claimed_total`, `jobs_completed_total`, `jobs_failed_total`, `jobs_retried_total`, `jobs_dead_lettered_total`, `job_execution_duration_ms`, `jobs_queue_depth` |
| Workers | `worker_poll_total`, `worker_lease_recovered_total`, `worker_active` |
| Dependencies | `redis_notify_total`, `dependency_operation_total`, `dependency_operation_duration_ms` |
| Telemetry | `telemetry_failures_total` |

Metrics are recorded only **after** the governing transaction commits: enqueue after
persistence, claim after the claim commits, completion/retry/fail/dead-letter after
the terminal transition, and lease recovery only for the rows this loop won. Redis
notify is counted separately from enqueue, so a coordination degradation is never
confused with an enqueue failure. Idle worker polls increment an aggregated counter
rather than logging, avoiding a log storm.

## Operator diagnostics

`GET /internal/system/telemetry` (operator-only: 401 anonymous, 403 non-operator, 200
operator) returns the observability posture — `logging_format`, `metrics_enabled`,
`exporter_status` (`disabled`/`healthy`/`degraded`), `telemetry_failures`,
`correlation_enabled`, `redaction_enabled`. It exposes bounded status values only:
no endpoint, credential, URL, payload, token, or tenant/request/job identifier.
