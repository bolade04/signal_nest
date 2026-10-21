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
# D4b ISOLATED CANDIDATE (SPIKE — DO NOT MERGE; operator continuation of 2026-10-21 §5). psycopg runs its pure-Python
# implementation over Debian's libpq5 instead of psycopg-binary's bundled libpq/OpenSSL/Kerberos/LDAP copies, so the
# image's OS-package scan lists libpq and the OpenSSL it links as Debian packages. Nothing here addresses the image's
# own CPython or numpy's bundled OpenBLAS.
FROM base AS runtime-libs

RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 \
    && rm -rf /var/lib/apt/lists/*

# The finished virtualenv (runtime dependencies only — no build toolchain).
COPY --from=builder /opt/venv /opt/venv

# D4b linkage proof (build time; reads only; fails the build otherwise): psycopg's implementation is "python"; no
# psycopg_binary / psycopg_c package and no bundled ".libs" directory is installed; the libpq, libssl and libcrypto
# the process maps are Debian's, under /usr/lib/x86_64-linux-gnu.
RUN /opt/venv/bin/python - <<'PY'
import importlib.util, pathlib, re
import psycopg
from psycopg import pq
assert pq.__impl__ == "python", f"psycopg implementation is {pq.__impl__!r}, expected 'python'"
for mod in ("psycopg_binary", "psycopg_c"):
    assert importlib.util.find_spec(mod) is None, f"{mod} is installed"
site = pathlib.Path("/opt/venv/lib/python3.12/site-packages")
bundled = sorted(str(x) for x in site.iterdir() if x.name.startswith("psycopg_binary") or (x.name.endswith(".libs") and "psycopg" in x.name))
assert not bundled, f"bundled psycopg libraries present: {bundled}"
mapped = sorted({l.split()[-1] for l in pathlib.Path("/proc/self/maps").read_text().splitlines() if len(l.split()) >= 6 and l.split()[-1].startswith("/")})
libs = [x for x in mapped if re.search(r"/lib(pq|ssl|crypto)\.so", x)]
print("D4b linkage: psycopg", psycopg.__version__, "impl", pq.__impl__, "libpq", pq.version(), "mapped:", libs)
assert any(re.fullmatch(r"/usr/lib/x86_64-linux-gnu/libpq\.so\.5(\.[0-9]+)*", x) for x in libs), libs
assert any(re.fullmatch(r"/usr/lib/x86_64-linux-gnu/libssl\.so\.3", x) for x in libs), libs
assert any(re.fullmatch(r"/usr/lib/x86_64-linux-gnu/libcrypto\.so\.3", x) for x in libs), libs
assert all(x.startswith("/usr/lib/x86_64-linux-gnu/") for x in libs), libs
PY

# D4b TLS proof (a throwaway stage: never part of any image; the runtime stage below only bind-mounts its result). A
# local PostgreSQL server with a test CA proves, through the same libpq/OpenSSL the runtime uses: sslmode=require (the
# API's documented production minimum) negotiates TLS; verify-full (the reader's mode) accepts the issuing CA and
# refuses another; sslmode=require refuses a server that offers no TLS.
FROM runtime-libs AS d4b-tls-proof
# No default cluster (it would start on 5432); the client libpq5 must stay the runtime's exact package version.
RUN dpkg-query -W -f='${Package} ${Version}\n' libpq5 > /tmp/libpq5-runtime.txt \
    && mkdir -p /etc/postgresql-common && echo 'create_main_cluster = false' > /etc/postgresql-common/createcluster.conf \
    && apt-get update \
    && apt-get install --no-install-recommends -y postgresql \
    && rm -rf /var/lib/apt/lists/* \
    && dpkg-query -W -f='${Package} ${Version}\n' libpq5 | cmp - /tmp/libpq5-runtime.txt \
    && cat /tmp/libpq5-runtime.txt
RUN <<'SH'
set -eu
PGBIN=$(ls -d /usr/lib/postgresql/*/bin | head -n 1)
mkdir -p /proof /tmp/tls && cd /tmp/tls
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=d4b-test-ca" -keyout ca.key -out ca.pem 2>/dev/null
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=d4b-other-ca" -keyout other.key -out other-ca.pem 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" -keyout server.key -out server.csr 2>/dev/null
printf 'subjectAltName=DNS:localhost\n' > san.ext
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 1 -out server.crt -extfile san.ext 2>/dev/null
chown postgres:postgres server.key server.crt && chmod 600 server.key
for c in tls plain; do install -d -o postgres -g postgres /tmp/pg-$c; su postgres -s /bin/sh -c "$PGBIN/initdb -D /tmp/pg-$c/data -A trust -U postgres >/dev/null"; done
printf "ssl = on\nssl_cert_file = '/tmp/tls/server.crt'\nssl_key_file = '/tmp/tls/server.key'\nlisten_addresses = 'localhost'\nport = 55432\nunix_socket_directories = '/tmp/pg-tls'\n" >> /tmp/pg-tls/data/postgresql.conf
printf "hostssl all all 127.0.0.1/32 trust\nhostssl all all ::1/128 trust\nhostnossl all all 0.0.0.0/0 reject\nhostnossl all all ::/0 reject\n" > /tmp/pg-tls/data/pg_hba.conf
printf "ssl = off\nlisten_addresses = 'localhost'\nport = 55433\nunix_socket_directories = '/tmp/pg-plain'\n" >> /tmp/pg-plain/data/postgresql.conf
for c in tls plain; do su postgres -s /bin/sh -c "$PGBIN/pg_ctl -D /tmp/pg-$c/data -l /tmp/pg-$c/log -w start >/dev/null"; done
/opt/venv/bin/python - > /proof/d4b-tls.txt <<'PY'
import pathlib, re
import psycopg
from psycopg import pq
out = [f"psycopg {psycopg.__version__} impl {pq.__impl__} libpq {pq.version()}"]
base = "host=localhost dbname=postgres user=postgres connect_timeout=5"
with psycopg.connect(f"{base} port=55432 sslmode=require") as c:
    pg = c.pgconn
    assert pg.ssl_in_use, "no TLS under sslmode=require"
    ssl, version, cipher, bits = c.execute("select ssl, version, cipher, bits from pg_stat_ssl where pid = pg_backend_pid()").fetchone()
    assert ssl is True, "the server reports the session as not TLS"
    out.append(f"sslmode=require against the TLS server: connected, client ssl_in_use=True, server pg_stat_ssl: ssl={ssl} version={version} cipher={cipher} bits={bits}")
