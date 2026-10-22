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

# Install the LOCKED runtime dependency set first, as its own layer, so a code
# change does not re-resolve the dependency graph. ``requirements-runtime.txt`` is
# compiled by pip-tools from ``pyproject.toml`` with the ``full`` extra (psycopg,
# pgvector, redis, boto3) and carries every transitive pin plus artifact hashes;
# ``--require-hashes`` makes pip refuse any artifact not listed there (P6-PLAT-4).
# dev/test deps are never installed into the image. Two installs stay OUTSIDE the lock, as
# before this change: the ``pip`` upgrade below (unpinned) and the build-isolation fetch of
# ``setuptools>=68`` that ``pip install .`` performs; neither ships application code.
COPY requirements-runtime.txt pyproject.toml ./
RUN python -m pip install --upgrade pip \
    && python -m pip install --require-hashes -r requirements-runtime.txt
# The project itself (metadata only — a minimal package tree lets ``pip install .``
# build it without the full source) with ``--no-deps``: every dependency is already
# present from the locked set, so no application dependency can be pulled in outside
# the lock (the build backend fetch noted above is the one remaining unpinned install).
RUN mkdir -p app && touch app/__init__.py \
    && python -m pip install --no-deps .
# The venv is copied forward wholesale, and ``pip install --upgrade pip`` above put the
# latest pip INTO it — together with pip's vendored copies of urllib3, msgpack and
# pkg_resources/setuptools, which the merge-path image scan reports at their vendored
# (older, vulnerable) versions although the application's own locked urllib3 is current.
# Nothing at runtime installs packages, so pip is removed from the venv after the last
# install; the locked application dependencies and the project metadata stay as installed.
RUN python -m pip uninstall --yes pip \
    && test ! -e "$VENV_PATH/lib/python3.12/site-packages/pip" \
    && test ! -d "$VENV_PATH/lib/python3.12/site-packages/pip/_vendor"

# ---------------------------------------------------------------------------
# runtime — the shared runtime stage (venv + application source, non-root).
# ---------------------------------------------------------------------------
FROM base AS runtime

# The base image's own installer stays out of the final images (P6-CI-6 coverage gaps): python:3.12-slim
# ships pip in /usr/local (with its launchers) and the interpreter's bundled ensurepip wheel, and pip
# vendors packages (urllib3, requests, msgpack, setuptools/pkg_resources, ...) without dist-info, which an
# image scan does not analyse. Nothing at runtime installs packages or creates a virtualenv with pip, so
# both are removed here, as root, before the venv is copied in; the venv's own pip was removed in the
# builder stage above. After the last COPY below, a second check proves that no installer reached the
# final filesystem by any layer, and the merge-path image gate fails on any pip its scan lists.
RUN /usr/local/bin/python -m pip uninstall --yes pip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
    && rm -f /usr/local/lib/python3.12/ensurepip/_bundled/*.whl \
    && test ! -e /usr/local/lib/python3.12/site-packages/pip \
    && ! ls /usr/local/lib/python3.12/site-packages | grep -qi '^pip' \
    && ! ls /usr/local/lib/python3.12/ensurepip/_bundled | grep -q '\.whl$'

WORKDIR /app

# The finished virtualenv (runtime dependencies only — no build toolchain).
COPY --from=builder /opt/venv /opt/venv

# Application source, migrations and Alembic config. ``.dockerignore`` excludes
# .env, local SQLite databases, VCS metadata, caches and virtualenvs.
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 alembic ./alembic
COPY --chown=10001:10001 alembic.ini ./alembic.ini
COPY --chown=10001:10001 pyproject.toml ./pyproject.toml

# No package installer in the final filesystem (P6-CI-6 coverage gaps), checked after every layer that adds
# files: no pip package, pip metadata or wheel file under the interpreter prefix, the venv or the application,
# no pip launcher, and neither interpreter can import pip. The build fails otherwise. Reads only; writes nothing.
RUN ! find /usr/local /opt/venv /app \( -name '*.whl' -o -iname 'pip-*.dist-info' -o \( -type d -name pip -path '*-packages/pip' \) \) -print | grep -q . \
    && for f in /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.12; do \
           test ! -e "$f" && test ! -L "$f" || exit 1; \
       done \
    && ! /usr/local/bin/python -c "import pip" 2>/dev/null \
    && ! /opt/venv/bin/python -c "import pip" 2>/dev/null

# Make the source importable as the ``app`` package without an editable install.
ENV PYTHONPATH=/app

# Build provenance (INFRA-5; deployment-sha-wiring-plan.md §B). CI passes the
# exact trusted checkout SHA and an RFC 3339 timestamp; both default to "" so a
# local ``docker build`` without args is byte-for-byte unaffected at runtime —
# no BUILD_REVISION/APPLICATION_VERSION ENV is baked here because an empty ENV
# would override the application's safe defaults (build_revision=None); runtime
# injection is owned by the ECS task definition (wiring plan §C). The OCI
# ``revision`` label is the mandatory registry-side provenance the G4 preflight
# cross-checks against the source SHA and the ECR digest.
ARG GIT_REVISION=""
ARG IMAGE_CREATED=""
LABEL org.opencontainers.image.source="https://github.com/bolade04/signal_nest" \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      org.opencontainers.image.created="${IMAGE_CREATED}" \
      org.opencontainers.image.version="${GIT_REVISION}"

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
