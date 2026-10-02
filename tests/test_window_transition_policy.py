"""P6-W0-TRANSITION (2026-10-02): the SEPARATE, EXPIRING window-transition principal.

Operator selections D1 = A (restore W0 to the reviewed baseline; a separate window principal), D3 (per-window
expiry ≤ 24 h, no plan/apply START within 3 h of expiry, 12 h session), D4 (RDS stop/start inside the window
principal), D5 (the window principal carries the ONE boundary-conditioned API role-policy write). Repository
delivery only: nothing here provisions, restores or changes anything in AWS.

What this file proves, positively AND negatively, with real request contexts:
  * the reviewed W0 document is unchanged byte-for-byte by this tranche;
  * the three emitted documents are discovered, structurally valid, deterministic, inside the quotas, and
    the generator REFUSES an unprovisionable (oversized) document, a malformed/placeholder hostname, an
    unauthorized expiry (longer than 24 h after the issuance, at or before the issuance, shorter than 15 min —
    the authorization is bound to the DECLARED issuance, never to the wall clock) and a missing input;
  * every window write is granted on its exact resource under the request context the provider will send,
    and DENIED (not merely unmatched) off-scope: wrong zone, wrong record name/type/action, wrong role,
    wrong boundary, missing boundary, wrong Name tag, other EIP, secrets CMK, other table, other object;
  * the forbidden capabilities stay EXPLICITLY denied (incl. ecs:RegisterTaskDefinition, rds:CreateDBSnapshot,
    rds:ModifyDBInstance, iam:CreateRole, cloudtrail:StopLogging); the ceiling never expires; every Allow does;
  * the generator output matches the independently authored window_transition_closure contract section;
  * the module-deterministic names the scopes bind to are the ones the .tf sources declare;
  * the evaluator's new dead-grant knowledge fires on the exact defect the sealed review found (an
    ElastiCache encryption Bool on the delete path) and on a templated tag key AWS never populates.
"""
from __future__ import annotations

import copy
import datetime
import json
import re
import subprocess
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
IN_WINDOW = "2026-08-15T06:00:00Z"   # inside the ACTIVE pair
AFTER_EXPIRY = "2026-08-15T10:00:01Z"
BASE = {"aws:RequestedRegion": gen.REGION, "aws:CurrentTime": IN_WINDOW}
BOUNDARY = gen.ARN["boundary"]


def _ctx(**extra) -> dict:
    return {**BASE, **extra}


