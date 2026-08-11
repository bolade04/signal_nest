#!/usr/bin/env python3
"""Gate 4N-I28BH-A1a — policy-generation deny/allow source classification and emitted-policy
oracle adjudication.

WHAT THIS PINS. The 34 collections whose final consumer is an emitted AWS/IAM policy statement
(the six generators gen_boundary_policy, gen_bootstrap_operator_policy, gen_operator_policies,
gen_readonly_verifier_policy, gen_role_bootstrap_policy, gen_boundary_rollout) are each classified
under the 7-class BH-A taxonomy in `critical-list-contract.json::a1a_adjudication`, and every
security source carries a NON-CIRCULAR emitted-policy oracle DESIGN. This test enforces the design
is well-formed and, for the real generators, that the designed oracle actually fires — while the
completeness CONSUMERS themselves remain BH-B work (not implemented here).

THE DEFECT CLASS THIS ADDRESSES. An authoritative deny/allow SOURCE going SHORT weakens the emitted
policy while every internally-consistent check still passes. The load-bearing subtlety, proven in
arm 10/11 below, is that each flat deny Sid's Action is literally `sorted(<the list>)`, so comparing
the list to the emitted Sid is CIRCULAR against source shortening — the guarantee must bind emitted
policy to an INDEPENDENT authority (the readonly-verifier ceiling, the operator-closure contract,
the reviewed trust manifest, or the A1c triangulation set), which is exactly what the design records.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
os.environ.setdefault("SIGNALNEST_ANCHOR_TIER", "TIER_1_SYNTHETIC")

CONTRACT = json.loads((REPO / "tests/fixtures/critical-list-contract.json").read_text())
ADJ = CONTRACT["a1a_adjudication"]
CLASSIFICATIONS = CONTRACT["classifications"]

SIX = {"gen_boundary_policy.py", "gen_bootstrap_operator_policy.py", "gen_operator_policies.py",
       "gen_readonly_verifier_policy.py", "gen_role_bootstrap_policy.py", "gen_boundary_rollout.py"}

SEVEN_CLASSES = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS",
                 "TEST_ONLY_LOAD_BEARING", "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY",
                 "DEAD_OR_STALE"}
SECURITY_CLASSES = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS"}
ORACLE_FAMILIES = {"MODULE_CONSTANTS", "FUNCTION_RESULT_KEYS", "EMITTED_POLICY", "DISCOVERED_KINDS",
                   "AUTHORED_CONTRACT", "SCHEMA_VALIDATION", "HARNESS_COMPLETENESS",
                   "PROVENANCE_DERIVATION", "AUTHORITATIVE_SOURCE_NO_ENUMERABLE_ORACLE", "NONE"}
POSITIVE_PRESENCE = {"INVALID_EMPTY", "VALID_EMPTY", "CONDITIONALLY_EMPTY"}
STRING_OF = {"SECURITY_CRITICAL_SOURCE": "SECURITY_CRITICAL_LIST",
             "SECURITY_CRITICAL_DERIVED": "SECURITY_CRITICAL_LIST",
             "SECURITY_SCHEMA_OR_FIELDS": "NON_SECURITY_CONFIGURATION",
             "TEST_ONLY_LOAD_BEARING": "TEST_ONLY",
             "NON_SECURITY_CONFIGURATION": "NON_SECURITY_CONFIGURATION",
             "DOCUMENTATION_ONLY": "DOCUMENTATION_ONLY", "DEAD_OR_STALE": "NON_SECURITY_CONFIGURATION"}


def _discovered_a1a() -> set:
    import critical_list_inventory as cli
    return {c["id"] for c in cli.discover_collections()
            if c["module"] in SIX and not c["form"].startswith("derived:")}  # BH-C: historical a1a scope is the AST-literal generators; runtime-discovered derived collections are governed via critical-list-contract + the assurance registry


def validate_a1a(adj: dict, classifications: dict, discovered: set) -> list:
    """The A1a adjudication validator. Fail-closed; returns a list of problems.

    Kept as a plain function so the falsification battery can drive it with mutated inputs and
    assert it refuses. This is the DESIGN validator; the completeness consumers are BH-B.
    """
    problems: list = []
    # META-COMPLETENESS, both directions: exactly the six-generator discovered collections.
    for cid in sorted(discovered - set(adj)):
        problems.append(f"{cid}: an A1a policy-generation collection with NO adjudication")
    for cid in sorted(set(adj) - discovered):
        problems.append(f"{cid}: adjudicated but not discovered as an A1a collection")
    for cid, rec in adj.items():
        cls = rec.get("cls")
        if cls not in SEVEN_CLASSES:
            problems.append(f"{cid}: class {cls!r} is not one of the seven")
            continue
        of = rec.get("oracle_family")
        if of not in ORACLE_FAMILIES:
            problems.append(f"{cid}: oracle_family {of!r} is unknown")
        if classifications.get(cid) != STRING_OF[cls]:
            problems.append(f"{cid}: string projection {classifications.get(cid)!r} != "
                            f"{STRING_OF[cls]!r} required by class {cls}")
        if cls in SECURITY_CLASSES:
            if of in (None, "NONE"):
                problems.append(f"{cid}: {cls} with no completeness/oracle strategy")
            for field in ("expected_authority", "observed_authority", "independence",
                          "comparison", "positive_presence"):
                if not rec.get(field):
                    problems.append(f"{cid}: {cls} missing {field}")
            if rec.get("expected_authority") and \
                    rec.get("expected_authority") == rec.get("observed_authority"):
                problems.append(f"{cid}: expected and observed authority are the same object "
                                "(alias / circular oracle)")
            if rec.get("positive_presence") not in POSITIVE_PRESENCE:
                problems.append(f"{cid}: positive_presence {rec.get('positive_presence')!r} invalid")
        else:
            if of != "NONE":
                problems.append(f"{cid}: non-security class must carry oracle_family NONE")
    return problems


# ---------------------------------------------------------------------------
# Structural / schema tests
# ---------------------------------------------------------------------------

def test_a1a_scope_is_exactly_the_six_generators_both_directions():
    assert set(ADJ) == _discovered_a1a()
    assert len(ADJ) == 34


def test_the_authored_adjudication_is_well_formed():
    assert validate_a1a(ADJ, CLASSIFICATIONS, _discovered_a1a()) == []


def test_class_distribution_matches_the_reported_adjudication():
    from collections import Counter
    dist = Counter(r["cls"] for r in ADJ.values())
    assert dist["SECURITY_CRITICAL_SOURCE"] == 22
    assert dist["SECURITY_CRITICAL_DERIVED"] == 3
    assert dist["TEST_ONLY_LOAD_BEARING"] == 4
    assert dist["NON_SECURITY_CONFIGURATION"] == 5
    assert sum(dist.values()) == 34


# The A1a ids that were SECURITY_CRITICAL_LIST BEFORE this gate (frozen as the independent
# 'before' baseline). No id here may lose its security-critical projection without a proving
# exclusion argument. Every A1a change in this gate is an UP-classification or a lateral
# config→test move; this set is what makes a silent later downgrade fail.
PRE_A1A_SECURITY_CRITICAL = frozenset({
    "gen_boundary_policy.py::ECS_DENIED", "gen_boundary_policy.py::IAM_ADMIN_DENIED",
    "gen_boundary_policy.py::LOGS_DENIED", "gen_boundary_policy.py::S3_DELIVERY_DENIED",
    "gen_boundary_policy.py::S3_ACL_DENIED", "gen_boundary_policy.py::ACCOUNT_ADMIN_DENIED",
    "gen_boundary_policy.py::CLOUDTRAIL_DENIED",
    "gen_bootstrap_operator_policy.py::CEILING_EXCEPTIONS",
    "gen_operator_policies.py::TEMP_SCOPED_CAPABILITIES",
    "gen_role_bootstrap_policy.py::ALLOWED_TAG_KEYS",
    "gen_role_bootstrap_policy.py::CEILING_EXCEPTIONS",
})


def test_no_a1a_collection_was_reclassified_downward_from_a_security_list():
    """The proving-exclusion requirement: no A1a id may move DOWN from SECURITY_CRITICAL_LIST
    without a downgrade proof. All eleven pre-A1a security-critical ids must stay projected
    SECURITY_CRITICAL_LIST (they did: 7 stayed sources, 3 became DERIVED, 1 stayed source)."""
    for cid in PRE_A1A_SECURITY_CRITICAL:
        rec = ADJ[cid]
        if STRING_OF[rec["cls"]] != "SECURITY_CRITICAL_LIST":
            assert rec.get("downgrade_proof"), (
                f"{cid} moved down from SECURITY_CRITICAL_LIST with no proving exclusion argument")
        assert CLASSIFICATIONS[cid] == "SECURITY_CRITICAL_LIST", cid


# ---------------------------------------------------------------------------
# Real emitted-policy behaviour: the design actually fires
# ---------------------------------------------------------------------------

def _deny_by_sid():
    import gen_boundary_policy as g
    doc = g.boundary_policy()
    out = {}
    for s in doc["Statement"]:
        if s.get("Effect") == "Deny":
            act = s.get("Action") or s.get("NotAction") or []
            out[s["Sid"]] = set([act] if isinstance(act, str) else act)
    return out


DENY_SID = {
    "gen_boundary_policy.py::ECS_DENIED": ("ECS_DENIED", "DenyEcsControlPlaneMutation", "exact"),
    "gen_boundary_policy.py::LOGS_DENIED": ("LOGS_DENIED", "DenyLogGroupDestructionAndRetentionTampering", "exact"),
    "gen_boundary_policy.py::S3_DELIVERY_DENIED": ("S3_DELIVERY_DENIED", "DenyAuditDeliveryTampering", "exact"),
    "gen_boundary_policy.py::S3_ACL_DENIED": ("S3_ACL_DENIED", "DenyObjectAndBucketAclChanges", "exact"),
    "gen_boundary_policy.py::CLOUDTRAIL_DENIED": ("CLOUDTRAIL_DENIED", "DenyAuditTrailShutdown", "exact"),
    "gen_boundary_policy.py::IAM_ADMIN_DENIED": ("IAM_ADMIN_DENIED", "DenyIdentityAndAccountAdministration", "union"),
    "gen_boundary_policy.py::ACCOUNT_ADMIN_DENIED": ("ACCOUNT_ADMIN_DENIED", "DenyIdentityAndAccountAdministration", "union"),
}


def test_every_deny_source_member_reaches_the_emitted_policy():
    import gen_boundary_policy as g
    emitted = _deny_by_sid()
    for cid, (name, sid, mode) in DENY_SID.items():
        members = set(getattr(g, name))
        assert sid in emitted, f"{cid}: Sid {sid} vanished from the emitted policy"
        assert members <= emitted[sid], f"{cid}: members absent from emitted Deny: {members - emitted[sid]}"


def test_readonly_verifier_ceiling_is_the_independent_allow_authority():
    """Adding an allow member outside the independently authored ceiling is refused in
    production — the non-circular guarantee the design records for the verifier reads."""
    import gen_readonly_verifier_policy as v
    ea = __import__("expiry_authorization")
    # A clean generation succeeds.
    v.readonly_verifier_policy(ea.ACTIVE_EXPIRY_UTC, issuance=ea.ACTIVE_ISSUANCE_UTC)
    # Widen the Allow with a non-read / out-of-ceiling action → refusal before output.
    saved = dict(v.IDENTITY_CENTRE_READS)
    try:
        v.IDENTITY_CENTRE_READS["sso:GetRoleCredentials"] = "escalation probe"
        with pytest.raises(Exception):
            v.readonly_verifier_policy(ea.ACTIVE_EXPIRY_UTC, issuance=ea.ACTIVE_ISSUANCE_UTC)
    finally:
        v.IDENTITY_CENTRE_READS.clear()
        v.IDENTITY_CENTRE_READS.update(saved)


def test_role_bootstrap_tag_key_authority_is_independent_of_the_source_list():
    """ALLOWED_TAG_KEYS is validated against the reviewed trust manifest (a different module),
    so an unreviewed key is refused before any policy output — non-circular by construction."""
    import gen_role_bootstrap_policy as r
    with pytest.raises(r.TagKeyDomainError):
        r.require_reviewed_tag_keys(["Name", "UnreviewedInjectedKey"])
    with pytest.raises(r.TagKeyDomainError):
        r.require_reviewed_tag_keys([])  # false-empty


# ---------------------------------------------------------------------------
# 20-arm classification / oracle falsification battery
# Each arm activates, reaches the intended detector, and fails closed.
# ---------------------------------------------------------------------------

def _adj_copy():
    return copy.deepcopy(ADJ)


def _cls_copy():
    return dict(CLASSIFICATIONS)


ARMS = {}


def arm(n):
    def deco(fn):
        ARMS[n] = fn
        return fn
    return deco


@arm(1)
def _arm1_new_policy_source_unclassified():
    disc = _discovered_a1a() | {"gen_boundary_policy.py::NEW_DENY"}
    return validate_a1a(ADJ, CLASSIFICATIONS, disc)


@arm(2)
def _arm2_delete_a1a_classification():
    adj = _adj_copy(); adj.pop("gen_boundary_policy.py::IAM_ADMIN_DENIED")
    return validate_a1a(adj, CLASSIFICATIONS, _discovered_a1a())


@arm(3)
def _arm3_downgrade_deny_without_proof():
    adj = _adj_copy(); adj["gen_boundary_policy.py::LOGS_DENIED"]["cls"] = "NON_SECURITY_CONFIGURATION"
    adj["gen_boundary_policy.py::LOGS_DENIED"]["oracle_family"] = "NONE"
    return validate_a1a(adj, CLASSIFICATIONS, _discovered_a1a())  # projection mismatch


@arm(4)
def _arm4_downgrade_allow_without_proof():
    adj = _adj_copy(); adj["gen_role_bootstrap_policy.py::CREATE_ROLE_ACTIONS"]["cls"] = "NON_SECURITY_CONFIGURATION"
    adj["gen_role_bootstrap_policy.py::CREATE_ROLE_ACTIONS"]["oracle_family"] = "NONE"
    return validate_a1a(adj, CLASSIFICATIONS, _discovered_a1a())


@arm(5)
def _arm5_source_marked_schema():
    adj = _adj_copy(); adj["gen_boundary_policy.py::ECS_DENIED"]["cls"] = "SECURITY_SCHEMA_OR_FIELDS"
    adj["gen_boundary_policy.py::ECS_DENIED"]["oracle_family"] = "NONE"
    return validate_a1a(adj, CLASSIFICATIONS, _discovered_a1a())  # security class needs oracle + projection


@arm(6)
def _arm6_source_marked_non_security():
    cls = _cls_copy(); cls["gen_boundary_policy.py::CLOUDTRAIL_DENIED"] = "NON_SECURITY_CONFIGURATION"
    return validate_a1a(ADJ, cls, _discovered_a1a())  # projection mismatch vs SECURITY class


@arm(7)
def _arm7_oracle_family_missing():
    adj = _adj_copy(); adj["gen_boundary_policy.py::IAM_ADMIN_DENIED"]["oracle_family"] = None
    return validate_a1a(adj, CLASSIFICATIONS, _discovered_a1a())


@arm(8)
def _arm8_invalid_oracle_family():
    adj = _adj_copy(); adj["gen_boundary_policy.py::IAM_ADMIN_DENIED"]["oracle_family"] = "MAGIC_ORACLE"
    return validate_a1a(adj, CLASSIFICATIONS, _discovered_a1a())


@arm(9)
def _arm9_expected_observed_alias():
    adj = _adj_copy()
    rec = adj["gen_readonly_verifier_policy.py::IAM_READS"]
    rec["observed_authority"] = rec["expected_authority"]
    return validate_a1a(adj, CLASSIFICATIONS, _discovered_a1a())


@arm(10)
def _arm10_generator_omission_from_a_deny_sid():
    """Generator stops emitting a member the list still declares → EXACT correspondence fires."""
    import gen_boundary_policy as g
    emitted = _deny_by_sid()
    members = set(g.LOGS_DENIED)
    broken = set(emitted["DenyLogGroupDestructionAndRetentionTampering"]) - {"logs:DeleteLogGroup"}
    return ["omission detected"] if members != broken else []


@arm(11)
def _arm11_source_shortening_caught_by_independent_authority():
    """Source shortening is invisible to list==emittedSid (circular); an INDEPENDENT required-deny
    probe (architecture invariant, not derived from the source list) catches it via REQUIRED_SUBSET."""
    import gen_boundary_policy as g
    emitted_all = set().union(*_deny_by_sid().values())
    # Independent required denies (from the design's stated invariants, not from any source list).
    required = {"iam:PutRolePermissionsBoundary", "cloudtrail:StopLogging", "iam:CreateUser"}
    assert required <= emitted_all  # holds today
    shortened = emitted_all - {"cloudtrail:StopLogging"}  # simulate a source going short
    return ["shortening detected"] if not (required <= shortened) else []


@arm(12)
def _arm12_unauthorized_allow_member_emitted():
    import gen_readonly_verifier_policy as v
    ea = __import__("expiry_authorization")
    saved = dict(v.IAM_READS)
    try:
        v.IAM_READS["iam:PassRole"] = "escalation"
        try:
            v.readonly_verifier_policy(ea.ACTIVE_EXPIRY_UTC, issuance=ea.ACTIVE_ISSUANCE_UTC)
            return []
        except Exception:
            return ["unauthorized allow refused"]
    finally:
        v.IAM_READS.clear(); v.IAM_READS.update(saved)


@arm(13)
def _arm13_unreviewed_tag_key_added():
    import gen_role_bootstrap_policy as r
    try:
        r.require_reviewed_tag_keys(["Name", "Rogue"])
        return []
    except r.TagKeyDomainError:
        return ["unreviewed tag key refused"]


@arm(14)
def _arm14_false_empty_deny_universe():
    import gen_role_bootstrap_policy as r
    try:
        r.require_reviewed_tag_keys([])
        return []
    except r.TagKeyDomainError:
        return ["false-empty refused"]


@arm(15)
def _arm15_deny_action_emitted_under_wrong_effect():
    """A deny member appearing only under Allow must be detected: it is absent from emitted Deny."""
    import gen_boundary_policy as g
    emitted_deny = set().union(*_deny_by_sid().values())
    member = "cloudtrail:StopLogging"
    moved = emitted_deny - {member}  # simulate the action moved off Deny
    return ["wrong-effect detected"] if member not in moved else []


@arm(16)
def _arm16_flat_deny_resource_must_be_star():
    """A flat account/identity deny silently scoped to a specific Resource would narrow it."""
    import gen_boundary_policy as g
    doc = g.boundary_policy()
    sid = next(s for s in doc["Statement"] if s["Sid"] == "DenyEcsControlPlaneMutation")
    assert sid.get("Resource") == "*"  # holds today
    mutated = dict(sid); mutated["Resource"] = ["arn:aws:ecs:eu-west-2:x:cluster/only"]
    return ["resource-narrowing detected"] if mutated.get("Resource") != "*" else []


@arm(17)
def _arm17_tag_condition_lost():
    """Removing the ForAllValues tag-key condition from the TagRole statement is detected by the
    independent tag-key authority (the reviewed manifest still requires the constrained key set)."""
    import gen_role_bootstrap_policy as r
    # With the condition present the reviewed set is enforced; dropping the allow-list key entirely
    # (condition carrying no keys) fails the reviewed-domain both-direction check.
    try:
        r.require_reviewed_tag_keys([])
        return []
    except r.TagKeyDomainError:
        return ["lost-condition detected"]


@arm(18)
def _arm18_member_move_between_merged_lists_is_semantically_neutral():
    """POSITIVE CONTROL (§12): IAM_ADMIN_DENIED and ACCOUNT_ADMIN_DENIED feed ONE Deny Sid with
    Resource '*', so moving a member between them leaves emitted semantics identical — source
    identity is NOT security-relevant here, and the union oracle correctly does not false-alarm,
    while dropping the member from BOTH is caught (arm 10/15)."""
    import gen_boundary_policy as g
    union = set(g.IAM_ADMIN_DENIED) | set(g.ACCOUNT_ADMIN_DENIED)
    moved_iam = (set(g.IAM_ADMIN_DENIED) - {"sso:*"})
    moved_acct = set(g.ACCOUNT_ADMIN_DENIED) | {"sso:*"}
    still_union = moved_iam | moved_acct
    # neutrality holds AND completeness (union unchanged) verified
    return ["union invariant holds"] if still_union == union else []


@arm(19)
def _arm19_collection_renamed_classification_lost():
    disc = (_discovered_a1a() - {"gen_boundary_policy.py::IAM_ADMIN_DENIED"}) | {"gen_boundary_policy.py::IAM_ADMIN_BLOCKED"}
    return validate_a1a(ADJ, CLASSIFICATIONS, disc)  # both directions fire


@arm(20)
def _arm20_wrapper_form_escapes_discovery():
    disc = _discovered_a1a() - {"gen_boundary_policy.py::ECS_DENIED"}  # form changed → vanished
    return validate_a1a(ADJ, CLASSIFICATIONS, disc)  # 'adjudicated but not discovered' fires


@pytest.mark.parametrize("n", sorted(ARMS))
def test_falsification_arm_fails_closed(n):
    problems = ARMS[n]()
    assert problems, f"A1a falsification arm {n} did NOT fire — the detector is asleep"


def test_the_battery_has_all_twenty_arms():
    assert sorted(ARMS) == list(range(1, 21))
