# Security Review — Phase 3A.4b (Batch 5C)

Scope: the production data-plane, telemetry, container and lifecycle work delivered in
Phase 3A.4b (Batches 1–4) plus the Batch 5 operator runbooks and resilience suite. This
review is grounded in the actual runtime — `apps/api/app/auth/`, `app/core/`,
`app/jobs/`, `app/db/`, `apps/api/Dockerfile`, `scripts/docker-security-check.sh` — not
in aspiration. It records the **threat model**, per-area findings with evidence, and a
consolidated **residual-risk** disposition. It is an internal review by the implementing
engineer; it is **not** an independent third-party audit (see "Review independence").

## Threat model

**Assets.** Tenant data (organizations/workspaces/locations and their content), durable
job payloads, credentials (DB/Redis/S3 URLs, JWT signing key, object-storage keys), and
operational visibility.

**Trust boundaries.**
- Untrusted: anonymous internet clients; inbound HTTP headers (`authorization`,
  `x-request-id`/`x-trace-id`, `traceparent`, forwarding headers); transported job
  messages on the wire.
- Semi-trusted: authenticated customers (scoped to their org/workspace by role).
- Trusted: platform operators (`is_operator`), the single migration actor, the internal
  service network (PostgreSQL, Redis, object storage), the container runtime/secret store.

**Primary threats considered.** Cross-tenant data exposure; auth bypass / privilege
escalation; secret leakage via logs, traces, error messages or images; header/proxy
spoofing; job hijack / double-execution; availability abuse (request floods, poison
jobs, pool exhaustion); telemetry-induced denial of service.

**Out of scope for this phase** (tracked as deferred, not evaluated as merge gates):
network-layer DDoS mitigation, WAF, orchestrator/cloud IAM posture, and a distributed
rate-limit control (see F-1).

## Findings by area

Severity scale: **Critical** (exploitable cross-tenant/secret exposure) · **High**
(auth/isolation weakness under plausible conditions) · **Medium** (defense-in-depth gap
or degraded control) · **Low/Informational** (hardening note).

### Authentication & authorization

- **Evidence.** `app/auth/dependencies.py`: bearer token required (`get_current_user`,
  401 on missing/invalid/inactive); operator gate is a **server-controlled**
  `user.is_operator`, never derived from client input, email domain or org role
  (`require_operator`, 403 for authenticated non-operator). Role checks use a fixed rank
  table (`require_role`). `app/core/security.py`: JWT is `HS256` with `algorithms=["HS256"]`
  **pinned on decode**, so algorithm-confusion / `alg=none` downgrade is not accepted;
  bcrypt for password hashing.
- **Assessment.** No auth-bypass or privilege-escalation finding. The operator endpoints
  (`/internal/system/*`) correctly return 401 anon / 403 non-operator / 200 operator.

- **F-4 — Weak-secret detection is literal-match only.** *Severity: Low.*
  `app/core/config.py` rejects `secret_key == "dev-insecure-change-me"` or empty in
  staging/production, but enforces **no minimum length or entropy**. A short/guessable
  non-default key passes validation.
  *Exploit:* an operator who sets a weak but non-default `SECRET_KEY` could expose JWTs to
  offline brute force. Requires an operator misconfiguration; not remotely triggerable.
  *Mitigation / action:* documented here; recommend a minimum-length assertion as a small
  follow-up. **Not required before merge** (operator-controlled, defense-in-depth).
  *Owner:* backend. *Phase:* follow-up.

- **F-5 — bcrypt 72-byte truncation.** *Severity: Informational.*
  `_prepare()` truncates passwords to bcrypt's documented 72-byte limit. Standard and
  intentional (prevents a raise on long input); noted so it is not mistaken for silent
  data loss. No action required.

### Tenant isolation

