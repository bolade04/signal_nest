#!/usr/bin/env bash
# CI-equivalent local reproduction with a genuinely empty HOME (Gate 4N-I16, Phase X).
#
# WHY THIS FILE EXISTS AS A FILE. Gate 4N-I15 reported "1560 passed, 92 skipped" under an
# empty HOME, and the artifact recording it was a one-line summary. The command itself
# existed nowhere — not in scripts/, not in ci.yml, not in the artifact set — so no reviewer
# could re-run it and no reviewer could check whether HOME had been SET to an empty
# directory or merely UNSET. Those two are not equivalent: with HOME unset, Python's
# Path.home() falls back to the password database and resolves to the developer's real home,
# which is how the Gate 4N-I10 "clean checkout" silently read a developer-local anchor.
#
# This script SETS HOME to a fresh empty directory. It never unsets it.
#
# WHAT THIS IS AND IS NOT. This is a LOCAL REPRODUCTION of the CI environment. It is not a
# GitHub Actions run and must never be recorded as one. The provenance label for its output
# is CI_EQUIVALENT_LOCAL_REPRODUCTION, which is non-certifying by construction.
#
# Usage:
#   scripts/empty_home_ci.sh [pytest args...]
# Exit: the pytest exit status, or non-zero if the isolation preconditions fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SANDBOX_HOME="$(mktemp -d)"
trap 'rm -rf "$SANDBOX_HOME"' EXIT

# The interpreter must be able to import pytest. Resolving this explicitly matters: on some
# hosts the default python3 has no pytest, and a harness that fails for that reason looks
# identical to one that fails for a real reason.
PYBIN=""
for candidate in "${PYTHON:-}" python3 python /opt/miniconda3/bin/python3; do
  [ -n "$candidate" ] || continue
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import pytest" >/dev/null 2>&1; then
    PYBIN="$(command -v "$candidate")"
    break
  fi
done
[ -n "$PYBIN" ] || { echo "no interpreter on PATH can import pytest" >&2; exit 2; }

# PRECONDITION 1 — the sandbox HOME must be empty, and must not be the real one.
if [ "$SANDBOX_HOME" = "${HOME:-}" ]; then
  echo "refusing to run: sandbox HOME equals the real HOME" >&2; exit 2
fi
if [ -e "$SANDBOX_HOME/.signalnest" ]; then
  echo "refusing to run: sandbox HOME already contains .signalnest" >&2; exit 2
fi

# PRECONDITION 2 — prove the real anchor is unreachable from inside the sandbox. This is the
# check that distinguishes "isolated" from "happened not to look".
if env -i PATH="$PATH" HOME="$SANDBOX_HOME" "$PYBIN" - <<'PROBE'
import pathlib, sys
sys.exit(0 if (pathlib.Path.home() / ".signalnest" / "anchor").exists() else 1)
PROBE
then
  echo "refusing to run: the real anchor is still reachable under the sandbox HOME" >&2
  exit 2
fi

echo "empty-HOME CI-equivalent reproduction"
echo "  HOME              : <fresh empty directory>"
echo "  anchor tier       : TIER_1_SYNTHETIC (tracked fixture, non-certifying)"
echo "  candidate manifest: tests/fixtures/candidate-manifest.json"
echo "  AWS credentials   : none supplied; no AWS call is made"
echo

# `env -i` clears the environment entirely, so no AWS_* credential, no developer anchor
# variable, and no inherited SIGNALNEST_* setting can leak in. Everything the run needs is
# passed explicitly below.
set +e
env -i \
  PATH="$PATH" \
  HOME="$SANDBOX_HOME" \
  LANG="${LANG:-C.UTF-8}" \
  SIGNALNEST_ANCHOR_TIER=TIER_1_SYNTHETIC \
  SIGNALNEST_CANDIDATE_MANIFEST="$REPO_ROOT/tests/fixtures/candidate-manifest.json" \
  "$PYBIN" -m pytest tests/ -q "$@"
STATUS=$?
set -e

echo
echo "CI_EQUIVALENT_LOCAL_REPRODUCTION exit=$STATUS"
echo "This was a LOCAL reproduction. GitHub Actions has not run for this candidate."
exit "$STATUS"
