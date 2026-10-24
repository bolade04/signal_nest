"""P6-CI-6 key apply (PKT-K0 K0-2, freeze allowlist AL-2a, 2026-10-21): the SEPARATE, EXPIRING key-apply principal.

PR #230 added two customer-managed keys, three bucket-encryption changes, the trail key and two task-role policy
updates. The root takes no targeted apply and no reviewed principal can apply them, so ONE root apply under this
principal carries them (with the merged carry-overs: the API alias record CREATE, the budget update, api_ses_send).
Repository delivery only: nothing here provisions, applies or changes anything in AWS. The content encodes the
operator decisions of 2026-10-23: D-K1-3 = (b-wide) — kms:PutKeyPolicy allowed on "*" (expiring, region-conditioned)
for the CreateKey policy-lockout check, carved out of the flat ceiling, still explicitly denied on the two existing
CMKs and with the lockout bypass flag; D-K1-2 carve-outs as rendered; D-K1-4 <= 24 h; D-K1-5/6/7 (i). The offline
evaluator cannot show that AWS's lockout check accepts the conditioned Allow; that remains a STOP condition at apply.

What this file proves, positively AND negatively, with real request contexts:
  * the three documents (inline + two customer managed) are discovered, valid, deterministic, inside the quotas, and
    the generator refuses an oversized document, a malformed hostname and an unauthorized expiry;
  * every key-apply write is granted on its exact resource under the request context the provider sends, and is
    DENIED off-scope: other aliases, existing keys, other buckets, other trails, other roles, other zones, other record
    actions, keys with the lockout bypass or multi-region, CreateKey with other tags;
  * the must-not actions stay EXPLICITLY denied (kms:ScheduleKeyDeletion, cloudtrail:StopLogging, iam:CreateRole,
    secretsmanager:GetSecretValue, rds:ModifyDBInstance …; kms:PutKeyPolicy on the existing CMKs and with the bypass
    flag); every Allow expires; the ceiling never does;
  * the generator output matches the independently authored key_apply_closure contract section;
  * the names the scopes bind to are the ones the .tf sources declare;
  * the reviewed W0, temporary-operator and window documents are untouched (the P6-CI-6 pending-key tripwires in
    tests/test_closure_independence.py keep covering them).
"""
from __future__ import annotations

import copy
import datetime
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import expiry_authorization as _ea  # noqa: E402
import gen_operator_policies as gen  # noqa: E402
import iam_eval  # noqa: E402
import signalnest_identity as identity  # noqa: E402
from iam_eval import Decision  # noqa: E402

CONTRACT_PATH = REPO_ROOT / "infra" / "aws" / "operator-closure-contract.json"
EXPIRY = _ea.ACTIVE_EXPIRY_UTC
ISSUANCE = _ea.ACTIVE_ISSUANCE_UTC
FQDN = "api.synthetic.example.com"  # the synthetic fixture value; no real hostname exists in the repository
IN_WINDOW = "2026-08-15T06:00:00Z"
AFTER_EXPIRY = "2026-08-15T10:00:01Z"
BASE = {"aws:RequestedRegion": gen.REGION, "aws:CurrentTime": IN_WINDOW,
        "kms:BypassPolicyLockoutSafetyCheck": "false", "kms:MultiRegion": "false"}
BOUNDARY = gen.ARN["boundary"]
NEW_KEY = f"arn:aws:kms:{gen.REGION}:{gen.ACCOUNT}:key/22222222-2222-2222-2222-222222222222"


def _ctx(**extra) -> dict:
    return {**BASE, **extra}