def _shift(instant: str, **delta) -> str:
    parsed = datetime.datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ")
    return (parsed + datetime.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def inline() -> dict:
    return gen.window_transition_inline_policy(EXPIRY, FQDN)


@pytest.fixture(scope="module")
def reads() -> dict:
    return gen.window_transition_read_closure_policy(EXPIRY)


@pytest.fixture(scope="module")
def ceiling() -> dict:
    return gen.window_transition_deny_ceiling_policy()


@pytest.fixture(scope="module")
def effective() -> dict:
    return gen.window_transition_effective_policy(EXPIRY, FQDN)


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["window_transition_closure"]


@pytest.fixture(scope="module")
def arns() -> dict:
    return gen.window_resource_arns()


def decision(policy, action, resource, ctx) -> Decision:
    return iam_eval.decide(policy, action, resource, ctx).decision


# =====================================================================================
# 0. The reviewed W0 is untouched; the new documents exist, validate and are deterministic
# =====================================================================================


def test_the_reviewed_w0_document_is_unchanged_by_this_tranche():
    """The sealed preparation set recorded the synthetic render of permanent_w0_policy at main
    52f2bb96 (canonical sha256 17c6cf56…, 23 statements). Restoring W0 means restoring THAT document;
    this tranche may add builders beside it but must not move a byte of it."""
    import hashlib

    doc = gen.permanent_w0_policy()
    assert len(doc["Statement"]) == 23
    assert hashlib.sha256(gen.canonical(doc)).hexdigest() == \
        "17c6cf56ce85085f8c3c69a4f62c5c19efcf67f8b47d1c470eb8fda721dbae32"


def test_all_three_documents_and_the_effective_composition_are_structurally_valid(inline, reads, ceiling, effective):
    for doc in (inline, reads, ceiling, effective):
        assert iam_eval.validate_policy(doc) == []
    sids = [s["Sid"] for s in effective["Statement"]]
    assert len(sids) == len(set(sids)), "Sids must be unique across the effective composition"
    assert effective["Statement"] == inline["Statement"] + reads["Statement"] + ceiling["Statement"]


def test_the_documents_are_discovered_and_validated_by_the_policy_inventory():
    import policy_inventory

    report = policy_inventory.validate_all()
    rows = {r["policy"]: r for r in report["rows"]}
    for name in ("window_transition_inline_policy", "window_transition_read_closure_policy",
                 "window_transition_deny_ceiling_policy", "window_transition_effective_policy"):
        row = rows.get(f"gen_operator_policies.{name}")
        assert row is not None, f"{name} is not discovered — an undiscovered policy is an unvalidated one"
        assert row["result"] == "VALID", row["problems"]
    assert rows["gen_operator_policies.window_transition_inline_policy"]["temporary"] is True
    assert rows["gen_operator_policies.window_transition_deny_ceiling_policy"]["temporary"] is False


def test_generation_is_deterministic_and_the_inputs_reach_the_bytes():
    a = gen.canonical(gen.window_transition_inline_policy(EXPIRY, FQDN))
    assert a == gen.canonical(gen.window_transition_inline_policy(EXPIRY, FQDN))
    assert a != gen.canonical(gen.window_transition_inline_policy(EXPIRY, "api2.synthetic.example.com"))
    early = _shift(ISSUANCE, hours=4)
    assert a != gen.canonical(gen.window_transition_inline_policy(early, FQDN))


# =====================================================================================
# 1. Quotas — measured, and REFUSED when exceeded
# =====================================================================================


def test_every_document_is_inside_its_quota(inline, reads, ceiling):
    sizes = gen.require_window_policy_quotas(inline, reads, ceiling)["sizes"]
    # m4: the read-closure and ceiling builders gate their own size too (not only the inline path)
    assert gen.require_window_policy_quotas(inline, gen.window_transition_read_closure_policy(EXPIRY), gen.window_transition_deny_ceiling_policy())["sizes"] == sizes
    assert sizes["inline"] <= gen.IAM_ROLE_INLINE_POLICY_MAX_CHARS == 10240
    assert sizes["read_closure"] <= gen.IAM_MANAGED_POLICY_MAX_CHARS == 6144
    assert sizes["deny_ceiling"] <= gen.IAM_MANAGED_POLICY_MAX_CHARS
    # the whitespace-free canonical form IS the IAM-counted size
    assert " " not in gen.canonical(inline).decode() and "\n" not in gen.canonical(inline).decode()


def test_an_oversized_document_is_refused_not_emitted(inline, reads, ceiling):
    fat = copy.deepcopy(inline)
    fat["Statement"].append({"Sid": "Pad", "Effect": "Allow", "Action": ["sts:GetCallerIdentity"],
                             "Resource": "arn:aws:iam::111122223333:role/" + "x" * 700,
                             "Condition": {"DateLessThan": {"aws:CurrentTime": EXPIRY}}})
    with pytest.raises(ValueError, match="exceed the IAM quota"):
        gen.require_window_policy_quotas(fat, reads, ceiling)
    fat_reads = copy.deepcopy(reads)
    fat_reads["Statement"].append({"Sid": "Pad", "Effect": "Allow", "Action": ["sts:GetCallerIdentity"],
                                   "Resource": "arn:aws:iam::111122223333:role/" + "x" * 400,
                                   "Condition": {"DateLessThan": {"aws:CurrentTime": EXPIRY}}})
    with pytest.raises(ValueError, match="read_closure"):
        gen.require_window_policy_quotas(inline, fat_reads, ceiling)


def test_the_longest_legal_hostname_still_fits():
    """A 253-character api_fqdn is legal for the root; the inline document must still fit the quota
    (measured: it does — pinned so a later growth of the inline document is noticed here, not at provisioning)."""
    label = "a" * 63
    longest = ".".join([label, label, label, "a" * 61])  # 253 chars
    assert len(longest) == 253
    doc = gen.window_transition_inline_policy(EXPIRY, longest)
    assert len(gen.canonical(doc)) <= gen.IAM_ROLE_INLINE_POLICY_MAX_CHARS
    assert len(gen.canonical(doc)) > len(gen.canonical(gen.window_transition_inline_policy(EXPIRY, FQDN)))


# =====================================================================================
# 2. Inputs — fail closed
# =====================================================================================


@pytest.mark.parametrize("bad", [None, "", "<API_FQDN>", "API.Example.COM", "api.example.com.",
                                 "https://api.example.com", "api", "api..example.com", "-api.example.com",
                                 "api_x.example.com", "a" * 64 + ".example.com"])
def test_a_malformed_or_placeholder_hostname_is_refused(bad):
    with pytest.raises(ValueError):
        gen.window_transition_inline_policy(EXPIRY, bad)


def test_the_hostname_is_required_with_no_default():
    with pytest.raises(TypeError):
        gen.window_transition_inline_policy(EXPIRY)  # type: ignore[call-arg]


@pytest.mark.parametrize("expiry", ["2020-01-01T00:00:00Z", "2099-12-31T23:59:59Z",
                                    _shift(ISSUANCE, hours=24, seconds=1), ISSUANCE,
                                    _shift(ISSUANCE, minutes=14)])
def test_a_window_outside_the_authorized_bound_is_refused_before_any_output(expiry):
    for build in (lambda e: gen.window_transition_inline_policy(e, FQDN),
                  gen.window_transition_read_closure_policy,
                  lambda e: gen.window_transition_effective_policy(e, FQDN)):
        with pytest.raises(_ea.ExpiryAuthorizationError):
            build(expiry)


def test_the_purpose_is_registered_and_bounded_by_the_same_twenty_four_hours():
    assert gen.WINDOW_PRINCIPAL_PURPOSE in _ea.PURPOSES
    assert _ea.MAX_DURATION == datetime.timedelta(hours=24)
    # a per-window re-stamp: an explicit issuance 23 h before the expiry is accepted …
    issuance = _shift(EXPIRY, hours=-23)
    assert gen.window_transition_read_closure_policy(EXPIRY, issuance=issuance)["Statement"]
    # … and 25 h is not, however the issuance is supplied
    with pytest.raises(_ea.ExpiryAuthorizationError):
        gen.window_transition_read_closure_policy(EXPIRY, issuance=_shift(EXPIRY, hours=-25))


@pytest.mark.parametrize("bad", [None, "", "<EXPIRY-ISO8601>", "not-a-date", "2026-01-01T00:00:00", 12345])
def test_a_placeholder_or_malformed_expiry_cannot_reach_an_artifact(bad):
    with pytest.raises((ValueError, iam_eval.UnsupportedPolicyFeature, _ea.ExpiryAuthorizationError)):
        gen.window_transition_inline_policy(bad, FQDN)


def test_the_no_start_rule_is_three_hours_before_expiry():
    assert gen.WINDOW_NO_START_BEFORE_EXPIRY == datetime.timedelta(hours=3)
    assert gen.latest_window_start(EXPIRY) == _shift(EXPIRY, hours=-3)
    assert gen.WINDOW_SESSION_DURATION == "PT12H"


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


@pytest.mark.parametrize("action,resource_key", [
    ("ec2:DeleteNatGateway", "natgateway"), ("elasticloadbalancing:DeleteLoadBalancer", "load_balancer"),
    ("rds:StopDBInstance", "db"), ("s3:PutObject", None), ("budgets:ModifyBudget", "budget"),
])
def test_after_expiry_every_grant_falls_to_implicit_deny(effective, arns, action, resource_key):
    resource = gen.ARN["state_object"] if resource_key is None else arns[resource_key]
    live_ctx = _ctx(**{"ec2:ResourceTag/Name": f"{gen.PREFIX}-nat"})
    expired_ctx = {**live_ctx, "aws:CurrentTime": AFTER_EXPIRY}
    assert decision(effective, action, resource, live_ctx) is Decision.EXPLICIT_ALLOW
    assert decision(effective, action, resource, expired_ctx) is Decision.IMPLICIT_DENY


def test_removing_an_expiry_from_a_statement_is_detected(inline):
    broken = copy.deepcopy(inline)
    for s in broken["Statement"]:
        if s["Sid"] == "WinStateObjectReadWrite":
            s.pop("Condition")
    expired = {**BASE, "aws:CurrentTime": AFTER_EXPIRY}
    assert decision(broken, "s3:PutObject", gen.ARN["state_object"], expired) is Decision.EXPLICIT_ALLOW
    assert decision(inline, "s3:PutObject", gen.ARN["state_object"], expired) is Decision.IMPLICIT_DENY


# =====================================================================================
# 4. Positive grants under the provider's request context, and the off-scope denials
# =====================================================================================


def test_network_open_and_close_grants(effective, arns):
    p = gen.PREFIX
    assert decision(effective, "ec2:AllocateAddress", arns["elastic_ip"], _ctx(**{"aws:RequestTag/Name": f"{p}-nat-eip"})) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "ec2:AllocateAddress", arns["elastic_ip"], _ctx(**{"aws:RequestTag/Name": "something-else"})) is Decision.IMPLICIT_DENY
    assert decision(effective, "ec2:CreateNatGateway", arns["natgateway"], _ctx(**{"aws:RequestTag/Name": f"{p}-nat"})) is Decision.EXPLICIT_ALLOW
    for leg, tag in (("subnet", f"{p}-public-a"), ("elastic_ip", f"{p}-nat-eip"), ("vpc", f"{p}-vpc")):
        assert decision(effective, "ec2:CreateNatGateway", arns[leg], _ctx(**{"ec2:ResourceTag/Name": tag})) is Decision.EXPLICIT_ALLOW, leg
    assert decision(effective, "ec2:CreateNatGateway", arns["subnet"], _ctx(**{"ec2:ResourceTag/Name": f"{p}-private-a"})) is Decision.IMPLICIT_DENY
    assert decision(effective, "ec2:CreateTags", arns["elastic_ip"], _ctx(**{"ec2:CreateAction": "AllocateAddress"})) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "ec2:CreateTags", arns["elastic_ip"], _ctx(**{"ec2:CreateAction": "RunInstances"})) is Decision.IMPLICIT_DENY
    assert decision(effective, "ec2:CreateTags", arns["route_table"], _ctx(**{"ec2:CreateAction": "AllocateAddress"})) is Decision.IMPLICIT_DENY
    for action in ("ec2:CreateRoute", "ec2:DeleteRoute"):
        assert decision(effective, action, arns["route_table"], _ctx(**{"ec2:ResourceTag/Name": f"{p}-private-rt"})) is Decision.EXPLICIT_ALLOW
        assert decision(effective, action, arns["route_table"], _ctx(**{"ec2:ResourceTag/Name": f"{p}-public-rt"})) is Decision.IMPLICIT_DENY
    assert decision(effective, "ec2:DeleteNatGateway", arns["natgateway"], _ctx(**{"ec2:ResourceTag/Name": f"{p}-nat"})) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "ec2:ReleaseAddress", arns["elastic_ip"], _ctx(**{"ec2:ResourceTag/Name": f"{p}-nat-eip"})) is Decision.EXPLICIT_ALLOW
    # the ALB-service-managed second address carries NO Name tag: never releasable or disassociable by this principal
    assert decision(effective, "ec2:ReleaseAddress", arns["elastic_ip"], _ctx()) is Decision.MISSING_CONTEXT
    assert decision(effective, "ec2:DisassociateAddress", arns["elastic_ip"], _ctx(**{"ec2:ResourceTag/Name": f"{p}-nat-eip"})) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "ec2:DisassociateAddress", arns["elastic_ip"], _ctx(**{"ec2:ResourceTag/Name": "other"})) is Decision.IMPLICIT_DENY
    assert decision(effective, "ec2:DisassociateAddress", arns["network_interface"], _ctx()) is Decision.EXPLICIT_ALLOW
    # the persistent networking is never writable
    for action in ("ec2:DeleteVpc", "ec2:DeleteSubnet", "ec2:AuthorizeSecurityGroupIngress", "ec2:DeleteNetworkInterface",
                   "ec2:DetachNetworkInterface", "ec2:CreateVpc"):
        assert decision(effective, action, "*", _ctx()) is Decision.IMPLICIT_DENY, action


