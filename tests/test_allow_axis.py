"""Allow-axis widening and the both-sided ceiling proof (Gate 4N-I7, Defects 3 and 6).

THE DEFECT. Every mutation test through Gate 4N-I6 worked on the Deny axis: remove a Deny,
observe a failure. Nothing tested the Allow axis at all. A statement granting one action
too many, or a wildcard quietly swallowing a forbidden one, moved no test — and the
previous gate's ceiling proof scored 63/64, an unexplained escape that was reported and
then not chased.

The scores this file requires are absolute:

  0 escapes            over all four principals, all 39 must-not capabilities
  0 losses             no required capability removed
  0 unclassified       nothing granted without an external justification

The escape half is what makes the ceiling real. For an unexempted capability the policy is
first WIDENED with a fresh unconditioned `Allow <action> on *` — the most permissive
mistake an edit can make — and the decision must still be EXPLICIT_DENY. For an exempted
capability the proof is two-sided: allowed on the resource the exemption names, explicitly
denied on every resource outside it.

No AWS access, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import allow_model  # noqa: E402
import expiry_authorization as _ea  # noqa: E402
import iam_eval  # noqa: E402
import must_not_contract  # noqa: E402
from iam_eval import Decision  # noqa: E402

REPORT = allow_model.run()
POLICIES = sorted(allow_model.TARGETS)


# --- the scores -----------------------------------------------------------------------


@pytest.mark.parametrize("name", POLICIES)
def test_the_ceiling_absorbs_every_forbidden_capability(name):
    """0 escapes. The prior gate's 63/64 was not acceptable and neither is 38/39."""
    entry = REPORT["policies"][name]
    detail = "\n".join(
        f"  {row['action']}: {row.get('defect') or row.get('decision')} "
        f"— an attacker would {row['consequence']}"
        for row in entry["escapes"])
    assert not entry["escapes"], (
        f"{name} scored {entry['escape_score']}; every escape is a capability the "
        f"must-not contract forbids and this principal can reach:\n{detail}")


@pytest.mark.parametrize("name", POLICIES)
def test_no_required_capability_is_lost(name):
    """0 losses. A ceiling that breaks the work it caps gets reverted, not fixed."""
    losses = REPORT["policies"][name].get("losses", [])
    assert not losses, f"{name} lost required capabilities: {losses}"


@pytest.mark.parametrize("name", POLICIES)
def test_every_grant_is_externally_justified(name):
    """0 unclassified. 'It was already there' is not a justification."""
    unclassified = REPORT["policies"][name]["unclassified"]
    assert not unclassified, (
        f"{name} grants actions justified in no external source: "
        f"{[row['action'] for row in unclassified]}")


def test_the_overall_report_is_clean():
    assert REPORT["clean"], REPORT["totals"]


def test_the_score_denominator_is_the_whole_contract():
    """A shrinking contract would raise every score without improving anything."""
    for name in POLICIES:
        scored, total = REPORT["policies"][name]["escape_score"].split("/")
        assert int(total) == len(must_not_contract.FORBIDDEN_CAPABILITIES)
        assert int(scored) == int(total)


# --- controls on the proof itself -----------------------------------------------------


def test_the_widening_mutation_is_not_a_no_op():
    """If the injection did nothing, every 'absorbed' result would be meaningless."""
    policy = {"Version": "2012-10-17",
              "Statement": [{"Sid": "S", "Effect": "Allow", "Action": "s3:GetObject",
                             "Resource": "arn:aws:s3:::b/x"}]}
    widened = allow_model._inject_into_allow(policy, "iam:CreateRole")
    assert iam_eval.decide(policy, "iam:CreateRole", "*", {}).decision is Decision.IMPLICIT_DENY
    assert iam_eval.decide(widened, "iam:CreateRole", "*", {}).decision is Decision.EXPLICIT_ALLOW


def test_the_injected_statement_carries_no_inherited_condition():
    """The first draft appended to an existing Allow and inherited its expiry condition.

    That produced MISSING_CONTEXT — an inconclusive decision that appeared in the escape
    column and would have read as a pass to anything checking only for EXPLICIT_ALLOW.
    """
    import gen_operator_policies as gen

    widened = allow_model._inject_into_allow(gen.bootstrap_temp_policy(_ea.ACTIVE_EXPIRY_UTC), "iam:CreateUser")
    injected = widened["Statement"][0]
    assert injected["Sid"] == "InjectedWideningMutation"
    assert "Condition" not in injected and injected["Resource"] == "*"


@pytest.mark.parametrize("name", POLICIES)
def test_removing_a_ceiling_statement_produces_escapes(name):
    """Mutation control: with the ceiling gone, the proof must FAIL.

    A proof that passes on a policy with no ceiling at all is proving nothing about the
    ceiling.
    """
    build, context, probe = allow_model.TARGETS[name]
    policy = build()
    without_denies = {**policy,
                      "Statement": [s for s in policy["Statement"] if s["Effect"] != "Deny"]}
    result = allow_model.prove_ceiling(name, without_denies, context, probe)
    assert result["escapes"], f"{name} scored clean with every Deny removed"


