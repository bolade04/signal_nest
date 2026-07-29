# syntax=docker/dockerfile:1
# Dedicated minimal revision-reader image (Gate 4J).
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
# Shell absence below is DEFENCE IN DEPTH, not the primary control. Do not describe the
# image as safe *because* it is shell-free — with ENTRYPOINT pinned the shell is already
# unreachable, and attributing the control to shell absence would break silently if the
# base image were ever swapped.

# --- builder: resolve and install the reader into an isolated prefix -----------------
# PYTHON MINOR IS LOAD-BEARING. gcr.io/distroless/python3-debian12 ships Python 3.11, so
# this builder MUST be 3.11: psycopg[binary] resolves to a cp311/cp312-specific ABI wheel
# and a minor mismatch produces an image that cannot import its only dependency.
# tests/test_dockerfile.py asserts builder minor == entrypoint minor == PYTHONPATH minor
# so this can never drift silently again.
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src
COPY pyproject.toml ./
COPY revision_reader ./revision_reader

# Install into a relocatable prefix copied forward wholesale. The application package is
# NEVER installed here — that is what keeps alembic (a base dependency of apps/api) out.
RUN python -m pip install --prefix=/install .

# --- final: distroless, no shell, no package manager, no build tooling ---------------
# Debian-based distroless is required: psycopg[binary]'s manylinux wheel bundles libpq
# and is glibc-linked, so an Alpine/musl base would not work.
FROM gcr.io/distroless/python3-debian12 AS reader

# Provenance, stamped by the publication workflow from the trusted checkout.
ARG GIT_REVISION=unknown
ARG IMAGE_CREATED=unknown
LABEL org.opencontainers.image.title="signalnest-revision-reader" \
      org.opencontainers.image.description="Reads the live Alembic revision. No migration capability." \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      org.opencontainers.image.created="${IMAGE_CREATED}"

COPY --from=builder /install /usr/local

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