def test_alb_grants(effective, arns):
    for action in ("elasticloadbalancing:CreateLoadBalancer", "elasticloadbalancing:ModifyLoadBalancerAttributes",
                   "elasticloadbalancing:DeleteLoadBalancer", "elasticloadbalancing:CreateListener"):
        assert decision(effective, action, arns["load_balancer"].replace("*", "0123456789abcdef"), _ctx()) is Decision.EXPLICIT_ALLOW, action
        assert decision(effective, action, f"arn:aws:elasticloadbalancing:{gen.REGION}:{gen.ACCOUNT}:loadbalancer/app/other-alb/0123", _ctx()) is Decision.IMPLICIT_DENY, action
    for action in ("elasticloadbalancing:CreateTargetGroup", "elasticloadbalancing:ModifyTargetGroupAttributes", "elasticloadbalancing:DeleteTargetGroup"):
        assert decision(effective, action, arns["target_group"].replace("*", "abc"), _ctx()) is Decision.EXPLICIT_ALLOW
    for action in ("elasticloadbalancing:DeleteListener", "elasticloadbalancing:ModifyListenerAttributes"):
        assert decision(effective, action, arns["listener"].replace("/*/*", "/lb0/li0"), _ctx()) is Decision.EXPLICIT_ALLOW
    # CreateListener authorizes against the LOAD BALANCER (SAR); a listener-ARN-only grant would be dead
    assert decision(effective, "elasticloadbalancing:CreateListener", arns["listener"].replace("/*/*", "/lb0/li0"), _ctx()) is Decision.IMPLICIT_DENY
    for res in (arns["load_balancer"], arns["target_group"], arns["listener"]):
        assert decision(effective, "elasticloadbalancing:AddTags", res, _ctx(**{"elasticloadbalancing:CreateAction": "CreateLoadBalancer"})) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "elasticloadbalancing:AddTags", arns["load_balancer"], _ctx()) is Decision.MISSING_CONTEXT
    assert decision(effective, "elasticloadbalancing:RemoveTags", arns["load_balancer"], _ctx()) is Decision.IMPLICIT_DENY


