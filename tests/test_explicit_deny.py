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

DENY_SID = "DenyDangerous"
FENCE_STATE_OBJECT = "DenyStateObjectAccessOutsideTheStateObject"
FENCE_LOCK = "DenyLockItemsOutsideTheLockTable"
FENCE_STATE_CMK = "DenyStateCmkUseOutsideTheStateCmk"
FENCE_TASK_DEF = "DenyTaskDefinitionRegistrationOutsideTheFamilies"

OTHER_TABLE = f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/some-other-table"
AUDIT_OBJECT = gen.ARN["audit_bucket"] + "/AWSLogs/o.json.gz"

# The mandatory safety set: (action, resource, sid). Every entry must be an EXPLICIT Deny
# from the named Sid, and every entry is mutation-tested below. Adding an action here
# without it being denied breaks the suite, which is the point.
#
# INFRA-9 B-3 (2026-08-16), the governance re-authoring the ownership sweep called HAZARD 2:
# permanent W0 became the APPLY IDENTITY, so the state-object/lock/decrypt/registration
# capabilities are carved out of the flat DenyDangerous ceiling and re-denied by NotResource
# FENCES. The carved rows therefore assert the fence Sid at an OUT-OF-SCOPE resource — the
# safety property is now "denied everywhere EXCEPT the exact backend resource", and the
# in-scope allows are asserted positively in tests/test_operator_policies.py.
MANDATORY_PERMANENT_DENIES = [
    ("iam:PassRole", "*", DENY_SID),
    ("iam:CreateRole", gen.READER_ROLE_ARNS[0], DENY_SID),
    ("iam:PutRolePolicy", gen.READER_ROLE_ARNS[0], DENY_SID),
    ("iam:DeleteRole", gen.READER_ROLE_ARNS[0], DENY_SID),
    ("iam:DeleteRolePolicy", gen.READER_ROLE_ARNS[0], DENY_SID),
    ("iam:TagRole", gen.READER_ROLE_ARNS[0], DENY_SID),
    ("iam:UntagRole", gen.READER_ROLE_ARNS[0], DENY_SID),
    ("iam:PutRolePermissionsBoundary", gen.READER_ROLE_ARNS[0], DENY_SID),
    ("iam:UpdateAssumeRolePolicy", gen.READER_ROLE_ARNS[0], DENY_SID),
    ("cloudtrail:StopLogging", gen.ARN["trail"], DENY_SID),
    ("cloudtrail:DeleteTrail", gen.ARN["trail"], DENY_SID),
    ("cloudtrail:UpdateTrail", gen.ARN["trail"], DENY_SID),
    ("cloudtrail:PutEventSelectors", gen.ARN["trail"], DENY_SID),
    # B-3 carved: state-object read/write is FENCED — denied at every object except the
    # exact state object.
    ("s3:GetObject", AUDIT_OBJECT, FENCE_STATE_OBJECT),
    ("s3:PutObject", AUDIT_OBJECT, FENCE_STATE_OBJECT),
    # Deletion and version reads stay flatly denied, INCLUDING at the state object itself.
    ("s3:DeleteObject", STATE_OBJECT, DENY_SID),
    ("s3:GetObjectVersion", STATE_OBJECT, DENY_SID),
    ("s3:PutBucketPolicy", gen.ARN["state_bucket"], DENY_SID),
    ("s3:PutBucketVersioning", gen.ARN["state_bucket"], DENY_SID),
    # B-3 carved: lock items are FENCED — denied at every table except the lock table.
    ("dynamodb:GetItem", OTHER_TABLE, FENCE_LOCK),
    ("dynamodb:PutItem", OTHER_TABLE, FENCE_LOCK),
    ("dynamodb:DeleteItem", OTHER_TABLE, FENCE_LOCK),
    # UpdateItem stays flatly denied, INCLUDING at the lock table (adjudicated unused).
    ("dynamodb:UpdateItem", LOCK, DENY_SID),
    ("kms:ScheduleKeyDeletion", gen.ARN["cmk_state"], DENY_SID),
    ("kms:DisableKey", gen.ARN["cmk_state"], DENY_SID),
    ("kms:CreateGrant", gen.ARN["cmk_secrets"], DENY_SID),
    ("kms:PutKeyPolicy", gen.ARN["cmk_secrets"], DENY_SID),
    # B-3 carved: decrypt is FENCED — denied at every key except the state CMK (the secrets
    # CMK protects the database credential).
    ("kms:Decrypt", gen.ARN["cmk_secrets"], FENCE_STATE_CMK),
    ("kms:GenerateDataKey", gen.ARN["cmk_secrets"], FENCE_STATE_CMK),
    ("secretsmanager:GetSecretValue", "*", DENY_SID),
    ("secretsmanager:PutSecretValue", "*", DENY_SID),
    # B-3 carved: registration is FENCED — the "*" probe matches the fence because "*" is
    # not one of the four family ARNs, so the universal-invariant posture is preserved.
    ("ecs:RegisterTaskDefinition", "*", FENCE_TASK_DEF),
    ("ecs:TagResource", "*", FENCE_TASK_DEF),
    ("ecs:CreateService", "*", DENY_SID),
    ("ecs:RunTask", "*", DENY_SID),
    ("sts:AssumeRole", "*", DENY_SID),
]


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


@pytest.mark.parametrize("action,resource,sid", MANDATORY_PERMANENT_DENIES,
                         ids=[f"{a}@{r.rsplit(':', 1)[-1][:24]}" for a, r, _ in MANDATORY_PERMANENT_DENIES])