def _shift(instant: str, **delta) -> str:
    parsed = datetime.datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ")
    return (parsed + datetime.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tags(name: str) -> dict:
    return {"aws:RequestTag/Name": name, "aws:TagKeys": gen._key_apply_names()["tag_keys"]}


def _r53(**over) -> dict:
    keys = {"route53:ChangeResourceRecordSetsNormalizedRecordNames": [FQDN],
            "route53:ChangeResourceRecordSetsRecordTypes": ["A"],
            "route53:ChangeResourceRecordSetsActions": ["CREATE"]}
    keys.update(over)
    return keys


@pytest.fixture(scope="module")
def inline() -> dict:
    return gen.key_apply_inline_policy(EXPIRY, FQDN)


@pytest.fixture(scope="module")
def reads() -> dict:
    return gen.key_apply_read_closure_policy(EXPIRY)


@pytest.fixture(scope="module")
def ceiling() -> dict:
    return gen.key_apply_deny_ceiling_policy()


@pytest.fixture(scope="module")
def effective() -> dict:
    return gen.key_apply_effective_policy(EXPIRY, FQDN)


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["key_apply_closure"]


@pytest.fixture(scope="module")
def arns() -> dict:
    return gen.key_apply_resource_arns()


def decision(policy, action, resource, ctx) -> Decision:
    return iam_eval.decide(policy, action, resource, ctx).decision


# =====================================================================================
# 0. The other reviewed principals are untouched; the new documents exist, validate and are deterministic
# =====================================================================================


def test_the_reviewed_w0_document_is_unchanged_by_this_tranche():
    import hashlib

    doc = gen.permanent_w0_policy()
    assert len(doc["Statement"]) == 23
    assert hashlib.sha256(gen.canonical(doc)).hexdigest() == \
        "17c6cf56ce85085f8c3c69a4f62c5c19efcf67f8b47d1c470eb8fda721dbae32"


def test_the_window_principal_does_not_gain_the_key_apply_writes():
    window = gen.window_transition_effective_policy(EXPIRY, FQDN)
    for action, resource in (("kms:CreateKey", "*"), ("cloudtrail:UpdateTrail", gen.ARN["trail"]),
                             ("s3:PutEncryptionConfiguration", identity.s3_bucket_arn(identity.SPA_BUCKET_NAME))):
        assert decision(window, action, resource, _ctx(**_tags(f"{gen.PREFIX}-spa-cmk"))) is not Decision.EXPLICIT_ALLOW


def test_all_three_documents_and_the_effective_composition_are_structurally_valid(inline, reads, ceiling, effective):
    for doc in (inline, reads, ceiling, effective):
        assert iam_eval.validate_policy(doc) == []
    sids = [s["Sid"] for s in effective["Statement"]]
    assert len(sids) == len(set(sids)), "Sids must be unique across the effective composition"
    assert effective["Statement"] == inline["Statement"] + reads["Statement"] + ceiling["Statement"]
    assert all(s["Sid"].startswith("KeyApply") for s in effective["Statement"])


def test_the_documents_are_discovered_and_validated_by_the_policy_inventory():
    import policy_inventory

    report = policy_inventory.validate_all()
    rows = {r["policy"]: r for r in report["rows"]}
    for name in ("key_apply_inline_policy", "key_apply_read_closure_policy",
                 "key_apply_deny_ceiling_policy", "key_apply_effective_policy"):
        row = rows.get(f"gen_operator_policies.{name}")
        assert row is not None, f"{name} is not discovered — an undiscovered policy is an unvalidated one"
        assert row["result"] == "VALID", row["problems"]
    assert rows["gen_operator_policies.key_apply_inline_policy"]["temporary"] is True
    assert rows["gen_operator_policies.key_apply_deny_ceiling_policy"]["temporary"] is False


def test_generation_is_deterministic_and_the_inputs_reach_the_bytes():
    a = gen.canonical(gen.key_apply_inline_policy(EXPIRY, FQDN))
    assert a == gen.canonical(gen.key_apply_inline_policy(EXPIRY, FQDN))
    assert a != gen.canonical(gen.key_apply_inline_policy(EXPIRY, "api2.synthetic.example.com"))
    assert a != gen.canonical(gen.key_apply_inline_policy(_shift(ISSUANCE, hours=4), FQDN))


# =====================================================================================
# 1. Quotas — measured under the counting rules, and REFUSED when exceeded
# =====================================================================================


def test_every_document_is_inside_its_quota(inline, reads, ceiling):
    sizes = gen.require_key_apply_policy_quotas(inline, reads, ceiling)["sizes"]
    assert sizes["inline"] <= gen.IAM_ROLE_INLINE_POLICY_MAX_CHARS == 10240
    assert sizes["read_closure"] <= gen.IAM_MANAGED_POLICY_MAX_CHARS == 6144
    assert sizes["deny_ceiling"] <= gen.IAM_MANAGED_POLICY_MAX_CHARS
    # the counted size is the non-whitespace characters, == UTF-8 bytes (Identity Center's rule) for ASCII text
    for doc in (inline, reads, ceiling):
        text = gen.canonical(doc).decode("utf-8")
        assert not re.search(r"\s", text)
        assert text.isascii()


def test_one_single_inline_document_would_not_fit(inline, reads, ceiling):
    """Why three documents: Identity Center allows ONE inline policy per permission set, and the whole closure exceeds
    its 10,240 non-whitespace limit (== the IAM role inline aggregate). Splitting into managed policies is the
    supported design; several inline policies on one role would not beat the aggregate."""
    merged = {"Version": "2012-10-17", "Statement": inline["Statement"] + reads["Statement"] + ceiling["Statement"]}
    assert len(gen.canonical(merged)) > gen.IAM_ROLE_INLINE_POLICY_MAX_CHARS


def test_an_oversized_document_is_refused_not_emitted(inline, reads, ceiling):
    fat = copy.deepcopy(inline)
    fat["Statement"].append({"Sid": "Pad", "Effect": "Allow", "Action": ["sts:GetCallerIdentity"],
                             "Resource": "arn:aws:iam::111122223333:role/" + "x" * 3500,
                             "Condition": {"DateLessThan": {"aws:CurrentTime": EXPIRY}}})
    with pytest.raises(ValueError, match="exceed the IAM quota"):
        gen.require_key_apply_policy_quotas(fat, reads, ceiling)
    fat_reads = copy.deepcopy(reads)
    fat_reads["Statement"].append({"Sid": "Pad", "Effect": "Allow", "Action": ["sts:GetCallerIdentity"],
                                   "Resource": "arn:aws:iam::111122223333:role/" + "x" * 700,
                                   "Condition": {"DateLessThan": {"aws:CurrentTime": EXPIRY}}})
    with pytest.raises(ValueError, match="read_closure"):
        gen.require_key_apply_policy_quotas(inline, fat_reads, ceiling)


def test_the_longest_legal_hostname_still_fits():
    label = "a" * 63
    longest = ".".join([label, label, label, "a" * 61])
    assert len(longest) == 253
    doc = gen.key_apply_inline_policy(EXPIRY, longest)
    assert len(gen.canonical(doc)) <= gen.IAM_ROLE_INLINE_POLICY_MAX_CHARS


# =====================================================================================
# 2. Inputs — fail closed
# =====================================================================================


@pytest.mark.parametrize("bad", [None, "", "<API_FQDN>", "API.Example.COM", "api.example.com.",
                                 "https://api.example.com", "api", "api..example.com"])
def test_a_malformed_or_placeholder_hostname_is_refused(bad):
    with pytest.raises(ValueError):
        gen.key_apply_inline_policy(EXPIRY, bad)


def test_the_hostname_is_required_with_no_default():
    with pytest.raises(TypeError):
        gen.key_apply_inline_policy(EXPIRY)  # type: ignore[call-arg]


@pytest.mark.parametrize("expiry", ["2020-01-01T00:00:00Z", "2099-12-31T23:59:59Z",
                                    _shift(ISSUANCE, hours=24, seconds=1), ISSUANCE,
                                    _shift(ISSUANCE, minutes=14)])
def test_an_expiry_outside_the_authorized_bound_is_refused_before_any_output(expiry):
    for build in (lambda e: gen.key_apply_inline_policy(e, FQDN),
                  gen.key_apply_read_closure_policy,
                  lambda e: gen.key_apply_effective_policy(e, FQDN)):
        with pytest.raises(_ea.ExpiryAuthorizationError):
            build(expiry)


def test_the_purpose_is_registered_and_bounded_by_the_same_twenty_four_hours():
    assert gen.KEY_APPLY_PURPOSE == "key_apply" and gen.KEY_APPLY_PURPOSE in _ea.PURPOSES
    assert _ea.MAX_DURATION == datetime.timedelta(hours=24)
    assert gen.key_apply_read_closure_policy(EXPIRY, issuance=_shift(EXPIRY, hours=-23))["Statement"]
    with pytest.raises(_ea.ExpiryAuthorizationError):
        gen.key_apply_read_closure_policy(EXPIRY, issuance=_shift(EXPIRY, hours=-25))


@pytest.mark.parametrize("bad", [None, "", "<EXPIRY-ISO8601>", "not-a-date", "2026-01-01T00:00:00", 12345])
def test_a_placeholder_or_malformed_expiry_cannot_reach_an_artifact(bad):
    with pytest.raises((ValueError, iam_eval.UnsupportedPolicyFeature, _ea.ExpiryAuthorizationError)):
        gen.key_apply_inline_policy(bad, FQDN)


# =====================================================================================
# 3. Expiry controls
# =====================================================================================


def test_every_allow_expires_and_no_deny_does(inline, reads, ceiling):
    for doc in (inline, reads):
        for s in doc["Statement"]:
            if s["Effect"] == "Allow":
                assert s["Condition"]["DateLessThan"]["aws:CurrentTime"] == EXPIRY, s["Sid"]
            else:
                assert not any(op.startswith("Date") for op in s.get("Condition", {})), s["Sid"]
    assert all(s["Effect"] == "Deny" and "Condition" not in s for s in ceiling["Statement"])


@pytest.mark.parametrize("action,resource,extra", [
    ("kms:CreateKey", "*", "spa-tags"),
    ("kms:EnableKeyRotation", NEW_KEY, None),
    ("cloudtrail:UpdateTrail", "trail", None),
    ("s3:PutObject", "state_object", None),
    ("budgets:ModifyBudget", "budget", None),
])
def test_after_expiry_every_grant_falls_to_implicit_deny(effective, arns, action, resource, extra):
    res = {"trail": arns["trail"], "state_object": gen.ARN["state_object"], "budget": arns["budget"]}.get(resource, resource)
    live = _ctx(**(_tags(f"{gen.PREFIX}-spa-cmk") if extra else {}))
    assert decision(effective, action, res, live) is Decision.EXPLICIT_ALLOW
    assert decision(effective, action, res, {**live, "aws:CurrentTime": AFTER_EXPIRY}) is Decision.IMPLICIT_DENY


# =====================================================================================
# 4. Positive grants under the provider's request context, and the off-scope denials
# =====================================================================================


def test_the_two_keys_can_be_created_tagged_rotated_and_read_after_create(effective, arns):
    for kind in ("spa", "audit"):
        tags = _tags(f"{gen.PREFIX}-{kind}-cmk")
        assert decision(effective, "kms:CreateKey", "*", _ctx(**tags)) is Decision.EXPLICIT_ALLOW, kind
        assert decision(effective, "kms:TagResource", "*", _ctx(**tags)) is Decision.EXPLICIT_ALLOW, kind
    assert decision(effective, "kms:EnableKeyRotation", NEW_KEY, _ctx()) is Decision.EXPLICIT_ALLOW
    for action in ("kms:DescribeKey", "kms:GetKeyPolicy", "kms:GetKeyRotationStatus", "kms:ListResourceTags"):
        assert decision(effective, action, NEW_KEY, _ctx()) is Decision.EXPLICIT_ALLOW, action
    assert decision(effective, "kms:ListAliases", "*", _ctx()) is Decision.EXPLICIT_ALLOW


def test_key_creation_outside_the_two_reviewed_shapes_is_refused(effective):
    good = _tags(f"{gen.PREFIX}-spa-cmk")
    assert decision(effective, "kms:CreateKey", "*", _ctx(**_tags(f"{gen.PREFIX}-other-cmk"))) is Decision.IMPLICIT_DENY
    assert decision(effective, "kms:CreateKey", "*", _ctx(**{**good, "aws:TagKeys": ["Name", "Owner", "Extra"]})) is Decision.IMPLICIT_DENY
    assert decision(effective, "kms:CreateKey", "*", _ctx(**good, **{"kms:BypassPolicyLockoutSafetyCheck": "true"})) is Decision.EXPLICIT_DENY
    assert decision(effective, "kms:CreateKey", "*", _ctx(**good, **{"kms:MultiRegion": "true"})) is Decision.EXPLICIT_DENY
    assert decision(effective, "kms:CreateKey", "*", {**_ctx(**good), "aws:RequestedRegion": "eu-west-1"}) is Decision.IMPLICIT_DENY


def test_the_two_aliases_and_only_those(effective, arns):
    for alias in arns["aliases"]:
        assert decision(effective, "kms:CreateAlias", alias, _ctx()) is Decision.EXPLICIT_ALLOW, alias
    assert decision(effective, "kms:CreateAlias", NEW_KEY, _ctx()) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "kms:CreateAlias", f"arn:aws:kms:{gen.REGION}:{gen.ACCOUNT}:alias/{gen.PREFIX}/secrets", _ctx()) is Decision.EXPLICIT_DENY
    assert decision(effective, "kms:CreateAlias", f"arn:aws:kms:{gen.REGION}:{gen.ACCOUNT}:alias/other", _ctx()) is Decision.EXPLICIT_DENY
    for key in arns["existing_keys"]:
        assert decision(effective, "kms:CreateAlias", key, _ctx()) is Decision.EXPLICIT_DENY, key