def test_the_must_not_contract_is_not_derived_from_the_policies():
    """The contract must be an input to the policies, never a readback of them."""
    source = (REPO_ROOT / "scripts" / "must_not_contract.py").read_text(encoding="utf-8")
    # IMPORT statements only. The module names its consumers in prose deliberately, so a
    # substring search over the whole file would flag its own documentation.
    imports = [line.strip() for line in source.splitlines()
               if line.strip().startswith(("import ", "from "))]
    offenders = [line for line in imports
                 if any(module in line for module in
                        ("gen_boundary_policy", "gen_operator_policies",
                         "gen_bootstrap_operator_policy", "gen_boundary_rollout",
                         "iam_eval", "allow_model"))]
    assert not offenders, (
        f"must_not_contract.py imports a consumer: {offenders}. It would then restate the "
        "policies rather than constrain them")
    # Gate 4N-I8: the contract now DERIVES its set from scripts/deny_requirements.py, which
    # triangulates the external incident ledger with the architecture invariants. That import
    # is required, not tolerated — an earlier version of this test demanded zero imports,
    # which would have forced the hand-written list that Defect 2 was about.
    assert any("deny_requirements" in line for line in imports), (
        "the contract must DERIVE its set from the triangulated requirement, not define it")


def test_every_forbidden_capability_states_its_consequence():
    """An entry with no stated consequence cannot be argued with, so it cannot be trusted."""
    for action, consequence in must_not_contract.FORBIDDEN_CAPABILITIES.items():
        assert ":" in action, f"{action} is not an IAM action"
        assert len(consequence.split()) >= 4, (
            f"{action}: consequence {consequence!r} is too thin to review")


def test_capabilities_without_a_reviewed_note_are_visible_not_hidden():
    """Deriving the set added 24 capabilities the hand-written list had missed.

    Those carry the requirement source's justification rather than a reviewed consequence
    note. That is acceptable and it is REPORTED — `unnoted_actions()` exists so the gap is
    countable rather than invisible. What is not acceptable is dropping them from the set to
    make this file tidy, which is how the set shrank before.
    """
    unnoted = set(must_not_contract.unnoted_actions())
    assert unnoted <= set(must_not_contract.FORBIDDEN_CAPABILITIES)
    for action in unnoted:
        assert must_not_contract.FORBIDDEN_CAPABILITIES[action].startswith("SOURCE "), action


def test_the_contract_covers_every_escalation_path_named_in_the_prior_gates():
    """Regression anchor: these are the paths earlier gates established by evidence."""
    required = {
        "iam:CreateRole": "Gate 4N-H4 BR-2, the transitive escape",
        "iam:PassRole": "Gate 4N-H3, checked at RegisterTaskDefinition",
        "ecs:RegisterTaskDefinition": "Gate 4N-H4, the ECS path",
        "ecs:CreateService": "Gate 4N-H4, the live launch path",
        "s3:PutBucketPolicy": "Gate 4N-H4 BR-1, reaches the state bucket",
        "cloudtrail:StopLogging": "Gate 4N-I2, audit-trail protection",
        "sso:PutInlinePolicyToPermissionSet": "Gate 4N-H2, ICPermAdmin's standing grant",
        "sts:AssumeRole": "Gate 4N-I7, role chaining out of the account",
    }
    missing = {a: why for a, why in required.items()
               if a not in must_not_contract.FORBIDDEN_CAPABILITIES}
    assert not missing, f"the contract dropped established escalation paths: {missing}"


# --- scoped exemptions ------------------------------------------------------------------


EXEMPTION_CASES = [(policy, action)
                   for policy, actions in sorted(allow_model.EXEMPTIONS.items())
                   for action in sorted(actions)]


@pytest.mark.parametrize("name,action", EXEMPTION_CASES,
                         ids=[f"{p}:{a}" for p, a in EXEMPTION_CASES])
def test_each_exemption_is_allowed_in_scope_and_explicitly_denied_outside(name, action):
    """An exemption is a fence, not a hole. Both sides are load-bearing."""
    row = next(r for r in REPORT["policies"][name]["exempted"] if r["action"] == action)
    assert row["in_scope_decision"] == Decision.EXPLICIT_ALLOW.name, row
    assert row["out_of_scope"], "an exemption with no out-of-scope probe proves nothing"
    for probe in row["out_of_scope"]:
        assert probe["decision"] == Decision.EXPLICIT_DENY.name, (
            f"{name} {action} is not fenced: {probe}")


def test_every_exemption_states_a_reason():
    for name, actions in allow_model.EXEMPTIONS.items():
        for action, spec in actions.items():
            assert len(spec["reason"].split()) >= 5, f"{name}:{action} reason is too thin"
            assert spec["out_of_scope"](), f"{name}:{action} has no out-of-scope probe"


def test_the_temporary_operator_is_not_exempted_for_passrole():
    """A wrong exemption is worse than a missing one.

    The first draft exempted iam:PassRole here on the reasoning that the revision-reader
    runner needs it. The runner needs it at RUNTIME and is a different principal;
    stage_a_create_closure requires only CreateRole, PutRolePolicy and TagRole.
    """
    assert "iam:PassRole" not in allow_model.EXEMPTIONS["temporary_operator"]

    import gen_operator_policies as gen

    iam_eval.require_explicit_deny(
        gen.bootstrap_temp_policy(_ea.ACTIVE_EXPIRY_UTC), "iam:PassRole",
        gen.READER_ROLE_ARNS[1], {"aws:CurrentTime": "2000-01-01T00:00:00Z"})
