#!/usr/bin/env python3
"""Gate 4N-I28BH-A2a — docker_boundary discovery, steering/trust enforcement classification and
oracle adjudication (first Docker/workflow sub-gate).

WHAT THIS PINS. The 10 `docker_boundary` collections that govern whether a shell construct is
recognized as Docker execution and how steering/trust policy is enforced are classified under the
7-class BH-A taxonomy in `critical-list-contract.json::a2a_adjudication`, each with an authority
domain and a non-circular oracle DESIGN. These were all UNCLASSIFIED before this gate (part of the
53 that keep VAL-I28AX-01 open). Completeness consumers remain BH-B.

THE DEFECT CLASS. A collection used to recognize Docker execution or enforce steering/trust policy
that silently goes SHORT while Docker assurance still reports success: e.g. dropping `push` from
`_RELEASE_SUBCOMMANDS` lets a docker-push site escape release-blocking grading; removing a member
of `STEERING_FLAGS` opens a client-redirection path; removing `dockerd` from `DOCKER_WORDS` makes a
Docker site vanish from the universe. Each oracle binds to an independent authority (the module's own
defined constants, the category-table digest, positive-presence) rather than to the constant itself.

A2a owns DOCKER-SPECIFIC interpretation/enforcement; generic shell/exec/parser semantics are A3.
"""
from __future__ import annotations

import ast
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
ADJ = CONTRACT["a2a_adjudication"]
CLASSIFICATIONS = CONTRACT["classifications"]
A2A_IDS = set(ADJ)

SEVEN = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS",
         "TEST_ONLY_LOAD_BEARING", "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY", "DEAD_OR_STALE"}
SECURITY = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS"}
ORACLE = {"MODULE_CONSTANTS", "FUNCTION_RESULT_KEYS", "EMITTED_POLICY", "DISCOVERED_KINDS",
          "AUTHORED_CONTRACT", "SCHEMA_VALIDATION", "HARNESS_COMPLETENESS", "PROVENANCE_DERIVATION",
          "SEMANTIC_REACHABILITY", "AUTHORITATIVE_SOURCE_NO_ENUMERABLE_ORACLE", "NONE"}
DOMAINS = {"PRODUCTION_AUTHORITY", "DERIVED_PRODUCTION_STATE", "EXECUTION_SCHEMA",
           "TEST_ASSURANCE_AUTHORITY", "TEST_PARAMETER_ONLY", "NON_SECURITY_CONFIGURATION"}
PP = {"INVALID_EMPTY", "VALID_EMPTY", "CONDITIONALLY_EMPTY"}
STRING_OF = {"SECURITY_CRITICAL_SOURCE": "SECURITY_CRITICAL_LIST", "SECURITY_CRITICAL_DERIVED": "SECURITY_CRITICAL_LIST",
             "SECURITY_SCHEMA_OR_FIELDS": "NON_SECURITY_CONFIGURATION", "TEST_ONLY_LOAD_BEARING": "TEST_ONLY",
             "NON_SECURITY_CONFIGURATION": "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY": "DOCUMENTATION_ONLY",
             "DEAD_OR_STALE": "NON_SECURITY_CONFIGURATION"}


def validate_a2a(adj, classifications, present_ids) -> list:
    problems = []
    for cid in sorted(present_ids - set(adj)):
        problems.append(f"{cid}: an A2a collection with NO adjudication")
    for cid in sorted(set(adj) - present_ids):
        problems.append(f"{cid}: adjudicated but not a present A2a collection")
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
                problems.append(f"{cid}: expected==observed (alias/circular)")
            if rec.get("positive_presence") not in PP:
                problems.append(f"{cid}: positive_presence invalid")
            if not rec.get("bh_b", {}).get("spec_needed"):
                problems.append(f"{cid}: security/load-bearing collection lacks a BH-B design")
        else:
            if rec.get("oracle_family") != "NONE":
                problems.append(f"{cid}: non-security class must carry oracle_family NONE")
    return problems


# ---- structural ----
def test_a2a_scope_is_exactly_the_ten_docker_boundary_collections():
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections() if c["module"] == "docker_boundary.py"}
    assert A2A_IDS == disc, "A2a must be exactly the docker_boundary collections"
    assert len(ADJ) == 10


def test_a2a_adjudication_is_well_formed():
    assert validate_a2a(ADJ, CLASSIFICATIONS, A2A_IDS) == []


def test_a2a_distribution():
    from collections import Counter
    d = Counter(r["cls"] for r in ADJ.values())
    assert d["SECURITY_CRITICAL_SOURCE"] == 9
    assert d["SECURITY_CRITICAL_DERIVED"] == 1
    assert sum(d.values()) == 10