with psycopg.connect(f"{base} port=55432 sslmode=verify-full sslrootcert=/tmp/tls/ca.pem") as c:
    assert c.pgconn.ssl_in_use
    out.append("sslmode=verify-full with the issuing CA: connected")
for label, dsn, reason in (("sslmode=verify-full with a different CA", f"{base} port=55432 sslmode=verify-full sslrootcert=/tmp/tls/other-ca.pem", "certificate verify failed"),
                           ("sslmode=require against a server offering no TLS", f"{base} port=55433 sslmode=require", "server does not support SSL")):
    try:
        psycopg.connect(dsn).close()
    except psycopg.OperationalError as e:
        msg = " ".join(str(e).split())
        assert reason in msg, f"{label}: refused for another reason: {msg}"
        out.append(f"{label}: refused, reason contains {reason!r}: {msg}")
    else:
        raise SystemExit(f"{label}: unexpectedly connected")
mapped = sorted({l.split()[-1] for l in pathlib.Path("/proc/self/maps").read_text().splitlines() if len(l.split()) >= 6 and re.search(r"/lib(pq|ssl|crypto)\.so", l.split()[-1])})
out.append(f"libraries mapped by the client process: {mapped}")
print("\n".join(out))
PY
for c in tls plain; do su postgres -s /bin/sh -c "$PGBIN/pg_ctl -D /tmp/pg-$c/data -w stop >/dev/null"; done
cat /proof/d4b-tls.txt
SH

FROM runtime-libs AS runtime

# Print the D4b TLS proof into the build log (bind mount only: no file of the proof stage enters the image).
RUN --mount=type=bind,from=d4b-tls-proof,source=/proof,target=/d4b-proof cat /d4b-proof/d4b-tls.txt

WORKDIR /app

# Application source, migrations and Alembic config. ``.dockerignore`` excludes
# .env, local SQLite databases, VCS metadata, caches and virtualenvs.
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 alembic ./alembic
COPY --chown=10001:10001 alembic.ini ./alembic.ini
COPY --chown=10001:10001 pyproject.toml ./pyproject.toml

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