def test_elasticache_grants_and_the_create_only_encryption_condition(effective, arns):
    create_ctx = _ctx(**{"elasticache:AtRestEncryptionEnabled": "true", "elasticache:TransitEncryptionEnabled": "true"})
    assert decision(effective, "elasticache:CreateReplicationGroup", arns["replication_group"], create_ctx) is Decision.EXPLICIT_ALLOW
    unencrypted = _ctx(**{"elasticache:AtRestEncryptionEnabled": "false", "elasticache:TransitEncryptionEnabled": "true"})
    assert decision(effective, "elasticache:CreateReplicationGroup", arns["replication_group"], unencrypted) is Decision.IMPLICIT_DENY
    for res in (arns["parameter_group"], arns["subnet_group"], arns["member_clusters"].replace("*", "1")):
        assert decision(effective, "elasticache:CreateReplicationGroup", res, _ctx()) is Decision.EXPLICIT_ALLOW, res
    # the delete path carries NO encryption keys in its request: it must NOT be conditioned on them
    for action in ("elasticache:DeleteReplicationGroup", "elasticache:AddTagsToResource"):
        assert decision(effective, action, arns["replication_group"], _ctx()) is Decision.EXPLICIT_ALLOW, action
    assert decision(effective, "elasticache:DeleteReplicationGroup",
                    f"arn:aws:elasticache:{gen.REGION}:{gen.ACCOUNT}:replicationgroup:other-redis", _ctx()) is Decision.IMPLICIT_DENY
    for action in ("elasticache:CreateSnapshot", "elasticache:DeleteCacheSubnetGroup", "elasticache:ModifyReplicationGroup"):
        assert decision(effective, action, arns["replication_group"], _ctx()) is Decision.IMPLICIT_DENY, action


