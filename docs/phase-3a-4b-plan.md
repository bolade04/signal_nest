# Phase 3A.4b — Observability and Deployment Hardening (Plan)

Base branch: `feat/phase-3a-observability-deployment`
Base `main` SHA at branch creation: `3fefb36d432c7c9c46118e29bf631c32120f5e65`

## Objective

Harden SignalNest's production runtime with:

- Concurrency-safe recovery and worker identity
- Structured logs
- Safe metrics
- Distributed tracing
- Production API and worker containers
- Graceful lifecycle behavior
- Deployment and migration procedures
- Operational dashboards and alerts
- Failure-injection and recovery tests

## Delivery strategy

This phase is large. It is delivered as reviewable batches, each a coherent set of
focused commits that passes the full repository gate before the next batch begins.

- **Batch 1 (complete) — concurrency and data-plane hardening.** The seven
  accepted Phase 3A.4a follow-ups below that concern correctness under concurrency
  and hostile input. Opened as a **draft** PR (#31) once green.
- **Batch 2 (this session) — structured logging, redaction, request/job
  correlation, and bounded/provider-neutral metrics.** Merges the original
  "structured logging" and "bounded metrics" batches: the two share the same
  telemetry seams (config, contextvars, failure isolation) and are cheaper to land
  and review together.
- **Batch 3 (this session) — distributed tracing + end-to-end telemetry.** A
  provider-neutral, OTel-compatible tracing seam (no-op default, in-memory test
  exporter, optional import-guarded OTLP), HTTP server spans, durable-job trace-context
  propagation, worker execution spans, bounded Redis/S3/DB spans, trace/log correlation,
  configurable parent-based sampling, exporter-failure isolation, and operator-safe
  trace diagnostics.
- **Batch 4 — production containers + graceful lifecycle + migration strategy.**
- **Batch 5 — operational runbooks, dashboards/alerts, failure-injection expansion,
  CI hardening, security review, acceptance report.**

### Batch 1 — completion record

Delivered on this branch (base `main` @ `3fefb36`), all focused commits:

- Single-winner expired-lease recovery (dialect-aware guarded CAS; exactly one
  winner, exactly one `lease_recovered` event).
- True PostgreSQL `FOR UPDATE SKIP LOCKED` lock-contention test (CI, `postgres:16`).
- Worker-registration generation fencing (opaque token rotated on register; a stale
  generation cannot heartbeat or transition its replacement).
- SQLite claim compare-and-set isolated in a per-candidate SAVEPOINT (a lost race
  rolls back only that attempt and preserves uncommitted caller work).
- Final S3 tenant-key validation (org/workspace segments validated individually and
  the fully composed key re-validated).
- Collision-resistant Redis cache-key encoding (percent-encoded components).
- Additive migration `df66ff0426d2` (nullable `worker_registrations.generation_token`).

Gate at completion (_historical Batch 1 checkpoint — superseded by the current
authoritative total of **364 passed / 0 skipped** in CI_): backend **224 passed** /
2 skipped (PG-gated), frontend **20/20**, smoke **13/13** (four-market isolation, no
cross-market contamination), migration upgrade/check/downgrade/re-upgrade green, ruff
clean, no contract drift, `npm audit` 0 vulnerabilities.

### Batch 2 — scope (this session)

1. **Structured logging.** Centralized JSON logging with a stable field set; a
   human-readable dev formatter behind validated configuration.
2. **Secret redaction.** One recursive redaction layer applied before any structured
   value is emitted; never raises into application code.
3. **HTTP request correlation.** Strict, bounded request-id middleware storing an id
   in request-local context, echoing it in a response header, and clearing context
   after every request.
4. **Durable-job correlation.** A safe opaque correlation id, distinct from the
   database job id / lease token / worker id / tenant ids, generated at enqueue,
   persisted (additive migration), and propagated through the whole job lifecycle.
5. **Worker correlation restoration.** The worker restores a job's correlation id
   into logging context for the duration of execution and clears it afterward.
6. **Provider-neutral metrics abstraction.** Counter/histogram/gauge behind an
   interface with a no-op default and an in-memory test backend; no hosted vendor in
   core code.
7. **Safe metric instrumentation.** Counters incremented only at authoritative
   commit points; durations recorded with monotonic time.
8. **Cardinality controls.** A controlled label allow-list with enforced rejection of
   forbidden high-cardinality labels; routes normalized to templates.
9. **Telemetry failure isolation.** Formatter/redaction/metrics/exporter failure can
   never break a request, a commit, job claiming, worker execution, or shutdown.
10. **Operator-safe observability diagnostics.** Coarse, operator-only telemetry
    status (modes/enabled flags/recent failure counts) with no secrets or ids.

Deferred to later batches (not in Batch 2): distributed tracing, production
containers, graceful deployment lifecycle, deployment runbooks, dashboards and
alerts, and the broad failure-injection suite.

If the draft PR's diff grows beyond what stays reviewable, the remaining
observability and deployment batches split into their own follow-up PRs.

### Batch 2 — completion record

Delivered on this branch as focused commits:

- **Structured logging + redaction** — a stable-field JSON formatter (human-readable
  console formatter in development) with a reusable recursive secret-redaction layer
  applied to every emitted structured value; the formatter never raises into the
  application (`app/core/logging.py`, `app/core/redaction.py`).
- **Request + job correlation** — strict bounded request-id middleware (accepts a
  valid inbound id or mints one, echoes it in a response header, resets context after
  every request) and an opaque durable-job `correlation_id` distinct from the row id /
  lease token / worker id / tenant ids, persisted via additive nullable migration
  `e7c2a9b4f1d3`, restored into the worker (and heartbeat thread) logging context for
  the duration of execution and cleared afterward (`app/core/middleware.py`,
  `app/core/log_context.py`, `app/jobs/service.py`, `app/jobs/worker.py`).
- **Provider-neutral metrics** — a fixed metric-name catalog and a strict label
  allow-list that rejects unknown names and any high-cardinality/identifying label at
  dev/test time; no-op default, in-memory test backend, runtime failures isolated and
  counted (`app/core/metrics.py`).
- **Lifecycle instrumentation** — bounded metrics at authoritative commit points for
  the HTTP path, enqueue, claim, completion, retry/fail/dead-letter, execution
  duration, single-winner lease recovery and Redis notify (counted separately from
  enqueue); idle polls increment an aggregated counter instead of logging.
- **Operator-only telemetry status** — `GET /internal/system/telemetry` (401 anon /
  403 non-operator / 200 operator) reporting logging mode, metrics enabled, exporter
  health, swallowed-failure count, and correlation/redaction flags — bounded status
  only, no ids/URLs/credentials/tokens.

Gate at completion (_historical Batch 2 checkpoint — superseded by the current
authoritative total of **364 passed / 0 skipped** in CI_): backend **309 passed** /
2 skipped (PG-gated), ruff clean, migration head `e7c2a9b4f1d3`
upgrade/check/downgrade/re-upgrade green. Full repository gate (frontend, smoke,
contracts, `npm audit`) recorded in `phase-3a-4b-acceptance.md`.

**Batch 2 is complete.**

### Batch 3 — scope (this session)

Distributed tracing and end-to-end telemetry, delivered as focused commits. Twelve
items:

1. **OpenTelemetry-compatible tracing abstraction.** A pure-Python provider-neutral
   seam (`app/core/tracing.py`) mirroring the Batch 2 metrics seam: no-op default
   tracer, in-memory test exporter, optional import-guarded OTLP exporter, W3C
   `traceparent` propagation. Core code never imports a hosted vendor SDK.
2. **HTTP request spans.** A server span per request when tracing is enabled, extending
   `CorrelationMiddleware`: strict inbound `traceparent` extraction, normalized route
   templates, method + status class, controlled error class, `trace_id` bound into the
   log context and cleared on exit.
3. **Durable-job trace propagation.** A safe W3C `traceparent` captured at enqueue and
   persisted in a new additive nullable `jobs.trace_context` column (migration chaining
   after `e7c2a9b4f1d3`); the worker restores it from the claimed row.
4. **Worker execution spans.** A linked execution span for the whole `run_claimed`
   lifecycle, restored from the persisted parent context.
5. **Dependency spans.** Bounded Redis (notify/cache/lock), S3 (upload/sign-url) and
   controlled DB spans (claim txn, recovery, registration, readiness) — no keys, URLs,
   tenant ids or payloads.
6. **Trace/log correlation.** Logs carry `trace_id` (and optionally `span_id`) only
   while a span is active; no leakage between requests/jobs; redaction preserved; trace
   ids never become metric labels.
7. **Trace/metric correlation.** The existing bounded metrics keep firing at the same
   authoritative commit points; spans and metrics agree on outcome without spans adding
   any high-cardinality label.
8. **Configurable sampling.** Parent-based ratio sampling, conservative default,
   reduced/suppressed for health/readiness and idle polls, validated config.
9. **Exporter-failure isolation.** A collector down at startup degrades to no-op;
   an export failure mid-request/job never breaks the request, the DB transaction, job
   claiming, worker execution, or shutdown; a bounded flush on shutdown.
10. **Operator-safe trace diagnostics.** `GET /internal/system/telemetry` gains coarse
    tracing fields (enabled, provider, exporter state, sampling mode/ratio, export
    failure count, last failure category, flush status) — never an endpoint, credential,
    trace id or span id.
11. **In-memory tracing tests.** Assertions run against the in-memory exporter with no
    external collector.
12. **CI validation.** The existing gates plus the new tracing tests; no real collector,
    hosted vendor, container, or deployment-pipeline change.

Deferred to later batches (not in Batch 3): production containers, graceful deployment
lifecycle, migration-execution containers, deployment/incident runbooks, dashboards and
alerts, the broad failure-injection suite, and Phase 3B.

### Batch 3 — completion record

Delivered on this branch as focused commits:

- **Provider-neutral tracing seam** — a pure-Python, OTel-compatible tracer
  (`app/core/tracing.py`) with a `NoOpTracer` default, an `InMemoryTracer` test
  exporter, an import-guarded OTLP builder that fails closed to no-op, a bounded
  span-name catalog + attribute allow-list, strict W3C `traceparent` parse/format, and
  deterministic parent-based ratio sampling (low-value roots reduced ×1/10). Core code
  imports no hosted vendor SDK; validated config (`app/core/config.py`).
- **HTTP request spans** — `CorrelationMiddleware` opens a bounded server span per
  request: inbound `traceparent` becomes the remote parent, route template (never the
  raw path), method + status code, safe error class; `trace_id` is bound into the log
  context only while a recording span is active and reset on exit.
- **Durable-job propagation** — enqueue persists the active traceparent on the `jobs`
  row via additive nullable migration `a1b2c3d4e5f6` (chains after `e7c2a9b4f1d3`); the
  worker restores it as the remote parent of `job.execute`. `worker.poll` scopes only
  recovery + claim, and execution runs after it closes so a claimed job's trace is never
  parented to the reduced-sampled poll (fresh root when no context was persisted).
- **Lifecycle + dependency spans** — job claim/complete/fail/recover, a single
  transaction-level DB claim span (never per-statement), and bounded Redis cache and S3
  upload/sign-url spans carrying only component/dependency/operation/outcome — never a
  key, value, object key, bucket, endpoint or signed URL.
- **Safe exception recording** — spans record only the exception class + error status,
  never the message or a stack trace.
- **Operator-safe trace diagnostics** — `GET /internal/system/telemetry` gains
  `tracing_enabled`, `tracing_exporter`, `tracing_status`, `tracing_sample_ratio` and
  `trace_export_failures` (bounded enums + counts only; no endpoint, credential, trace
  or span id).
- **Collector-free tests** — a 39-test tracing suite (`app/tests/test_tracing.py`)
  asserting against the in-memory exporter with no external collector, plus the
  isolation-test extensions for the new telemetry fields.

Gate at completion (_historical Batch 3 checkpoint — superseded by the current
authoritative total of **364 passed / 0 skipped** in CI_): backend **348 passed** /
2 skipped (PG-gated), ruff clean, migration head `a1b2c3d4e5f6`
upgrade/check/downgrade/re-upgrade green, frontend lint/type-check clean + **20/20**
tests, contracts regenerated with no residual drift, smoke **13/13** (four-market
isolation), `npm audit` **0 vulnerabilities**.

**Batch 3 is complete.**

### Batch 4 — scope (this session)

Production containers, graceful lifecycle, and migration strategy, delivered as focused
commits. Fourteen items:

1. **Production API image.** Multi-stage, pinned base, explicit Python version,
   non-root runtime, minimal final layer (runtime deps only, no build toolchain, no
   test deps, no VCS metadata), an explicit production server command.
2. **Production worker image.** A separate command from the API on the same verified
   runtime base; non-root; no bundled API server; no exposed ports it does not need.
3. **Multi-stage builds.** A shared build stage installs locked dependencies; the
   runtime stage copies only what it needs.
4. **Non-root runtime.** Both images create and run as a dedicated unprivileged user;
   the effective UID is never 0.
5. **Minimal runtime filesystem.** No package-manager cache, no compilers, no `.git`,
   no `.env`, no local databases; compatible with a read-only root filesystem.
6. **Health checks.** A container liveness probe hitting `/health` (liveness, not
   readiness); readiness stays the operator/probe surface.
7. **API startup + shutdown lifecycle.** Explicit ordered startup (config → logging →
   metrics → tracing → db → schema-compat → optional deps → readiness) and a bounded,
   idempotent drain that closes db/redis and flushes telemetry within a grace budget.
8. **Worker draining + shutdown lifecycle.** Register-before-claim, drain on SIGTERM
   (stop claiming, finish in-flight within grace, let the lease expire on a forced
   exit), generation-fenced STOPPED — hardening/covering the existing state machine.
9. **Telemetry flush during shutdown.** Bounded metrics + trace flush that can never
   block or fail the exit.
10. **Database, Redis, and tracing cleanup.** Engines/pools and Redis clients closed on
    shutdown; tracer flushed; all best-effort and bounded.
11. **Migration single-actor strategy.** One explicit migration actor
    (`python -m app.db.migrate`); API and worker replicas never auto-migrate. Documented
    in `docs/operations/migrations.md`.
12. **Rolling-deployment compatibility.** A startup schema-compatibility check (verify,
    never mutate) plus a documented additive-first, exact-head policy for this phase.
13. **Container CI validation.** A CI job that builds both images, proves non-root,
    proves the build context excludes secrets, and exercises the API/worker commands.
14. **Deployment documentation foundation.** `docs/operations/deployment.md` and updated
    `docs/operations/observability.md` lifecycle coverage.

Deferred to Batch 5 (not in Batch 4): final operations runbooks, the incident-response
runbook, dashboards and alert definitions, the broad failure-injection expansion, the
final security acceptance and independent review, and the final acceptance
classification. Phase 3B is not started.

### Batch 4 — completion record

Delivered on branch `feat/phase-3a-observability-deployment` as focused commits:

- **Production images.** A single multi-stage `apps/api/Dockerfile` with `api`
  (uvicorn) and `worker` (`python -m app.jobs.worker`) targets sharing one locked
  `.[full]` install; both non-root (UID/GID 10001), read-only-root compatible
  (only `/tmp` written), no build toolchain / dev deps / secrets / local DB. A
  build-context `.dockerignore` plus a CI secret-exclusion check.
- **Graceful API lifecycle.** Ordered startup (tracer → read-only schema-compat
  gate → readiness) and a bounded, idempotent drain (`app/core/lifecycle.py`) that
  flushes telemetry and closes the Redis clients + database pool, best-effort.
- **Worker draining.** Second-signal-shortens-grace
  (`worker_force_shutdown_grace_seconds`), a shared drain deadline, and the same
  telemetry-flush + resource-close after the generation-fenced `STOPPED`.
- **Single-actor migrations.** `python -m app.db.migrate`
  (upgrade/check/downgrade) with a startup schema-compatibility classifier
  (compatible/ahead/pending/uninitialized); replicas verify, never migrate.
  Documented in `docs/operations/migrations.md`.
- **Lifecycle telemetry.** Bounded `service_startups_total`,
  `service_shutdowns_total`, `migration_runs_total` on the existing metrics seam.
- **CI + compose + docs.** A `container-build` job (build both targets, prove
  non-root + secret-free, exercise the migration actor and the schema gate), an
  optional `infra/docker-compose.yml` local full-mode stack, and
  `docs/operations/deployment.md` + updated `observability.md`.
- **Tests.** `app/tests/test_deployment_lifecycle.py` (schema classifier + gate,
  migrate check/downgrade contract, bounded idempotent shutdown, worker forced
  drain).

Gate (_local run; the current authoritative CI total is **364 passed / 0 skipped**,
see "Batch 4 authoritative validation evidence" below_): backend `ruff` clean,
`pytest` **362 passed / 2 skipped** (the 2 skips are the PostgreSQL-gated tests, which
run and pass in CI); alembic head `a1b2c3d4e5f6` unchanged, no drift,
downgrade/re-upgrade green;
frontend lint/type-check clean, **20/20** tests, build green; `npm audit` **0**;
contract regenerated with no drift; smoke **13/13**, four-market isolation. Docker
and PostgreSQL are unavailable locally, so the `container-build` job and the
PG-gated claim test run in CI. **Batch 4 is complete; Batch 5 remains deferred.**

### Batch 4 authoritative validation evidence

CI-verified evidence for Batch 4. Consistent with the acceptance report's
"authoritative CI-green evidence" section. The phase remains **IN PROGRESS**
(Batches 1–4 complete, Batch 5 outstanding); this is not a phase acceptance.

#### Implementation head

- SHA: `e9e1678e3137748c4e5cd3ccf8a792f275c657ec`
- This is the authoritative Batch 4 **implementation** head, containing the
  production-container, graceful-lifecycle, migration-strategy,
  schema-compatibility, container-CI, and documentation changes.

#### Implementation CI

- Run ID: `29347362541`
- URL: <https://github.com/bolade04/signal_nest/actions/runs/29347362541>
- Conclusion: `success`

All five jobs succeeded:

- Frontend quality — success
- Backend quality — success
- Migrations and API contract — success
- Container build and security — success
- Integration smoke — success

#### Validation totals

- Backend CI: **364 passed, 0 skipped**.
- PostgreSQL-gated tests: executed in CI using `TEST_POSTGRES_URL`
  (0 skips remaining confirms the gated cross-worker claim/recovery test ran).
- Local backend: **362 passed, 2 PostgreSQL-gated skips** (no local PostgreSQL).
- Frontend: **20/20**.
- Smoke: **13/13**.
- Four-market isolation: passed, no cross-market contamination.
- Ruff: clean.
- Alembic: upgrade, drift check, downgrade, and re-upgrade all passed
  (head `a1b2c3d4e5f6` unchanged).
- Generated contracts: no residual drift.
- `npm audit`: **0 vulnerabilities**.

#### Container and lifecycle evidence

- API image built successfully.
- Worker image built successfully.
- Both images validated as non-root; runtime UID `10001`.
- Build-context secret scan passed when correctly scoped to `/app`.
- API import/startup validation passed.
- Migration actor command passed.
- Schema-compatibility gate: rejected an un-migrated database; reported
  `compatible` after migration.
- API lifecycle tests passed.
- Worker graceful-drain and bounded-shutdown tests passed.

#### Narrow CI corrections

1. **Docker security scan** — the original scan inspected the full base-image
   filesystem and incorrectly flagged public CA trust bundles (e.g.
   `/etc/ssl/certs/*.pem`, the certifi CA bundle). The scan scope was corrected to
   `/app`. No application secret leak was present — this was a false positive.
2. **Lifecycle metrics test** — the test hardcoded the `development` environment,
   but CI correctly runs with `ENVIRONMENT=test`. The test now derives `service`
   and `environment` from `Settings()`. No production behavior changed.

#### Annotation

- One non-blocking GitHub platform annotation, concerning Docker third-party
  actions running on Node.js 24 instead of the deprecated Node.js 20 runner.
- It is **not** an application defect, **not** a failed test, and **not** a skipped
  required test. All five jobs succeeded.

#### Documentation-synchronization note

The substantive Batch 4 **implementation** evidence is `e9e1678` (run
`29347362541`). Subsequent **documentation-only** commits (the acceptance, plan and
architecture-audit stamps) each re-ran the full CI and stayed green without changing
any implementation; those docs-only runs do not replace the implementation evidence
and are not re-tabulated here (the per-run breakdown lives once in
`phase-3a-4b-architecture-audit.md`).

### Batch 5 — scope (this session)

Operational readiness, resilience validation, security review, and final
acceptance — the closing batch of Phase 3A.4b. Delivered as focused commits across
three internal workstreams. Batch 5 adds **no customer features, no real external
connectors, and no orchestrator/cloud infrastructure**; it hardens, documents, and
validates the runtime that Batches 1–4 already built, then determines PR #31
merge-readiness.

#### Batch 5A — Operational documentation

Operator-facing runbooks and monitoring recommendations, grounded in the actual
repository (real commands, real config keys, and only metrics that the code emits).

1. **Worker-operations runbook** (`docs/operations/worker_operations.md`): starting,
   stopping, scaling, health inspection, lease/generation-fencing behavior, draining
   semantics (second-signal-shortens-grace), and troubleshooting — using real
   `python -m app.jobs.worker` invocation and real `worker_*` settings.
2. **Incident-response runbook** (`docs/operations/incident_response.md`): PostgreSQL
   outage, Redis outage, object-storage outage, worker-fleet loss, queue backlog,
   dead-letter spike, lease-recovery spike, connection-pool exhaustion, migration
   failure, telemetry outage, credential rotation, and a suspected cross-tenant
   incident — each with detection signal, triage, containment, and recovery.
3. **Dashboard recommendations** (`docs/operations/dashboards.md`): API health,
   durable jobs, worker fleet, Redis, storage, and deployment views built **only**
   from metrics in the `metrics.py` catalog; any useful-but-not-yet-emitted signal
   (oldest-job age, fencing-rejection counters, polling fallback) is explicitly
   labelled *recommended / not yet emitted*.
4. **Alert definitions** (`docs/operations/alerts.md`): a catalog of alerts, each with
   name, purpose, signal, recommended starting threshold, window, severity,
   false-positive notes, operator action, and the backing metric. Thresholds are
   documented as **starting points for tuning**, not validated production values.
5. **Rate-limit production decision**: resolve the `RateLimitMiddleware` placeholder
   as either (A) implement distributed enforcement now, or (B) defer with a documented
   accepted risk plus compensating controls. The decision is recorded in the security
   review and the residual-risk register, and finalized after the threat model.

#### Batch 5B — Resilience and failure injection

Deterministic, seam- and fake-based failure-injection tests (no brittle sleeps, no
real network) proving the runtime degrades safely. Adds a dedicated resilience test
module rather than editing unrelated suites.

6. **Dependency outages**: PostgreSQL, Redis, and object-storage unavailability —
   API liveness stays independent of optional dependencies; errors are typed and
   contained; no secret leaks into logs or responses.
7. **Worker termination mid-job**: a job in flight when the worker exits is not lost —
   the lease expires and a single winner recovers exactly one event.
8. **Worker-fleet loss**: with `require_worker_fleet=False`, API liveness/readiness
   never depend on worker presence; the queue simply accumulates.
9. **Queue backlog / poison job**: backlog does not corrupt state; a repeatedly
   failing ("poison") job is retried within policy and then dead-lettered, not looped
   forever.
10. **Connection-pool pressure**: pool exhaustion surfaces as a bounded, typed timeout
    rather than an unbounded hang.
11. **Migration failure**: a failed/incompatible migration is caught by the read-only
    schema gate; replicas refuse to start against an incompatible schema and never
    mutate it.
12. **Telemetry failure**: metrics/trace exporter failures fail closed to no-op and
    never break request handling or job execution (`telemetry_failures_total`).
13. **Invalid configuration**: production rejects local backends by name at `Settings`
    construction with a typed, secret-safe error.

#### Batch 5C — Security review and final acceptance

14. **Security review** (`docs/security/phase-3a-4b-security-review.md`): authn/authz,
    tenant isolation, secret handling, header/proxy trust, container security, durable-
    job security, and availability-abuse surface — each finding with id, severity,
    evidence, exploitability, mitigation, required-before-merge flag, action, and owner.
15. **Residual-risk register** (in `phase-3a-4b-acceptance.md`): distributed rate
    limiting, live Redis/S3 coverage, PR review burden, monitoring-threshold tuning,
    cloud/orchestrator validation, incident rehearsal, and capacity testing — each
    classified Resolved / Accepted-before-merge / Required-before-merge / Deferred.
16. **Live-integration decision**: decide whether live Redis/S3 integration is required
    before merge or acceptably covered by the existing fakes plus CI service coverage.
17. **Full-repository validation gate**: the complete regression + integration gate is
    re-run green in CI before acceptance.
18. **Final acceptance**: a single merge-readiness classification — exactly one of
    `BLOCKED`, `CHANGES RECOMMENDED`, `ACCEPTABLE WITH FOLLOW-UPS`, or
    `ACCEPTABLE TO REQUEST FINAL REVIEW` — with the PR kept **draft** until a human
    performs the final review.

Deferred beyond Batch 5 (explicitly **not** in scope): Phase 3B, real external
connectors, customer-facing features, Kubernetes/Nomad manifests, Terraform, and any
cloud-provider-specific infrastructure. These remain future work and are not Batch 5
gates unless separately approved.

### Phase status

- Batches 1–4 are complete.
- **Batch 5 remains outstanding**: final worker-operations runbook,
  incident-response runbook, dashboard recommendations, alert definitions, expanded
  failure-injection tests, final security review, final acceptance review, and the
  merge-readiness classification.
- Phase 3A.4b is **not** complete.
- PR #31 must remain **draft and unmerged**.
- Phase 3B has **not** started.
- Orchestrator manifests (Kubernetes/Nomad) and cloud infrastructure remain
  **optional / future** work, not a mandatory Batch 5 gate unless later approved.

## Accepted Phase 3A.4a follow-ups (Batch 1 scope)

1. Make expired-lease recovery single-winner under concurrent workers.
2. Add a true simultaneous PostgreSQL `FOR UPDATE SKIP LOCKED` contention test.
3. Add atomic worker registration or registration-generation fencing.
4. Revalidate the fully composed S3 tenant key.
5. Clarify and harden SQLite compare-and-set rollback and retry behavior.
6. Encode Redis key components to prevent delimiter collisions.
7. Measure worker-registry readiness-query performance before adding a composite index.

Item 7 is a measurement task, not a schema change; it is recorded here and carried
into Batch 6 with evidence, so Batch 1 does **not** add a speculative composite index.

## Non-goals

Explicitly excluded from Phase 3A.4b:

- Phase 3B
- Real external connectors
- Paid data providers
- Customer-facing observability UI
- Automated production posting
- Billing changes
- Large product-feature additions
- Redis becoming the authoritative job queue (the database remains authoritative)
- Removing SQLite support
- Changing public API contracts without explicit evidence it is required
- Kubernetes manifests (no approved Kubernetes direction exists in this repo)

## Completion gates

Phase 3A.4b is not complete until all of the following pass. (Batch 1 must satisfy
the Concurrency and Regression gates; later batches satisfy the rest.)

### Concurrency
- Expired-lease recovery has exactly one winner and creates exactly one event.
- True PostgreSQL lock contention is exercised in CI (not two sequential calls).
- Duplicate first registration is handled safely.
- An old registration generation cannot heartbeat or transition the replacement.
- Job lease-token fencing remains intact.
- SQLite compare-and-set semantics remain safe and match documented behavior.

### Observability
- Structured production logs exist with the agreed field set.
- Secret-redaction tests pass for the known secret patterns.
- Request/job correlation propagates across the enqueue→claim→execute→terminal flow.
- Metrics cover API, jobs, workers, Redis, S3, and database.
- Metric labels are bounded (no ids/keys/urls/messages as labels).
- Traces cover the API-to-worker flow; export failure never breaks core behavior.

### Deployment
- Separate API and worker production images exist.
- Both run as non-root and handle SIGTERM.
- Worker drains correctly; API readiness and liveness stay distinct.
- Migration execution is single-actor; no secret is baked into images.

### Operations
- Deployment, migration, worker, and incident-response runbooks exist.
- Alert recommendations and a rollback procedure exist.

### Regression
- Frontend lint, type-check, tests pass.
- Backend Ruff and full suite pass.
- PostgreSQL integration, Redis, and S3 tests pass.
- Migration upgrade/check/downgrade/re-upgrade passes.
- Smoke and four-market isolation pass.
- Generated contracts have no drift.
- `npm audit` reports zero vulnerabilities or every exception is documented
  (without running `npm audit fix`).
