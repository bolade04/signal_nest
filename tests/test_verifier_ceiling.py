"""The ReadOnlyVerifier ceiling and fail-closed action classification (Gate 4N-I19, AWS-1).

WHAT WENT WRONG. Gate 4N-I17's AWS-permissions lane executed an escalation against the real
generator. Two defects composed:

  1. `action_classifier` fell through to a verb-prefix rule. 157 of 235 in-use actions were
     authorised by spelling, 79 of them as reads — and `sso:GetRoleCredentials`, which returns
     live temporary AWS credentials for a permission set, classifies as a "read" under any rule
     that trusts `Get`.
  2. The verifier's only Deny was `NotAction: ALL_ACTIONS`, computed from the union of its own
     Allow sets. A Deny whose exemption list is derived from the Allow list cannot constrain it:
     widening the Allow silently widens the exemption too.

Adding `sso:GetRoleCredentials` was accepted, placed on `Resource "*"`, and evaluated
EXPLICIT_ALLOW.

THE LOAD-BEARING TEST IN THIS FILE is `test_the_i17_escalation_fails_even_when_allow_and_deny_
are_widened_together`. It reproduces the attack exactly — including widening the generated Deny
so the policy stays internally consistent — and asserts the independent ceiling still refuses.
It also asserts that the SELF-DERIVED Deny is fooled, because a test that did not show the old
control failing would not demonstrate why the new one is needed.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import action_classifier as ac  # noqa: E402
import expiry_authorization as ea  # noqa: E402
import gen_readonly_verifier_policy as rv  # noqa: E402
import iam_eval  # noqa: E402
import verifier_ceiling as vc  # noqa: E402

CEILING_PATH = REPO_ROOT / "tests" / "fixtures" / "readonly-verifier-ceiling.json"
CTX = {"aws:CurrentTime": "2026-08-01T18:00:00Z", "aws:RequestedRegion": "us-east-1"}


def base_policy() -> dict:
    return rv.readonly_verifier_policy(ea.ACTIVE_EXPIRY_UTC)


def _widen(policy: dict, action: str, *, also_widen_deny: bool, star: bool = False) -> dict:
    """Add `action` to an Allow, optionally keeping the generated Deny internally consistent."""
    mutated = copy.deepcopy(policy)
    for statement in mutated["Statement"]:
        if statement.get("Effect") == "Allow":
            statement["Action"] = sorted(set(statement["Action"]) | {action})
            if star:
                statement["Resource"] = "*"
            break
    if also_widen_deny:
        for statement in mutated["Statement"]:
            if statement.get("Effect") == "Deny" and "NotAction" in statement:
                statement["NotAction"] = sorted(set(statement["NotAction"]) | {action})
    return mutated


# =====================================================================================
# The ceiling is independent of the thing it measures
# =====================================================================================


def test_the_ceiling_is_tracked():
    import subprocess

    # GATE 4N-I20, ARCH-H3/AWS-3. `git ls-files` reports the INDEX, not history. These fixtures are STAGED ADDITIONS on a branch that is zero commits ahead, so the old assertion passed while `git ls-tree HEAD` returned nothing — and a staged anchor has exactly the 'no history, no review trail' weakness the check was written to exclude. The state is now named exactly, and the property that actually matters — the file reaches the commit that will be made — is asserted against the PREDICTED COMMIT TREE.
    import tracked_state

    rel = str(CEILING_PATH.relative_to(REPO_ROOT))
    state = tracked_state.state_of(rel)
    assert state in (tracked_state.STAGED_ADDITION, tracked_state.TRACKED_IN_HEAD), (
        f"the authored verifier ceiling is {state}; it must be at least staged for addition")
    assert rel in tracked_state.predicted_commit_tree()["entries"], (
        f"the authored verifier ceiling would not be part of the commit this branch would produce")


def test_the_ceiling_is_not_derived_from_the_policy_it_measures():
    """The two sides must share no producer. Equality here would be the I16 defect class."""
    ceiling = json.loads(CEILING_PATH.read_text(encoding="utf-8"))
    permitted = set(ceiling["permitted_actions"])
    generated = set(rv.ALL_ACTIONS)
    assert permitted != generated, (
        "the authored ceiling is set-identical to the generator's own action list. Even if that "
        "is momentarily true by coincidence, authoring it that way would make the comparison "
        "self-referential.")
    assert permitted >= generated, (
        "the generated policy grants actions the authored ceiling does not permit")


def test_the_ceiling_names_no_action_by_prefix():
    """Every entry is an exact action. A wildcard here would re-admit open-ended grants."""
    ceiling = json.loads(CEILING_PATH.read_text(encoding="utf-8"))
    for action in ceiling["permitted_actions"]:
        assert "*" not in action, f"{action}: the ceiling must name exact actions"
        assert ":" in action, f"{action}: not a service-qualified action"


# =====================================================================================
# THE DECISIVE TEST — the Gate 4N-I17 escalation
# =====================================================================================


def test_the_i17_escalation_fails_even_when_allow_and_deny_are_widened_together():
    ACTION = "sso:GetRoleCredentials"
    mutated = _widen(base_policy(), ACTION, also_widen_deny=True, star=True)

    # The old control is genuinely fooled. Asserting this is the point: it shows the new check
    # is doing work the old one could not, rather than duplicating it.
    assert iam_eval.decide(mutated, ACTION, "*", CTX).decision is (
        iam_eval.Decision.EXPLICIT_ALLOW), (
        "the self-derived Deny was expected to be defeated by the both-sides attack; if it now "
        "catches it, this test no longer demonstrates why the independent ceiling exists")

    result = vc.check(mutated)
    assert not result["clean"], "the independent ceiling accepted a credential-returning action"
    assert any(ACTION in f for f in result["findings"])


def test_the_generator_itself_refuses_to_emit_a_policy_outside_the_ceiling():
    """The check runs at generation time, not only in review."""
    original_caller = set(rv.CALLER_READS)
    original_all = list(rv.ALL_ACTIONS)
    try:
        rv.CALLER_READS = original_caller | {"sso:GetRoleCredentials"}
        rv.ALL_ACTIONS = sorted(set(original_all) | {"sso:GetRoleCredentials"})
        with pytest.raises((vc.CeilingViolation, ValueError), match="GetRoleCredentials"):
            rv.readonly_verifier_policy(ea.ACTIVE_EXPIRY_UTC)
    finally:
        rv.CALLER_READS = original_caller
        rv.ALL_ACTIONS = original_all


def test_the_ceiling_still_refuses_when_the_classifier_pre_check_is_bypassed(monkeypatch):
    """The two refusals are INDEPENDENT, and this proves it rather than assuming it.

    The generator has a classifier pre-check that fires first — defence in depth, and welcome.
    But Gate 4N-I17's lesson is that a control which only works while another control works is
    one control. Here the pre-check is neutralised (the Phase K "stop consuming classification"
    mutation) and the authored ceiling must still refuse, because it reads the policy rather
    than trusting the classifier's verdict about it.
    """
    monkeypatch.setattr(ac, "is_read_only", lambda action: True)
    monkeypatch.setattr(ac, "classify",
                        lambda action: {"action": action, "categories": [ac.READ_ONLY],
                                        "provenance": "BYPASSED", "source_2": "NOT_FORBIDDEN",
                                        "vetoed_by_source_2": False, "prefix_hint": None,
                                        "is_read_only": True, "disqualifying": [],
                                        "conflict": None})
    mutated = _widen(base_policy(), "sso:GetRoleCredentials", also_widen_deny=True, star=True)
    result = vc.check(mutated)
    assert not result["clean"], (
        "with the classifier neutralised the ceiling accepted a credential-returning action — "
        "the ceiling is not independent of the classifier")
    assert any("EXPLICITLY FORBIDDEN" in f for f in result["findings"])


def test_the_generator_actually_CONSUMES_the_ceiling(monkeypatch):
    """Deleting the ceiling call from the generator must break something.

    FOUND BY THIS GATE'S OWN FALSIFICATION SWEEP. Every other test here passed with
    `verifier_ceiling.require_within_ceiling(document)` removed from the generator, because the
    classifier pre-check raised first and masked its absence. A control that is only reached
    when another control has already fired is not independently wired — which is the entire
    lesson of Gate 4N-I17.

    Neutralising the pre-check makes the ceiling the ONLY thing left. If the generator has
    stopped consuming it, this test fails.
    """
    monkeypatch.setattr(ac, "is_read_only", lambda action: True)
    monkeypatch.setattr(ac, "classify",
                        lambda action: {"action": action, "categories": [ac.READ_ONLY],
                                        "provenance": "BYPASSED", "source_2": "NOT_FORBIDDEN",
                                        "vetoed_by_source_2": False, "prefix_hint": None,
                                        "is_read_only": True, "disqualifying": [],
                                        "conflict": None})
    # CALLER_READS feeds an ALLOW statement. ALL_ACTIONS feeds only the Deny's NotAction, so
    # patching that alone would never place the action in a grant — the ceiling would correctly
    # see nothing wrong, and this test would pass while proving nothing. (It did, until the
    # falsification sweep showed the mutation surviving.)
    original_caller = set(rv.CALLER_READS)
    original_all = list(rv.ALL_ACTIONS)
    try:
        rv.CALLER_READS = original_caller | {"sso:GetRoleCredentials"}
        rv.ALL_ACTIONS = sorted(set(original_all) | {"sso:GetRoleCredentials"})
        with pytest.raises(vc.CeilingViolation, match="GetRoleCredentials"):
            rv.readonly_verifier_policy(ea.ACTIVE_EXPIRY_UTC)
    finally:
        rv.CALLER_READS = original_caller
        rv.ALL_ACTIONS = original_all


def test_the_category_rule_is_exercised_independently_of_the_action_allowlist(monkeypatch):
    """A permitted action that starts classifying as risky must still be rejected.

    FOUND BY THIS GATE'S OWN FALSIFICATION SWEEP. Deleting CREDENTIAL_RETURNING from the
    ceiling's forbidden_categories changed no test result, because every credential action was
    ALSO named in explicitly_forbidden_actions and the allowlist caught it first. Two rules that
    only ever fire together are one rule, and the weaker one can be deleted unnoticed.

    This isolates the category axis: an action the ceiling permits BY NAME is made to classify
    as credential-returning, and the ceiling must refuse it on the category alone. That is the
    case that matters in practice — an AWS action whose behaviour changes under a name we
    already trust.
    """
    permitted_action = "sts:GetCallerIdentity"
    # Generate BEFORE patching: with the patch in place the generator's own ceiling check
    # refuses to emit at all, which is correct behaviour but would mask what this test is
    # isolating. (That refusal is covered by test_the_generator_actually_CONSUMES_the_ceiling.)
    policy = base_policy()
    real_classify = ac.classify

    def reclassified(action):
        if action == permitted_action:
            return {"action": action, "categories": [ac.CREDENTIAL_RETURNING],
                    "provenance": "CURATED_REVIEWED", "source_2": "NOT_FORBIDDEN",
                    "vetoed_by_source_2": False, "prefix_hint": None,
                    "is_read_only": False, "categories_disqualifying": True,
                    "disqualifying": [ac.CREDENTIAL_RETURNING], "conflict": None}
        return real_classify(action)

    monkeypatch.setattr(ac, "classify", reclassified)
    result = vc.check(policy)
    assert not result["clean"], (
        "an action the ceiling permits by name was allowed to keep a CREDENTIAL_RETURNING "
        "classification — the category rule is not doing independent work")
    assert any("CREDENTIAL_RETURNING" in f for f in result["findings"])


def test_the_ceiling_partitions_every_known_category():
    """permitted + forbidden must together cover every category, with no overlap.

    FOUND BY THIS GATE'S FALSIFICATION SWEEP. Deleting CREDENTIAL_RETURNING from
    forbidden_categories changed no test result, because the permitted-categories ALLOWLIST
    already refuses anything not explicitly admitted. That redundancy is safe, but it left
    forbidden_categories with no job — and a field with no job is one a future editor deletes
    or lets rot, taking its documentation value with it.

    This invariant gives it a job: the two lists must PARTITION the classifier's categories. A
    category added to the classifier and not placed in exactly one of them fails here, which is
    the moment a reviewer should be asked which side it belongs on.
    """
    contract = json.loads(CEILING_PATH.read_text(encoding="utf-8"))
    permitted = set(contract["permitted_categories"])
    forbidden = set(contract["forbidden_categories"])
    known = set(ac.CATEGORIES)

    assert not (permitted & forbidden), (
        f"categories on both sides of the ceiling: {sorted(permitted & forbidden)}")
    assert permitted | forbidden == known, (
        "the ceiling does not partition the classifier's categories. Unplaced: "
        f"{sorted(known - (permitted | forbidden))}; unknown to the classifier: "
        f"{sorted((permitted | forbidden) - known)}")
    assert permitted == set(ac.PERMITTED_READ_CATEGORIES), (
        "the ceiling's permitted set has drifted from the classifier's read allowlist")


FORBIDDEN_CASES = [
    "sso:GetRoleCredentials", "sts:AssumeRole", "sts:AssumeRoleWithWebIdentity",
    "iam:PassRole", "ecs:RunTask", "cloudtrail:StopLogging",
    "secretsmanager:GetSecretValue", "kms:Decrypt", "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
    # unknown actions, one per read-shaped prefix the old rule trusted
    "acme:GetSomethingNew", "acme:ListSomethingNew", "acme:DescribeSomethingNew",
    # a service wildcard admits every future action that service gains
    "iam:*",
]


@pytest.mark.parametrize("action", FORBIDDEN_CASES)
def test_each_forbidden_action_is_rejected(action):
    mutated = _widen(base_policy(), action, also_widen_deny=True)
    assert not vc.check(mutated)["clean"], f"{action} was accepted by the ceiling"


@pytest.mark.parametrize("action", FORBIDDEN_CASES)
def test_no_forbidden_action_evaluates_to_allow_under_the_shipped_policy(action):
    """Where an evaluator can run, the shipped document must not permit these."""
    if action.endswith(":*"):
        pytest.skip("a wildcard action is a structural finding, not an evaluable request")
    assert iam_eval.decide(base_policy(), action, "*", CTX).decision is not (
        iam_eval.Decision.EXPLICIT_ALLOW)


# =====================================================================================
# Fail-closed classification
# =====================================================================================


def test_no_action_is_classified_by_spelling():
    """Every in-use action must come from an EXACT source."""
    rows = ac.run()["rows"]
    guessed = [r["action"] for r in rows if r["provenance"] not in
               ("CURATED_REVIEWED", "REFRESH_OBSERVED_ZERO_WRITE", "REPOSITORY_FORBIDDEN_INVARIANT")]
    assert not guessed, f"{len(guessed)} actions still classified without an exact source: {guessed[:5]}"


def test_coverage_is_complete_and_nothing_is_unknown():
    result = ac.run()
    assert result["coverage"] == 1.0
    assert result["unclassified"] == []


@pytest.mark.parametrize("action,expected", [
    ("sso:GetRoleCredentials", ac.CREDENTIAL_RETURNING),
    ("sts:AssumeRole", ac.CREDENTIAL_RETURNING),
    ("sts:AssumeRoleWithWebIdentity", ac.CREDENTIAL_RETURNING),
    ("sts:GetFederationToken", ac.TOKEN_RETURNING),
    ("ecr:GetAuthorizationToken", ac.TOKEN_RETURNING),
    ("secretsmanager:GetSecretValue", ac.SECRET_RETURNING),
    ("secretsmanager:BatchGetSecretValue", ac.SECRET_RETURNING),
    ("kms:Decrypt", ac.SENSITIVE_DATA_RETURNING),
    ("logs:GetLogEvents", ac.SENSITIVE_DATA_RETURNING),
    ("iam:PassRole", ac.AUTHORITY_BEARING),
    ("ecs:RunTask", ac.EXECUTION_TRIGGERING),
    ("cloudtrail:StopLogging", ac.DESTRUCTIVE),
])
def test_high_risk_actions_carry_their_risk_category(action, expected):
    result = ac.classify(action)
    assert expected in result["categories"], result
    assert not result["is_read_only"], f"{action} classified read-only"


@pytest.mark.parametrize("action,expected", [
    ("sts:GetCallerIdentity", ac.READ_ONLY_METADATA),
    ("iam:GetRole", ac.READ_ONLY_CONFIGURATION),
])
def test_the_genuine_reads_are_classified_exactly(action, expected):
    result = ac.classify(action)
    assert expected in result["categories"]
    assert result["is_read_only"]


@pytest.mark.parametrize("action", [
    "acme:GetBrandNewThing", "acme:ListBrandNewThing", "acme:DescribeBrandNewThing",
    "acme:LookupBrandNewThing", "acme:HeadBrandNewThing", "acme:BatchGetBrandNewThing",
    "acme:SelectBrandNewThing", "acme:SearchBrandNewThing",
])
def test_an_unclassified_action_fails_closed_whatever_its_prefix(action):
    result = ac.classify(action)
    assert result["categories"] == [ac.UNKNOWN]
    assert not result["is_read_only"]
    assert result["conflict"], "an unknown action must be a finding, not a silent pass"
    with pytest.raises(ac.ClassificationError):
        ac.is_read_only(action)


def test_the_prefix_hint_is_diagnostic_and_never_authoritative():
    """The hint may say 'looks like a read'; it may not make one."""
    result = ac.classify("acme:GetBrandNewThing")
    assert result["prefix_hint"] == "looks-like-read"
    assert result["categories"] == [ac.UNKNOWN], (
        "the prefix hint leaked back into the classification")


def test_the_permitted_read_set_is_an_allowlist():
    """A category added later must be disqualifying until deliberately admitted."""
    assert set(ac.NON_READ) == set(ac.CATEGORIES) - set(ac.PERMITTED_READ_CATEGORIES)
    for risky in (ac.CREDENTIAL_RETURNING, ac.TOKEN_RETURNING, ac.SECRET_RETURNING,
                  ac.SENSITIVE_DATA_RETURNING, ac.UNKNOWN):
        assert risky in ac.NON_READ