def test_the_sealed_blocking_defect_is_now_a_detected_dead_grant():
    """Round-1 review F1: a Bool encryption condition on DeleteReplicationGroup can never match. The
    validator must say so, so the defect cannot return silently."""
    bad = {"Version": "2012-10-17", "Statement": [{
        "Sid": "Dead", "Effect": "Allow", "Action": ["elasticache:DeleteReplicationGroup"], "Resource": "*",
        "Condition": {"Bool": {"elasticache:AtRestEncryptionEnabled": "true"}}}]}
    problems = iam_eval.validate_policy(bad)
    assert any("elasticache:DeleteReplicationGroup does not support condition key" in p for p in problems), problems
    good = {"Version": "2012-10-17", "Statement": [{
        "Sid": "Ok", "Effect": "Allow", "Action": ["elasticache:CreateReplicationGroup"], "Resource": "*",
        "Condition": {"Bool": {"elasticache:AtRestEncryptionEnabled": "true"}}}]}
    assert iam_eval.validate_policy(good) == []


def test_templated_tag_keys_are_recognised_and_unsupported_tag_keys_are_not():
    ok = {"Version": "2012-10-17", "Statement": [{
        "Sid": "Ok", "Effect": "Allow", "Action": ["ec2:AllocateAddress"], "Resource": "*",
        "Condition": {"StringEquals": {"aws:RequestTag/Name": "x"}}}]}
    assert iam_eval.validate_policy(ok) == []
    dead = {"Version": "2012-10-17", "Statement": [{
        "Sid": "Dead", "Effect": "Allow", "Action": ["ec2:AllocateAddress"], "Resource": "*",
        "Condition": {"StringEquals": {"ec2:ResourceTag/Name": "x"}}}]}  # AllocateAddress has no resource tag yet
    assert any("does not support condition key ec2:ResourceTag/Name" in p for p in iam_eval.validate_policy(dead))


def test_route53_record_scope_is_exact_and_change_scope_is_distinct(effective, arns):
    zone = arns["hosted_zone"]
    good = _ctx(**{"route53:ChangeResourceRecordSetsNormalizedRecordNames": [FQDN],
                   "route53:ChangeResourceRecordSetsRecordTypes": ["A"],
                   "route53:ChangeResourceRecordSetsActions": ["CREATE"]})
    assert decision(effective, "route53:ChangeResourceRecordSets", zone, good) is Decision.EXPLICIT_ALLOW
    for key, bad in (("route53:ChangeResourceRecordSetsNormalizedRecordNames", ["app.synthetic.example.com"]),
                     ("route53:ChangeResourceRecordSetsNormalizedRecordNames", [FQDN, "app.synthetic.example.com"]),
                     ("route53:ChangeResourceRecordSetsRecordTypes", ["CNAME"]),
                     ("route53:ChangeResourceRecordSetsRecordTypes", ["A", "TXT"]),
                     ("route53:ChangeResourceRecordSetsActions", ["UPSERT", "DELETE", "CREATE", "X"])):
        assert decision(effective, "route53:ChangeResourceRecordSets", zone, {**good, key: bad}) is Decision.IMPLICIT_DENY, (key, bad)
    # every other zone is EXPLICITLY denied by the fence, whatever the record
    iam_eval.require_explicit_deny(effective, "route53:ChangeResourceRecordSets",
                                   "arn:aws:route53:::hostedzone/ZSYNTHOTHER0000000000", good,
                                   sid="WinDenyRecordChangesOutsideTheConsumedZone")
    # GetChange is CHANGE-scoped; a zone-scoped request for it is not what the provider sends
    assert decision(effective, "route53:GetChange", "arn:aws:route53:::change/C0123456789", _ctx()) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "route53:GetChange", zone, _ctx()) is Decision.IMPLICIT_DENY
    assert decision(effective, "route53:ListResourceRecordSets", zone, _ctx()) is Decision.EXPLICIT_ALLOW
    for action in ("route53:CreateHostedZone", "route53:DeleteHostedZone", "route53:ChangeTagsForResource"):
        assert decision(effective, action, zone, _ctx()) is Decision.IMPLICIT_DENY


def test_budget_grant_is_the_one_budget(effective, arns):
    assert decision(effective, "budgets:ModifyBudget", arns["budget"], _ctx()) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "budgets:ModifyBudget", f"arn:aws:budgets::{gen.ACCOUNT}:budget/other", _ctx()) is Decision.IMPLICIT_DENY
    assert decision(effective, "budgets:ViewBudget", arns["budget"], _ctx()) is Decision.EXPLICIT_ALLOW
    for action in ("budgets:TagResource", "budgets:UntagResource", "aws-portal:ModifyBilling", "aws-portal:ViewBilling"):
        assert decision(effective, action, arns["budget"], _ctx()) is Decision.IMPLICIT_DENY, action


