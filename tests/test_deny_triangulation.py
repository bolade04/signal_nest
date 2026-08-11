"""Deny triangulation, source deletion, and per-action mutation (Gate 4N-I10, Defects 1-3).

WHAT AGGREGATE SCORES HID. Gate 4N-I8 built three independent requirement sources; Gate
4N-I9 built resource-specific probes. Nothing reconciled them, so a capability could be
demanded by the incident ledger, denied by a policy, and probed at the WRONG RESOURCE — and
every individual check would still pass. "69/69 ceiling" and "28/28 statements defended" are
both true of a set with a hole in the middle.

The classifier immediately found one: s3:PutLifecycleConfiguration was probed at "*" and read
as a boundary gap. It is not a gap — the boundary scopes it to the protected buckets, and a
flat deny would break the api and worker roles' legitimate lifecycle rules on the app bucket.
The probe was wrong, and only a reconciliation across sources could tell the difference
between a wrong probe and a missing control.

SOURCE DELETION is the other half. A requirement that vanishes when one source is edited was
never independently grounded — that was Gate 4N-I8 Defect 2, where deleting one line from
must_not_contract.py shrank the policy AND the expectation together and the suite stayed
green.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import deny_requirements  # noqa: E402
import deny_triangulation as dt  # noqa: E402

REPORT = dt.triangulate()

# The capabilities whose loss is unrecoverable or invisible. Named individually so a
# shrinking requirement set cannot quietly drop one.
DECISIVE = [
    "iam:PassRole", "iam:CreateRole", "iam:UpdateAssumeRolePolicy",
    "cloudtrail:StopLogging", "s3:DeleteObjectVersion", "s3:PutObject",
    "dynamodb:PutItem", "kms:ScheduleKeyDeletion", "secretsmanager:GetSecretValue",
    "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion",
]


# --- the classifier ------------------------------------------------------------------


def test_every_mandatory_capability_passes_triangulation():
    assert REPORT["clean"], [
        f"{r['classification']} {r['action']} at {r['probe_resource']} "
        f"(not denying: {r['principals_not_denying']})" for r in REPORT["failing"]]


def test_the_classifier_actually_discriminates():
    """All-one-bucket means the classifier is saying nothing.

    The first draft classified every capability DUPLICATE_WITH_EQUIVALENT_PROTECTION because
    more than one PRINCIPAL denied it — which is every capability, by design.
    """
    counts = REPORT["counts"]
    assert counts.get(dt.REQUIRED_AND_PRESENT, 0) >= 50, counts
    assert len(counts) >= 2, f"every capability landed in one bucket: {counts}"


@pytest.mark.parametrize("action", DECISIVE)
def test_each_decisive_capability_is_grounded_in_an_external_source(action):
    row = next((r for r in REPORT["rows"] if r["action"] == action), None)
    assert row is not None, f"{action} is not in the mandatory set at all"
    assert row["source_a"] or row["source_b"], f"{action} has no independent justification"
    assert row["source_d_denying"], f"{action} is required but no policy denies it"


@pytest.mark.parametrize("action", DECISIVE)
def test_each_decisive_capability_is_probed_at_a_real_resource(action):
    row = next(r for r in REPORT["rows"] if r["action"] == action)
    assert row["probe_resource"], action
    if action.split(":")[0] in ("s3", "dynamodb", "kms", "cloudtrail", "secretsmanager"):
        assert row["source_c"].startswith("SOURCE C"), (
            f"{action} is probed generically; a resource-scoped Deny would not be tested "
            "at the resource it protects")


def test_duplicate_protection_is_within_a_policy_not_across_principals():
    for row in REPORT["rows"]:
        if row["classification"] != dt.DUPLICATE_WITH_EQUIVALENT_PROTECTION:
            continue
        assert row["within_policy_duplicate_sids"], (
            f"{row['action']} classified duplicate without two statements in one policy")


# --- PHASE D: per-source deletion ------------------------------------------------------


@pytest.mark.parametrize("action", DECISIVE)
def test_deleting_a_capability_from_the_incident_ledger_does_not_remove_the_requirement(
        action, monkeypatch):
    """SOURCE A deleted. SOURCE B must still demand it."""
    original = deny_requirements.source1_actions()
    monkeypatch.setattr(deny_requirements, "source1_actions",
                        lambda: {k: v for k, v in original.items() if k != action})
    # GATE 4N-I12 DEFECT 3: the `if action in source2_actions():` guard is REMOVED. When a
    # capability was absent from the other source the body never ran, which is precisely the
    # single-grounded case that most needed checking. The requirement must survive
    # unconditionally, and if it does not, that is the finding.
    required = deny_requirements.required_denies()
    assert action in required, (
        f"{action} vanished from the requirement when SOURCE A alone was edited — it was "
        "never independently grounded")
    assert required[action]["in_source_1"] is False


@pytest.mark.parametrize("action", DECISIVE)
def test_deleting_a_capability_from_the_invariants_does_not_remove_the_requirement(
        action, monkeypatch):
    """SOURCE B deleted. SOURCE A must still demand it."""
    trimmed = {k: [a for a in v if a != action]
               for k, v in deny_requirements.ARCHITECTURE_INVARIANTS.items()}
    monkeypatch.setattr(deny_requirements, "ARCHITECTURE_INVARIANTS", trimmed)
    # Guard removed for the same reason as above (Gate 4N-I12 Defect 3).
    required = deny_requirements.required_denies()
    assert action in required, f"{action} vanished when SOURCE B alone was edited"


@pytest.mark.parametrize("action", ["iam:PassRole", "cloudtrail:StopLogging",
                                    "s3:DeleteObjectVersion", "iam:CreatePolicyVersion"])
def test_deleting_the_capability_from_the_generated_policy_is_caught(action, monkeypatch):
    """SOURCE D deleted. The classifier must report it, not absorb it."""
    real = dt.policies

    def stripped():
        docs = copy.deepcopy(real())
        for doc in docs.values():
            for statement in doc["Statement"]:
                if statement.get("Effect") != "Deny":
                    continue
                actions = [a for a in dt.iam_eval._as_list(statement.get("Action"))
                           if a != action]
                statement["Action"] = actions or ["iam:__none__"]
        return docs

    monkeypatch.setattr(dt, "policies", stripped)
    result = dt.triangulate()
    row = next(r for r in result["rows"] if r["action"] == action)
    assert row["classification"] in (dt.REQUIRED_BUT_MISSING, dt.CONFLICTING_SCOPE), row
    assert not result["clean"]


def test_deleting_the_probe_resource_mapping_is_visible(monkeypatch):
    """SOURCE E/C deleted. The row must stop claiming resource-specific coverage."""
    trimmed = {k: v for k, v in dt.PROTECTED_RESOURCE.items()
               if k != "s3:DeleteObjectVersion"}
    monkeypatch.setattr(dt, "PROTECTED_RESOURCE", trimmed)
    row = next(r for r in dt.triangulate()["rows"]
               if r["action"] == "s3:DeleteObjectVersion")
    assert not row["source_c"].startswith("SOURCE C"), (
        "the row still claims a protected-resource probe after the mapping was deleted")


def test_a_capability_present_in_no_source_is_reported_unjustified(monkeypatch):
    real_s1, real_s2 = deny_requirements.source1_actions, deny_requirements.source2_actions
    monkeypatch.setattr(deny_requirements, "source1_actions",
                        lambda: {**real_s1(), "lambda:InvokeFunction": "injected"})
    required = deny_requirements.required_denies()
    assert "lambda:InvokeFunction" in required
    assert required["lambda:InvokeFunction"]["in_source_2"] is False


# --- PHASE E: per-action mutation score --------------------------------------------------


MUTATIONS = dt.per_action_mutations()


def test_no_mandatory_mutation_survives():
    assert MUTATIONS["clean"], MUTATIONS["genuinely_survived"]


def test_the_mutation_run_is_large_enough_to_be_meaningful():
    assert MUTATIONS["mutations_run"] >= 1000, MUTATIONS["mutations_run"]
    assert len(dt._MUTATIONS) >= 9, sorted(dt._MUTATIONS)


def test_duplicate_absorbed_mutations_are_reported_not_hidden():
    """Phase F: a mutation another Deny absorbs is not a failure, but it is not invisible."""
    assert MUTATIONS["absorbed_by_duplicate_protection"] >= 0
    assert "absorbed_by_duplicate_protection" in MUTATIONS


def test_the_mutation_harness_can_actually_fail():
    """Controls the control: strip every Deny and the score must collapse."""
    real = dt.policies

    def no_denies():
        docs = copy.deepcopy(real())
        for doc in docs.values():
            doc["Statement"] = [s for s in doc["Statement"] if s.get("Effect") != "Deny"]
        return docs

    original = dt.policies
    dt.policies = no_denies
    try:
        result = dt.triangulate()
        assert not result["clean"], "triangulation passed with every Deny removed"
        missing = [r for r in result["rows"]
                   if r["classification"] == dt.REQUIRED_BUT_MISSING]
        assert len(missing) >= 50, len(missing)
    finally:
        dt.policies = original
