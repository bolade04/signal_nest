# Deployment (Phase 3A.4b Batch 4)

This guide covers the production **container images**, their **runtime contract**,
and the **rolling-deployment** model. Migrations are covered in
[migrations.md](./migrations.md); runtime observability in
[observability.md](./observability.md).

> Scope note: Batch 4 delivers images, lifecycle and the migration model. Batch 5
> adds the operator runbooks — [worker_operations.md](./worker_operations.md),
> [incident_response.md](./incident_response.md), [dashboards.md](./dashboards.md),
> and [alerts.md](./alerts.md). Orchestrator manifests (Kubernetes/Nomad) and
> cloud infrastructure remain **out of scope / future** unless separately approved.
> This document describes the runtime contract those artifacts must honor.

## Cloud staging target (Phase 4B-C — planning only)

The selected cloud hosting for the internal, non-customer **SIGNALNEST_STAGING** (canary)
environment is **Amazon ECS on AWS Fargate in us-east-1**, under a hard **$200/month** budget
ceiling. This is a *planning selection*: **no AWS environment exists** merely because the
architecture was chosen, and **deployment requires the separately reviewed INFRA tranches**.

- Decision record: [adr-0001-aws-ecs-fargate-staging.md](../architecture/adr-0001-aws-ecs-fargate-staging.md)
- Authoritative runtime/security/cost contract: [aws-staging-runtime-contract.md](./aws-staging-runtime-contract.md)
- Implementation roadmap (INFRA-1…INFRA-9): [phase-4b-c-infra-plan.md](../phase-4b-c-infra-plan.md)

The full topology, network isolation, IAM, secrets, backups, observability, and cost
contract live in the runtime-contract document above and are **not** duplicated here. Two
requirements are load-bearing for any first staging deployment: it must build and deploy
**exact source SHA `3aadb8a1da0f26ffd183a4b05161747038d5957c`** (not merely an image
containing an earlier commit), and every artifact must be an **immutable, digest-pinned
image** deployed through a protected, human-approved workflow.

Note that `infra/docker-compose.yml` (below) remains a **local-development convenience
only** — it is never SIGNALNEST_STAGING and never a canary runtime.

## Images

A single multi-stage `apps/api/Dockerfile` produces two runtime targets from one
locked dependency install:

| Target | Command | Purpose | Ports |
| --- | --- | --- | --- |
| `api` | `uvicorn app.main:app` | FastAPI HTTP service | 8000 |
| `worker` | `python -m app.jobs.worker` | durable job worker | none |

```bash
docker build -f apps/api/Dockerfile --target api    -t signalnest-api    apps/api
docker build -f apps/api/Dockerfile --target worker -t signalnest-worker apps/api
```

Both images share these guarantees, verified in CI
(`scripts/docker-security-check.sh`, `container-build` job):

- **Pinned base + explicit Python** (`python:3.12-slim`, never `latest`).
- **Multi-stage:** a build stage installs the locked `.[full]` dependencies into a
  virtualenv; the runtime stage copies only that venv — **no compiler or build
  toolchain, no dev/test dependencies** ship.
- **Non-root:** both run as the dedicated unprivileged user `app` (UID/GID
  `10001`). The effective UID is never `0`.
- **Read-only-root compatible:** `PYTHONDONTWRITEBYTECODE=1`, no `.pyc` writes;
  only `/tmp` needs to be writable at runtime. Run with a read-only root filesystem
  and a writable `/tmp` mount.
- **No secrets in the image:** `.dockerignore` excludes `.env*`, `*.db`/`*.sqlite`,
  private keys, VCS metadata, caches, the virtualenv and the test suite; CI fails
  the build if any secret or local database would have shipped.

The `worker` image runs **no** HTTP server and exposes **no** port. The API image
exposes `8000` and declares a `HEALTHCHECK` against `GET /health` (liveness only).

## Runtime contract

- **Configuration is environment-driven** (`app/core/config.py`, Pydantic
  Settings). Provide configuration via environment variables / mounted secrets;
  never bake them into the image. Production (`ENVIRONMENT=production`,
  `APP_MODE=full`) is validated at startup and fails fast on a local backend,
  a weak `SECRET_KEY`, or a missing production dependency.
- **Signals reach the process directly.** Both images use exec-form commands so the
  application is PID 1 and receives `SIGTERM` directly, driving the graceful
  lifecycle below.
- **Logs go to stdout/stderr** as structured JSON (`LOG_FORMAT=auto` → JSON outside
  development). The container runtime collects them; the app writes no log files.
- **Liveness vs readiness.** `GET /health` is liveness (process is up). Readiness is
  the operator/probe surface (`app/system/probes.py`) that actively verifies
  backends; wire your orchestrator's readiness probe to that, not to `/health`.