def test_the_one_iam_write_is_boundary_conditioned_and_fenced(effective, arns):
    role = arns["api_task_role"]
    assert role == identity.iam_role_arn(f"{gen.PREFIX}-api-task")
    assert decision(effective, "iam:PutRolePolicy", role, _ctx(**{"iam:PermissionsBoundary": BOUNDARY})) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "iam:PutRolePolicy", role, _ctx(**{"iam:PermissionsBoundary": f"arn:aws:iam::{gen.ACCOUNT}:policy/wrong"})) is Decision.IMPLICIT_DENY
    assert decision(effective, "iam:PutRolePolicy", role, _ctx()) is Decision.MISSING_CONTEXT, "a role without a boundary must not match"
    for other in (identity.iam_role_arn(f"{gen.PREFIX}-worker-task"), gen.READER_ROLE_ARNS[0],
                  f"arn:aws:iam::{gen.ACCOUNT}:role/anything",
                  f"arn:aws:iam::{gen.ACCOUNT}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_x_0123456789abcdef"):
        iam_eval.require_explicit_deny(effective, "iam:PutRolePolicy", other, _ctx(**{"iam:PermissionsBoundary": BOUNDARY}),
                                       sid="WinDenyInlinePolicyOutsideTheApiTaskRole")
    for action in ("iam:CreateRole", "iam:DeleteRole", "iam:DeleteRolePolicy", "iam:TagRole", "iam:PassRole",
                   "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary", "iam:AttachRolePolicy",
                   "iam:CreateServiceLinkedRole", "iam:CreatePolicy"):
        iam_eval.require_explicit_deny(effective, action, role, _ctx(**{"iam:PermissionsBoundary": BOUNDARY}), sid="WinDenyDangerous")


def test_rds_stop_start_on_the_one_instance_and_nothing_destructive(effective, arns):
    for action in ("rds:StartDBInstance", "rds:StopDBInstance"):
        assert decision(effective, action, arns["db"], _ctx()) is Decision.EXPLICIT_ALLOW
        assert decision(effective, action, f"arn:aws:rds:{gen.REGION}:{gen.ACCOUNT}:db:other", _ctx()) is Decision.IMPLICIT_DENY
    assert decision(effective, "rds:DescribeDBInstances", "*", _ctx()) is Decision.EXPLICIT_ALLOW
    for action in ("rds:CreateDBSnapshot", "rds:DeleteDBInstance", "rds:ModifyDBInstance", "rds:RestoreDBInstanceFromDBSnapshot"):
        iam_eval.require_explicit_deny(effective, action, arns["db"], _ctx(), sid="WinDenyDangerous")
    assert decision(effective, "rds:RebootDBInstance", arns["db"], _ctx()) is Decision.IMPLICIT_DENY


def test_state_backend_scopes_are_exact_and_fenced(effective):
    assert decision(effective, "s3:GetObject", gen.ARN["state_object"], _ctx()) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "s3:PutObject", gen.ARN["state_object"], _ctx()) is Decision.EXPLICIT_ALLOW
    iam_eval.require_explicit_deny(effective, "s3:PutObject", f"{gen.ARN['state_bucket']}/other/object", _ctx(),
                                   sid="WinDenyStateObjectAccessOutsideTheStateObject")
    iam_eval.require_explicit_deny(effective, "s3:GetObject", f"{gen.ARN['audit_bucket']}/AWSLogs/x", _ctx(),
                                   sid="WinDenyStateObjectAccessOutsideTheStateObject")
    for action in ("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"):
        assert decision(effective, action, gen.ARN["lock"], _ctx()) is Decision.EXPLICIT_ALLOW
        iam_eval.require_explicit_deny(effective, action, f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/other", _ctx(),
                                       sid="WinDenyLockItemsOutsideTheLockTable")
    via = _ctx(**{"kms:ViaService": f"s3.{gen.REGION}.amazonaws.com"})
    assert decision(effective, "kms:Decrypt", gen.ARN["cmk_state"], via) is Decision.EXPLICIT_ALLOW
    assert decision(effective, "kms:Decrypt", gen.ARN["cmk_state"], _ctx()) is Decision.MISSING_CONTEXT, "a direct Decrypt of the state blob never matches"
    iam_eval.require_explicit_deny(effective, "kms:Decrypt", gen.ARN["cmk_secrets"], via, sid="WinDenyStateCmkUseOutsideTheStateCmk")
    assert decision(effective, "dynamodb:UpdateItem", gen.ARN["lock"], _ctx()) is Decision.EXPLICIT_DENY
    assert decision(effective, "s3:DeleteObject", gen.ARN["state_object"], _ctx()) is Decision.EXPLICIT_DENY
    assert decision(effective, "kms:Encrypt", gen.ARN["cmk_state"], _ctx()) is Decision.IMPLICIT_DENY