- **Evidence.** Client-supplied tenant IDs are never trusted: `get_tenant_context`
  resolves a workspace to its organization and verifies membership server-side
  (`_membership`). Durable-job scope is derived from **persisted job columns**, not the
  transported message (`_context_from_job` in the worker), so a tampered in-flight
  message cannot redirect a job to another tenant. Storage/cache keys are tenant-scoped
  with collision-resistant encoding (Batch 1). The four-market isolation suite
  (Dallas/London/Lagos/Nairobi) and `test_api_isolation.py` guard this in CI.
- **Assessment.** No cross-tenant finding. This is the highest-severity class and is
  covered by both server-side enforcement and regression tests.

### Secret handling

- **Evidence.** Secret-bearing settings use `Field(repr=False)` and Pydantic
  `hide_input_in_errors=True` (`app/core/config.py`), so they never appear in model reprs
  or validation errors. One recursive redaction layer (`app/core/redaction.py`) scrubs
  sensitive keys and credential-bearing URL/DSN strings from every emitted structured
  value; `sanitize_exception` strips secret-bearing messages. Metrics/traces are bounded
  to an allow-list that forbids ids/URLs/payloads/tokens. `.dockerignore` excludes
  `.env*`, local databases and private keys; `scripts/docker-security-check.sh` fails the
  build on any baked secret or local database.
- **Assessment.** No secret-leak finding across logs, traces, error messages or images.

### Header & proxy trust

- **Evidence.** `CorrelationMiddleware` accepts an inbound `x-request-id`/`x-trace-id`
  and `traceparent` **only** when it matches a strict, bounded, newline-free opaque
  format; anything malformed/oversized is discarded and a fresh id minted, and the id is
  reset on exit so ids cannot bleed between requests. Inbound correlation is used only for
  observability, never for authorization.
- **F-2 — Client IP is taken from the transport peer, not a validated forwarding
  header.** *Severity: Low (Medium only if the limiter in F-1 is treated as a real
  control).* `RateLimitMiddleware` keys on `request.client.host`
  (`app/core/middleware.py`). Behind a reverse proxy/load balancer without configured
  proxy-header trust, every request appears to originate from the proxy IP, collapsing
  per-client limiting into a single bucket. There is **no** blind trust of a spoofable
  `X-Forwarded-For` here — which is the safe default — but it also means per-client
  limiting is not accurate behind a proxy.
  *Mitigation / action:* when a distributed limiter is implemented (F-1), derive the
  client identity from a trusted proxy hop, not a raw header. Tie-in to F-1; **not
  required before merge**. *Owner:* backend/platform. *Phase:* with F-1.

### Container security

- **Evidence.** Multi-stage `apps/api/Dockerfile`: pinned `python:3.12-slim`; runtime
  stage copies only the locked venv (no compiler/toolchain, no dev/test deps); runs as
  non-root `app` (UID/GID 10001, effective UID never 0); read-only-root compatible
  (`PYTHONDONTWRITEBYTECODE=1`, only `/tmp` writable); worker exposes no port. CI
  (`container-build`) runs the security check on both images and proves non-root.
- **Assessment.** No container finding. (The Batch 4 secret-scan false positive on public
  CA bundles was corrected to scan `/app` only — documented in the acceptance report.)

### Durable-job security

- **Evidence.** Every job mutation is fenced by `worker_id` + `lease_token`; registry
  mutations by a `generation_token`; expired-lease recovery is a single-winner guarded
  compare-and-set (exactly one winner, one `lease_recovered` event) — verified by
  `test_durable_jobs.py` and the new `test_resilience.py`
  (`test_abandoned_in_flight_job_is_recovered_and_completed`). Poison jobs are
  dead-lettered within the retry budget rather than looping
  (`test_poison_job_is_dead_lettered_within_budget`). The database is authoritative;
  Redis is advisory-only, so a Redis compromise/outage cannot cause job loss or
  double-execution.
- **Assessment.** No job-hijack or double-execution finding.

### Availability & abuse