## Graceful lifecycle

**API startup** (`app/main.py` lifespan), in order: install the tracer → run the
read-only **schema-compatibility gate** (verify, never mutate) → record the startup
metric. A database that is behind the code fails startup fast with an instruction to
run the migration actor.

**API shutdown** on `SIGTERM`: record the shutdown metric, then run the shared
bounded, idempotent sequence (`app/core/lifecycle.py`) — flush metrics, flush traces
under `TRACING_SHUTDOWN_FLUSH_SECONDS`, close the Redis cache/notifier clients, and
dispose the database pool. Every step is best-effort; a slow exporter or unreachable
backend can never block exit.

**Worker shutdown:** the first `SIGTERM` stops claiming and lets in-flight jobs
finish within `WORKER_SHUTDOWN_GRACE_SECONDS`; a **second** signal escalates to
`WORKER_FORCE_SHUTDOWN_GRACE_SECONDS`, abandoning still-running work so its lease
expires and the next worker recovers it. After the generation-fenced `STOPPED`
transition the worker runs the same bounded telemetry-flush + resource-close
sequence.

Set the orchestrator's termination grace period **≥ `WORKER_SHUTDOWN_GRACE_SECONDS`**
so a draining worker can finish in-flight jobs before the runtime sends `SIGKILL`.

## Staging operating window

Staging runs **windowed** (`staging_window_active`, `docs/operations/staging-window.md`): the NAT gateway and its EIP, the ALB with listener and
target group, and the ElastiCache replication group exist only inside an authorized window; RDS is stopped between windows by the runbook; the
workload stage is refused outside a window. Every procedure below executes inside an open window. The rollback floor (next section) is compared by
image digest and source revision, because task-definition revisions are re-registered per window.

## Rolling deployment

1. Build and publish the images at the new revision.
2. Run the **single migration actor** (`python -m app.db.migrate`) as a one-shot
   job and wait for success. Replicas never migrate.
3. Roll API and worker replicas. During the window in which old and new replicas
   coexist, old replicas run against the newer, additive-first schema and report
   `ahead` (startup-safe); new replicas report `compatible`. See
   [migrations.md](./migrations.md) for the additive-first policy that makes this
   safe.
4. A replica that starts against a database the migration actor has not advanced
   reports `pending` and fails fast rather than corrupting data.

**Rollback:** redeploy the previous image — never one below `AUTH4_ROLLBACK_FLOOR`
(see the P6-AUTH-4 cutover below). Because migrations are additive-first, the
previous code runs against the newer schema (`ahead`). Only run a `downgrade`
(single actor, explicit target revision) if a specific migration must be reversed —
never the P6-AUTH-4 migration as part of a rollback.

### P6-AUTH-4 one-way cutover (no old/new overlap)

The P6-AUTH-4 revision binds every access token to a server-side session (an
`auth_sessions` row checked on every request) and is **not** rolled out by the
rolling deployment above. A pre-AUTH4 replica starts cleanly against the AUTH4
schema (`ahead`) but ignores session revocation, issues session-less tokens that
every AUTH4 replica refuses, and re-mints any token it accepts. Behind a round-robin
load balancer without stickiness, old and new replicas together would make a
sign-out depend on which task answers and bounce users between signed in and signed
out. The cutover therefore quiesces both services to zero, migrates, and starts
AUTH4 tasks only: old and new backend replicas never serve at the same time.

Every step that changes infrastructure is applied from a **saved plan inspected
first** (`tofu plan -out`, `tofu show -json`, then `tofu apply` of that same file —
never a fresh plan); a plan that changes anything else is rejected:

0. **Pre-check (read-only).** Record whether a pre-AUTH4 API/worker service or task
   is running, and that no ECS deployment is `IN_PROGRESS` on either service (a
   service that does not exist yet counts as none). Otherwise stop.
1. **Preflight.** The AUTH4 image's migration head is the AUTH4 revision (parent
   `a452ee007cc2`); the live database revision is read with the revision reader;
   secrets are ready; `SESSION_ABSOLUTE_LIFETIME_MINUTES` and
   `ACCESS_TOKEN_EXPIRE_MINUTES` pass the startup validator
   (`0 < ACCESS_TOKEN_EXPIRE_MINUTES <= SESSION_ABSOLUTE_LIFETIME_MINUTES <= 720`).
2. **AUTH4-capable build.** Both image digests carry an
   `org.opencontainers.image.revision` label that contains the AUTH4 merge commit;
   record the digests and the revision.