@pytest.mark.parametrize("action,resource", [
    ("ecs:RegisterTaskDefinition", f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/{gen.PREFIX}-api:1"),
    ("ecs:RunTask", "*"), ("ecs:CreateService", "*"), ("ecs:UpdateService", "*"),
    ("cloudtrail:StopLogging", "*"), ("cloudtrail:DeleteTrail", "*"),
    ("kms:ScheduleKeyDeletion", "*"), ("kms:CreateGrant", "*"),
    ("secretsmanager:GetSecretValue", "*"), ("secretsmanager:DeleteSecret", "*"),
    ("s3:PutBucketPolicy", "*"), ("s3:DeleteBucket", "*"), ("logs:DeleteLogGroup", "*"),
    ("sts:AssumeRole", "*"), ("sso:CreatePermissionSet", "*"), ("sso:ProvisionPermissionSet", "*"),
])
def test_the_ceiling_explicitly_denies_the_forbidden_capabilities(effective, action, resource):
    iam_eval.require_explicit_deny(effective, action, resource, _ctx(**{"iam:PermissionsBoundary": BOUNDARY}), sid="WinDenyDangerous")


def test_the_ceiling_is_the_reviewed_ceiling_with_exactly_the_scoped_carve(ceiling):
    from must_not_contract import FORBIDDEN_CAPABILITIES

    actions = set(ceiling["Statement"][0]["Action"])
    assert actions == (set(gen.PERMANENT_DENY) | set(FORBIDDEN_CAPABILITIES)) - gen.WINDOW_SCOPED_CAPABILITIES
    w0 = next(s for s in gen.permanent_w0_policy()["Statement"] if s["Sid"] == "DenyDangerous")
    assert set(w0["Action"]) - actions == {"iam:PutRolePolicy"}
    assert actions - set(w0["Action"]) == {"ecs:RegisterTaskDefinition"}
    assert "rds:CreateDBSnapshot" in actions and "iam:PassRole" in actions


def test_adding_an_allow_for_a_denied_action_cannot_widen_the_principal(effective):
    for action, resource in (("iam:CreateRole", gen.READER_ROLE_ARNS[0]), ("cloudtrail:StopLogging", gen.ARN["trail"]),
                             ("ecs:RegisterTaskDefinition", "*")):
        broken = copy.deepcopy(effective)
        broken["Statement"].insert(0, {"Sid": "Sneak", "Effect": "Allow", "Action": action, "Resource": "*"})
        assert decision(broken, action, resource, _ctx()) is Decision.EXPLICIT_DENY


# =====================================================================================
# 5. Contract independence — the generator is compared against the authored section
# =====================================================================================


def _contract_actions(contract: dict) -> set[str]:
    out: set[str] = set()
    for group, actions in contract.items():
        if group.startswith("_") or group in ("fences", "scoped_capabilities") or not isinstance(actions, list):
            continue
        out.update(actions)
    return out


def test_the_generator_writes_match_the_contract_exactly(contract):
    generated = {a for group in gen.WINDOW_TRANSITION_WRITES.values() for a in group}
    generated |= {a for group in gen.WINDOW_READ_ADDITIONS.values() for a in group}
    assert generated == _contract_actions(contract)
    assert set(contract["scoped_capabilities"]) == gen.WINDOW_SCOPED_CAPABILITIES


def test_every_contract_write_is_granted_somewhere_in_the_inline_document(inline, contract):
    allowed = {a for s in inline["Statement"] if s["Effect"] == "Allow" for a in s["Action"]}
    writes = _contract_actions(contract) - set(contract["reads_added"])
    assert writes <= allowed, writes - allowed


def test_every_contract_fence_exists_and_is_a_notresource_deny(inline, contract):
    by_sid = {s["Sid"]: s for s in inline["Statement"]}
    for sid in contract["fences"]:
        assert by_sid[sid]["Effect"] == "Deny" and "NotResource" in by_sid[sid] and "Condition" not in by_sid[sid], sid


def test_changing_the_generator_while_the_contract_stands_fails(contract):
    reduced = copy.deepcopy(gen.WINDOW_TRANSITION_WRITES)
    reduced["network_destroy"] = [a for a in reduced["network_destroy"] if a != "ec2:ReleaseAddress"]
    generated = {a for group in reduced.values() for a in group} | {a for g in gen.WINDOW_READ_ADDITIONS.values() for a in g}
    assert generated != _contract_actions(contract)


def test_changing_the_contract_while_the_generator_stands_fails(contract):
    widened = copy.deepcopy(contract)
    widened["network_destroy"].append("ec2:DeleteVpc")
    generated = {a for group in gen.WINDOW_TRANSITION_WRITES.values() for a in group} | {a for g in gen.WINDOW_READ_ADDITIONS.values() for a in g}
    assert generated != _contract_actions(widened)


def test_the_read_closure_is_w0s_read_closure_plus_exactly_the_two_additions(reads):
    perm = gen.permanent_w0_policy()
    perm_reads = {a for s in perm["Statement"] if s["Effect"] == "Allow" and s["Sid"] not in
                  ("StateBucketRead", "StateObjectReadWrite", "StateLock", "StateCmkUseViaBackendServices",
                   "TaskDefinitionFamiliesRegister", "TaskDefinitionDescribeStar") for a in s["Action"]}
    window_reads = {a for s in reads["Statement"] for a in s["Action"]}
    assert window_reads == perm_reads | {"ec2:DescribeNetworkInterfaces", "ecs:DescribeTaskDefinition"}


# =====================================================================================
# 6. The names the scopes bind to are the ones the modules declare
# =====================================================================================


def _tf(rel: str) -> str:
    return (REPO_ROOT / "infra" / "aws" / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("module_file,expr", [
    ("modules/network/main.tf", 'Name = "${var.name_prefix}-nat-eip"'),
    ("modules/network/main.tf", 'Name = "${var.name_prefix}-nat"'),
    ("modules/network/main.tf", 'Name = "${var.name_prefix}-private-rt"'),
    ("modules/network/main.tf", 'Name = "${var.name_prefix}-public-${each.key}"'),
    ("modules/network/main.tf", 'Name = "${var.name_prefix}-vpc"'),
    ("modules/alb/main.tf", 'name               = "${var.name_prefix}-alb"'),
    ("modules/alb/main.tf", 'name        = "${var.name_prefix}-api-tg"'),
    ("modules/alb/main.tf", 'count = var.enabled ? 1 : 0'),
    ("modules/data_cache/main.tf", 'replication_group_id = "${var.name_prefix}-redis"'),
    ("modules/data_cache/main.tf", 'name        = "${var.name_prefix}-redis-params"'),
    ("modules/data_cache/main.tf", 'name       = "${var.name_prefix}-redis-subnets"'),
    ("modules/data_cache/main.tf", 'at_rest_encryption_enabled = true'),
    ("modules/data_cache/main.tf", 'transit_encryption_enabled = true'),
    ("modules/cost/main.tf", 'name        = "${var.name_prefix}-monthly"'),
    ("modules/iam/main.tf", 'name                 = "${var.name_prefix}-api-task"'),
    ("modules/iam/main.tf", 'resource "aws_iam_role_policy" "api_ses_send"'),
    ("modules/alb/main.tf", 'resource "aws_route53_record" "api"'),
    ("modules/alb/main.tf", 'type = "A"'),
])
def test_the_module_names_the_scopes_bind_to_are_declared(module_file, expr):
    assert expr in _tf(module_file), f"{module_file} no longer declares {expr!r}"


def test_the_data_cache_module_requests_no_final_snapshot_and_uses_the_aws_managed_key():
    text = _tf("modules/data_cache/main.tf")
    assert "final_snapshot_identifier" not in text
    assert 'default     = null' in _tf("modules/data_cache/variables.tf").split('variable "kms_key_id"')[1].split("}")[0]


def test_the_prefix_is_the_repository_prefix():
    assert gen.PREFIX == "signalnest-staging"
    names = gen._window_names()
    assert names["api_task_role_arn"].endswith(":role/signalnest-staging-api-task")


# =====================================================================================
# 7. The CLI refuses missing or misplaced inputs; the operator-held values never default
# =====================================================================================


def _cli(*args):
    import os

    env = {**os.environ, "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC", "PYTHONPATH": str(REPO_ROOT / "scripts")}
    return subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "gen_operator_policies.py"), *args],
                          capture_output=True, text=True, env=env)