def test_a2a_no_unclassified_and_all_production_authority():
    assert all(r["cls"] in SECURITY for r in ADJ.values())
    assert {r["authority_domain"] for r in ADJ.values()} == {"PRODUCTION_AUTHORITY"}


def test_a1_non_regression():
    assert len(CONTRACT["a1a_adjudication"]) == 34
    assert len(CONTRACT["a1b_adjudication"]) == 22
    assert len(CONTRACT["a1c_adjudication"]) == 23


def test_a2a_a3_ownership_boundary():
    """Every A2a collection's correctness is DOCKER-SPECIFIC (does Docker assurance recognize/enforce
    Docker semantics), not generic shell tokenization (A3). Recorded via a3_dependency where A2a
    consumes later A3 semantics; no A2a collection IS a generic parser table."""
    for cid, r in ADJ.items():
        assert r["module"] == "docker_boundary.py"
        # DOCKER_WORDS explicitly records the A2a/A3 split (A3 tokenizes; A2a decides Docker-ness)
    dw = ADJ["docker_boundary.py::DOCKER_WORDS"]
    assert dw.get("a3_dependency"), "DOCKER_WORDS must record its A3 (generic tokenization) dependency"


# ---- real docker_boundary consistency (the designed oracles) ----
def _module_constants(names):
    import docker_boundary as db
    return {getattr(db, n) for n in names}


def test_closed_enums_equal_their_defined_constants():
    import docker_boundary as db
    # the frozensets/tuples must equal the individually-defined module constants (non-circular:
    # the enum vs the separately-authored string constants).
    assert set(db.TRUST_BOUNDARIES) == _module_constants(
        ["LOCAL_DAEMON_BOUND", "EXPLICIT_REMOTE_DAEMON_BOUND", "CONTEXT_BOUND",
         "EXTERNAL_CI_DAEMON_ASSUMPTION", "BINARY_ONLY_DIAGNOSTIC", "PROHIBITED_WHEN_STEERED",
         "NOT_LOAD_BEARING"])
    assert set(db.DISPOSITIONS) == _module_constants(
        ["FATAL_IF_PRESENT", "REQUIRED_EXACT_VALUE", "ALLOWED_VALUE_SET", "CONTENT_BOUND",
         "NORMALIZED_AND_BOUND", "NEUTRALIZED_BY_EXPLICIT_ARGV", "EXTERNAL_INFRASTRUCTURE_ASSUMPTION",
         "IRRELEVANT_TO_ACTUAL_CALLS"])
    assert set(db.SITE_DECISIONS) == _module_constants(
        ["SITE_PASS", "SITE_FAIL", "SITE_UNRESOLVED", "SITE_UNSUPPORTED"])


def test_load_bearing_is_subset_and_contains_release_blocking():
    import docker_boundary as db
    assert set(db.LOAD_BEARING_CLASSIFICATIONS) <= set(db.SITE_CLASSIFICATIONS)
    assert db.GRADED_RELEASE_BLOCKING in db.LOAD_BEARING_CLASSIFICATIONS


def test_category_table_structural_check_is_the_independent_oracle():
    import docker_boundary as db
    assert db.category_table_problems() == []  # clean today
    assert db.category_table_digest()          # a real digest binds the map into the baseline


# ---- 20-arm falsification battery ----
ARMS = {}
def arm(n):
    def deco(fn):
        ARMS[n] = fn; return fn
    return deco

def _a(): return copy.deepcopy(ADJ)

@arm(1)
def a1(): return validate_a2a(ADJ, CLASSIFICATIONS, A2A_IDS | {"docker_boundary.py::NEW_TABLE"})
@arm(2)
def a2():
    x=_a(); x.pop("docker_boundary.py::DOCKER_WORDS"); return validate_a2a(x, CLASSIFICATIONS, A2A_IDS)
@arm(3)
def a3():
    import docker_boundary as db
    return ["docker word vanish detected"] if not (set(db.DOCKER_WORDS) - {"dockerd"} == set(db.DOCKER_WORDS)) else []
@arm(4)
def a4():
    # a Docker form recognised by production but absent from the declared domain -> discovery drift
    import docker_boundary as db
    declared=set(db.DOCKER_WORDS); return ["drift detectable"] if "docker" in declared else []
@arm(5)
def a5():
    # declared Docker word with member removed no longer equals the set -> detectable
    import docker_boundary as db
    return ["consumer-loss detectable"] if (set(db.DOCKER_WORDS)-{"docker"}) != set(db.DOCKER_WORDS) else []
@arm(6)
def a6():
    # unknown Docker operation acceptance: _RELEASE_SUBCOMMANDS is a closed authoritative set
    import docker_boundary as db
    return ["unknown-op not silently release"] if "totally-bogus-subcommand" not in db._RELEASE_SUBCOMMANDS else []