3. **No unauthorized rollback path.** Nothing can redeploy an older digest meanwhile.
4. **Apply 1 — quiesce and register.** Inputs: the AUTH4 digests,
   `api_desired_count = 0`, `worker_desired_count = 0`,
   `ecs_deployment_rollback_enabled = false`, `deploy_workload = true`. Allowed: the
   API, worker and migration task definitions created, or replaced to the AUTH4
   digests (the only destroys allowed); both services created, or updated in place to
   the AUTH4 revision with desired count 0 and breaker rollback off (the breaker
   itself stays on). Then verify: 0 running and 0 pending tasks on both services, the
   deployment `COMPLETED`, and no healthy or draining target in the API target group.
5. **Migration.** `aws ecs run-task` with the exact migration task-definition
   revision ARN that Apply 1 registered (read it with
   `tofu state show 'module.ecs.aws_ecs_task_definition.migration[0]'`; never a
   family name or "latest"); record the ARN, exit 0 and the post-migration revision.
6. **Apply 2 — start AUTH4 only.** Desired count 0 → 1 on both services and nothing
   else (reject any task-definition change, and rollback must still be off). A failed
   AUTH4 deployment stops at zero tasks; recover by rolling **forward** to a repaired
   AUTH4 build.
7. **Verify.** Every running or pending task of the api, worker and migration
   families runs the AUTH4 revision and digest (a revision-reader task is allowed;
   any other family: stop), and each service has exactly one deployment, `COMPLETED`.
8. **Frontend.** Publish the AUTH4 SPA only after step 7.
9. **One-time re-login.** A token stored before AUTH4 carries no session id: its
   first request answers 401, the SPA clears it locally and shows sign-in once; a
   fresh sign-in opens a session with a 12-hour absolute limit.
10. **Session smoke** (a synthetic staging identity; record no token): sign in →
    `/auth/me` 200 → sign out 204 → the same token 401 on repeated requests, so every
    task is reached → sign in again → sign out everywhere 204 → every token of that
    identity 401; a token without a session id → 401.
11. **Apply 3 — the floor.** Record `AUTH4_ROLLBACK_FLOOR` (below); then, once each
    service's last completed deployment is the recorded floor revision, set
    `ecs_deployment_rollback_enabled = true` — breaker rollback back on for both
    services, and nothing else.

**`AUTH4_ROLLBACK_FLOOR`** is the first verified AUTH4-capable backend revision: the
API and worker task-definition revisions, image digests and image source revisions
that passed steps 7 and 10, recorded at step 11. Once AUTH4 sessions have been used,
no deployment — automatic or manual — may roll below it. The breaker's automatic
rollback is off for the whole cutover and returns only after the floor deployment
completed, so its target is always at or above the floor. A normal rollback is
another AUTH4-capable image at or above the floor; the preferred recovery is to roll
forward. Below the floor, revoked and expired sessions would work again, and a
pre-AUTH4 `/auth/me` would re-mint any accepted token into a fresh 12-hour one.

**Emergency only.** Restoring a pre-AUTH4 revision is a security-reset incident
procedure that needs explicit incident authorization naming the restore. Before any
pre-AUTH4 task serves a request: quiesce to zero (no overlap), then invalidate every
credential with the tracked `SECRET_KEY` hard cutover
([aws-staging-operational-procedures.md](./aws-staging-operational-procedures.md)
§5.1) — or, if rotation is impossible, a separately authorized bulk `auth_epoch`
increment for every user — and every user signs in again. `SECRET_KEY` rotation is
never the session-revocation mechanism of a normal rollout: sessions are revoked per
session (sign out) or per user (sign out everywhere).

### P6-AUTH-5 security headers and the SPA's CSP (repository side only)

**What ships.** The API sets these headers on every response — success, 401, 404,
422, CORS preflight, the rate limiter's 429 and the catch-all 500 (whose handler sets
them itself, because it runs outside every user middleware):