def test_cli_requires_both_inputs_for_the_inline_document_and_rejects_them_elsewhere():
    assert _cli("--emit", "window-transition-inline", "--expiry", EXPIRY).returncode == 2
    assert _cli("--emit", "window-transition-inline", "--api-fqdn", FQDN).returncode == 2
    assert _cli("--emit", "window-transition-deny-ceiling", "--expiry", EXPIRY).returncode == 2
    assert _cli("--emit", "permanent-w0", "--api-fqdn", FQDN).returncode == 2
    assert _cli("--emit", "window-transition-read-closure", "--expiry", EXPIRY, "--api-fqdn", FQDN).returncode == 2
    ok = _cli("--emit", "window-transition-inline", "--expiry", EXPIRY, "--api-fqdn", FQDN, "--hash")
    assert ok.returncode == 0 and "canonical" in ok.stdout and "statements" in ok.stdout
    assert _cli("--emit", "window-transition-deny-ceiling", "--hash").returncode == 0
    bad = _cli("--emit", "window-transition-inline", "--expiry", "2099-12-31T23:59:59Z", "--api-fqdn", FQDN)
    assert bad.returncode != 0 and not bad.stdout.strip(), "an unauthorized window must produce NO document"


def test_no_operator_zone_or_live_account_is_committed():
    """The operator's real DNS apex and account id are protected identifiers: the generator and this
    test must carry neither (the hygiene scanner assembles its own control the same way, so this file
    contains no matching literal either)."""
    source = (REPO_ROOT / "scripts" / "gen_operator_policies.py").read_text(encoding="utf-8")
    here = Path(__file__).read_text(encoding="utf-8")
    zone_apex = "signal" + "nest" + "." + "pro"
    synthetic_account = "111122223333"  # the AWS documentation placeholder the Tier-1 anchor uses
    for text in (source, here):
        assert zone_apex not in text
        twelve_digit = set(re.findall(r"(?<!\d)\d{12}(?!\d)", text))
        assert twelve_digit <= {synthetic_account}, twelve_digit - {synthetic_account}
    assert gen.ACCOUNT == synthetic_account, "tests render under the synthetic tier; no live account reaches a committed file"