def test_permanent_w0_explicitly_denies(permanent, action, resource, sid):
    """EXPLICIT_DENY from the named Sid — implicit denial fails this test."""
    iam_eval.require_explicit_deny(permanent, action, resource, PERM_CTX, sid=sid)


def test_the_deny_statement_is_unconditional_and_global(permanent):
    """A conditioned or scoped safety Deny can fail open when the key or ARN shifts."""
    deny = [s for s in permanent["Statement"] if s.get("Sid") == DENY_SID]
    assert len(deny) == 1
    assert "Condition" not in deny[0]
    assert deny[0]["Resource"] == "*"
    assert "NotAction" not in deny[0] and "NotResource" not in deny[0]


# INFRA-9 B-3: fence sid -> the Allow sid whose exact Resource scope it must mirror. This is
# the shape contract for the carve-outs: a fence that widens beyond its paired Allow's scope
# breaks a legitimate capability; one that narrows leaves the capability implicit elsewhere.
FENCE_PAIRING = {
    FENCE_STATE_OBJECT: "StateObjectReadWrite",
    FENCE_LOCK: "StateLock",
    FENCE_STATE_CMK: "StateCmkUseViaBackendServices",
    FENCE_TASK_DEF: "TaskDefinitionFamiliesRegister",
}


def test_every_fence_is_unconditional_and_mirrors_its_allow_scope(permanent):
    """Each NotResource fence must carve EXACTLY the resources its paired Allow grants.

    Part of the B-3 governance re-authoring: the old single-Deny shape assertion survives
    for the flat ceiling above; this is the new shape assertion for the fences.
    """
    by_sid = {s.get("Sid"): s for s in permanent["Statement"]}
    deny_sids = [s.get("Sid") for s in permanent["Statement"] if s["Effect"] == "Deny"]
    assert deny_sids == [DENY_SID, *FENCE_PAIRING], (
        "the Deny statements must be exactly the flat ceiling plus the four B-3 fences")
    for fence_sid, allow_sid in FENCE_PAIRING.items():
        fence, allow = by_sid[fence_sid], by_sid[allow_sid]
        assert "Condition" not in fence, fence_sid
        assert "Resource" not in fence and "NotAction" not in fence, fence_sid
        assert fence["NotResource"] == allow["Resource"], (
            f"{fence_sid} must carve exactly what {allow_sid} grants")
        fence_actions = set(fence["Action"] if isinstance(fence["Action"], list) else [fence["Action"]])
        allow_actions = set(allow["Action"] if isinstance(allow["Action"], list) else [allow["Action"]])
        assert fence_actions <= allow_actions, (
            f"{fence_sid} fences an action {allow_sid} does not grant")


def test_a_competing_allow_cannot_override_the_safety_deny(permanent):
    """Explicit Deny must win even against a maximally broad Allow."""
    for action, resource, _sid in MANDATORY_PERMANENT_DENIES:
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


def _mutate(policy: dict, action: str, kind: str, sid: str = DENY_SID) -> dict:
    out = copy.deepcopy(policy)
    stmts = out["Statement"]
    target = next(s for s in stmts if s.get("Sid") == sid)
    fence = "NotResource" in target
    if kind == "remove_action":
        target["Action"] = [a for a in target["Action"] if a != action]
    elif kind == "misspell_action":
        target["Action"] = [a.replace(action, action + "X") for a in target["Action"]]
    elif kind == "flip_effect_to_allow":
        target["Effect"] = "Allow"
    elif kind == "narrow_resource":
        # For the flat ceiling: scope it to an ARN nothing matches. For a fence the
        # equivalent evasion is carving out the PROBED resource — the fence still exists
        # but no longer denies the thing the row protects.
        if fence:
            existing = target["NotResource"]
            existing = existing if isinstance(existing, list) else [existing]
            target["NotResource"] = existing + ["*"]
        else:
            target["Resource"] = "arn:aws:iam::000000000000:role/nothing-matches-this"
    elif kind == "add_notresource_escape":
        target.pop("Resource", None)
        target["NotResource"] = "*"          # excludes everything -> deny never applies
    elif kind == "add_expiring_condition":
        target["Condition"] = {"DateLessThan": {"aws:CurrentTime": "2020-01-01T00:00:00Z"}}
    elif kind == "rename_sid":
        target["Sid"] = "RenamedDeny"
    elif kind == "delete_statement":
        out["Statement"] = [s for s in stmts if s.get("Sid") != sid]
    else:  # pragma: no cover
        raise AssertionError(kind)
    return out


@pytest.mark.parametrize("action,resource,sid", MANDATORY_PERMANENT_DENIES,
                         ids=[a for a, _, _ in MANDATORY_PERMANENT_DENIES])
@pytest.mark.parametrize("kind", MUTATIONS)
def test_every_mandatory_deny_is_mutation_protected(permanent, action, resource, sid, kind):
    """Each mutation must break the safety assertion for THIS action.

    `add_expiring_condition` is included because a Deny that lapses is not a Deny; the
    evaluated context is inside the window only for the Allow statements. For fence rows the
    mutations target the FENCE statement — the carved capability's only out-of-scope control.
    """
    mutated = _mutate(permanent, action, kind, sid=sid)
    with pytest.raises(AssertionError):
        iam_eval.require_explicit_deny(mutated, action, resource, PERM_CTX, sid=sid)


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
    missing = [a for a, _, _ in MANDATORY_PERMANENT_DENIES if a not in denied]
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