- **F-1 — Rate limiting is a process-local, in-memory placeholder, not a distributed
  control.** *Severity: Medium.* `RateLimitMiddleware` is a per-process fixed-window
  counter (`limit=240 / 60s` per client) whose `_hits` dict is **not** shared across API
  replicas and is **not swept on a timer** (entries are pruned only when a given client
  key is next seen). Two consequences:
  1. The effective global limit scales with replica count (N replicas → ~N× the intended
     limit), so it is not a reliable abuse control at the edge.
  2. Under a high-cardinality source-IP flood the `_hits` dict grows with unique clients
     until those keys are revisited — a bounded but real memory-pressure vector on a
     single replica.
  *Exploit:* a distributed client can exceed the intended global request budget by
  spreading load across replicas; a spoofed-source flood can inflate per-replica memory.
  Neither breaches tenant isolation or leaks secrets — this is an availability/abuse
  concern only.
  *Mitigation / decision:* **see the rate-limit decision below.** Real edge protection is
  expected to come from a gateway/load-balancer or a shared-backend (Redis) limiter.
  *Owner:* platform/backend. *Phase:* deferred with accepted risk (Outcome B).

- **Pool exhaustion — bounded, not a finding.** DB checkout is bounded by
  `DB_POOL_TIMEOUT_SECONDS`; a checkout that exceeds it raises a typed, bounded error
  rather than hanging (`test_exhausted_connection_pool_raises_bounded_timeout`,
  `test_production_engine_pool_timeout_is_bounded`). Documented in
  `incident_response.md` §8.

- **Telemetry DoS — mitigated, not a finding.** Metrics/trace export failures fail closed
  to no-op and are counted (`telemetry_failures_total` / `trace_export_failures`); a
  failing exporter can never break a request, commit, job execution or shutdown
  (`test_job_execution_completes_despite_metrics_backend_failure`).

## Rate-limit production decision (Batch 5 step 8)

**Decision: Outcome B — defer distributed rate limiting with explicitly accepted risk.
Not implemented in Phase 3A.4b.**

- **Why not Outcome A (implement now).** A correct distributed limiter belongs at the
  edge (gateway/load balancer) or on a shared backend (Redis). Building a bespoke
  Redis-coordinated limiter inside the app in this phase would (a) promote Redis toward a
  correctness-bearing role the phase deliberately keeps advisory-only, (b) add a new
  failure mode to the request path, and (c) duplicate a control most production
  deployments terminate at the gateway. None of the Phase 3A.4b threat-model assets
  (tenant data, secrets, job integrity) depend on it.
- **Accepted risk.** Until an edge or shared-backend limiter is in place, the in-process
  limiter is best-effort only: the effective global limit scales with replica count and
  it is not accurate behind a proxy (F-1, F-2). This is an **availability/abuse** risk,
  **not** an isolation or confidentiality risk.
- **Conditions on the acceptance.** The residual risk is recorded in the acceptance
  register; the code comment and `alerts.md` (Alert 20) already flag the limiter as
  process-local; operators are directed to terminate abuse controls at the gateway. If a
  future threat model requires per-client global enforcement, implement it at the edge or
  on a shared backend and derive client identity from a trusted proxy hop.

This makes the rate-limit item **PARTIALLY COMPLETED — production distributed enforcement
intentionally deferred**, consistent with the architecture audit.

## Review independence

This review was performed by the implementing engineer against the code as written, with
test evidence cited inline. It is a substantive self-review, **not** an independent
third-party security audit or penetration test. Where this document says "no finding," it
means no finding was identified in this review — it does not assert external validation.
An independent review/pentest before or shortly after production exposure remains a
recommended follow-up (recorded in the residual-risk register).

## Finding summary

| ID | Area | Severity | Required before merge? |
| --- | --- | --- | --- |
| F-1 | Rate limiting not distributed | Medium | No — deferred, accepted (Outcome B) |
| F-2 | Client IP from transport peer behind proxy | Low | No — with F-1 |
| F-4 | Weak-secret detection literal-only | Low | No — follow-up |
| F-5 | bcrypt 72-byte truncation | Informational | No — no action |

No Critical or High findings. No finding requires code change before merging PR #31.