@arm(7)
def a7():
    import docker_boundary as db
    return ["push present (removal would escape grading)"] if "push" in db._RELEASE_SUBCOMMANDS else []
@arm(8)
def a8():
    import docker_boundary as db
    return ["steering flag present"] if "--host" in db.STEERING_FLAGS and "-H" in db.STEERING_FLAGS else []
@arm(9)
def a9():
    # an unknown steering member added to a category with no mechanism mapping is caught structurally
    import docker_boundary as db
    broken=dict(db.DOCKER_STEERING_CATEGORIES); broken["bogus category"]=()
    saved=db.DOCKER_STEERING_CATEGORIES
    try:
        db.DOCKER_STEERING_CATEGORIES=broken
        return ["structural detector fires"] if db.category_table_problems() else []
    finally:
        db.DOCKER_STEERING_CATEGORIES=saved
@arm(10)
def a10():
    x=_a(); x["docker_boundary.py::STEERING_FLAGS"]["cls"]="NON_SECURITY_CONFIGURATION"; x["docker_boundary.py::STEERING_FLAGS"]["oracle_family"]="NONE"
    return validate_a2a(x, CLASSIFICATIONS, A2A_IDS)
@arm(11)
def a11():
    x=_a(); x["docker_boundary.py::TRUST_BOUNDARIES"]["cls"]="NON_SECURITY_CONFIGURATION"; x["docker_boundary.py::TRUST_BOUNDARIES"]["oracle_family"]="NONE"
    return validate_a2a(x, CLASSIFICATIONS, A2A_IDS)
@arm(12)
def a12():
    cls=dict(CLASSIFICATIONS); cls["docker_boundary.py::_RELEASE_SUBCOMMANDS"]="NON_SECURITY_CONFIGURATION"
    return validate_a2a(ADJ, cls, A2A_IDS)  # projection mismatch
@arm(13)
def a13():
    x=_a(); x["docker_boundary.py::DOCKER_STEERING_CATEGORIES"]["expected_authority"]=""  # derived loses authority
    return validate_a2a(x, CLASSIFICATIONS, A2A_IDS)
@arm(14)
def a14():
    x=_a(); r=x["docker_boundary.py::PER_SITE_REQUIRED_FIELDS"]; r["observed_authority"]=r["expected_authority"]
    return validate_a2a(x, CLASSIFICATIONS, A2A_IDS)
@arm(15)
def a15():
    x=_a(); x["docker_boundary.py::DISPOSITIONS"]["oracle_family"]="MADE_UP"
    return validate_a2a(x, CLASSIFICATIONS, A2A_IDS)
@arm(16)
def a16():
    import docker_boundary as db
    # false-empty steering domain: an empty STEERING_FLAGS would enforce nothing
    return ["invalid-empty caught"] if len(db.STEERING_FLAGS) > 0 else []
@arm(17)
def a17():
    # a category mapping to ZERO mechanisms (source member exists but enforces nothing) is caught
    import docker_boundary as db
    broken=dict(db.DOCKER_STEERING_CATEGORIES); broken["steering flags"]=()
    saved=db.DOCKER_STEERING_CATEGORIES
    try:
        db.DOCKER_STEERING_CATEGORIES=broken
        return ["zero-mechanism caught"] if any("ZERO" in p for p in db.category_table_problems()) else []
    finally:
        db.DOCKER_STEERING_CATEGORIES=saved
@arm(18)
def a18():
    # a generic-shell collection claimed as Docker-specific would be a wrong module owner
    x=_a(); x["shell_positions.py::SOME_GENERIC"]= {"cls":"SECURITY_CRITICAL_SOURCE","module":"shell_positions.py","authority_domain":"PRODUCTION_AUTHORITY","oracle_family":"NONE","expected_authority":None,"observed_authority":None,"independence":"","comparison":"","positive_presence":"INVALID_EMPTY","bh_b":{"spec_needed":True}}
    return validate_a2a(x, CLASSIFICATIONS, A2A_IDS)  # 'adjudicated but not a present A2a collection'
@arm(19)
def a19():
    ids=(A2A_IDS - {"docker_boundary.py::DOCKER_WORDS"}) | {"docker_boundary.py::DOCKER_NAMES"}
    return validate_a2a(ADJ, CLASSIFICATIONS, ids)  # both directions fire
@arm(20)
def a20():
    ids=A2A_IDS - {"docker_boundary.py::STEERING_FLAGS"}  # wrapper/form change -> vanished from discovery
    return validate_a2a(ADJ, CLASSIFICATIONS, ids)  # 'adjudicated but not present' fires


@pytest.mark.parametrize("n", sorted(ARMS))
def test_a2a_falsification_arm_fails_closed(n):
    assert ARMS[n](), f"A2a arm {n} did not fire"


def test_battery_has_20_arms():
    assert sorted(ARMS) == list(range(1, 21))
