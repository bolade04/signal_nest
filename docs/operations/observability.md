# Observability (Phase 3A.4b Batches 2–3)

SignalNest's runtime observability is built from five cooperating, provider-neutral
pieces: **structured logging**, **secret redaction**, **request/job correlation**,
**bounded metrics**, and **distributed tracing**. All are safe by construction — they
never emit secrets or high-cardinality identifiers, and a telemetry failure can never
break a request, a database commit, job claiming, worker execution, or shutdown.

Production containers, deployment lifecycle, dashboards/alerts, and the broad
failure-injection suite are **deferred** to later batches.

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

## Distributed tracing

`app/core/tracing.py` is a pure-Python, provider-neutral tracing seam that mirrors the
metrics seam: core code opens spans through it and never imports an OpenTelemetry SDK.

- **Off by default.** The process tracer is a `NoOpTracer` unless one is installed via
  `configure_tracer(...)`. `configure_tracing_from_settings` selects the backend from
  config: `TRACING_ENABLED=false` → no-op; `TRACING_EXPORTER=memory` → `InMemoryTracer`
  (used in tests); `TRACING_EXPORTER=otlp` → import-guarded OTLP builder that
  **fails closed to a no-op** if the SDK is absent, so tracing can never block startup.
  A no-op span never touches `trace_id` context, so disabled tracing leaves existing
  log-correlation behavior unchanged.
- **Bounded span names + attributes.** Every span name must be in the catalog
  (`http.request`, `job.enqueue/claim/execute/complete/fail/retry/recover/dead_letter`,
  `worker.poll/heartbeat/register/shutdown`, `redis.notify/cache/lock`,
  `storage.upload/sign_url`, `database.transaction`, `readiness.check`) and every
  attribute key in the allow-list (`component`, `dependency`, `operation`, `outcome`,
  `http.request.method`, `http.route`, `http.response.status_code`, `worker.type`,
  `job.type`, `recovered`, `retryable`, `job.status`). A forbidden key (any id, URL,
  message, payload or token) raises `TraceError` at dev/test time.
- **W3C propagation.** Spans serialize to a strict `traceparent`
  (`00-<32hex>-<16hex>-<2hex>`); `parse_traceparent` rejects wrong-version, malformed,
  all-zero and newline-bearing headers. Inbound HTTP `traceparent` becomes the remote
  parent of the request span. Enqueue persists the active traceparent on the `jobs`
  row (additive nullable `trace_context`, migration `a1b2c3d4e5f6`); the worker
  restores it as the remote parent of `job.execute`, so a job's execution links back to
  the request or scheduler that enqueued it. A job with no persisted context starts a
  fresh root.
- **Deterministic, parent-based sampling.** The decision is derived from the trace id
  (RNG-free, so tests are deterministic) against `TRACING_SAMPLE_RATIO`. A parent's
  sampled flag is always honored (a child of a sampled parent records even at ratio 0).
  High-volume low-value roots (`worker.poll`, `readiness.check`, heartbeats) sample at
  one tenth of the ratio. `worker.poll` scopes only recovery + claim; `job.execute`
  runs after it closes so a claimed job's trace is never parented to the reduced-sampled
  poll.
- **Safe exception recording.** A span records only the exception **class** and an error
  status — never the message (which could carry a secret) and never a stack trace.
- **Bounded dependency spans.** Redis cache, S3 upload/sign-url and the job-claim DB
  transaction carry only `component`/`dependency`/`operation`/`outcome`; never a key,
  value, object key, bucket, endpoint or signed URL. DB tracing is a single
  transaction-level span, never a per-statement span, so cardinality stays bounded.
- **Failure isolation.** A `_SafeTracer` swallows and counts runtime export/flush
  failures (`trace_export_failure_count()`), never propagating them into the caller;
  only validation errors (coding bugs) raise.

## Operator diagnostics

`GET /internal/system/telemetry` (operator-only: 401 anonymous, 403 non-operator, 200
operator) returns the observability posture — `logging_format`, `metrics_enabled`,
`exporter_status` (`disabled`/`healthy`/`degraded`), `telemetry_failures`,
`correlation_enabled`, `redaction_enabled`, plus the coarse tracing posture
`tracing_enabled`, `tracing_exporter` (`none`/`memory`/`otlp`), `tracing_status`
(`disabled`/`healthy`/`degraded`), `tracing_sample_ratio`, and `trace_export_failures`.
It exposes bounded status values and counts only: no endpoint, credential, URL,
payload, token, or tenant/request/job identifier.
