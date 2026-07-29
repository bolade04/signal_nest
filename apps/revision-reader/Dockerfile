# syntax=docker/dockerfile:1
# Dedicated minimal revision-reader image (Gate 4J / 4J.1).
#
# THE CONTROL THIS IMAGE EXISTS TO PROVIDE, stated once and precisely:
# ECS RunTask `ContainerOverride` exposes {name, command, environment, environmentFiles,
# cpu, memory, memoryReservation, resourceRequirements}. It has NO `entryPoint` member.
# So a fixed exec-form ENTRYPOINT CANNOT be replaced at RunTask time; an override
# `command` becomes argv appended to it, and this program rejects all argv as its first
# action (exit 50). That converts command override from DETECTED (Gate 4I) to PREVENTED.
#
# The task definition must therefore NOT set `entryPoint`, or it would shadow this one.
#
# DESTINATION AUTHENTICITY (Gate 4J.1): the host, database, and role are BAKED here from
# build args into `revision_reader/_pinned.py` (a SOURCE CONSTANT, never an ENV — an image
# ENV value is caller-overridable via ContainerOverride.environment, a source constant is
# not), and the committed AWS RDS CA bundle is baked at a fixed path for `sslmode=verify-full`.
# The reader takes only the password from the injected DSN and connects to the baked host.
#
# Shell absence below is DEFENCE IN DEPTH, not the primary control. Do not describe the
# image as safe *because* it is shell-free — with ENTRYPOINT pinned the shell is already
# unreachable, and attributing the control to shell absence would break silently if the
# base image were ever swapped.

# --- builder: resolve and install the reader into an isolated prefix -----------------
# PYTHON MINOR IS LOAD-BEARING. gcr.io/distroless/python3-debian12 ships Python 3.11, so
# this builder MUST be 3.11: psycopg[binary] resolves to a cp311/cp312-specific ABI wheel
# and a minor mismatch produces an image that cannot import its only dependency.
# Both bases are DIGEST-PINNED (not just tag-pinned) so a registry retag cannot change the
# bytes; tests/test_dockerfile.py asserts the digest pin and the minor agreement.
FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src
COPY pyproject.toml ./
COPY revision_reader ./revision_reader
COPY assets ./assets

# Verify the committed CA bundle is byte-identical to the reviewed, checksum-pinned asset
# BEFORE it can flow into the image. A swapped or truncated bundle fails the build here.
# The full-file SHA-256 supersedes per-cert fingerprint pinning: any substitution — same
# length or not — changes this digest, and a legitimate bundle refresh is meant to change
# it (and force PR review) anyway.
RUN printf '%s  assets/rds-global-bundle.pem\n' \
      'e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3' | sha256sum -c -

# Bake the destination pins from build args into a generated source constant. EMPTY args
# FAIL the build, so a real image can never ship the committed sentinels; values are
# embedded via json.dumps so no arg content can break out of the string literal.
ARG EXPECTED_DB_HOST=""
ARG EXPECTED_DB_NAME=""
ARG EXPECTED_DB_USER=""
RUN EXPECTED_DB_HOST="$EXPECTED_DB_HOST" \
    EXPECTED_DB_NAME="$EXPECTED_DB_NAME" \
    EXPECTED_DB_USER="$EXPECTED_DB_USER" \
    python - <<'PY'
import json, os, sys
h = os.environ["EXPECTED_DB_HOST"]
n = os.environ["EXPECTED_DB_NAME"]
u = os.environ["EXPECTED_DB_USER"]
if not (h and n and u):
    sys.exit("EXPECTED_DB_HOST / EXPECTED_DB_NAME / EXPECTED_DB_USER build args are "
             "required and must be non-empty (refusing to ship placeholder pins)")
with open("revision_reader/_pinned.py", "w") as f:
    f.write(
        '"""Build-baked deployment pins (GENERATED at image build — DO NOT EDIT)."""\n'
        "from __future__ import annotations\n\n"
        f"EXPECTED_DB_HOST = {json.dumps(h)}\n"
        f"EXPECTED_DB_NAME = {json.dumps(n)}\n"
        f"EXPECTED_DB_USER = {json.dumps(u)}\n"
        'CA_BUNDLE_PATH = "/etc/ssl/rds/rds-global-bundle.pem"\n'
    )
PY

# Install into a relocatable prefix copied forward wholesale. The application package is
# NEVER installed here — that is what keeps alembic (a base dependency of apps/api) out.
RUN python -m pip install --prefix=/install .

# --- final: distroless, no shell, no package manager, no build tooling ---------------
# Debian-based distroless is required: psycopg[binary]'s manylinux wheel bundles libpq
# and is glibc-linked, so an Alpine/musl base would not work.
FROM gcr.io/distroless/python3-debian12@sha256:2fdb05402a2cf21cf78fdb3ba4c5db167241e9e498140f5bf689d7efb773731f AS reader

# Provenance, stamped by the publication workflow from the trusted checkout.
ARG GIT_REVISION=unknown
ARG IMAGE_CREATED=unknown
LABEL org.opencontainers.image.title="signalnest-revision-reader" \
      org.opencontainers.image.description="Reads the live Alembic revision. No migration capability." \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      org.opencontainers.image.created="${IMAGE_CREATED}"

COPY --from=builder /install /usr/local
# The checksum-verified CA bundle, carried forward FROM THE BUILDER so the bytes in the
# final image are exactly the bytes the build verified (not a second COPY from context).
COPY --from=builder /src/assets/rds-global-bundle.pem /etc/ssl/rds/rds-global-bundle.pem

ENV PYTHONPATH=/usr/local/lib/python3.11/site-packages \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Numeric, because distroless carries no matching /etc/passwd entry. The kernel and ECS
# only need the id. Matches the fleet convention (10001:10001).
USER 10001:10001

# Fixed, exec form, absolute interpreter path, module form. NOT `sh -c`, NOT a bare
# `python` accepting arbitrary module arguments, NOT an overridable dispatcher.
#
# `-m` is deliberate and is STRONGER than the installed console script here, for two
# independent reasons:
#   1. The console script's pip-generated shebang points at the BUILDER's interpreter
#      (/usr/local/bin/python), which does not exist in this base — it would not exec.
#   2. CPython stops parsing interpreter options at `-m <module>`: everything after it is
#      passed to the module as sys.argv. An override command of `["-c", "..."]` therefore
#      arrives as argv to be rejected, NOT as an interpreter flag that would run code.
ENTRYPOINT ["/usr/bin/python3.11", "-m", "revision_reader.reader"]
# Empty: any RunTask `command` override therefore lands in argv and is rejected.
CMD []
