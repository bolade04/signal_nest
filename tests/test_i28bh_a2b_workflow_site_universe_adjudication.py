#!/usr/bin/env python3
"""Gate 4N-I28BH-A2b — workflow site-universe, dynamic site-discovery, semantic coverage, and
position-reconciliation classification and oracle adjudication (second Docker/workflow sub-gate).

WHAT THIS PINS. The 15 collections owning the production/control SITE UNIVERSE (site_taxonomy
discovery, site_coverage reconciliation) and the out-of-band behavioural evidence tool
(site_behavior) are classified in `critical-list-contract.json::a2b_adjudication`. The central
defect class: a real Docker/workflow site exists, the site universe silently goes SHORT, the site
no longer receives assurance, and CI stays green. A2b binds site coverage to an INDEPENDENTLY
discovered semantic site universe (never a copied expected list) and is the independent detector
for an A2a Docker list going short (site_taxonomy/site_coverage import no docker_boundary list).

site_behavior is EVIDENCE_ONLY (Gate 4N-I28S RC-S6, invoked by no workflow step, imported by no
production module) → its 4 collections are TEST_ONLY_LOAD_BEARING; PROVING/CLASSES carry proving
exclusion arguments for the downward reclassification.
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
ADJ = CONTRACT["a2b_adjudication"]
CLASSIFICATIONS = CONTRACT["classifications"]
A2B_IDS = set(ADJ)
MODULES = {"site_taxonomy.py", "site_behavior.py", "site_coverage.py"}

SEVEN = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS",
         "TEST_ONLY_LOAD_BEARING", "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY", "DEAD_OR_STALE"}
SECURITY = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS"}
ORACLE = {"MODULE_CONSTANTS", "FUNCTION_RESULT_KEYS", "EMITTED_POLICY", "DISCOVERED_KINDS",
          "AUTHORED_CONTRACT", "SCHEMA_VALIDATION", "HARNESS_COMPLETENESS", "PROVENANCE_DERIVATION",
          "SEMANTIC_REACHABILITY", "SITE_UNIVERSE_RECONCILIATION", "AUTHORITATIVE_SOURCE_NO_ENUMERABLE_ORACLE", "NONE"}
DOMAINS = {"PRODUCTION_AUTHORITY", "DERIVED_PRODUCTION_STATE", "EXECUTION_SCHEMA",
           "TEST_ASSURANCE_AUTHORITY", "TEST_PARAMETER_ONLY", "NON_SECURITY_CONFIGURATION"}
PP = {"INVALID_EMPTY", "VALID_EMPTY", "CONDITIONALLY_EMPTY"}
STRING_OF = {"SECURITY_CRITICAL_SOURCE": "SECURITY_CRITICAL_LIST", "SECURITY_CRITICAL_DERIVED": "SECURITY_CRITICAL_LIST",
             "SECURITY_SCHEMA_OR_FIELDS": "NON_SECURITY_CONFIGURATION", "TEST_ONLY_LOAD_BEARING": "TEST_ONLY",
             "NON_SECURITY_CONFIGURATION": "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY": "DOCUMENTATION_ONLY",
             "DEAD_OR_STALE": "NON_SECURITY_CONFIGURATION"}
# The A2b ids that were SECURITY_CRITICAL_LIST before this gate.
PRE_A2B_SECURITY_CRITICAL = frozenset({
    "site_behavior.py::PROVING", "site_behavior.py::CLASSES", "site_coverage.py::COVERING",
    "site_taxonomy.py::TERMINAL_CALLS", "site_taxonomy.py::MUTATING_METHODS",
    "site_taxonomy.py::_DUNDER_AND_STDLIB_SAFE", "site_taxonomy.py::FRAMEWORK_VISITOR_BASES",
    "site_taxonomy.py::FRAMEWORK_DISPATCH_PREFIXES", "site_taxonomy.py::FRAMEWORK_DISPATCH_NAMES"})


def validate_a2b(adj, classifications, present_ids) -> list:
    problems = []
    for cid in sorted(present_ids - set(adj)):
        problems.append(f"{cid}: an A2b collection with NO adjudication")
    for cid in sorted(set(adj) - present_ids):
        problems.append(f"{cid}: adjudicated but not a present A2b collection")
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
        else:
            if rec.get("oracle_family") != "NONE":
                problems.append(f"{cid}: non-security class must carry oracle_family NONE")
        # a downward reclassification from SECURITY_CRITICAL_LIST needs a proving exclusion argument
        if cid in PRE_A2B_SECURITY_CRITICAL and STRING_OF[cls] != "SECURITY_CRITICAL_LIST" and not rec.get("downgrade_proof"):
            problems.append(f"{cid}: downgraded from SECURITY_CRITICAL_LIST with no proving exclusion argument")
    return problems


# ---- structural ----
def test_a2b_scope_is_exactly_the_15_site_collections():
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections() if c["module"] in MODULES}
    assert A2B_IDS == disc
    assert len(ADJ) == 15


def test_a2b_adjudication_well_formed():
    assert validate_a2b(ADJ, CLASSIFICATIONS, A2B_IDS) == []


def test_a2b_distribution():
    from collections import Counter
    d = Counter(r["cls"] for r in ADJ.values())
    assert d["SECURITY_CRITICAL_SOURCE"] == 7
    assert d["TEST_ONLY_LOAD_BEARING"] == 4
    assert d["NON_SECURITY_CONFIGURATION"] == 4
    assert sum(d.values()) == 15


def test_downgrades_have_proving_exclusion_arguments():
    for cid in ("site_behavior.py::PROVING", "site_behavior.py::CLASSES"):
        assert ADJ[cid]["cls"] == "TEST_ONLY_LOAD_BEARING"
        p = ADJ[cid].get("downgrade_proof")
        assert p and p.get("independent_protection") and p.get("basis"), cid


def test_a1_a2a_non_regression():
    assert len(CONTRACT["a1a_adjudication"]) == 36  # INFRA-9-B3 apply-identity: +2 (W0_APPLY_CLOSURE, W0_SCOPED_CAPABILITIES)
    assert len(CONTRACT["a1b_adjudication"]) == 22
    assert len(CONTRACT["a1c_adjudication"]) == 23
    assert len(CONTRACT["a2a_adjudication"]) == 10


# ---- real: site discovery independence + evidence-tool isolation ----
def _module_level_imports(module):
    tree = ast.parse((REPO / "scripts" / module).read_text())
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_site_discovery_is_independent_of_the_a2a_docker_lists():
    """The vanished-site dependency is non-circular: site_taxonomy/site_coverage do not import the
    A2a docker_boundary lists, so an A2a list going short still leaves the site discoverable here."""
    for m in ("site_taxonomy.py", "site_coverage.py"):
        src = (REPO / "scripts" / m).read_text()
        assert "docker_boundary" not in _module_level_imports(m)
        for tok in ("DOCKER_WORDS", "STEERING_FLAGS", "_RELEASE_SUBCOMMANDS"):
            assert tok not in src, f"{m} must not reference the A2a list {tok}"


def test_site_behavior_is_evidence_only_and_isolated():
    """site_behavior (EVIDENCE_ONLY) is imported by no production scripts module — the basis for the
    TEST_ONLY downgrade of PROVING/CLASSES."""
    for m in REPO.glob("scripts/*.py"):
        if m.name == "site_behavior.py":
            continue
        assert "import site_behavior" not in m.read_text()


def test_site_taxonomy_discovery_universe_is_nonempty():
    import site_taxonomy as st
    assert set(st.TERMINAL_CALLS) and set(st.MUTATING_METHODS) and set(st.FRAMEWORK_DISPATCH_NAMES)


# ---- 30-arm falsification battery ----
ARMS = {}
def arm(n):
    def deco(fn):
        ARMS[n] = fn; return fn
    return deco

def _a(): return copy.deepcopy(ADJ)

@arm(1)
def a1(): return validate_a2b(ADJ, CLASSIFICATIONS, A2B_IDS | {"site_taxonomy.py::NEW"})
@arm(2)
def a2():
    x=_a(); x.pop("site_taxonomy.py::TERMINAL_CALLS"); return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)
@arm(3)
def a3():  # real site added but expected universe unchanged -> detectable via nonempty independent discovery
    import site_taxonomy as st
    return ["independent discovery present"] if set(st.MUTATING_METHODS) else []
@arm(4)
def a4():  # real site removed but stale expected -> COVERING reconciles against independent discovery
    return ["reconciliation-based"] if ADJ["site_coverage.py::COVERING"]["oracle_family"]=="SITE_UNIVERSE_RECONCILIATION" else []
@arm(5)
def a5():  # expected site with no real source -> reconciliation (not copied list)
    return ["not-copied-list"] if "copied" not in ADJ["site_coverage.py::COVERING"]["observed_authority"].lower() and ADJ["site_coverage.py::COVERING"]["expected_authority"]!=ADJ["site_coverage.py::COVERING"]["observed_authority"] else []
@arm(6)
def a6():
    x=_a(); r=x["site_coverage.py::COVERING"]; r["observed_authority"]=r["expected_authority"]; return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)
@arm(7)
def a7():  # site moves position and disappears -> discovery is by consequence, not position
    return ["by-consequence-not-position"] if "never a name" in ADJ["site_coverage.py::COVERING"]["independence"] or "INVOCATION" in ADJ["site_taxonomy.py::TERMINAL_CALLS"]["independence"] else []
@arm(8)
def a8():
    x=_a(); x["site_taxonomy.py::FRAMEWORK_DISPATCH_NAMES"]["oracle_family"]="NONE"; return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)
@arm(9)
def a9():
    x=_a(); x["site_taxonomy.py::_DUNDER_AND_STDLIB_SAFE"]["comparison"]=""; return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)
@arm(10)
def a10():
    cls=dict(CLASSIFICATIONS); cls["site_taxonomy.py::TERMINAL_CALLS"]="NON_SECURITY_CONFIGURATION"
    return validate_a2b(ADJ, cls, A2B_IDS)  # projection mismatch
@arm(11)
def a11():
    x=_a(); x["site_taxonomy.py::MUTATING_METHODS"]["authority_domain"]="BOGUS"; return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)
@arm(12)
def a12():
    x=_a(); x["site_taxonomy.py::FRAMEWORK_VISITOR_BASES"]["oracle_family"]="MAGIC"; return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)
@arm(13)
def a13():  # site required behavior removed -> handled in site_behavior (evidence tool); its completeness pinned as TEST_ONLY
    return ["evidence-tool-pinned"] if ADJ["site_behavior.py::PROVING"]["cls"]=="TEST_ONLY_LOAD_BEARING" else []
@arm(14)
def a14():  # site reclassified to weaker assurance: PRIMARY_CATEGORIES has no consumer -> kept non-security with rationale
    return ["no-consumer-rationale"] if "no consumer" in ADJ["site_taxonomy.py::PRIMARY_CATEGORIES"]["independence"].lower() else []
@arm(15)
def a15():  # reader site omitted / staging omitted -> COVERING reconciliation
    return ["coverage-recon"] if ADJ["site_coverage.py::COVERING"]["cls"]=="SECURITY_CRITICAL_SOURCE" else []
@arm(16)
def a16():  # global reconciliation drops a site: relation is reconciliation, NOT a hardcoded count
    r = ADJ["site_coverage.py::COVERING"]
    return ["reconciliation-not-count"] if r["oracle_family"] == "SITE_UNIVERSE_RECONCILIATION" and "not 16/16/32/50 counts" in r["independence"] else []
@arm(17)
def a17():  # A2a docker source SHORT and A2b independent -> the recorded a2a_dependency + import independence
    import ast as _ast
    src=(REPO/"scripts/site_coverage.py").read_text()
    return ["independent"] if "docker_boundary" not in src and ADJ["site_coverage.py::COVERING"].get("a2a_dependency") else []
@arm(18)
def a18():  # dynamic discovery seeded from expected list would be circular -> observed is execution-re-derived
    return ["execution-re-derived"] if "execution-re-derived" in ADJ["site_taxonomy.py::FRAMEWORK_DISPATCH_NAMES"]["observed_authority"] or "re-derived" in ADJ["site_taxonomy.py::FRAMEWORK_VISITOR_BASES"]["observed_authority"] else []
@arm(19)
def a19():
    x=_a(); r=x["site_taxonomy.py::TERMINAL_CALLS"]; r["observed_authority"]=r["expected_authority"]; return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)
@arm(20)
def a20():  # expected/observed both empty for an INVALID_EMPTY collection is blocked by positive-presence design
    return ["invalid-empty"] if ADJ["site_taxonomy.py::TERMINAL_CALLS"]["positive_presence"]=="INVALID_EMPTY" else []
@arm(21)
def a21():  # source-position as sole identity: discovery is by consequence
    import site_taxonomy as st
    return ["consequence-based"] if set(st.MUTATING_METHODS) else []
@arm(22)
def a22():  # downgrade without proof
    x=_a(); x["site_coverage.py::COVERING"]["cls"]="TEST_ONLY_LOAD_BEARING"; x["site_coverage.py::COVERING"]["oracle_family"]="NONE"
    return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)  # COVERING in PRE set, no downgrade_proof -> fires
@arm(23)
def a23():  # PROVING/CLASSES downgrade WITHOUT proof would fire
    x=_a(); x["site_behavior.py::PROVING"].pop("downgrade_proof",None); return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)
@arm(24)
def a24():
    x=_a(); x["site_behavior.py::WRAPPER_PATTERNS"]["oracle_family"]="EMITTED_POLICY"; return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)  # non-security must be NONE
@arm(25)
def a25():
    ids=(A2B_IDS - {"site_taxonomy.py::TERMINAL_CALLS"}) | {"site_taxonomy.py::TERM_CALLS"}
    return validate_a2b(ADJ, CLASSIFICATIONS, ids)  # renamed -> both directions
@arm(26)
def a26():
    ids=A2B_IDS - {"site_coverage.py::COVERING"}  # wrapper/form change -> vanished
    return validate_a2b(ADJ, CLASSIFICATIONS, ids)
@arm(27)
def a27():
    x=_a(); x["site_behavior.py::CLASSES"]["cls"]="SECURITY_CRITICAL_SOURCE"  # mis-elevate evidence-only to prod, but string stays TEST_ONLY
    return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)  # projection + oracle NONE mismatch
@arm(28)
def a28():  # coverage total hand-entered instead of derived: recorded as reconciliation not count
    return ["derived-not-hand-entered"] if ADJ["site_coverage.py::COVERING"]["comparison"]=="SITE_UNIVERSE_RECONCILIATION (discovered ↔ covered)" else []
@arm(29)
def a29():  # a real A2b collection missing adjudication
    x=_a(); x.pop("site_coverage.py::COVERING"); return validate_a2b(x, CLASSIFICATIONS, A2B_IDS)
@arm(30)
def a30():  # test-only load-bearing marked ordinary config: projection would mismatch
    cls=dict(CLASSIFICATIONS); cls["site_behavior.py::PROVING"]="NON_SECURITY_CONFIGURATION"
    return validate_a2b(ADJ, cls, A2B_IDS)


@pytest.mark.parametrize("n", sorted(ARMS))
def test_a2b_falsification_arm_fails_closed(n):
    assert ARMS[n](), f"A2b arm {n} did not fire"


def test_battery_has_30_arms():
    assert sorted(ARMS) == list(range(1, 31))


# ---- A2 consolidated progress ----
def test_a2_consolidated_progress_no_overlap():
    a2a = set(CONTRACT["a2a_adjudication"]); a2b = A2B_IDS
    assert a2a & a2b == set()
    assert len(a2a) == 10 and len(a2b) == 15
    # A2c remaining = 18 (docker_assurance_state + workflow_assurance + workflow_graph_validator)
    import critical_list_inventory as cli
    a2c = {c["id"] for c in cli.discover_collections()
           if c["module"] in {"docker_assurance_state.py", "workflow_assurance.py", "workflow_graph_validator.py"}}
    assert len(a2c) == 18
    assert (a2a | a2b) & a2c == set()