def test_the_existing_state_and_secrets_keys_cannot_be_changed(effective, arns):
    for key in arns["existing_keys"]:
        for action in ("kms:PutKeyPolicy", "kms:TagResource", "kms:UntagResource", "kms:EnableKeyRotation",
                       "kms:ScheduleKeyDeletion", "kms:DisableKey", "kms:CreateGrant"):
            assert decision(effective, action, key, _ctx(**_tags(f"{gen.PREFIX}-spa-cmk"))) is Decision.EXPLICIT_DENY, (action, key)
    assert decision(effective, "kms:Decrypt", gen.ARN["cmk_secrets"], _ctx(**{"kms:ViaService": f"s3.{gen.REGION}.amazonaws.com"})) is Decision.EXPLICIT_DENY


def test_b_wide_key_policy_write_for_the_lockout_check_and_no_destruction_on_the_new_keys(effective, arns):
    """D-K1-3 = (b-wide), operator decision 2026-10-23: kms:PutKeyPolicy is EXPLICIT_ALLOW on a new key only while the
    principal is unexpired, in the region and without the bypass flag; it stays EXPLICIT_DENY on the two existing CMKs
    and with the bypass flag; destruction and crypto on the new keys stay refused."""
    tagged = _ctx(**{"aws:ResourceTag/Name": f"{gen.PREFIX}-spa-cmk"})
    assert decision(effective, "kms:PutKeyPolicy", NEW_KEY, tagged) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "kms:PutKeyPolicy", NEW_KEY, _ctx()) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "kms:PutKeyPolicy", NEW_KEY, {**tagged, "aws:CurrentTime": AFTER_EXPIRY}) is Decision.IMPLICIT_DENY
    assert decision(effective, "kms:PutKeyPolicy", NEW_KEY, {**tagged, "aws:RequestedRegion": "eu-west-1"}) is Decision.IMPLICIT_DENY
    assert decision(effective, "kms:PutKeyPolicy", NEW_KEY, {**tagged, "kms:BypassPolicyLockoutSafetyCheck": "true"}) is Decision.EXPLICIT_DENY
    for key in arns["existing_keys"]:
        assert decision(effective, "kms:PutKeyPolicy", key, tagged) is Decision.EXPLICIT_DENY, key
        assert decision(effective, "kms:PutKeyPolicy", key, _ctx()) is Decision.EXPLICIT_DENY, key
    for action in ("kms:ScheduleKeyDeletion", "kms:DisableKey", "kms:CreateGrant"):
        assert decision(effective, action, NEW_KEY, tagged) is Decision.EXPLICIT_DENY, action
    for action in ("kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DeleteAlias"):
        assert decision(effective, action, NEW_KEY, tagged) is not Decision.EXPLICIT_ALLOW, action


def _without(policy: dict, sid: str) -> dict:
    return {**policy, "Statement": [s for s in policy["Statement"] if s["Sid"] != sid]}


def _edited(policy: dict, sid: str, **fields) -> dict:
    return {**policy, "Statement": [({**s, **fields} if s["Sid"] == sid else s) for s in policy["Statement"]]}


def test_each_b_wide_put_key_policy_control_is_load_bearing(effective, arns):
    """Falsification: each control around the b-wide grant flips its decision when removed, so none is decorative."""
    tagged = _ctx(**{"aws:ResourceTag/Name": f"{gen.PREFIX}-spa-cmk"})
    bypass = {**tagged, "kms:BypassPolicyLockoutSafetyCheck": "true"}
    secrets = gen.ARN["cmk_secrets"]
    # positive instance first: the unmutated composition
    assert decision(effective, "kms:PutKeyPolicy", secrets, tagged) is Decision.EXPLICIT_DENY
    assert decision(effective, "kms:PutKeyPolicy", NEW_KEY, bypass) is Decision.EXPLICIT_DENY
    assert decision(effective, "kms:PutKeyPolicy", NEW_KEY, tagged) is Decision.EXPLICIT_ALLOW
    # the existing-key Deny protects the secrets CMK
    assert decision(_without(effective, "KeyApplyDenyWritesOnTheExistingKeys"), "kms:PutKeyPolicy", secrets, tagged) \
        is Decision.EXPLICIT_ALLOW
    # the bypass Deny's PutKeyPolicy leg protects the new key
    assert decision(_edited(effective, "KeyApplyDenyRiskyKeyShapes", Action=["kms:CreateKey"]), "kms:PutKeyPolicy",
                    NEW_KEY, bypass) is Decision.EXPLICIT_ALLOW
    # the ceiling carve-out is what lets the Allow work at all
    ceiling = next(s for s in effective["Statement"] if s["Sid"] == "KeyApplyDenyDangerous")
    uncarved = _edited(effective, "KeyApplyDenyDangerous", Action=sorted(set(ceiling["Action"]) | {"kms:PutKeyPolicy"}))
    assert decision(uncarved, "kms:PutKeyPolicy", NEW_KEY, tagged) is Decision.EXPLICIT_DENY
    # and the new Allow is the only grant of it
    assert decision(_without(effective, "KeyApplyLockoutCheckPutKeyPolicy"), "kms:PutKeyPolicy", NEW_KEY, tagged) \
        is Decision.IMPLICIT_DENY


