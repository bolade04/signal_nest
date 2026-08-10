"""Trust-policy containment and mutation matrix (Gate 4N-I9, Defect 1).

THE DEFECT. `iam:CreateRole` accepts the AssumeRolePolicyDocument in the request and AWS
provides NO condition key comparing the whole submitted trust document to an approved hash.
Through Gate 4N-I8 the Stage-A operator held CreateRole on the three reader role ARNs,
conditioned on `iam:PermissionsBoundary` — which constrains what the role may DO and says
nothing about who may ASSUME it. A role created with an external-account or wildcard trust
SURVIVES that operator's expiry.

Two halves, and neither alone is sufficient:

  CONTAINMENT  Stage-A now holds no role-authoring capability at all. The capability lives
               on a separate, minimal RoleBootstrapOperator.
  DETECTION    the exact trust bytes are reviewed and hashed, and an INDEPENDENT validator
               re-derives what they should be from the external anchor, the git remote and
               the role-purpose contract — never from the generator.

The mutation matrix below is the proof that the validator is load-bearing. Every one of the
14 mutations must be rejected: a validator that accepts an attacker-controlled principal is
worse than none, because it is believed.

HONEST LIMIT, stated here so review does not have to discover it: this is DETECT-AND-REVERT,
not PREVENT. Between CreateRole returning and the read-back comparison completing, a wrong
trust document exists in the account. AWS offers no way to prevent it.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_operator_policies as gen  # noqa: E402
import gen_role_bootstrap_policy as rb  # noqa: E402
import expiry_authorization as _ea  # noqa: E402
import iam_eval  # noqa: E402
import trust_policies  # noqa: E402
import trust_validator  # noqa: E402

# GATE 4N-I13: trust validation compares against the ANCHORED account, so it certifies only
# under Tier 2. Under Tier 1 the synthetic account differs by construction.
REAL_ANCHOR = Path.home() / ".signalnest" / "anchor" / "signalnest-account-environment-anchor.json"
def tier2(fn):
    """Mark a check as Tier 2 and run it ONLY when Tier 2 is DECLARED.

    Composed as a plain decorator: pytest.mark.tier2(pytest.mark.skipif(...))
    wraps the mark object rather than applying both to the test, so the skip
    never fired and empty-HOME runs FAILED instead of skipping.

    GATE 4N-I18, SEC-1. The condition used to be "the real anchor FILE EXISTS". That is tier
    by DISCOVERY, which is the Gate 4N-I10 defect shape — a developer machine that happens to
    hold the anchor silently ran production-certifying checks while the environment declared
    TIER_1_SYNTHETIC. Once identity became tier-resolved, the two disagreed and these checks
    compared synthetic documents against the real anchor. The tier must be DECLARED; the file
    merely has to be there too.
    """
    import os

    declared = os.environ.get("SIGNALNEST_ANCHOR_TIER")
    fn = pytest.mark.tier2(fn)
    return pytest.mark.skipif(
        declared != "TIER_2_PROTECTED" or not REAL_ANCHOR.exists(),
        reason=("TIER_2_PROTECTED not declared, or the real anchor is absent (both expected in "
                "ordinary CI); this check certifies production identity and cannot run on a "
                "synthetic fixture"))(fn)


EXPIRY = _ea.ACTIVE_EXPIRY_UTC
CTX = {"aws:CurrentTime": "2026-07-31T12:00:00Z", "aws:RequestedRegion": "us-east-1",
       "iam:PermissionsBoundary": gen.ARN["boundary"]}
MANIFEST = trust_policies.trust_manifest()
OIDC_ROLES = [r for r in MANIFEST if r.endswith(("publisher", "runner"))]


# --- containment: Stage-A cannot submit trust bytes -------------------------------------


# GATE 4N-I16 DEFECT 3. iam:PutRolePolicy was removed from this list and given its own
# containment test below. The Gate 4N-I9 rule this list encodes is about the ASSUME-ROLE
# TRUST DOCUMENT, which PutRolePolicy does not carry; keeping it here forced the previous
# gate to hide a required action behind a false closure exclusion. It is now denied
# EXPLICITLY everywhere except the enumerated declared roles, which the companion test
# asserts directly rather than by category.
@pytest.mark.parametrize("action", [
    "iam:CreateRole", "iam:TagRole",
    "iam:UpdateAssumeRolePolicy", "iam:DeleteRole",
])
def test_stage_a_cannot_author_roles_at_all(action):
    """THE containment half. Explicit deny, on the reader roles themselves."""
    policy = gen.bootstrap_temp_policy(EXPIRY)
    for arn in gen.READER_ROLE_ARNS:
        iam_eval.require_explicit_deny(policy, action, arn, CTX)


def test_stage_a_inline_policy_writes_are_fenced_to_the_declared_roles():
    """Replaces the blanket PutRolePolicy deny with a containment that is actually checked.

    The reader roles remain writable ONLY because the composition declares inline policies
    for them. Every role outside the declared set — including a same-prefix role that the
    Stage-A READ grant would match — must be EXPLICITLY denied.
    """
    policy = gen.bootstrap_temp_policy(EXPIRY)
    for arn in gen.INLINE_POLICY_ROLE_ARNS:
        assert iam_eval.decide(policy, "iam:PutRolePolicy", arn, CTX).decision \
            is iam_eval.Decision.EXPLICIT_ALLOW, arn
    for outside in (
        f"arn:aws:iam::{gen.ACCOUNT}:role/{gen.PREFIX}-not-a-declared-role",
        f"arn:aws:iam::{gen.ACCOUNT}:role/some-unrelated-role",
    ):
        iam_eval.require_explicit_deny(policy, "iam:PutRolePolicy", outside, CTX)


def test_stage_a_policy_contains_no_role_authoring_grant_at_all():
    """Static companion: not merely denied, but never granted."""
    policy = gen.bootstrap_temp_policy(EXPIRY)
    for statement in policy["Statement"]:
        if statement["Effect"] != "Allow":
            continue
        actions = iam_eval._as_list(statement.get("Action"))
        for action in actions:
            assert not action.startswith("iam:CreateRole"), statement.get("Sid")
            # GATE 4N-I16 DEFECT 3: iam:PutRolePolicy left this set deliberately. It carries
            # no trust document, and it is granted only on the enumerated declared roles
            # under the boundary condition, with an explicit fence outside them. See
            # tests/test_putrolepolicy_closure.py for the classification and the twelve
            # negative controls, and test_iam_eval_semantics for the fence assertions.
            assert action not in ("iam:TagRole",
                                  "iam:UpdateAssumeRolePolicy"), statement.get("Sid")
            if action == "iam:PutRolePolicy":
                assert statement.get("Sid") == "TempInlineRolePolicyBounded", (
                    "PutRolePolicy may appear in exactly ONE reviewed statement")
                assert "iam:PermissionsBoundary" in json.dumps(
                    statement.get("Condition", {})), statement.get("Sid")


def test_the_role_bootstrap_operator_cannot_rewrite_trust_after_read_back():
    """UpdateAssumeRolePolicy would defeat the only trust control that exists."""
    policy = rb.role_bootstrap_policy(EXPIRY)
    for arn in rb.TARGET_ROLE_ARNS:
        iam_eval.require_explicit_deny(policy, "iam:UpdateAssumeRolePolicy", arn, CTX)
        iam_eval.require_explicit_deny(policy, "iam:PutRolePolicy", arn, CTX)
        iam_eval.require_explicit_deny(policy, "iam:PassRole", arn, CTX)


def test_the_role_bootstrap_operator_can_do_its_job():
    """Positive control. A principal that cannot create the roles is not safer."""
    policy = rb.role_bootstrap_policy(EXPIRY)
    for arn in rb.TARGET_ROLE_ARNS:
        for action in ("iam:CreateRole", "iam:GetRole", "iam:DeleteRole"):
            assert iam_eval.decide(policy, action, arn, CTX).decision \
                is iam_eval.Decision.EXPLICIT_ALLOW, f"{action} on {arn}"


# --- the documents themselves -------------------------------------------------------------


@tier2
def test_every_trust_document_is_valid_against_the_independent_validator():
    result = trust_validator.validate_all()
    assert result["clean"], result["invalid"]


def test_no_trust_artifact_carries_an_unresolved_placeholder():
    text = json.dumps(MANIFEST)
    for marker in ("<", "${", "PLACEHOLDER", "TODO", "CHANGEME"):
        assert marker not in text, f"trust artifacts contain {marker!r}"


def test_the_validator_does_not_import_the_generator_as_an_authority():
    """The mistake this whole gate chain keeps re-learning."""
    source = (REPO_ROOT / "scripts" / "trust_validator.py").read_text(encoding="utf-8")
    body = source.split("def validate_all(")[0]
    assert "import trust_policies" not in body, (
        "the validator derives expectations above validate_all(); importing the generator "
        "there would make it a mirror")
    assert "load_anchor" in body and "github_repository" in body, (
        "the validator must derive from the external anchor and the git remote")


def test_hashes_are_stable():
    assert trust_policies.trust_manifest() == MANIFEST


# --- PHASE F: the 14-mutation matrix, required score 100% ---------------------------------


def _statement(doc):
    return doc["Statement"][0]


def _add_account(doc):
    _statement(doc)["Principal"] = {"AWS": "arn:aws:iam::999988887777:root"}


def _replace_account(doc):
    principal = _statement(doc)["Principal"]
    if "Federated" in principal:
        principal["Federated"] = principal["Federated"].replace(
            trust_policies.ACCOUNT, "999988887777")
    else:
        _statement(doc)["Condition"]["StringEquals"]["aws:SourceAccount"] = "999988887777"


def _wildcard_principal(doc):
    _statement(doc)["Principal"] = "*"


def _broaden_subject(doc):
    equals = _statement(doc)["Condition"]["StringEquals"]
    key = "token.actions.githubusercontent.com:sub"
    if key in equals:
        equals[key] = f"repo:{trust_policies.GITHUB_REPOSITORY}:*"


def _remove_repository_restriction(doc):
    equals = _statement(doc)["Condition"]["StringEquals"]
    key = "token.actions.githubusercontent.com:sub"
    if key in equals:
        equals[key] = "repo:someone-else/other:environment:staging-reader-run"


def _remove_environment(doc):
    equals = _statement(doc)["Condition"]["StringEquals"]
    key = "token.actions.githubusercontent.com:sub"
    if key in equals:
        equals[key] = f"repo:{trust_policies.GITHUB_REPOSITORY}:ref:refs/heads/main"


def _remove_audience(doc):
    _statement(doc)["Condition"]["StringEquals"].pop(
        "token.actions.githubusercontent.com:aud", None)


def _change_audience(doc):
    equals = _statement(doc)["Condition"]["StringEquals"]
    if "token.actions.githubusercontent.com:aud" in equals:
        equals["token.actions.githubusercontent.com:aud"] = "attacker.example"


def _add_service_principal(doc):
    _statement(doc)["Principal"]["Service"] = "lambda.amazonaws.com"


def _add_second_statement(doc):
    doc["Statement"].append({
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam::999988887777:root"},
        "Action": "sts:AssumeRole",
    })


def _change_action(doc):
    _statement(doc)["Action"] = "sts:AssumeRoleWithSAML"


def _remove_condition(doc):
    _statement(doc).pop("Condition", None)


def _stringlike_wildcard(doc):
    statement = _statement(doc)
    equals = statement.get("Condition", {}).pop("StringEquals", None)
    if equals:
        statement["Condition"]["StringLike"] = {k: "*" for k in equals}


def _alter_oidc_provider(doc):
    principal = _statement(doc)["Principal"]
    if "Federated" in principal:
        principal["Federated"] = principal["Federated"].replace(
            "token.actions.githubusercontent.com", "attacker-idp.example.com")


MUTATIONS = {
    "1_add_attacker_account": (_add_account, None),
    "2_replace_approved_account": (_replace_account, None),
    "3_wildcard_principal": (_wildcard_principal, None),
    "4_broaden_oidc_subject": (_broaden_subject, OIDC_ROLES),
    "5_remove_repository_restriction": (_remove_repository_restriction, OIDC_ROLES),
    "6_remove_environment_restriction": (_remove_environment, OIDC_ROLES),
    "7_remove_audience": (_remove_audience, OIDC_ROLES),
    "8_change_audience": (_change_audience, OIDC_ROLES),
    "9_add_service_principal": (_add_service_principal, None),
    "10_add_second_statement": (_add_second_statement, None),
    "11_change_sts_action": (_change_action, None),
    "12_remove_condition": (_remove_condition, None),
    "13_stringlike_wildcard": (_stringlike_wildcard, OIDC_ROLES),
    "14_alter_oidc_provider": (_alter_oidc_provider, OIDC_ROLES),
}

CASES = [(name, role) for name, (_, roles) in MUTATIONS.items()
         for role in (roles if roles is not None else sorted(MANIFEST))]


@pytest.mark.parametrize("mutation,role", CASES, ids=[f"{m}:{r.split('-')[-1]}"
                                                      for m, r in CASES])
def test_every_trust_mutation_is_rejected(mutation, role):
    """Required score: 100%. Each is a real way an attacker keeps access past expiry."""
    apply_mutation, _ = MUTATIONS[mutation]
    document = copy.deepcopy(MANIFEST[role]["trust_policy"])
    original = copy.deepcopy(document)
    apply_mutation(document)
    assert document != original, f"mutation {mutation} was a no-op on {role}"

    problems = trust_validator.validate_document(role, document)
    assert problems, (
        f"the independent validator ACCEPTED {mutation} on {role}. That mutation is a live "
        f"path to persistent unauthorized access:\n{json.dumps(document, indent=2)}")


@tier2
def test_the_unmutated_documents_pass():
    """Controls the matrix: a validator that rejects everything proves nothing."""
    for role, entry in MANIFEST.items():
        assert trust_validator.validate_document(role, entry["trust_policy"]) == [], role


def test_the_mutation_matrix_covers_every_required_category():
    """A shrinking matrix would raise the score without improving anything."""
    assert len(MUTATIONS) >= 14, sorted(MUTATIONS)
    assert len(CASES) >= 30, len(CASES)
