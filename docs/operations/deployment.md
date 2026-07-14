# Deployment (Phase 3A.4b Batch 4)

This guide covers the production **container images**, their **runtime contract**,
and the **rolling-deployment** model. Migrations are covered in
[migrations.md](./migrations.md); runtime observability in
[observability.md](./observability.md).

> Scope note: Batch 4 delivers images, lifecycle and the migration model.
> Orchestrator manifests (Kubernetes/Nomad), cloud infrastructure, dashboards,
> alerts and the incident-response runbook are **deferred to Batch 5**. This
> document describes the contract those artifacts must honor, not the artifacts.

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

**Rollback:** redeploy the previous image. Because migrations are additive-first,
the previous code runs against the newer schema (`ahead`). Only run a `downgrade`
(single actor, explicit target revision) if a specific migration must be reversed.

## Local full-mode stack (optional)

`infra/docker-compose.yml` runs the production images against real PostgreSQL and
Redis for local verification. It is a developer convenience, **not** a production
manifest (throwaway passwords, no orchestration/secrets/scaling):

```bash
docker compose -f infra/docker-compose.yml up --build
```

The `migrate` service runs the migration actor to completion first; `api` and
`worker` start only after it succeeds.