def test_the_lockout_check_allow_is_exactly_the_reviewed_b_wide_statement(inline):
    """The one added Allow, verbatim in shape: PutKeyPolicy only, Resource "*", the expiry and the region, nothing else;
    placed right after the alias grant; and the bypass-flag Deny covers CreateKey AND PutKeyPolicy."""
    sids = [s["Sid"] for s in inline["Statement"]]
    stmt = inline["Statement"][sids.index("KeyApplyLockoutCheckPutKeyPolicy")]
    assert sids.index("KeyApplyLockoutCheckPutKeyPolicy") == sids.index("KeyApplyCreateTheTwoAliases") + 1
    assert stmt == {"Sid": "KeyApplyLockoutCheckPutKeyPolicy", "Effect": "Allow", "Action": ["kms:PutKeyPolicy"],
                    "Resource": "*", "Condition": {"DateLessThan": {"aws:CurrentTime": EXPIRY},
                                                   "StringEquals": {"aws:RequestedRegion": gen.REGION}}}
    granting = [s["Sid"] for s in inline["Statement"] if s["Effect"] == "Allow" and "kms:PutKeyPolicy" in s["Action"]]
    assert granting == ["KeyApplyLockoutCheckPutKeyPolicy"]
    risky = inline["Statement"][sids.index("KeyApplyDenyRiskyKeyShapes")]
    assert risky["Action"] == ["kms:CreateKey", "kms:PutKeyPolicy"] and risky["Resource"] == "*"
    assert risky["Condition"] == {"Bool": {"kms:BypassPolicyLockoutSafetyCheck": "true"}}


def test_bucket_encryption_on_the_three_buckets_only(effective, arns):
    for bucket in arns["buckets"]:
        assert decision(effective, "s3:PutEncryptionConfiguration", bucket, _ctx()) is Decision.EXPLICIT_ALLOW, bucket
    for other in (gen.ARN["state_bucket"], identity.s3_bucket_arn(identity.ALB_LOGS_BUCKET_NAME), "arn:aws:s3:::someone-else"):
        assert decision(effective, "s3:PutEncryptionConfiguration", other, _ctx()) is Decision.EXPLICIT_DENY, other
    for action in ("s3:PutBucketPolicy", "s3:PutLifecycleConfiguration", "s3:DeleteBucket"):
        assert decision(effective, action, arns["buckets"][1], _ctx()) is Decision.EXPLICIT_DENY, action


def test_the_trail_key_on_the_one_trail_only(effective, arns):
    assert decision(effective, "cloudtrail:UpdateTrail", arns["trail"], _ctx()) is Decision.EXPLICIT_ALLOW
    other = f"arn:aws:cloudtrail:{gen.REGION}:{gen.ACCOUNT}:trail/other"
    assert decision(effective, "cloudtrail:UpdateTrail", other, _ctx()) is Decision.EXPLICIT_DENY
    for action in ("cloudtrail:StopLogging", "cloudtrail:DeleteTrail", "cloudtrail:PutEventSelectors"):
        assert decision(effective, action, arns["trail"], _ctx()) is Decision.EXPLICIT_DENY, action


def test_task_role_policies_on_the_two_roles_with_the_boundary_only(effective, arns):
    for role in arns["task_roles"]:
        assert decision(effective, "iam:PutRolePolicy", role, _ctx(**{"iam:PermissionsBoundary": BOUNDARY})) is Decision.EXPLICIT_ALLOW, role
        assert decision(effective, "iam:PutRolePolicy", role, _ctx(**{"iam:PermissionsBoundary": BOUNDARY + "x"})) is Decision.IMPLICIT_DENY, role
    for other in (identity.iam_role_arn(f"{gen.PREFIX}-ecs-execution"), identity.iam_role_arn(f"{gen.PREFIX}-migration-task"),
                  gen.READER_ROLE_ARNS[0]):
        assert decision(effective, "iam:PutRolePolicy", other, _ctx(**{"iam:PermissionsBoundary": BOUNDARY})) is Decision.EXPLICIT_DENY, other
    for action in ("iam:CreateRole", "iam:PassRole", "iam:DeleteRolePolicy", "iam:AttachRolePolicy"):
        assert decision(effective, action, arns["task_roles"][0], _ctx(**{"iam:PermissionsBoundary": BOUNDARY})) is Decision.EXPLICIT_DENY, action


def test_the_budget_and_the_api_alias_record_create(effective, arns):
    assert decision(effective, "budgets:ModifyBudget", arns["budget"], _ctx()) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "budgets:ModifyBudget", f"arn:aws:budgets::{gen.ACCOUNT}:budget/other", _ctx()) is Decision.IMPLICIT_DENY
    assert decision(effective, "route53:ChangeResourceRecordSets", arns["hosted_zone"], _ctx(**_r53())) is Decision.EXPLICIT_ALLOW
    for over in ({"route53:ChangeResourceRecordSetsActions": ["UPSERT"]},
                 {"route53:ChangeResourceRecordSetsActions": ["DELETE"]},
                 {"route53:ChangeResourceRecordSetsRecordTypes": ["AAAA"]},
                 {"route53:ChangeResourceRecordSetsNormalizedRecordNames": ["app.synthetic.example.com"]}):
        assert decision(effective, "route53:ChangeResourceRecordSets", arns["hosted_zone"], _ctx(**_r53(**over))) is Decision.IMPLICIT_DENY, over
    assert decision(effective, "route53:ChangeResourceRecordSets", "arn:aws:route53:::hostedzone/ZSYNTHOTHER0000000000", _ctx(**_r53())) is Decision.EXPLICIT_DENY
    assert decision(effective, "route53:GetChange", "arn:aws:route53:::change/C0SYNTHETIC", _ctx()) is Decision.EXPLICIT_ALLOW


