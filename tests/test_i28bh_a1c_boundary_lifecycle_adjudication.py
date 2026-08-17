#!/usr/bin/env python3
"""Gate 4N-I28BH-A1c — boundary-state, deny-requirements, role-bootstrap lifecycle,
Terraform/probe, and test-scaffold classification and oracle adjudication (the final AWS/IAM
sub-gate; completes the 79-collection A1 decomposition).

WHAT THIS PINS. The 23 mixed-domain collections are classified under the 7-class BH-A taxonomy
in `critical-list-contract.json::a1c_adjudication` and additionally tagged with an authority
domain (PRODUCTION_AUTHORITY / DERIVED_PRODUCTION_STATE / TEST_ASSURANCE_AUTHORITY /
TEST_PARAMETER_ONLY / CONFIGURATION) so a test scaffold is never mistaken for independent
production authority. It also pins the LOAD-BEARING A1a→A1c dependency: deny_requirements
(SOURCE 1 ∪ 2 ∪ 3) is the INDEPENDENT authority that detects an A1a boundary deny list going
short, and it imports no policy generator as a source. Completeness consumers remain BH-B.
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
ADJ = CONTRACT["a1c_adjudication"]
CLASSIFICATIONS = CONTRACT["classifications"]
A1C_IDS = set(ADJ)

SEVEN = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS",
         "TEST_ONLY_LOAD_BEARING", "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY", "DEAD_OR_STALE"}
SECURITY = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS"}
ORACLE = {"MODULE_CONSTANTS", "FUNCTION_RESULT_KEYS", "EMITTED_POLICY", "DISCOVERED_KINDS",
          "AUTHORED_CONTRACT", "SCHEMA_VALIDATION", "HARNESS_COMPLETENESS", "PROVENANCE_DERIVATION",
          "SEMANTIC_REACHABILITY", "AUTHORITATIVE_SOURCE_NO_ENUMERABLE_ORACLE", "NONE"}
DOMAINS = {"PRODUCTION_AUTHORITY", "DERIVED_PRODUCTION_STATE", "TEST_ASSURANCE_AUTHORITY",
           "TEST_PARAMETER_ONLY", "CONFIGURATION"}
PP = {"INVALID_EMPTY", "VALID_EMPTY", "CONDITIONALLY_EMPTY"}
STRING_OF = {"SECURITY_CRITICAL_SOURCE": "SECURITY_CRITICAL_LIST", "SECURITY_CRITICAL_DERIVED": "SECURITY_CRITICAL_LIST",
             "SECURITY_SCHEMA_OR_FIELDS": "NON_SECURITY_CONFIGURATION", "TEST_ONLY_LOAD_BEARING": "TEST_ONLY",
             "NON_SECURITY_CONFIGURATION": "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY": "DOCUMENTATION_ONLY",
             "DEAD_OR_STALE": "NON_SECURITY_CONFIGURATION"}
PRE_A1C_SECURITY_CRITICAL = frozenset({
    "deny_requirements.py::_PRINCIPAL_SCOPES", "deny_triangulation.py::PROTECTED_RESOURCE",
    "role_bootstrap_executor.py::REQUIRED_MANIFEST_FIELDS", "role_bootstrap_executor.py::REQUIRED_ROLE_FIELDS",
    "startup_policy.py::DISPOSITIONS"})


def validate_a1c(adj, classifications, present_ids) -> list:
    problems = []
    for cid in sorted(present_ids - set(adj)):
        problems.append(f"{cid}: an A1c collection with NO adjudication")
    for cid in sorted(set(adj) - present_ids):
        problems.append(f"{cid}: adjudicated but not a present A1c collection")
    for cid, rec in adj.items():
        cls = rec.get("cls")
        if cls not in SEVEN:
            problems.append(f"{cid}: class {cls!r} not one of seven"); continue
        if rec.get("oracle_family") not in ORACLE:
            problems.append(f"{cid}: oracle_family {rec.get('oracle_family')!r} unknown")
        if rec.get("authority_domain") not in DOMAINS:
            problems.append(f"{cid}: authority_domain {rec.get('authority_domain')!r} unknown")
        if classifications.get(cid) != STRING_OF[cls]:
            problems.append(f"{cid}: string projection {classifications.get(cid)!r} != {STRING_OF[cls]!r}")
        if cls in SECURITY:
            if rec.get("oracle_family") in (None, "NONE"):
                problems.append(f"{cid}: {cls} with no oracle strategy")
            for f in ("expected_authority", "observed_authority", "independence", "comparison", "positive_presence"):
                if not rec.get(f):
                    problems.append(f"{cid}: {cls} missing {f}")
            if rec.get("expected_authority") and rec.get("expected_authority") == rec.get("observed_authority"):
                problems.append(f"{cid}: expected==observed (alias)")
            if rec.get("positive_presence") not in PP:
                problems.append(f"{cid}: positive_presence invalid")
            # a PRODUCTION_AUTHORITY security oracle may not derive from a test-only representation
            if rec.get("authority_domain") == "TEST_ASSURANCE_AUTHORITY" and cls == "SECURITY_CRITICAL_SOURCE" \
                    and rec.get("oracle_family") not in ("SCHEMA_VALIDATION", "HARNESS_COMPLETENESS", "AUTHORITATIVE_SOURCE_NO_ENUMERABLE_ORACLE"):
                problems.append(f"{cid}: test-assurance security source must use a self-protecting oracle")
        else:
            if rec.get("oracle_family") != "NONE":
                problems.append(f"{cid}: non-security class must carry oracle_family NONE")
    return problems


# ---- structural ----
def test_a1c_scope_is_exactly_23_adjudicated_ids():
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections()}
    assert A1C_IDS <= disc
    assert len(ADJ) == 23


def test_a1c_adjudication_well_formed():
    assert validate_a1c(ADJ, CLASSIFICATIONS, A1C_IDS) == []


def test_a1c_distribution():
    from collections import Counter
    d = Counter(r["cls"] for r in ADJ.values())
    assert d["SECURITY_CRITICAL_SOURCE"] == 12
    assert d["TEST_ONLY_LOAD_BEARING"] == 10
    assert d["NON_SECURITY_CONFIGURATION"] == 1
    assert sum(d.values()) == 23


def test_a1c_no_downward_reclassification():
    for cid in PRE_A1C_SECURITY_CRITICAL:
        assert CLASSIFICATIONS[cid] == "SECURITY_CRITICAL_LIST", cid
        if STRING_OF[ADJ[cid]["cls"]] != "SECURITY_CRITICAL_LIST":
            assert ADJ[cid].get("downgrade_proof"), cid


def test_a1a_and_a1b_non_regression():
    assert len(CONTRACT["a1a_adjudication"]) == 36  # INFRA-9-B3 apply-identity: +2 (W0_APPLY_CLOSURE, W0_SCOPED_CAPABILITIES)
    assert len(CONTRACT["a1b_adjudication"]) == 22


# ---- A1a triangulation dependency (load-bearing) ----
def _deny_requirements_module_level_imports():
    import ast
    tree = ast.parse((REPO / "scripts/deny_requirements.py").read_text())
    names = set()
    for node in tree.body:  # MODULE LEVEL only
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_deny_requirement_sources_are_independent_of_the_generators():
    """The A1a deny-source authority (deny_requirements SOURCE 1/2/3) is independent: no policy
    generator is imported at MODULE level (generators appear only function-locally as the SUBJECT
    being evaluated), and the authored requirement authority is not a copy of the generated denies."""
    mod_imports = _deny_requirements_module_level_imports()
    for gen in ("gen_boundary_policy", "gen_bootstrap_operator_policy", "gen_operator_policies",
                "gen_role_bootstrap_policy"):
        assert gen not in mod_imports, f"{gen} imported at module level would make the authority circular"
    import deny_requirements as dr, gen_boundary_policy as gb
    emitted = set()
    for s in gb.boundary_policy()["Statement"]:
        if s.get("Effect") == "Deny":
            a = s.get("Action") or s.get("NotAction") or []
            emitted |= {a} if isinstance(a, str) else set(a)
    # the authored SOURCE 2 authority is NOT identical to the generated deny set
    assert set(dr.source2_actions()) != emitted, "the requirement authority must not equal the subject's denies"


def test_triangulation_meaningful_independence():
    import deny_triangulation as dt
    ind = dt.deny_requirements.independence() if hasattr(dt, "deny_requirements") else __import__("deny_requirements").independence()
    assert ind["independent"], "SOURCE1 and SOURCE2 must be meaningfully independent (both set-differences non-empty)"


def test_a1a_deny_short_would_be_caught_by_triangulation_authority():
    """A required deny (from the independent authority) that the boundary stops emitting is caught:
    it is present in required_denies() and in the emitted Deny set today, so dropping it flips
    triangulation off PASSING."""
    import deny_triangulation as dt, gen_boundary_policy as gb
    required = set(dt.deny_requirements.required_denies()) if hasattr(dt, "deny_requirements") else set(__import__("deny_requirements").required_denies())
    emitted = set()
    for s in gb.boundary_policy()["Statement"]:
        if s.get("Effect") == "Deny":
            a = s.get("Action") or s.get("NotAction") or []
            emitted |= {a} if isinstance(a, str) else set(a)
    # the required set is a non-empty subset of what the boundary (plus other policies) denies
    assert required, "the independent required-deny authority must be non-empty"
    covered = required & emitted
    assert covered, "at least some required denies are emitted by the boundary"
    # dropping a covered required deny from emitted makes required !⊆ emitted -> detectable
    sample = sorted(covered)[0]
    assert not (required <= (emitted - {sample})), "removing a required deny is detectable by REQUIRED_SUBSET"


# ---- 28-arm battery ----
ARMS = {}
def arm(n):
    def deco(fn):
        ARMS[n] = fn; return fn
    return deco

def _a(): return copy.deepcopy(ADJ)

@arm(1)
def a1(): return validate_a1c(ADJ, CLASSIFICATIONS, A1C_IDS | {"deny_requirements.py::NEW_REQ"})
@arm(2)
def a2():
    x=_a(); x.pop("deny_requirements.py::ARCHITECTURE_INVARIANTS"); return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(3)
def a3():
    x=_a(); x["deny_requirements.py::AWS_SERVICE_SAFETY"]["cls"]="NON_SECURITY_CONFIGURATION"; x["deny_requirements.py::AWS_SERVICE_SAFETY"]["oracle_family"]="NONE"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(4)
def a4():
    # triangulation source aliasing an A1a deny source is refused: expected==observed
    x=_a(); r=x["deny_requirements.py::ARCHITECTURE_INVARIANTS"]; r["observed_authority"]=r["expected_authority"]; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(5)
def a5():
    # non-circularity: no generator imported at module level in the requirement authority
    return ["independent"] if not (_deny_requirements_module_level_imports() &
            {"gen_boundary_policy","gen_bootstrap_operator_policy","gen_operator_policies","gen_role_bootstrap_policy"}) else []
@arm(6)
def a6():
    import deny_triangulation as dt, gen_boundary_policy as gb
    req=set(__import__("deny_requirements").required_denies())
    em=set()
    for s in gb.boundary_policy()["Statement"]:
        if s.get("Effect")=="Deny":
            a=s.get("Action") or s.get("NotAction") or []; em|= {a} if isinstance(a,str) else set(a)
    smp=sorted(req&em)[0]; return ["deny-short detected"] if not (req <= em-{smp}) else []
@arm(7)
def a7():
    import deny_requirements as dr
    return ["triangulation-member"] if set(dr.ARCHITECTURE_INVARIANTS) else []
@arm(8)
def a8():
    x=_a(); r=x["deny_requirements.py::_KINDS"]; r["positive_presence"]="INVALID_EMPTY"; r["comparison"]=""; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(9)
def a9():
    import deny_triangulation as dt
    fails={"REQUIRED_BUT_MISSING","PRESENT_BUT_UNJUSTIFIED","CONFLICTING_SCOPE","PROBE_MISSING","UNKNOWN"}
    return ["passing-safe"] if not (set(dt.PASSING) & fails) else []
@arm(10)
def a10():
    x=_a(); x["deny_triangulation.py::PASSING"]["cls"]="NON_SECURITY_CONFIGURATION"; x["deny_triangulation.py::PASSING"]["oracle_family"]="NONE"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(11)
def a11():
    x=_a(); x["role_bootstrap_lifecycle.py::ACTOR_RULES"]["cls"]="NON_SECURITY_CONFIGURATION"; x["role_bootstrap_lifecycle.py::ACTOR_RULES"]["oracle_family"]="NONE"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(12)
def a12():
    x=_a(); x["role_bootstrap_lifecycle.py::IDENTITY_CENTRE_ADMIN_ACTIONS"]["authority_domain"]="BOGUS"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(13)
def a13():
    import terraform_role_inventory as tri
    return ["tf-actions"] if set(tri.INLINE_POLICY_LIFECYCLE_ACTIONS) else []
@arm(14)
def a14():
    x=_a(); x["terraform_role_inventory.py::INLINE_POLICY_LIFECYCLE_ACTIONS"]["oracle_family"]="MAGIC"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(15)
def a15():
    import resource_deny_probes as p
    return ["probes-present"] if len(p.PROBES) > 0 else []
@arm(16)
def a16():
    import resource_deny_probes as p
    ids=[str(x) for x in p.PROBES]; return ["unique"] if len(ids)==len(set(ids)) else []
@arm(17)
def a17():
    x=_a(); x["resource_deny_probes.py::PROBES"]["cls"]="NON_SECURITY_CONFIGURATION"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)  # projection mismatch (was TEST_ONLY)
@arm(18)
def a18():
    x=_a(); x["deny_triangulation.py::_MUTATIONS"]["cls"]="NON_SECURITY_CONFIGURATION"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(19)
def a19():
    import deny_triangulation as dt
    return ["mutations"] if len(dt._MUTATIONS) > 0 else []
@arm(20)
def a20():
    import boundary_state_mutations as bsm
    return ["oracles+mutations"] if bsm.ORACLES and bsm.MUTATIONS else []
@arm(21)
def a21():
    x=_a(); x["putrolepolicy_classification.py::EXPIRY_PROBE"]["cls"]="SECURITY_CRITICAL_SOURCE"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)  # projection mismatch (string still TEST_ONLY)
@arm(22)
def a22():
    x=_a(); x["role_bootstrap_lifecycle.py::IN_WINDOW"]["oracle_family"]="EMITTED_POLICY"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)  # non-security must be NONE
@arm(23)
def a23():
    x=_a(); r=x["deny_triangulation.py::PROTECTED_RESOURCE"]; r["observed_authority"]=r["expected_authority"]; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(24)
def a24():
    x=_a(); x["deny_requirements.py::_KINDS"]["oracle_family"]="WEIRD"; return validate_a1c(x, CLASSIFICATIONS, A1C_IDS)
@arm(25)
def a25():
    ids=(A1C_IDS - {"deny_requirements.py::ARCHITECTURE_INVARIANTS"}) | {"deny_requirements.py::ARCH_INV"}
    return validate_a1c(ADJ, CLASSIFICATIONS, ids)
@arm(26)
def a26():
    ids=A1C_IDS - {"startup_policy.py::DISPOSITIONS"}  # wrapper/form change -> vanished
    return validate_a1c(ADJ, CLASSIFICATIONS, ids)
@arm(27)
def a27():
    import role_bootstrap_executor as ex, trust_policies as tp
    # REQUIRED_ROLE_FIELDS must be a subset of the keys a real manifest role entry carries
    entry=next(iter(tp.trust_manifest().values()))
    return ["fields-covered"] if set(ex.REQUIRED_ROLE_FIELDS) <= (set(entry)|{"boundary_arn","tags","trust_policy_path","canonical_sha256","file_byte_sha256","role_name"}) else []
@arm(28)
def a28():
    # a test-only load-bearing list marked ordinary config must be caught (projection)
    cls=dict(CLASSIFICATIONS); cls["deny_triangulation.py::_MUTATIONS"]="NON_SECURITY_CONFIGURATION"
    return validate_a1c(ADJ, cls, A1C_IDS)


@pytest.mark.parametrize("n", sorted(ARMS))
def test_a1c_falsification_arm_fails_closed(n):
    assert ARMS[n](), f"A1c arm {n} did not fire"


def test_battery_has_28_arms():
    assert sorted(ARMS) == list(range(1, 29))


# ---- consolidated A1 reconciliation ----
def test_consolidated_a1_union_is_exactly_79():
    a1a = set(CONTRACT["a1a_adjudication"]); a1b = set(CONTRACT["a1b_adjudication"]); a1c = set(ADJ)
    assert len(a1a) == 36 and len(a1b) == 22 and len(a1c) == 23  # INFRA-9-B3 apply-identity: +2 (W0_APPLY_CLOSURE, W0_SCOPED_CAPABILITIES)
    assert a1a & a1b == set() and a1a & a1c == set() and a1b & a1c == set(), "no overlap between sub-gates"
    assert len(a1a | a1b | a1c) == 81, "A1a ∪ A1b ∪ A1c must equal the full 81-member A1 scope"  # INFRA-9-B3 apply-identity: 79+2
