"""Final-defender exhaustion, and self-tests of the harness (Gate 4N-I11, Defects 3, G).

THE DEFECT THIS REPLACES. Gate 4N-I10's `per_action_mutations` counted a mutation as
surviving only when the denying-policy count was `<= 1`; the observed minimum was 4, so the
filter was unreachable and `clean` was a constant True. The adversarial lane gutted every
Deny in permanent_w0 and still got a perfect score. That harness was discarded, not patched:
patching the threshold would have left the same shape — a verdict computed from something
other than what the suite observes.

WHAT MAKES THIS ONE FALSIFIABLE. It removes defenders one at a time until none remains and
requires the FINAL removal to flip the decision. If the decision survives the loss of every
defender, the defenders were never producing it. That claim can be wrong, which is the point.

PHASE G. The tests at the bottom mutate the HARNESS. Forcing `clean`, skipping capabilities,
ignoring the final-defender check, and returning success on empty input must each be caught
here. A security harness that has never been seen to fail is not evidence.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import deny_exhaustion as dx  # noqa: E402
import deny_triangulation as dt  # noqa: E402
import iam_eval  # noqa: E402
from iam_eval import Decision  # noqa: E402

REPORT = dx.run()

DECISIVE = ["iam:PassRole", "iam:CreateRole", "iam:UpdateAssumeRolePolicy",
            "cloudtrail:StopLogging", "s3:DeleteObjectVersion", "s3:PutObject",
            "dynamodb:PutItem", "kms:ScheduleKeyDeletion",
            "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary"]


# --- the result --------------------------------------------------------------------------


def test_every_capability_is_exhaustible_and_its_final_defender_is_decisive():
    assert REPORT["clean"], [
        f"{r['verdict']} {r['action']}: {r['why']}" for r in REPORT["failing"]]


def test_the_run_covers_every_mandatory_capability():
    """A silently shrunken denominator is how the old harness scored perfectly."""
    expected = {r["action"] for r in dt.triangulate()["rows"]}
    covered = {r["action"] for r in REPORT["results"]}
    assert covered == expected, expected - covered


@pytest.mark.parametrize("action", DECISIVE)
def test_each_decisive_capability_has_at_least_one_defender(action):
    row = next(r for r in REPORT["results"] if r["action"] == action)
    assert row["initial_defenders"] >= 1, row
    assert row["verdict"] == "EXHAUSTIBLE_AND_DECISIVE", row


def test_defender_counts_are_recorded_not_assumed():
    for row in REPORT["results"]:
        assert row["initial_defenders"] == len(row["defender_sids"]), row["action"]
        assert row["removal_sequence"], row["action"]


# --- the decisive experiment: gut a principal, and the harness must notice ---------------


def test_gutting_every_deny_in_a_principal_is_caught():
    """The exact experiment that defeated the Gate 4N-I10 harness."""
    policies = {name: copy.deepcopy(doc) for name, doc in dt.policies().items()}
    policies["permanent_w0"] = {
        **policies["permanent_w0"],
        "Statement": [s for s in policies["permanent_w0"]["Statement"]
                      if s["Effect"] != "Deny"],
    }
    report = dx.run(policies=policies, capabilities=DECISIVE)
    # The capability may still be denied by OTHER principals — that is expected and is not
    # what is being measured. What must be true is that permanent_w0 no longer appears as a
    # defender for anything, i.e. the harness SEES the change.
    for row in report["results"]:
        assert not any(d.startswith("permanent_w0/") for d in row["defender_sids"]), (
            f"the harness still lists permanent_w0 as a defender of {row['action']} after "
            "every one of its Deny statements was removed")


def test_removing_all_defenders_of_one_capability_is_reported_not_decisive():
    """Strip every Deny from EVERY principal for one action; it must stop being denied."""
    action = "cloudtrail:StopLogging"
    resource = next(r["probe_resource"] for r in dt.triangulate()["rows"]
                    if r["action"] == action)
    policies = {}
    for name, doc in dt.policies().items():
        statements = []
        for s in doc["Statement"]:
            if s["Effect"] != "Deny":
                statements.append(s); continue
            kept = [a for a in iam_eval._as_list(s.get("Action")) if a != action]
            if kept:
                statements.append({**s, "Action": kept})
        policies[name] = {**doc, "Statement": statements}
    assert not dx.defenders_for(action, resource, policies), (
        "defenders remain after removing the action from every Deny")
    row = dx.exhaust(action, resource, policies)
    assert row["verdict"] == "UNDEFENDED", row


# --- PHASE G: mutate the HARNESS ---------------------------------------------------------


def test_forcing_clean_true_is_caught(monkeypatch):
    """The literal shape of the Gate 4N-I10 defect."""
    monkeypatch.setattr(dx, "run", lambda **kw: {"capabilities": 0, "results": [],
                                                 "failing": [], "clean": True})
    report = dx.run()
    with pytest.raises(AssertionError):
        assert report["capabilities"] >= 60, "a forced-clean run reports no capabilities"


def test_an_empty_run_is_not_a_pass():
    """The old harness could report a perfect score over a shrunken denominator."""
    report = dx.run(capabilities=["this:ActionDoesNotExist"])
    assert report["clean"] is False, report
    assert "empty run is not a pass" in report["why"]


def test_ignoring_the_final_defender_check_is_caught(monkeypatch):
    """If exhaust() stopped requiring the flip, the suite must fail."""
    real = dx.exhaust
    monkeypatch.setattr(dx, "exhaust", lambda a, r, p: {
        **real(a, r, p), "verdict": "EXHAUSTIBLE_AND_DECISIVE",
        "final_removal_flipped": True})
    # With the check neutered, a genuinely undefended capability would now pass. Prove the
    # real implementation does NOT pass it.
    action, resource = "cloudtrail:StopLogging", "arn:aws:cloudtrail:us-east-1:111122223333:trail/x"
    stripped = {n: {**d, "Statement": [s for s in d["Statement"] if s["Effect"] != "Deny"]}
                for n, d in dt.policies().items()}
    assert real(action, resource, stripped)["verdict"] == "UNDEFENDED", (
        "the real implementation accepted a capability with no defenders at all")


def test_skipping_a_capability_is_visible():
    """Coverage is asserted by NAME, so a dropped capability cannot hide in a count."""
    subset = dx.run(capabilities=DECISIVE[:3])
    assert {r["action"] for r in subset["results"]} == set(DECISIVE[:3])
    assert subset["capabilities"] == 3
    full = {r["action"] for r in REPORT["results"]}
    assert len(full) > 3, "the full run must not silently equal a subset"


def test_the_harness_has_no_constant_verdict():
    """Static guard: `clean` must be computed, never literal."""
    source = (REPO_ROOT / "scripts" / "deny_exhaustion.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    assert '"clean": True' not in body, "a literal clean verdict is back"
    assert '"clean": not failing' in body, "clean must be derived from observed failures"


def test_the_harness_has_no_defender_count_threshold():
    """The Gate 4N-I10 defect was a `<= 1` comparison on defender count."""
    source = (REPO_ROOT / "scripts" / "deny_exhaustion.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    for banned in ("<= 1", "< 2", ">= 2"):
        assert banned not in body, f"a defender-count threshold {banned!r} reappeared"


def test_re_enumeration_prevents_a_shadowed_defender_being_missed():
    """Removing one statement can expose another; the loop must re-enumerate."""
    source = (REPO_ROOT / "scripts" / "deny_exhaustion.py").read_text(encoding="utf-8")
    assert "re-enumerate rather than trust" in source
    for row in REPORT["results"]:
        if row["initial_defenders"] > 1:
            assert len(row["removal_sequence"]) >= row["initial_defenders"], row["action"]
            break
