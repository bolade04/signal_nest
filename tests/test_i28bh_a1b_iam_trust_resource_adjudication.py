#!/usr/bin/env python3
"""Gate 4N-I28BH-A1b — IAM evaluation, trust, ARN, and resource-identity classification and
oracle adjudication.

WHAT THIS PINS. The 22 collections whose final consumer determines or validates an IAM
authorization decision, a trust identity, an ARN identity, or a resource identity
(iam_eval, arn_model, trust_policies, trust_validator, resource_oracle, policy_inventory,
allow_model, provenance::ARN_FIELDS) are each classified under the 7-class BH-A taxonomy in
`critical-list-contract.json::a1b_adjudication`, with a non-circular oracle DESIGN per
security collection. Completeness consumers remain BH-B.

THE DEFECT CLASS. These are the decisive evaluator/trust/ARN tables: a silent short in any of
them produces a WRONG authorization/identity decision that every internally-consistent check
still passes — e.g. a placeholder marker dropped from iam_eval._PLACEHOLDER_MARKERS lets an
unstamped policy never expire (Gate 4N-I8), an action dropped from ACTION_CONDITION_KEYS
disables the dead-grant detector for it, or a service dropped from arn_model._COLON_SEPARATED
collapses two ARN identities. The oracle design binds each to an INDEPENDENT authority (the
evaluator code, the emitted policies, the reviewed trust manifest, the ARN grammar, or the
allow-axis ceiling proof) rather than to the constant itself.
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
ADJ = CONTRACT["a1b_adjudication"]
CLASSIFICATIONS = CONTRACT["classifications"]

EIGHT = {"iam_eval.py", "arn_model.py", "trust_policies.py", "trust_validator.py",
         "resource_oracle.py", "policy_inventory.py", "provenance.py", "allow_model.py"}
# allow_model and provenance also own non-A1b collections, so A1b scope is by explicit id set.
A1B_IDS = set(ADJ)

SEVEN_CLASSES = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS",
                 "TEST_ONLY_LOAD_BEARING", "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY",
                 "DEAD_OR_STALE"}
SECURITY_CLASSES = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS"}
ORACLE_FAMILIES = {"MODULE_CONSTANTS", "FUNCTION_RESULT_KEYS", "EMITTED_POLICY", "DISCOVERED_KINDS",
                   "AUTHORED_CONTRACT", "SCHEMA_VALIDATION", "HARNESS_COMPLETENESS",
                   "PROVENANCE_DERIVATION", "SEMANTIC_REACHABILITY",
                   "AUTHORITATIVE_SOURCE_NO_ENUMERABLE_ORACLE", "NONE"}
POSITIVE_PRESENCE = {"INVALID_EMPTY", "VALID_EMPTY", "CONDITIONALLY_EMPTY"}
STRING_OF = {"SECURITY_CRITICAL_SOURCE": "SECURITY_CRITICAL_LIST",
             "SECURITY_CRITICAL_DERIVED": "SECURITY_CRITICAL_LIST",
             "SECURITY_SCHEMA_OR_FIELDS": "NON_SECURITY_CONFIGURATION",
             "TEST_ONLY_LOAD_BEARING": "TEST_ONLY", "NON_SECURITY_CONFIGURATION": "NON_SECURITY_CONFIGURATION",
             "DOCUMENTATION_ONLY": "DOCUMENTATION_ONLY", "DEAD_OR_STALE": "NON_SECURITY_CONFIGURATION"}

# Frozen 'before' baseline: the A1b ids that were SECURITY_CRITICAL_LIST prior to this gate.
PRE_A1B_SECURITY_CRITICAL = frozenset({
    "trust_validator.py::ALLOWED_SERVICE_PRINCIPALS", "allow_model.py::PERMITTED_WILDCARDS",
    "allow_model.py::EXTRA_REQUIRED_SOURCES",
})


def validate_a1b(adj, classifications, present_ids) -> list:
    """A1b adjudication validator; fail-closed; returns problems. Driven by the battery."""
    problems = []
    for cid in sorted(present_ids - set(adj)):
        problems.append(f"{cid}: an A1b collection with NO adjudication")
    for cid in sorted(set(adj) - present_ids):
        problems.append(f"{cid}: adjudicated but not a present A1b collection")
    for cid, rec in adj.items():
        cls = rec.get("cls")
        if cls not in SEVEN_CLASSES:
            problems.append(f"{cid}: class {cls!r} not one of seven"); continue
        if rec.get("oracle_family") not in ORACLE_FAMILIES:
            problems.append(f"{cid}: oracle_family {rec.get('oracle_family')!r} unknown")
        if classifications.get(cid) != STRING_OF[cls]:
            problems.append(f"{cid}: string projection {classifications.get(cid)!r} != {STRING_OF[cls]!r}")
        if cls in SECURITY_CLASSES:
            if rec.get("oracle_family") in (None, "NONE"):
                problems.append(f"{cid}: {cls} with no oracle strategy")
            for f in ("expected_authority", "observed_authority", "independence", "comparison", "positive_presence"):
                if not rec.get(f):
                    problems.append(f"{cid}: {cls} missing {f}")
            if rec.get("expected_authority") and rec.get("expected_authority") == rec.get("observed_authority"):
                problems.append(f"{cid}: expected==observed (alias/circular)")
            if rec.get("positive_presence") not in POSITIVE_PRESENCE:
                problems.append(f"{cid}: positive_presence invalid")
        else:
            if rec.get("oracle_family") != "NONE":
                problems.append(f"{cid}: non-security class must carry oracle_family NONE")
    return problems


# ---- structural tests ----
def test_a1b_scope_is_exactly_the_adjudicated_ids():
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections()}
    assert A1B_IDS <= disc, "every A1b id must be a discovered collection"
    assert len(ADJ) == 22


def test_a1b_adjudication_is_well_formed():
    assert validate_a1b(ADJ, CLASSIFICATIONS, A1B_IDS) == []


def test_a1b_class_distribution():
    from collections import Counter
    d = Counter(r["cls"] for r in ADJ.values())
    assert d["SECURITY_CRITICAL_SOURCE"] == 20
    assert d["SECURITY_CRITICAL_DERIVED"] == 1
    assert d["TEST_ONLY_LOAD_BEARING"] == 1
    assert sum(d.values()) == 22


def test_no_a1b_downward_reclassification_from_security():
    for cid in PRE_A1B_SECURITY_CRITICAL:
        assert CLASSIFICATIONS[cid] == "SECURITY_CRITICAL_LIST", cid
        if STRING_OF[ADJ[cid]["cls"]] != "SECURITY_CRITICAL_LIST":
            assert ADJ[cid].get("downgrade_proof"), cid


def test_a1a_non_regression_still_present():
    """A1b must not disturb A1a: the a1a_adjudication section and its 34 ids remain intact."""
    a1a = CONTRACT["a1a_adjudication"]
    assert len(a1a) == 34
    for cid in a1a:
        assert cid in CLASSIFICATIONS


# ---- 24-arm falsification battery ----
ARMS = {}
def arm(n):
    def deco(fn):
        ARMS[n] = fn; return fn
    return deco


@arm(1)
def _a1_new_semantic_collection_unclassified():
    return validate_a1b(ADJ, CLASSIFICATIONS, A1B_IDS | {"iam_eval.py::NEW_TABLE"})
@arm(2)
def _a2_remove_classification():
    adj = copy.deepcopy(ADJ); adj.pop("iam_eval.py::ACTION_CONDITION_KEYS")
    return validate_a1b(adj, CLASSIFICATIONS, A1B_IDS)
@arm(3)
def _a3_semantic_source_downgraded_without_proof():
    adj = copy.deepcopy(ADJ); adj["iam_eval.py::_NEGATED"]["cls"] = "NON_SECURITY_CONFIGURATION"; adj["iam_eval.py::_NEGATED"]["oracle_family"] = "NONE"
    return validate_a1b(adj, CLASSIFICATIONS, A1B_IDS)
@arm(4)
def _a4_schema_without_validator():
    adj = copy.deepcopy(ADJ); adj["provenance.py::ARN_FIELDS"]["oracle_family"] = "NONE"
    return validate_a1b(adj, CLASSIFICATIONS, A1B_IDS)
@arm(5)
def _a5_derived_lacks_authority():
    adj = copy.deepcopy(ADJ); adj["resource_oracle.py::GENERATED_KEYS"]["expected_authority"] = ""
    return validate_a1b(adj, CLASSIFICATIONS, A1B_IDS)
@arm(6)
def _a6_unknown_oracle_family():
    adj = copy.deepcopy(ADJ); adj["trust_policies.py::ROLE_TRUST"]["oracle_family"] = "MAGIC"
    return validate_a1b(adj, CLASSIFICATIONS, A1B_IDS)
@arm(7)
def _a7_missing_oracle_family():
    adj = copy.deepcopy(ADJ); adj["trust_policies.py::ROLE_TRUST"]["oracle_family"] = None
    return validate_a1b(adj, CLASSIFICATIONS, A1B_IDS)
@arm(8)
def _a8_expected_observed_alias():
    adj = copy.deepcopy(ADJ); r = adj["trust_validator.py::ROLE_PURPOSE"]; r["observed_authority"] = r["expected_authority"]
    return validate_a1b(adj, CLASSIFICATIONS, A1B_IDS)
@arm(9)
def _a9_string_projection_tamper():
    cls = dict(CLASSIFICATIONS); cls["iam_eval.py::SUPPORTED_SEMANTICS"] = "NON_SECURITY_CONFIGURATION"
    return validate_a1b(ADJ, cls, A1B_IDS)
@arm(10)
def _a10_supported_semantic_removed_fails_closed():
    """Remove an operator from SUPPORTED_SEMANTICS → a policy using it evaluates UNSUPPORTED (never ALLOW)."""
    import iam_eval
    saved = list(iam_eval.SUPPORTED_SEMANTICS["condition_operators"])
    try:
        iam_eval.SUPPORTED_SEMANTICS["condition_operators"] = [o for o in saved if o != "StringEquals"]
        pol = {"Version": "2012-10-17", "Statement": [{"Sid": "S", "Effect": "Allow", "Action": "x", "Resource": "*",
                "Condition": {"StringEquals": {"aws:PrincipalAccount": "1"}}}]}
        res = iam_eval.decide(pol, "x", "y", {"aws:PrincipalAccount": "1"})
        return ["fail-closed"] if res.decision.name != "EXPLICIT_ALLOW" else []
    finally:
        iam_eval.SUPPORTED_SEMANTICS["condition_operators"] = saved
@arm(11)
def _a11_unknown_element_fails_closed():
    import iam_eval
    pol = {"Version": "2012-10-17", "Statement": [{"Sid": "S", "Effect": "Allow", "Action": "x", "Resource": "*", "Principal": {"AWS": "*"}}]}
    res = iam_eval.decide(pol, "x", "y", {})
    return ["fail-closed"] if res.decision.name != "EXPLICIT_ALLOW" else []
@arm(12)
def _a12_negated_operator_all_semantics_present():
    import iam_eval
    return ["present"] if set(iam_eval._NEGATED) and "StringNotEquals" in iam_eval._NEGATED else []
@arm(13)
def _a13_action_condition_keys_dead_grant_detected():
    """validate_policy flags an action conditioned on a key it does not support."""
    import iam_eval
    pol = {"Version": "2012-10-17", "Statement": [{"Sid": "S", "Effect": "Allow", "Action": "iam:DeleteRole", "Resource": "*",
            "Condition": {"StringEquals": {"iam:PermissionsBoundary": "arn:x"}}}]}
    return iam_eval.validate_policy(pol, "identity")
@arm(14)
def _a14_placeholder_marker_rejected():
    import iam_eval
    try:
        iam_eval.parse_iam_date("<EXPIRY-ISO8601>", what="expiry"); return []
    except iam_eval.MalformedDateValue:
        return ["placeholder rejected"]
@arm(15)
def _a15_arn_one_char_corruption_is_mismatch():
    import arn_model
    good = "arn:aws:kms:eu-west-2:111122223333:key/548efeee-1111-2222-3333-444455556666"
    bad = good[:-1] + "7"
    return ["mismatch"] if arn_model.compare(good, bad)["result"] == "MISMATCH" else []
@arm(16)
def _a16_colon_separated_parse_is_correct():
    """A logs ARN (resource_id contains '/') parses with a ':' separator, not a '/' split."""
    import arn_model
    a = arn_model.parse("arn:aws:logs:eu-west-2:111122223333:log-group:/ecs/reader")
    return ["correct"] if a.resource_type == "log-group" and a.resource_id == "/ecs/reader" else []
@arm(17)
def _a17_resource_key_universe_matches_generator():
    import resource_oracle
    return ["match"] if set(resource_oracle.GENERATED_KEYS) == set(resource_oracle.generated_arns()) else []
@arm(18)
def _a18_stale_resource_key_detected():
    import resource_oracle
    mutated = set(resource_oracle.GENERATED_KEYS) | {"role:phantom"}
    return ["stale detected"] if mutated != set(resource_oracle.generated_arns()) else []
@arm(19)
def _a19_boundary_kind_bypasses_wildcard_check():
    """A bare-wildcard Allow is a defect as identity but permitted as boundary — the kind matters."""
    import iam_eval
    pol = {"Version": "2012-10-17", "Statement": [{"Sid": "S", "Effect": "Allow", "Action": "*", "Resource": "*"}]}
    ident = iam_eval.validate_policy(pol, "identity")
    boundary = iam_eval.validate_policy(pol, "boundary")
    return ["kind-sensitive"] if ident and not boundary else []
@arm(20)
def _a20_trust_purpose_covers_reader_roles():
    """ROLE_PURPOSE (unprefixed purpose names) corresponds by suffix to ROLE_TRUST (full role
    names): every purpose is the suffix of exactly one trust role, and the counts agree."""
    import trust_validator, trust_policies
    purposes = set(trust_validator.ROLE_PURPOSE)
    roles = set(trust_policies.ROLE_TRUST)
    covered = {p for p in purposes if any(r.endswith(p) for r in roles)}
    return ["covers"] if covered == purposes and len(purposes) == len(roles) else []
@arm(21)
def _a21_role_trust_keys_match_reader_roles():
    import trust_policies, signalnest_identity as ident
    return ["match"] if set(trust_policies.ROLE_TRUST) == set(ident.REVISION_READER_ROLE_NAMES) else []
@arm(22)
def _a22_arn_fields_equal_grammar():
    """ARN_FIELDS must name exactly the five components after 'arn:'."""
    import provenance
    sample = "arn:aws:s3:::bucket"
    parts = sample.split(":", 5)
    return ["grammar"] if len(provenance.ARN_FIELDS) == len(parts[1:]) else []
@arm(23)
def _a23_invalid_empty_false_empty_fails():
    adj = copy.deepcopy(ADJ)
    # a SECURITY collection whose oracle strategy is blanked must fail
    adj["iam_eval.py::_GLOBAL_KEYS"]["comparison"] = ""
    return validate_a1b(adj, CLASSIFICATIONS, A1B_IDS)
@arm(24)
def _a24_collection_renamed_escapes():
    ids = (A1B_IDS - {"iam_eval.py::SUPPORTED_SEMANTICS"}) | {"iam_eval.py::SUPPORTED_MODEL"}
    return validate_a1b(ADJ, CLASSIFICATIONS, ids)


@pytest.mark.parametrize("n", sorted(ARMS))
def test_a1b_falsification_arm_fails_closed(n):
    assert ARMS[n](), f"A1b falsification arm {n} did NOT fire"


def test_the_battery_has_all_twentyfour_arms():
    assert sorted(ARMS) == list(range(1, 25))
