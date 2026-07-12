#!/usr/bin/env bash
#
# Regression test for the CI failure-propagation fix.
#
# Background: the CI workflow logs each quality command with `cmd | tee log`.
# Under GitHub's default `bash -e {0}` shell (no pipefail) the pipeline exit
# status is tee's (always 0), so a failing `npm run lint`/`npm test`/... was
# silently reported as success. The workflow now sets:
#
#     defaults:
#       run:
#         shell: bash --noprofile --norc -euo pipefail {0}
#
# This script proves that, under that exact shell, a failing left-hand command
# piped through tee correctly fails the pipeline while the log is still written.
#
# It changes no repository files and works on macOS (bash 3.2) and Ubuntu.

set -euo pipefail

# The exact shell string configured in .github/workflows/ci.yml `defaults.run.shell`,
# with the trailing `{0}` placeholder replaced by `-c` so we can pass an inline script.
CI_SHELL=(bash --noprofile --norc -euo pipefail -c)

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/signalnest-ci-pipefail.XXXXXX")"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT INT TERM

FAIL_LOG="$WORKDIR/failure.log"
PASS_LOG="$WORKDIR/success.log"
failures=0

echo "== Case 1: failing command piped through tee must propagate as failure =="
set +e
"${CI_SHELL[@]}" 'false 2>&1 | tee "$0"' "$FAIL_LOG"
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  echo "  PASS: pipeline exited nonzero (rc=$rc) — failure is no longer masked"
else
  echo "  FAIL: pipeline exited 0 — failure is still masked by tee"
  failures=$((failures + 1))
fi
if [ -f "$FAIL_LOG" ]; then
  echo "  PASS: log file was still created at \$FAIL_LOG"
else
  echo "  FAIL: log file was not created"
  failures=$((failures + 1))
fi

echo "== Case 2: succeeding command piped through tee must still pass =="
set +e
"${CI_SHELL[@]}" 'printf "ok\n" 2>&1 | tee "$0"' "$PASS_LOG"
rc=$?
set -e
if [ "$rc" -eq 0 ] && [ -f "$PASS_LOG" ] && grep -q "ok" "$PASS_LOG"; then
  echo "  PASS: pipeline exited 0 and log captured output"
else
  echo "  FAIL: expected success with captured log (rc=$rc)"
  failures=$((failures + 1))
fi

echo
if [ "$failures" -eq 0 ]; then
  echo "ALL CHECKS PASSED — tee pipelines propagate failures under the CI shell"
  exit 0
fi
echo "$failures CHECK(S) FAILED"
exit 1
