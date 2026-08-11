"""Explicit-Deny verification and mutation testing (Gate 4N-I6).

THE DEFECT THIS CLOSES. Every safety assertion in the Gate 4N-I5 suite was
`not allowed(...)`, which `IMPLICIT_DENY` satisfies. Removing `iam:PassRole` from the
permanent deny — the exact regression the whole 4N-H saga was about — still left 166/166
green. All 102 deny actions were individually removable with a green suite, so the
harness protected nothing.

Two things are asserted here that the old suite could not express:

  1. the decision is EXPLICIT_DENY, from a NAMED Sid — an implicit deny fails the test;
  2. MUTATION COVERAGE — for every mandatory safety action, removing or weakening the
     Deny makes a specific test fail. A control nobody can break is a control nobody has
     verified.

No AWS access, no network.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_operator_policies as gen  # noqa: E402
import expiry_authorization as _ea  # noqa: E402
import iam_eval  # noqa: E402
from iam_eval import Decision  # noqa: E402

PERM_CTX = {"aws:RequestedRegion": gen.REGION}
TEMP_EXPIRY = _ea.ACTIVE_EXPIRY_UTC
TEMP_CTX = {
    "aws:RequestedRegion": gen.REGION,
    "aws:CurrentTime": "2026-07-31T12:00:00Z",
    "iam:PermissionsBoundary": gen.ARN["boundary"],
}

STATE_OBJECT = gen.ARN["state_object"]
LOCK = gen.ARN["lock"]

# The mandatory safety set. Every entry must be an EXPLICIT Deny from the named Sid, and
# every entry is mutation-tested below. Adding an action here without it being denied
# breaks the suite, which is the point.
MANDATORY_PERMANENT_DENIES = [
    ("iam:PassRole", "*"),
    ("iam:CreateRole", gen.READER_ROLE_ARNS[0]),
    ("iam:PutRolePolicy", gen.READER_ROLE_ARNS[0]),
    ("iam:DeleteRole", gen.READER_ROLE_ARNS[0]),
    ("iam:DeleteRolePolicy", gen.READER_ROLE_ARNS[0]),
    ("iam:TagRole", gen.READER_ROLE_ARNS[0]),
    ("iam:UntagRole", gen.READER_ROLE_ARNS[0]),
    ("iam:PutRolePermissionsBoundary", gen.READER_ROLE_ARNS[0]),
    ("iam:UpdateAssumeRolePolicy", gen.READER_ROLE_ARNS[0]),
    ("cloudtrail:StopLogging", gen.ARN["trail"]),
    ("cloudtrail:DeleteTrail", gen.ARN["trail"]),
    ("cloudtrail:UpdateTrail", gen.ARN["trail"]),
    ("cloudtrail:PutEventSelectors", gen.ARN["trail"]),
    ("s3:GetObject", STATE_OBJECT),
    ("s3:PutObject", STATE_OBJECT),
    ("s3:DeleteObject", STATE_OBJECT),
    ("s3:GetObjectVersion", STATE_OBJECT),
    ("s3:PutBucketPolicy", gen.ARN["state_bucket"]),
    ("s3:PutBucketVersioning", gen.ARN["state_bucket"]),
    ("dynamodb:PutItem", LOCK),
    ("dynamodb:DeleteItem", LOCK),
    ("dynamodb:UpdateItem", LOCK),
    ("kms:ScheduleKeyDeletion", gen.ARN["cmk_state"]),
    ("kms:DisableKey", gen.ARN["cmk_state"]),
    ("kms:CreateGrant", gen.ARN["cmk_secrets"]),
    ("kms:PutKeyPolicy", gen.ARN["cmk_secrets"]),
    ("secretsmanager:GetSecretValue", "*"),
    ("secretsmanager:PutSecretValue", "*"),
    ("ecs:RegisterTaskDefinition", "*"),
    ("ecs:CreateService", "*"),
    ("ecs:RunTask", "*"),
    ("sts:AssumeRole", "*"),
]

DENY_SID = "DenyDangerous"


@pytest.fixture(scope="module")
def permanent() -> dict:
    return gen.permanent_w0_policy()


@pytest.fixture(scope="module")
def temporary() -> dict:
    return gen.bootstrap_temp_policy(TEMP_EXPIRY)


# --- Phase E: the harness must distinguish implicit from explicit -------------------


def _p(*statements) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def test_no_matching_statement_is_implicit_deny():
    assert iam_eval.decide(_p(), "x:y", "*", {}).decision is Decision.IMPLICIT_DENY


def test_matching_allow_is_explicit_allow():
    r = iam_eval.decide(_p({"Sid": "A", "Effect": "Allow", "Action": "x:y", "Resource": "*"}), "x:y", "*", {})
    assert r.decision is Decision.EXPLICIT_ALLOW and r.matching_allow_sids == ("A",)


def test_matching_deny_is_explicit_deny():
    r = iam_eval.decide(_p({"Sid": "D", "Effect": "Deny", "Action": "x:y", "Resource": "*"}), "x:y", "*", {})
    assert r.decision is Decision.EXPLICIT_DENY and r.matching_deny_sids == ("D",)


def test_allow_and_deny_together_is_explicit_deny():
    r = iam_eval.decide(_p({"Sid": "A", "Effect": "Allow", "Action": "x:y", "Resource": "*"},
                           {"Sid": "D", "Effect": "Deny", "Action": "x:y", "Resource": "*"}), "x:y", "*", {})
    assert r.decision is Decision.EXPLICIT_DENY
    assert r.matching_allow_sids == ("A",) and r.matching_deny_sids == ("D",)


def test_malformed_policy_is_invalid():
    r = iam_eval.decide(_p({"Sid": "X", "Effect": "Maybe", "Action": "x:y", "Resource": "*"}), "x:y", "*", {})
    assert r.decision in (Decision.INVALID_POLICY, Decision.UNSUPPORTED_SEMANTICS)


def test_missing_condition_context_is_reported_distinctly():
    r = iam_eval.decide(_p({"Sid": "C", "Effect": "Allow", "Action": "x:y", "Resource": "*",
                            "Condition": {"StringEquals": {"k": "v"}}}), "x:y", "*", {})
    assert r.decision is Decision.MISSING_CONTEXT


def test_unsupported_operator_is_reported_distinctly():
    r = iam_eval.decide(_p({"Sid": "U", "Effect": "Allow", "Action": "x:y", "Resource": "*",
                            "Condition": {"IpAddress": {"aws:SourceIp": "1.2.3.4"}}}), "x:y", "*", {})
    assert r.decision is Decision.UNSUPPORTED_SEMANTICS


def test_require_explicit_deny_rejects_implicit_deny():
    """THE control that makes the rest of this file meaningful."""
    empty = _p({"Sid": "Unrelated", "Effect": "Allow", "Action": "other:action", "Resource": "*"})
    with pytest.raises(AssertionError, match="not a safety control"):
        iam_eval.require_explicit_deny(empty, "iam:PassRole", "*", {})


# --- Phase C: explicit-Deny assertions ---------------------------------------------


@pytest.mark.parametrize("action,resource", MANDATORY_PERMANENT_DENIES,
                         ids=[f"{a}@{r.rsplit(':', 1)[-1][:24]}" for a, r in MANDATORY_PERMANENT_DENIES])
def test_permanent_w0_explicitly_denies(permanent, action, resource):
    """EXPLICIT_DENY from the named Sid — implicit denial fails this test."""
    iam_eval.require_explicit_deny(permanent, action, resource, PERM_CTX, sid=DENY_SID)


def test_the_deny_statement_is_unconditional_and_global(permanent):
    """A conditioned or scoped safety Deny can fail open when the key or ARN shifts."""
    deny = [s for s in permanent["Statement"] if s.get("Sid") == DENY_SID]
    assert len(deny) == 1
    assert "Condition" not in deny[0]
    assert deny[0]["Resource"] == "*"
    assert "NotAction" not in deny[0] and "NotResource" not in deny[0]


def test_a_competing_allow_cannot_override_the_safety_deny(permanent):
    """Explicit Deny must win even against a maximally broad Allow."""
    for action, resource in MANDATORY_PERMANENT_DENIES:
        widened = copy.deepcopy(permanent)
        widened["Statement"].insert(0, {"Sid": "Sneak", "Effect": "Allow",
                                        "Action": action, "Resource": "*"})
        result = iam_eval.decide(widened, action, resource, PERM_CTX)
        assert result.decision is Decision.EXPLICIT_DENY, action
        assert "Sneak" in result.matching_allow_sids, "the competing Allow must genuinely match"


# --- Phase D: mutation testing, 100% of the mandatory set ---------------------------


MUTATIONS = ["remove_action", "misspell_action", "flip_effect_to_allow",
             "narrow_resource", "add_notresource_escape", "add_expiring_condition",
             "rename_sid", "delete_statement"]


def _mutate(policy: dict, action: str, kind: str) -> dict:
    out = copy.deepcopy(policy)
    stmts = out["Statement"]
    target = next(s for s in stmts if s.get("Sid") == DENY_SID)
    if kind == "remove_action":
        target["Action"] = [a for a in target["Action"] if a != action]
    elif kind == "misspell_action":
        target["Action"] = [a.replace(action, action + "X") for a in target["Action"]]
    elif kind == "flip_effect_to_allow":
        target["Effect"] = "Allow"
    elif kind == "narrow_resource":
        target["Resource"] = "arn:aws:iam::000000000000:role/nothing-matches-this"
    elif kind == "add_notresource_escape":
        target.pop("Resource", None)
        target["NotResource"] = "*"          # excludes everything -> deny never applies
    elif kind == "add_expiring_condition":
        target["Condition"] = {"DateLessThan": {"aws:CurrentTime": "2020-01-01T00:00:00Z"}}
    elif kind == "rename_sid":
        target["Sid"] = "RenamedDeny"
    elif kind == "delete_statement":
        out["Statement"] = [s for s in stmts if s.get("Sid") != DENY_SID]
    else:  # pragma: no cover
        raise AssertionError(kind)
    return out


@pytest.mark.parametrize("action,resource", MANDATORY_PERMANENT_DENIES,
                         ids=[a for a, _ in MANDATORY_PERMANENT_DENIES])
@pytest.mark.parametrize("kind", MUTATIONS)
def test_every_mandatory_deny_is_mutation_protected(permanent, action, resource, kind):
    """Each mutation must break the safety assertion for THIS action.

    `add_expiring_condition` is included because a Deny that lapses is not a Deny; the
    evaluated context is inside the window only for the Allow statements.
    """
    mutated = _mutate(permanent, action, kind)
    with pytest.raises(AssertionError):
        iam_eval.require_explicit_deny(mutated, action, resource, PERM_CTX, sid=DENY_SID)


def test_the_passrole_regression_is_caught(permanent):
    """THE Gate 4N-I5 regression, named explicitly so it can never silently return.

    Removing iam:PassRole from the permanent Deny left the old suite at 166/166.
    """
    broken = _mutate(permanent, "iam:PassRole", "remove_action")
    assert "iam:PassRole" not in [a for s in broken["Statement"]
                                  if s["Effect"] == "Deny" for a in s["Action"]]
    with pytest.raises(AssertionError):
        iam_eval.require_explicit_deny(broken, "iam:PassRole", "*", PERM_CTX, sid=DENY_SID)
    # and the decision must be a NON-deny, not merely "not allowed"
    assert iam_eval.decide(broken, "iam:PassRole", "*", PERM_CTX).decision is not Decision.EXPLICIT_DENY


def test_mutation_coverage_is_total_over_the_mandatory_set(permanent):
    """No mandatory action may be silently exempt from mutation testing."""
    denied = {a for s in permanent["Statement"] if s["Effect"] == "Deny" for a in s["Action"]}
    missing = [a for a, _ in MANDATORY_PERMANENT_DENIES if a not in denied]
    assert not missing, f"mandatory safety actions absent from the Deny: {missing}"


# --- the temporary operator's own ceiling -------------------------------------------


TEMP_CEILING = ["iam:PassRole", "iam:DeleteRole", "iam:CreatePolicy",
                "iam:PutRolePermissionsBoundary", "ecs:RegisterTaskDefinition",
                "ecs:CreateService", "ecs:RunTask", "secretsmanager:GetSecretValue",
                "cloudtrail:StopLogging", "cloudtrail:DeleteTrail", "sts:AssumeRole"]


@pytest.mark.parametrize("action", TEMP_CEILING)
def test_temporary_operator_ceiling_is_explicit(temporary, action):
    iam_eval.require_explicit_deny(temporary, action, "*", TEMP_CTX, sid="TempDenyEscalation")


@pytest.mark.parametrize("action", TEMP_CEILING)
def test_temporary_ceiling_is_mutation_protected(temporary, action):
    broken = copy.deepcopy(temporary)
    target = next(s for s in broken["Statement"] if s.get("Sid") == "TempDenyEscalation")
    target["Action"] = [a for a in target["Action"] if a != action]
    with pytest.raises(AssertionError):
        iam_eval.require_explicit_deny(broken, action, "*", TEMP_CTX, sid="TempDenyEscalation")


def test_temporary_ceiling_never_expires(temporary):
    """An expiring ceiling stops protecting exactly when the window is abused."""
    target = next(s for s in temporary["Statement"] if s.get("Sid") == "TempDenyEscalation")
    assert "Condition" not in target
    expired = dict(TEMP_CTX, **{"aws:CurrentTime": "2030-01-01T00:00:00Z"})
    iam_eval.require_explicit_deny(temporary, "iam:PassRole", "*", expired, sid="TempDenyEscalation")
