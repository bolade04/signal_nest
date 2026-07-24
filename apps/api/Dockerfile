# syntax=docker/dockerfile:1

# SignalNest production images (Phase 3A.4b Batch 4).
#
# A single multi-stage Dockerfile with two runtime targets that share one locked
# dependency install:
#
#   * ``api``    — the FastAPI application served by uvicorn.
#   * ``worker`` — the durable job worker (``python -m app.jobs.worker``).
#
# Build the API image:    docker build -f apps/api/Dockerfile --target api    -t signalnest-api    apps/api
# Build the worker image: docker build -f apps/api/Dockerfile --target worker -t signalnest-worker apps/api
#
# Both images:
#   * pin an explicit Python version (never ``latest``),
#   * install runtime dependencies in a build stage and copy only the resulting
#     virtualenv into the runtime stage (no compilers or build toolchain shipped),
#   * run as a dedicated unprivileged user (UID/GID 10001), never root,
#   * are compatible with a read-only root filesystem (only ``/tmp`` is written),
#   * never bake secrets, a local database, or VCS metadata into the image
#     (enforced by ``.dockerignore`` and a CI build-context check).

# ---------------------------------------------------------------------------
# base — the shared, minimal runtime foundation for every stage.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

# Fail fast and never buffer stdout/stderr: structured logs must reach the
# container runtime immediately. Do not write .pyc files (read-only-root safe).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VENV_PATH=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# A dedicated, unprivileged, no-login user. A fixed high UID/GID keeps volume
# ownership predictable and never collides with a host system account.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

# ---------------------------------------------------------------------------
# builder — install locked dependencies into an isolated virtualenv.
# ---------------------------------------------------------------------------
FROM base AS builder

# Build-only: a compiler toolchain in case a dependency has no wheel. It never
# reaches the runtime image because only the finished venv is copied forward.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VENV_PATH"

WORKDIR /build

# Install dependencies first (declared in pyproject) as their own layer so a code
# change does not re-resolve the dependency graph. The ``full`` extra pulls the
# production adapters (psycopg, pgvector, redis, boto3); dev/test deps are never
# installed into the image.
COPY pyproject.toml ./
# A minimal package tree so ``pip install .`` can build the project metadata
# without the full source; the real source is layered into the runtime image.
RUN mkdir -p app && touch app/__init__.py \
    && python -m pip install --upgrade pip \
    && python -m pip install ".[full]"

# ---------------------------------------------------------------------------
# runtime — the shared runtime stage (venv + application source, non-root).
# ---------------------------------------------------------------------------
FROM base AS runtime

WORKDIR /app

# The finished virtualenv (runtime dependencies only — no build toolchain).
COPY --from=builder /opt/venv /opt/venv

# Application source, migrations and Alembic config. ``.dockerignore`` excludes
# .env, local SQLite databases, VCS metadata, caches and virtualenvs.
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 alembic ./alembic
COPY --chown=10001:10001 alembic.ini ./alembic.ini
COPY --chown=10001:10001 pyproject.toml ./pyproject.toml

# Make the source importable as the ``app`` package without an editable install.
ENV PYTHONPATH=/app

# Drop privileges for every runtime target below.
USER 10001:10001

# ---------------------------------------------------------------------------
# api — FastAPI served by uvicorn. Liveness = GET /health.
# ---------------------------------------------------------------------------
FROM runtime AS api

# Container-internal bind. The signal reaches the uvicorn process directly (PID 1
# via exec form), so SIGTERM drives the app's graceful lifespan shutdown.
ENV API_HOST=0.0.0.0 \
    API_PORT=8000
EXPOSE 8000

# ``/health`` is liveness only (readiness is a separate operator/probe surface).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('API_PORT','8000')+'/health', timeout=4).status==200 else 1)"]

# exec form -> uvicorn is PID 1 and receives SIGTERM directly. No --reload, one
# worker process per container (horizontal scale is by replica, not in-process).
#
# FORWARDED_ALLOW_IPS bounds which direct peers may rewrite the client address
# via X-Forwarded-For (uvicorn ProxyHeadersMiddleware). Default 127.0.0.1 keeps
# local/dev behavior: proxy headers from arbitrary peers are ignored. Behind the
# staging ALB the deployment sets it to the VPC CIDR so the rate limiter keys on
# the real client, not the ALB ENI. Never "*": with every host trusted, uvicorn
# falls back to the leftmost (client-forgeable) X-Forwarded-For entry.
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$API_HOST\" --port \"$API_PORT\" --no-server-header --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]

# ---------------------------------------------------------------------------
# worker — the durable job worker. No HTTP server, no exposed port.
# ---------------------------------------------------------------------------
FROM runtime AS worker

# The worker is a pure consumer: it exposes no port and runs no API server.
# exec form -> the Python process is PID 1 and receives SIGTERM directly, driving
# the worker's drain-then-stop lifecycle.
CMD ["python", "-m", "app.jobs.worker"]