| Header | Value |
|---|---|
| `Strict-Transport-Security` | `max-age=31536000` (no `includeSubDomains`, no `preload`) |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` — omitted only on the three FastAPI HTML documentation pages (`/api/v1/docs`, `/docs/oauth2-redirect`, `/redoc`) |
| `Referrer-Policy` | `no-referrer` |
| `Cache-Control` | `no-store` |

The SPA build (`npm run build`) writes a `<meta>` Content-Security-Policy and a
`<meta name="referrer" content="no-referrer">` into `dist/index.html`, directly after
`<meta charset>`: `script-src 'self'` with no `'unsafe-inline'` and no `'unsafe-eval'`;
`style-src 'self' 'unsafe-inline'` (Radix's scroll lock injects a `<style>` with
runtime-computed text); `connect-src 'self'` plus the origin of `VITE_API_BASE_URL`;
everything else `'none'` or `'self'`. The build then runs
`scripts/assert-csp-build.mjs`, which fails it if the policy is missing, changed,
placed after a resource-loading tag, or does not match the one API origin compiled into
the bundle, if an inline `<script>` appears, or if a direct `eval(` / `new Function(`
appears. It detects only those two literal forms — other code-evaluation forms need a
real browser to notice. The build also fails unless `dist/index.html` opens with exactly
the prefix our template produces — `<!doctype html>`, `<html …>`, `<head>`,
`<meta charset="UTF-8" />`, the policy `<meta>` and the referrer `<meta>` — so no
comment, unclosed `<title>` or other markup can come before the policy and make the
browser ignore it. An absolute URL the bundle never fetches may be added to the guard's
never-fetched list only with that evidence; never add it to `connect-src` to make a
build pass.

**What does not ship (UNDELIVERED — tracked as `P6-INF-19`).** A `<meta>` policy cannot
carry `frame-ancestors`, and HSTS, `X-Frame-Options`, `X-Content-Type-Options`,
`Permissions-Policy` and the SPA's CSP and Referrer-Policy *as response headers* need a
CloudFront response-headers policy, which the edge decision in
[aws-staging-iac-plan.md](./aws-staging-iac-plan.md) §23 (decision 4) still excludes.
Until `P6-INF-19` ships, the SPA can be framed. The browser token also stays in
`localStorage` (founder decision FD-A5-1 (i)); moving it is deferred to Phase 7.

**Build inputs.** Build the SPA with `VITE_API_BASE_URL` set to the API's `https`
origin (a path is allowed; an empty value or a same-origin path such as `/backend`
yields `connect-src 'self'` only). Anything else — `//host`, another scheme, a bare
host — fails the build. The API's `CORS_ORIGINS` must contain the SPA's origin.

**Verify after every SPA publish — cutover step 8 included — and after every API
deploy** (read-only):

```bash
shasum -a 256 apps/web/dist/index.html       # at build time, after both guards pass: <index_sha256>
curl -sI https://<web_fqdn>/                 # 200; no security headers until P6-INF-19
curl -s  https://<web_fqdn>/ | shasum -a 256                # must equal <index_sha256>
curl -s  https://<web_fqdn>/reset-password | shasum -a 256  # deep link: must equal it too
curl -s  https://<web_fqdn>/ | grep -o 'http-equiv="Content-Security-Policy" content="[^"]*"'
curl -s -D - -o /dev/null https://<api_host>/health                # 200 GET: the six API headers
curl -s -D - -o /dev/null https://<api_host>/api/v1/no-such-route  # 404: the same six
```

Equal hashes prove the published page is, byte for byte, the page the guard checked
(`curl` without `--compressed` receives the stored bytes); the `grep` line only echoes
the policy for a reader and cannot tell a live policy from an inert copy of it. The
`connect-src` origin in the published page must equal the origin of the
`VITE_API_BASE_URL` the SPA was built with; a mismatch blocks every API call, and the
SPA then drops every returning user's stored token. For the AUTH4 cutover, build and
run the guard on the step-8 SPA before step 0, so a guard failure cannot strand a
half-finished cutover.

**Rollback.** There is no runtime switch that turns the headers off, by design. A defect
is fixed by rolling forward. If the AUTH4 cutover image is built from a commit that
contains P6-AUTH-5, the API headers are part of the image recorded as
`AUTH4_ROLLBACK_FLOOR`: they can be changed only by a later image, and the emergency
pre-AUTH4 restore removes them. A bad SPA policy is replaced by republishing a previous
build. HSTS already sent by the API stays in browsers for its `max-age`, harmless because
the API host is HTTPS-only (the SPA sends no HSTS until P6-INF-19).

**Browser verification (closure evidence).** One recorded real-browser run of a
production build (`npm run build` with `VITE_API_BASE_URL=http://127.0.0.1:8000`), served
by `vite preview` — which sends no CSP or Referrer-Policy header, so what is enforced is
the `<meta>` policy — against a local API whose `CORS_ORIGINS` contains the preview
origin: the app loads, sign-in, authenticated calls, reload and sign-out work, normal
use raises no CSP violation, an injected inline script and a `javascript:` URL are
blocked, and a fetch to another origin is refused. The run proves the tested source
tree, not a later published artifact; the live checks above cover that.

## Local full-mode stack (optional)

`infra/docker-compose.yml` runs the production images against real PostgreSQL and
Redis for local verification. It is a developer convenience, **not** a production
manifest (throwaway passwords, no orchestration/secrets/scaling):

```bash
docker compose -f infra/docker-compose.yml up --build
```

The `migrate` service runs the migration actor to completion first; `api` and
`worker` start only after it succeeds.
