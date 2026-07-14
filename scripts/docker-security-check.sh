#!/usr/bin/env bash
# Prove a built SignalNest image is production-safe (Phase 3A.4b Batch 4):
#   1. it runs as the dedicated unprivileged user (UID 10001), never root, and
#   2. no secret or local database was baked into the build context.
#
# Usage: scripts/docker-security-check.sh <image-tag>
set -euo pipefail

IMAGE="${1:?usage: docker-security-check.sh <image-tag>}"

echo "== [$IMAGE] assert non-root runtime user =="
uid="$(docker run --rm --entrypoint sh "$IMAGE" -c 'id -u')"
if [ "$uid" != "10001" ]; then
  echo "FAIL: image runs as uid '$uid' (expected 10001, non-root)" >&2
  exit 1
fi
echo "ok: runs as uid $uid (non-root)"

echo "== [$IMAGE] assert no secrets or local databases in the image =="
# Scan the application tree (/app) — the build context we control — for env
# files, private keys and SQLite databases that must never ship. Scoping to
# /app (not the whole filesystem) avoids false positives on the base image's
# and certifi's PUBLIC CA trust bundles (/etc/ssl/certs/*.pem, cacert.pem),
# which are trust anchors, not secrets.
found="$(docker run --rm --entrypoint sh "$IMAGE" -c '
  find /app \( -name ".env" -o -name "*.env" -o -name "*.sqlite" -o -name "*.db" \
       -o -name "*.pem" -o -name "*.key" -o -name "signalnest.db" \) -print 2>/dev/null
' || true)"
if [ -n "$found" ]; then
  echo "FAIL: forbidden file(s) baked into the image:" >&2
  echo "$found" >&2
  exit 1
fi
echo "ok: no .env / private key / *.db / *.sqlite under /app in the image"

echo "== [$IMAGE] assert no VCS metadata in the image =="
if docker run --rm --entrypoint sh "$IMAGE" -c 'test -e /app/.git'; then
  echo "FAIL: /app/.git is present in the image" >&2
  exit 1
fi
echo "ok: no .git directory in the image"

echo "PASS: $IMAGE is non-root and free of secrets/local databases"
