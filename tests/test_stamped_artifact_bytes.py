"""Current-candidate byte assertions, fail-closed (Gate 4N-I17, Defect 2, Phases J/K).

THE DEFECT. Gate 4N-I16 re-keyed this file from a hard-coded `4n-i10` directory to an explicit
candidate manifest — and then looked inside that candidate for a sidecar named
`stamped-policy-hashes.json` which the candidate did not have. A module-level `skipif` fired and
all ten tests SKIPPED. The skip reason claimed no candidate was declared, which was false: one was
declared, only the filename was wrong. Running the whole suite with the real candidate exported
and with it unset produced identical results, so declaring a candidate changed nothing anywhere.

The defect moved from "checked the wrong object" to "checked NOTHING", which is strictly harder to
notice: a stale target at least fails when the stale thing changes, whereas zero assertions never
fail at all.

THE RULE NOW. Targets come from the manifest's OWN declared artifact list — the thing every valid
candidate necessarily has — never from a filename this module guesses. And there is no skip path:
  * a missing manifest is a FAILURE, not a skip;
  * a manifest declaring zero policy artifacts is a FAILURE;
  * a declared artifact absent from disk is a FAILURE;
  * the suite asserts its own assertion COUNT, so "zero assertions executed" cannot read as green.

That last one is what would have caught Gate 4N-I16. A suite reporting success without having
asserted anything is indistinguishable from one that verified everything.

INHERITED PURPOSE, PRESERVED. This file began as the Gate 4N-I10 defence against a literal
`<EXPIRY-ISO8601>` surviving into shipped bytes, because every earlier layer built policies in
memory and never touched the bytes on disk. That defence is retained below and now runs against
whichever candidate is actually declared.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import candidate_manifest  # noqa: E402

# --- explicit resolution: no fallback, no guessing, no skip ---------------------------------
#
# load() raises CandidateError when SIGNALNEST_CANDIDATE_MANIFEST is unset or unusable, and that
# exception is allowed to propagate at collection time ON PURPOSE. An undeclared candidate must
# break the run rather than quietly removing its assertions from it.

try:
    CANDIDATE = candidate_manifest.load()
    RESOLUTION_ERROR = None
except candidate_manifest.CandidateError as exc:
    # Captured rather than raised at import time so a missing candidate FAILS THIS SUITE instead
    # of aborting collection for the whole repository. The distinction matters: an undeclared
    # candidate is this module's problem to report, not a reason to stop every other test from
    # running. It is still a FAILURE — never a skip.
    CANDIDATE, RESOLUTION_ERROR = None, str(exc)

ROOT = CANDIDATE.artifact_root if CANDIDATE else None

POLICY_ARTIFACTS = sorted(n for n, spec in CANDIDATE.artifacts.items()
                          if spec.get("role") == "policy") if CANDIDATE else []


def _require_candidate():
    if CANDIDATE is None:
        pytest.fail(
            "no candidate is declared, so this suite asserted NOTHING about candidate bytes. "
            "This is a FAILURE and never a skip — Gate 4N-I16 shipped a suite that skipped 10/10 "
            f"against the real candidate and read as green. Resolution error: {RESOLUTION_ERROR}")

# Every executed byte assertion registers here; the accounting test reconciles it.
EXECUTED: list[str] = []

PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_-]*>|\$\{|PLACEHOLDER|TODO|FIXME|TBD")
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def load_bytes(name: str) -> bytes:
    """The EXACT bytes on disk. No generator call, by design — calling a generator would
    reproduce the very defect this file exists to detect."""
    target = ROOT / name
    assert target.exists(), (
        f"{name} is declared by candidate {CANDIDATE.candidate_id} but is MISSING from {ROOT}. "
        "A declared-but-absent artifact is a failure, never a skip.")
    return target.read_bytes()


# =====================================================================================
# Contract
# =====================================================================================


def test_a_candidate_is_declared_and_resolves():
    _require_candidate()
    assert CANDIDATE.candidate_id
    assert ROOT.is_dir()
    EXECUTED.append("candidate_resolves")


def test_the_candidate_declares_at_least_one_policy_artifact():
    """expected > 0. A candidate cannot pass by having nothing to check."""
    _require_candidate()
    assert POLICY_ARTIFACTS, (
        f"candidate {CANDIDATE.candidate_id} declares no artifact with role 'policy'; there is "
        "nothing for the byte-level suite to assert against, which is itself the defect")
    EXECUTED.append("policy_artifacts_declared")


@pytest.mark.parametrize("name", POLICY_ARTIFACTS)
def test_declared_artifact_bytes_match_the_manifest_hash(name):
    assert hashlib.sha256(load_bytes(name)).hexdigest() == CANDIDATE.artifacts[name]["sha256"], (
        f"{name}: byte hash differs from the manifest")
    EXECUTED.append(f"hash:{name}")


@pytest.mark.parametrize("name", POLICY_ARTIFACTS)
def test_declared_policy_artifact_parses_as_json(name):
    doc = json.loads(load_bytes(name).decode("utf-8"))
    assert isinstance(doc, dict), f"{name} is not a JSON object"
    EXECUTED.append(f"parse:{name}")


@pytest.mark.parametrize("name", POLICY_ARTIFACTS)
def test_declared_policy_artifact_contains_no_placeholder(name):
    """The Gate 4N-I7/I8/I10 defect: a placeholder surviving into shipped bytes."""
    hits = sorted(set(PLACEHOLDER.findall(load_bytes(name).decode("utf-8"))))
    assert not hits, f"{name} contains placeholder tokens {hits}"
    EXECUTED.append(f"placeholder:{name}")


@pytest.mark.parametrize("name", POLICY_ARTIFACTS)
def test_date_conditions_are_real_utc_instants_and_no_deny_expires(name):
    doc = json.loads(load_bytes(name).decode("utf-8"))
    for statement in doc.get("Statement", []):
        for operator, pairs in (statement.get("Condition") or {}).items():
            if not operator.startswith("Date"):
                continue
            for value in pairs.values():
                assert ISO_UTC.match(str(value)), f"{name}: {value!r} is not ISO-8601 UTC"
            assert statement.get("Effect") != "Deny", (
                f"{name}: a Deny carries a date condition — an expiring Deny is not a Deny")
    EXECUTED.append(f"dates:{name}")


# =====================================================================================
# PHASE I — assertion accounting. The test that would have caught Gate 4N-I16.
# =====================================================================================


def test_zz_the_suite_actually_executed_its_assertions():
    """expected > 0, executed == expected, skipped == 0.

    Named `zz` so pytest's file-order execution runs it after the parametrised bodies. If a
    filename, manifest, directory or environment input is wrong, those bodies contribute nothing
    and this reconciliation fails — instead of the suite reporting success having asserted
    nothing at all.
    """
    _require_candidate()
    expected = 2 + len(POLICY_ARTIFACTS) * 4
    assert expected > 0, "no assertions were even planned"
    assert len(EXECUTED) == expected, (
        f"expected {expected} byte assertions for candidate {CANDIDATE.candidate_id}, executed "
        f"{len(EXECUTED)}. Executed: {sorted(EXECUTED)}")