def test_the_state_backend_is_scoped_exactly_as_for_w0(effective):
    via_s3 = {"kms:ViaService": f"s3.{gen.REGION}.amazonaws.com"}
    for action, resource in (("s3:GetObject", gen.ARN["state_object"]), ("s3:PutObject", gen.ARN["state_object"]),
                             ("dynamodb:GetItem", gen.ARN["lock"]), ("dynamodb:PutItem", gen.ARN["lock"]),
                             ("dynamodb:DeleteItem", gen.ARN["lock"])):
        assert decision(effective, action, resource, _ctx()) is Decision.EXPLICIT_ALLOW, action
    assert decision(effective, "kms:Decrypt", gen.ARN["cmk_state"], _ctx(**via_s3)) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "kms:Decrypt", gen.ARN["cmk_state"], _ctx()) is not Decision.EXPLICIT_ALLOW
    assert decision(effective, "s3:GetObject", f"{gen.ARN['state_bucket']}/other/object", _ctx()) is Decision.EXPLICIT_DENY
    assert decision(effective, "dynamodb:PutItem", f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/other", _ctx()) is Decision.EXPLICIT_DENY


@pytest.mark.parametrize("action,resource", [
    ("ec2:DeleteNatGateway", "*"), ("elasticloadbalancing:DeleteLoadBalancer", "*"),
    ("elasticache:DeleteReplicationGroup", "*"), ("rds:StopDBInstance", "*"), ("rds:ModifyDBInstance", "*"),
    ("ecs:RegisterTaskDefinition", "*"), ("ecs:UpdateService", "*"), ("cloudfront:UpdateDistribution", "*"),
    ("secretsmanager:GetSecretValue", "*"), ("sso:ProvisionPermissionSet", "*"), ("sts:AssumeRole", "*"),
    ("ec2:DeleteSecurityGroup", "*"), ("logs:FilterLogEvents", "*"),
])
def test_nothing_outside_the_key_apply_is_granted(effective, action, resource):
    assert decision(effective, action, resource, _ctx()) is not Decision.EXPLICIT_ALLOW


# =====================================================================================
# 5. The contract and the module text
# =====================================================================================


def test_the_write_groups_match_the_independent_contract_exactly(contract):
    for group, actions in gen.KEY_APPLY_WRITES.items():
        assert sorted(contract[group]) == sorted(actions), group
    authored = {g for g, v in contract.items() if not g.startswith("_") and isinstance(v, list)}
    assert authored - {"reads_widened", "scoped_capabilities", "fences", "explicit_denies"} == set(gen.KEY_APPLY_WRITES)


def test_every_granted_write_is_in_the_contract_and_vice_versa(inline, contract):
    backend = set(gen.W0_APPLY_CLOSURE["state_bucket_read"] + gen.W0_APPLY_CLOSURE["state_object_rw"]
                  + gen.W0_APPLY_CLOSURE["state_lock"] + gen.W0_APPLY_CLOSURE["state_cmk_use"])
    granted = set()
    for s in inline["Statement"]:
        if s["Effect"] == "Allow":
            granted |= set(s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    expected = {a for g, v in contract.items() if g in gen.KEY_APPLY_WRITES for a in v}
    assert granted - backend == expected


def test_the_scoped_capabilities_fences_and_denies_match_the_contract(inline, ceiling, contract):
    assert sorted(contract["scoped_capabilities"]) == sorted(gen.KEY_APPLY_SCOPED_CAPABILITIES)
    deny_sids = [s["Sid"] for s in inline["Statement"] if s["Effect"] == "Deny"]
    assert sorted(deny_sids) == sorted(contract["fences"] + contract["explicit_denies"])
    assert sorted(contract["reads_widened"]) == sorted(gen.REFRESH_CLOSURE["kms_exact"])
    flat = set(ceiling["Statement"][0]["Action"])
    assert not (flat & gen.KEY_APPLY_SCOPED_CAPABILITIES)
    # D-K1-3 = (b-wide): exactly kms:PutKeyPolicy leaves the ceiling; every other KMS member stays flatly denied
    assert "kms:PutKeyPolicy" not in flat
    assert {a for a in flat if a.startswith("kms:")} == {
        "kms:CreateGrant", "kms:DisableKey", "kms:ReEncryptFrom", "kms:ReEncryptTo", "kms:RetireGrant",
        "kms:RevokeGrant", "kms:ScheduleKeyDeletion"}


def test_the_read_closure_is_the_w0_read_closure_with_kms_widened_to_key_star(reads, arns):
    # W0's READ statements: every Allow except its apply surface (state backend + task-definition registration)
    apply_sids = {"StateBucketRead", "StateObjectReadWrite", "StateLock", "StateCmkUseViaBackendServices",
                  "TaskDefinitionFamiliesRegister"}
    w0_reads = {a for s in gen.permanent_w0_policy()["Statement"] if s["Effect"] == "Allow" and s["Sid"] not in apply_sids
                for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    key_reads = {a for s in reads["Statement"] for a in s["Action"]}
    assert key_reads == w0_reads
    kms = [s for s in reads["Statement"] if any(a.startswith("kms:") and a != "kms:ListAliases" for a in s["Action"])]
    assert len(kms) == 1 and kms[0]["Resource"] == arns["key_star"]


def test_the_names_the_scopes_bind_to_are_the_module_declarations():
    modules = REPO_ROOT / "infra" / "aws" / "modules"
    edge = (modules / "edge" / "main.tf").read_text(encoding="utf-8")
    obs = (modules / "observability" / "main.tf").read_text(encoding="utf-8")
    cost = (modules / "cost" / "main.tf").read_text(encoding="utf-8")
    iam = (modules / "iam" / "main.tf").read_text(encoding="utf-8")
    assert 'Name = "${var.name_prefix}-spa-cmk"' in edge and 'name          = "alias/${var.name_prefix}/spa"' in edge
    assert 'Name = "${var.name_prefix}-audit-cmk"' in obs and 'name          = "alias/${var.name_prefix}/audit"' in obs
    assert 'name        = "${var.name_prefix}-monthly"' in cost
    assert '"${var.name_prefix}-api-task"' in iam and '"${var.name_prefix}-worker-task"' in iam
    names = gen._key_apply_names()
    assert names["key_tag_names"] == [f"{gen.PREFIX}-spa-cmk", f"{gen.PREFIX}-audit-cmk"]
    assert names["alias_names"] == [f"alias/{gen.PREFIX}/spa", f"alias/{gen.PREFIX}/audit"]


def test_the_create_tag_keys_are_the_root_common_tags_plus_name():
    text = (REPO_ROOT / "infra" / "aws" / "locals.tf").read_text(encoding="utf-8")
    block = text[text.index("common_tags = {"):]
    block = block[:block.index("}")]
    common = set(re.findall(r"^\s*(\w+)\s*=", block, re.M)) - {"common_tags"}
    assert len(common) == 8
    assert set(gen._key_apply_names()["tag_keys"]) == common | {"Name"}


def test_the_cli_emits_and_refuses_like_the_window_emits(tmp_path):
    import subprocess
    script = REPO_ROOT / "scripts" / "gen_operator_policies.py"
    env_ok = [sys.executable, str(script), "--emit", "key-apply-inline", "--expiry", EXPIRY, "--api-fqdn", FQDN, "--hash"]
    out = subprocess.run(env_ok, capture_output=True, text=True, cwd=REPO_ROOT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("canonical  ")
    for bad in (["--emit", "key-apply-inline", "--expiry", EXPIRY],
                ["--emit", "key-apply-read-closure", "--expiry", EXPIRY, "--api-fqdn", FQDN],
                ["--emit", "key-apply-deny-ceiling", "--expiry", EXPIRY],
                ["--emit", "permanent-w0", "--api-fqdn", FQDN]):
        res = subprocess.run([sys.executable, str(script), *bad], capture_output=True, text=True, cwd=REPO_ROOT)
        assert res.returncode == 2, (bad, res.stderr)


# =====================================================================================
# 6. The ceiling, over the WHOLE forbidden universe (the allow-model idiom, applied to this principal)
# =====================================================================================


def test_every_forbidden_capability_is_explicitly_denied_everywhere_except_its_fenced_scope(effective, arns):
    """Every action in PERMANENT_DENY ∪ FORBIDDEN_CAPABILITIES is EXPLICIT_DENY at Resource "*" (the flat ceiling, or
    the NotResource fence for a scoped one); each scoped capability is EXPLICIT_ALLOW on its exact resource and
    EXPLICIT_DENY just outside it. A member silently dropped from the ceiling, or a fence that silently widens, fails."""
    from must_not_contract import FORBIDDEN_CAPABILITIES

    universe = set(gen.PERMANENT_DENY) | set(FORBIDDEN_CAPABILITIES)
    ctx = _ctx(**{"iam:PermissionsBoundary": BOUNDARY, "kms:ViaService": f"s3.{gen.REGION}.amazonaws.com"},
               **_tags(f"{gen.PREFIX}-spa-cmk"))
    not_denied = [a for a in sorted(universe) if not a.endswith("*") and decision(effective, a, "*", ctx) is not Decision.EXPLICIT_DENY]
    # D-K1-3 = (b-wide): kms:PutKeyPolicy is the ONE capability granted on "*" (no NotResource fence can name a key that
    # does not exist yet); its off-scope denials are the existing-CMK Deny and the bypass Deny, proven below and in
    # test_b_wide_key_policy_write_for_the_lockout_check_and_no_destruction_on_the_new_keys. Any other member here fails.
    assert not_denied == ["kms:PutKeyPolicy"], not_denied
    scoped = {
        "s3:GetObject": (gen.ARN["state_object"], f"{gen.ARN['state_bucket']}/other"),
        "s3:PutObject": (gen.ARN["state_object"], f"{gen.ARN['state_bucket']}/other"),
        "dynamodb:GetItem": (gen.ARN["lock"], f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/other"),
        "dynamodb:PutItem": (gen.ARN["lock"], f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/other"),
        "dynamodb:DeleteItem": (gen.ARN["lock"], f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/other"),
        "kms:Decrypt": (gen.ARN["cmk_state"], gen.ARN["cmk_secrets"]),
        "iam:PutRolePolicy": (arns["task_roles"][0], identity.iam_role_arn(f"{gen.PREFIX}-ecs-execution")),
        "cloudtrail:UpdateTrail": (arns["trail"], f"arn:aws:cloudtrail:{gen.REGION}:{gen.ACCOUNT}:trail/other"),
        "s3:PutEncryptionConfiguration": (arns["buckets"][2], gen.ARN["state_bucket"]),
        "kms:PutKeyPolicy": (NEW_KEY, gen.ARN["cmk_secrets"]),
    }
    assert set(scoped) == set(gen.KEY_APPLY_SCOPED_CAPABILITIES)
    for action, (inside, outside) in scoped.items():
        assert decision(effective, action, inside, ctx) is Decision.EXPLICIT_ALLOW, (action, inside)
        assert decision(effective, action, outside, ctx) is Decision.EXPLICIT_DENY, (action, outside)
    assert decision(effective, "kms:PutKeyPolicy", gen.ARN["cmk_state"], ctx) is Decision.EXPLICIT_DENY
    assert decision(effective, "kms:PutKeyPolicy", NEW_KEY, {**ctx, "kms:BypassPolicyLockoutSafetyCheck": "true"}) \
        is Decision.EXPLICIT_DENY


# =====================================================================================
# 7. The principal is registered in every per-principal governance analysis (close the class, not the instance)
# =====================================================================================


def test_the_allow_model_proves_the_key_apply_principal_both_directions():
    """allow_model.TARGETS carries the principal: every grant is justified by the INDEPENDENT contract (refresh,
    state backend, key_apply_closure), 0 escapes over the forbidden universe, 0 losses over its own requirement set,
    and exactly the nine forbidden capabilities it holds are exempted — each allowed in scope, denied out of scope
    (for kms:PutKeyPolicy, D-K1-3 = b-wide: out of scope = the two existing CMKs)."""
    import allow_model

    entry = allow_model.run()["policies"]["key_apply"]
    assert entry["unclassified"] == [] and entry["escapes"] == [] and entry["losses"] == []
    assert {row["action"] for row in entry["exempted"]} == set(gen.KEY_APPLY_SCOPED_CAPABILITIES) - {"s3:PutEncryptionConfiguration"}
    assert len(entry["exempted"]) == 9
    pkp = next(row for row in entry["exempted"] if row["action"] == "kms:PutKeyPolicy")
    assert pkp["in_scope_decision"] == "EXPLICIT_ALLOW"
    assert sorted(o["resource"] for o in pkp["out_of_scope"]) == sorted(gen.key_apply_resource_arns()["existing_keys"])
    assert {o["decision"] for o in pkp["out_of_scope"]} == {"EXPLICIT_DENY"}
    assert set(allow_model.key_apply_required_actions()) >= {a for v in gen.KEY_APPLY_WRITES.values() for a in v}


def test_every_key_apply_deny_statement_is_defended_by_a_probe_or_the_ceiling_proof():
    """Deleting any one of the principal's Deny statements must move a resource probe or the ceiling proof; none
    may be UNDEFENDED, and none may hide as REDUNDANT behind an implicit deny."""
    import resource_deny_probes as rdp

    rows = [m for m in rdp.statement_mutation() if m["policy"] == "key_apply"]
    effective = gen.key_apply_effective_policy(EXPIRY, FQDN)
    assert {m["deleted_statement"] for m in rows} == {s["Sid"] for s in effective["Statement"] if s["Effect"] == "Deny"}
    assert [m["deleted_statement"] for m in rows if m["classification"] != "DEFENDED"] == []
    assert [p for p in rdp.run_probes() if p["policy"] == "key_apply" and not p["ok"]] == []


def test_the_deny_analyses_and_the_classifier_include_the_principal():
    import action_classifier
    import deny_triangulation
    import putrolepolicy_classification

    assert "key_apply" in deny_triangulation.policies() and deny_triangulation.triangulate()["clean"]
    actions = action_classifier.reviewed_policy_actions()["key_apply"]
    assert [a for a in actions if action_classifier.classify(a)["conflict"]] == []
    sat = putrolepolicy_classification.run()["policy_satisfaction"]
    assert sat["key_apply_task_roles"] == ["EXPLICIT_ALLOW", "EXPLICIT_ALLOW"]
    assert sat["key_apply_other_role"] == "EXPLICIT_DENY" and sat["findings"] == []


def test_the_dead_grant_detector_covers_the_conditioned_kms_writes():
    """The CreateKey/TagResource request-tag conditions are the grants most likely to be silently dead; the
    detector now knows the keys those actions support, so an unsupported key is reported, not accepted."""
    doc = gen.key_apply_inline_policy(EXPIRY, FQDN)
    assert iam_eval.validate_policy(doc) == []
    mutated = copy.deepcopy(doc)
    stmt = next(s for s in mutated["Statement"] if s["Sid"] == "KeyApplyCreateTheTwoKeys")
    stmt["Condition"]["StringEquals"]["kms:GrantIsForAWSResource"] = "true"
    assert any("kms:GrantIsForAWSResource" in p for p in iam_eval.validate_policy(mutated))


def test_the_dead_grant_detector_covers_the_b_wide_put_key_policy_statements():
    """D-K1-3 = (b-wide): kms:PutKeyPolicy is conditioned in two statements (the lockout-check Allow and the bypass-flag
    Deny). The detector knows the keys PutKeyPolicy supports, so a Deny conditioned on a key it never populates — which
    would silently never match — is reported."""
    assert "kms:BypassPolicyLockoutSafetyCheck" in iam_eval.ACTION_CONDITION_KEYS["kms:PutKeyPolicy"]
    doc = gen.key_apply_inline_policy(EXPIRY, FQDN)
    for sid, op, key in (("KeyApplyDenyRiskyKeyShapes", "Bool", "kms:GrantIsForAWSResource"),
                         ("KeyApplyLockoutCheckPutKeyPolicy", "StringEquals", "kms:EncryptionAlgorithm")):
        mutated = copy.deepcopy(doc)
        stmt = next(s for s in mutated["Statement"] if s["Sid"] == sid)
        stmt["Condition"].setdefault(op, {})[key] = "true"
        assert any(f"kms:PutKeyPolicy does not support condition key {key}" in p for p in iam_eval.validate_policy(mutated)), sid


# =====================================================================================
# 8. The O11 requirement amendment (operator ruling 2026-10-23, "option 1"): the ONE pair (key_apply, kms:PutKeyPolicy)
#    replaces the universal Resource "*" EXPLICIT_DENY with the ruling's grounded expectations; nothing else changes.
# =====================================================================================

AMENDMENTS = REPO_ROOT / "tests" / "fixtures" / "synthetic-requirement-amendments.json"
REQUIREMENTS = REPO_ROOT / "tests" / "fixtures" / "synthetic-requirements.json"
AMENDMENT_ID = "O11-AMEND-2026-10-23-KEY-APPLY-PUTKEYPOLICY"


def _dr():
    import deny_requirements
    return deny_requirements


def _o11_problems(monkeypatch, *, documents=None, amendments=None, tmp_path=None):
    dr = _dr()
    if documents is not None:
        monkeypatch.setattr(dr, "_reviewed_documents", lambda: documents)
    if amendments is not None:
        target = tmp_path / "amendments.json"
        target.write_text(json.dumps(amendments), encoding="utf-8")
        monkeypatch.setenv("SIGNALNEST_REQUIREMENT_AMENDMENTS_PATH", str(target))
    return dr.principals_actually_deny_their_requirements()


def _docs_with_key_apply(mutate):
    docs = copy.deepcopy(_dr()._reviewed_documents())
    mutate(docs["key_apply"])
    return docs


def _stmt(policy, sid):
    return next(s for s in policy["Statement"] if s["Sid"] == sid)


def _amendment_doc():
    return json.loads(AMENDMENTS.read_text(encoding="utf-8"))


def test_o11_is_clean_with_the_ruled_amendment_and_the_original_requirement_is_intact():
    dr = _dr()
    assert dr.principals_actually_deny_their_requirements() == []
    o11 = next(e for e in json.loads(REQUIREMENTS.read_text(encoding="utf-8"))["entries"] if e["outcome_id"] == "O11")
    assert o11["principal"] == "any principal in the SignalNest design" and o11["requirement_kind"] == "permanent"
    assert "kms:PutKeyPolicy" in o11["actions"] and len(o11["actions"]) == 9
    active, problems = dr._active_amendments()
    assert problems == [] and active == {("O11", "key_apply", "kms:PutKeyPolicy"): AMENDMENT_ID}
    ruling = dr._REQUIREMENT_AMENDMENT_RULINGS["rulings"][AMENDMENT_ID]
    assert (ruling["ruling_date"], ruling["ruled_by"], ruling["principal"], ruling["action"]) == \
        ("2026-10-23", "operator", "key_apply", "kms:PutKeyPolicy")
    assert set(dr._REQUIREMENT_AMENDMENT_RULINGS["rulings"]) == {AMENDMENT_ID}


def test_without_the_amendment_record_the_universal_check_still_reports_the_pair(monkeypatch, tmp_path):
    doc = _amendment_doc()
    doc["amendments"], doc["amendment_count"] = [], 0
    problems = _o11_problems(monkeypatch, amendments=doc, tmp_path=tmp_path)
    assert problems == ["O11: principal scope is every designed principal, but key_apply resolves EXPLICIT_ALLOW for "
                        "kms:PutKeyPolicy rather than an explicit Deny"]


def test_an_external_requirement_source_never_inherits_the_repository_amendment(monkeypatch):
    monkeypatch.setenv("SIGNALNEST_REQUIREMENTS_PATH", str(REQUIREMENTS))
    monkeypatch.delenv("SIGNALNEST_REQUIREMENT_AMENDMENTS_PATH", raising=False)
    assert _dr().requirement_amendments()["amendments"] == []
    assert any("key_apply resolves EXPLICIT_ALLOW for kms:PutKeyPolicy" in p
               for p in _dr().principals_actually_deny_their_requirements())


def test_the_amended_pair_is_still_checked_not_skipped(monkeypatch):
    """Every other O11 action of key_apply keeps the "*" EXPLICIT_DENY check, and the amended pair is probed."""
    def unfence(policy):
        ceiling = _stmt(policy, "KeyApplyDenyDangerous")
        ceiling["Action"] = [a for a in ceiling["Action"] if a != "kms:ScheduleKeyDeletion"]
    problems = _o11_problems(monkeypatch, documents=_docs_with_key_apply(unfence))
    assert any("key_apply resolves" in p and "kms:ScheduleKeyDeletion rather than an explicit Deny" in p for p in problems)


@pytest.mark.parametrize("label,mutate,needle", [
    ("existing-key Deny removed",
     lambda p: p["Statement"].remove(_stmt(p, "KeyApplyDenyWritesOnTheExistingKeys")), gen.ARN["cmk_secrets"]),
    ("existing-key Deny on the state CMK only",
     lambda p: _stmt(p, "KeyApplyDenyWritesOnTheExistingKeys").update(Resource=[gen.ARN["cmk_state"]]), gen.ARN["cmk_secrets"]),
    ("existing-key Deny on a wrong ARN",
     lambda p: _stmt(p, "KeyApplyDenyWritesOnTheExistingKeys").update(
         Resource=[gen.ARN["cmk_state"], f"arn:aws:kms:{gen.REGION}:{gen.ACCOUNT}:key/99999999-9999-4999-8999-999999999999"]),
     gen.ARN["cmk_secrets"]),
    ("existing-key Deny widened to key/*",
     lambda p: _stmt(p, "KeyApplyDenyWritesOnTheExistingKeys").update(Resource=gen.key_apply_resource_arns()["key_star"]),
     "in window, in region"),
    # review r1 F1: condition mis-scoping of the protected-key Deny (kills consumer mutants that accept IMPLICIT_DENY on
    # the protected keys, or probe them only in window / in region)
    ("existing-key Deny made to expire",
     lambda p: _stmt(p, "KeyApplyDenyWritesOnTheExistingKeys").update(Condition={"DateLessThan": {"aws:CurrentTime": EXPIRY}}),
     "after expiry"),
    ("existing-key Deny made region-only",
     lambda p: _stmt(p, "KeyApplyDenyWritesOnTheExistingKeys").update(
         Condition={"StringEquals": {"aws:RequestedRegion": gen.REGION}}), "outside the region"),
    ("bypass-flag PutKeyPolicy leg removed",
     lambda p: _stmt(p, "KeyApplyDenyRiskyKeyShapes").update(Action=["kms:CreateKey"]), "bypass flag"),
    ("b-wide Allow without DateLessThan",
     lambda p: _stmt(p, "KeyApplyLockoutCheckPutKeyPolicy")["Condition"].pop("DateLessThan"), "after expiry"),
    ("b-wide Allow without RequestedRegion",
     lambda p: _stmt(p, "KeyApplyLockoutCheckPutKeyPolicy")["Condition"].pop("StringEquals"), "outside the region"),
])
def test_o11_amendment_negative_controls_on_the_documents(monkeypatch, label, mutate, needle):
    problems = _o11_problems(monkeypatch, documents=_docs_with_key_apply(mutate))
    assert any(AMENDMENT_ID in p and needle in p for p in problems), (label, problems)


@pytest.mark.parametrize("field,value", [
    ("principal", "permanent_w0"), ("principal", "window_transition"),
    ("action", "kms:ScheduleKeyDeletion"), ("action", "kms:CreateGrant"),
    ("amends_outcome_id", "O10"), ("ruling_date", "2026-10-24"), ("ruled_by", "coordinator"),
    ("ruling_sha256", "0" * 64), ("amends_action_set_sha256", "0" * 64), ("amendment_id", "O11-AMEND-OTHER"),
])
def test_the_exception_cannot_be_moved_or_reattributed(monkeypatch, tmp_path, field, value):
    doc = _amendment_doc()
    doc["amendments"][0][field] = value
    problems = _o11_problems(monkeypatch, amendments=doc, tmp_path=tmp_path)
    assert any("not applied" in p for p in problems), (field, problems)
    # and since nothing is applied, the original universal finding for key_apply's PutKeyPolicy returns
    assert any("key_apply resolves EXPLICIT_ALLOW for kms:PutKeyPolicy rather than an explicit Deny" in p for p in problems)


def test_an_exception_for_another_principal_is_refused_even_alongside_the_ruled_one(monkeypatch, tmp_path):
    doc = _amendment_doc()
    other = copy.deepcopy(doc["amendments"][0])
    other["principal"] = "window_transition"
    doc["amendments"].append(other)
    doc["amendment_count"] = 2
    problems = _o11_problems(monkeypatch, amendments=doc, tmp_path=tmp_path)
    assert any("duplicate amendment_id" in p for p in problems)


@pytest.mark.parametrize("field", ["amendment_id", "ruling_date", "ruled_by", "ruling_record", "ruling_sha256",
                                   "status", "replaces", "retains", "context", "amends_action_set_sha256"])
def test_an_amendment_with_missing_provenance_is_refused(monkeypatch, tmp_path, field):
    doc = _amendment_doc()
    doc["amendments"][0][field] = [] if field == "retains" else ""
    problems = _o11_problems(monkeypatch, amendments=doc, tmp_path=tmp_path)
    assert any("missing provenance" in p and field in p for p in problems), (field, problems)


def test_a_superseded_amendment_applies_nothing(monkeypatch, tmp_path):
    doc = _amendment_doc()
    doc["amendments"][0]["status"] = "SUPERSEDED"
    problems = _o11_problems(monkeypatch, amendments=doc, tmp_path=tmp_path)
    assert any("key_apply resolves EXPLICIT_ALLOW for kms:PutKeyPolicy" in p for p in problems)


def test_the_amendment_record_declares_its_size_and_states_exactly_the_authored_fields():
    """Every field of the record is either machine-checked against the authored ruling (ids, provenance, replaces,
    retains, context) or against the loaded requirement source (amends_action_set_sha256), or is the closed status."""
    doc = _amendment_doc()
    assert doc["amendment_count"] == len(doc["amendments"]) == 1
    assert set(doc["amendments"][0]) == set(_dr()._REQUIREMENT_AMENDMENT_RULINGS["provenance_fields"])
    ruling = _dr()._REQUIREMENT_AMENDMENT_RULINGS["rulings"][AMENDMENT_ID]
    rec = doc["amendments"][0]
    assert (rec["replaces"], tuple(rec["retains"]), rec["context"]) == (ruling["replaces"], ruling["retains"], ruling["context"])
    assert "Resource \"*\"" in rec["replaces"]


@pytest.mark.parametrize("field,value", [
    ("retains", ["nothing is retained"]), ("retains", None), ("replaces", "every principal, every action"),
    ("context", "unconditional"),
])
def test_the_amendment_narrative_must_equal_the_authored_ruling(monkeypatch, tmp_path, field, value):
    doc = _amendment_doc()
    doc["amendments"][0][field] = value
    problems = _o11_problems(monkeypatch, amendments=doc, tmp_path=tmp_path)
    assert any("not applied" in p and field in p for p in problems), (field, problems)


@pytest.mark.parametrize("extra", [{"principals": ["permanent_w0"]}, {"actions": ["kms:ScheduleKeyDeletion"]}, {"note": "x"}])
def test_an_amendment_with_an_unknown_field_is_refused(monkeypatch, tmp_path, extra):
    doc = _amendment_doc()
    doc["amendments"][0].update(extra)
    problems = _o11_problems(monkeypatch, amendments=doc, tmp_path=tmp_path)
    assert any("unknown field" in p and "not applied" in p for p in problems), problems
    assert any("key_apply resolves EXPLICIT_ALLOW for kms:PutKeyPolicy" in p for p in problems)


def test_an_external_source_refuses_an_amendment_record_inside_the_repository(monkeypatch, tmp_path):
    """review r1 F3: with an external requirement source, even an EXPLICIT amendments path into the repository (the
    tracked fixture) is refused, fail-closed; a copy outside the repository is accepted."""
    external = tmp_path / "external-requirements.json"
    external.write_text(REQUIREMENTS.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("SIGNALNEST_REQUIREMENTS_PATH", str(external))
    monkeypatch.setenv("SIGNALNEST_REQUIREMENT_AMENDMENTS_PATH", str(AMENDMENTS))
    problems = _dr().principals_actually_deny_their_requirements()
    assert any("resolves inside the repository" in p for p in problems), problems
    assert any("key_apply resolves EXPLICIT_ALLOW for kms:PutKeyPolicy" in p for p in problems)
    # the same record read from OUTSIDE the repository applies
    outside = tmp_path / "amendments.json"
    outside.write_text(AMENDMENTS.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("SIGNALNEST_REQUIREMENT_AMENDMENTS_PATH", str(outside))
    assert _dr().principals_actually_deny_their_requirements() == []


def test_the_requirements_guard_fails_on_an_unruled_amendment(tmp_path):
    import os
    import subprocess
    doc = _amendment_doc()
    doc["amendments"][0]["principal"] = "permanent_w0"
    target = tmp_path / "amendments.json"
    target.write_text(json.dumps(doc), encoding="utf-8")
    proc = subprocess.run([sys.executable, "scripts/deny_requirements.py"], cwd=REPO_ROOT, capture_output=True,
                          text=True, env={**os.environ, "SIGNALNEST_REQUIREMENT_AMENDMENTS_PATH": str(target)})
    assert proc.returncode == 1 and "not applied" in proc.stderr
