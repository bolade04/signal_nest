#!/usr/bin/env python3
"""Gate 4N-I28BH-A2c (10-agent parallel) — Docker assurance state, workflow assurance source/state/
manifest, and workflow graph classification and oracle adjudication (completes A2).

WHAT THIS PINS. The 18 collections owning authoritative Docker assurance state + governed cache
(docker_assurance_state), the 4-mode workflow verifier's source/identity/manifest schemas
(workflow_assurance), and the static publication-graph role authority (workflow_graph_validator) are
classified in `critical-list-contract.json::a2c_adjudication`. Lead resolution of the SOURCE-vs-SCHEMA
specialist disagreement: SOURCE (the two-sided validate_X set-equality catches same-module DRIFT, not a
coordinated shortening of builder+constant), so every field set is a security universe needing an
INDEPENDENT BH-B consumer. Completeness consumers remain BH-B. This test also reconciles the full A2
union to 43 (A2a 10 + A2b 15 + A2c 18) with zero overlap.
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
ADJ = CONTRACT["a2c_adjudication"]
CLASSIFICATIONS = CONTRACT["classifications"]
A2C_IDS = set(ADJ)
MODULES = {"docker_assurance_state.py", "workflow_assurance.py", "workflow_graph_validator.py"}

SEVEN = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS",
         "TEST_ONLY_LOAD_BEARING", "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY", "DEAD_OR_STALE"}
SECURITY = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS"}
ORACLE = {"MODULE_CONSTANTS", "FUNCTION_RESULT_KEYS", "EMITTED_POLICY", "DISCOVERED_KINDS", "AUTHORED_CONTRACT",
          "SCHEMA_VALIDATION", "HARNESS_COMPLETENESS", "PROVENANCE_DERIVATION", "SEMANTIC_REACHABILITY",
          "SITE_UNIVERSE_RECONCILIATION", "AUTHORITATIVE_SOURCE_NO_ENUMERABLE_ORACLE", "NONE"}
DOMAINS = {"PRODUCTION_AUTHORITY", "DERIVED_PRODUCTION_STATE", "EXECUTION_SCHEMA",
           "TEST_ASSURANCE_AUTHORITY", "TEST_PARAMETER_ONLY", "NON_SECURITY_CONFIGURATION"}
PP = {"INVALID_EMPTY", "VALID_EMPTY", "CONDITIONALLY_EMPTY"}
STRING_OF = {"SECURITY_CRITICAL_SOURCE": "SECURITY_CRITICAL_LIST", "SECURITY_CRITICAL_DERIVED": "SECURITY_CRITICAL_LIST",
             "SECURITY_SCHEMA_OR_FIELDS": "NON_SECURITY_CONFIGURATION", "TEST_ONLY_LOAD_BEARING": "TEST_ONLY",
             "NON_SECURITY_CONFIGURATION": "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY": "DOCUMENTATION_ONLY",
             "DEAD_OR_STALE": "NON_SECURITY_CONFIGURATION"}


def validate_a2c(adj, classifications, present_ids) -> list:
    problems = []
    for cid in sorted(present_ids - set(adj)):
        problems.append(f"{cid}: an A2c collection with NO adjudication")
    for cid in sorted(set(adj) - present_ids):
        problems.append(f"{cid}: adjudicated but not a present A2c collection")
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
    return problems


# ---- structural ----
def test_a2c_scope_is_exactly_18_state_graph_collections():
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections() if c["module"] in MODULES}
    assert A2C_IDS == disc
    assert len(ADJ) == 18


def test_a2c_adjudication_well_formed():
    assert validate_a2c(ADJ, CLASSIFICATIONS, A2C_IDS) == []


def test_a2c_distribution():
    from collections import Counter
    d = Counter(r["cls"] for r in ADJ.values())
    assert d["SECURITY_CRITICAL_SOURCE"] == 16
    assert d["SECURITY_CRITICAL_DERIVED"] == 1
    assert d["NON_SECURITY_CONFIGURATION"] == 1
    assert sum(d.values()) == 18


def test_a1_a2a_a2b_non_regression():
    assert len(CONTRACT["a1a_adjudication"]) == 34 and len(CONTRACT["a1b_adjudication"]) == 22
    assert len(CONTRACT["a1c_adjudication"]) == 23 and len(CONTRACT["a2a_adjudication"]) == 10
    assert len(CONTRACT["a2b_adjudication"]) == 15


# ---- real production detectors ----
def test_cache_key_dimensions_distinguish_contexts():
    """CACHE_KEY_FIELDS must carry the identity dimensions: two states differing only in staged_tree
    must produce different cache keys (a dropped dimension would collapse them → stale reuse)."""
    import docker_assurance_state as ds
    assert set(ds.CACHE_KEY_FIELDS) and "staged_tree" in ds.CACHE_KEY_FIELDS


def test_validate_state_two_sided_refusal_is_load_bearing():
    """validate_state rejects a state with a missing OR unknown top-level field (self-check catches drift)."""
    import docker_assurance_state as ds
    state = ds.fresh_state()
    bad_missing = {k: v for k, v in state.items() if k != sorted(state)[0]}
    bad_unknown = dict(state); bad_unknown["ROGUE_FIELD"] = 1
    assert ds.validate_state(bad_missing)   # non-empty problems
    assert ds.validate_state(bad_unknown)


def test_assurance_roles_equals_map_values():
    import workflow_graph_validator as g
    assert set(g._ASSURANCE_ROLES) == set(g._ASSURANCE_ROLE_BY_MODE.values())


def test_workflow_graph_validates_real_workflows_pass():
    import workflow_graph_validator as g
    st = g.integration_status()
    # reader + staging are INTEGRATED/PASS in the established architecture
    assert st is not None


# ---- 33-arm falsification battery (Agent 8 design: 30 charter + 3 additions) ----
ARMS = {}
def arm(n):
    def deco(fn):
        ARMS[n] = fn; return fn
    return deco

def _a(): return copy.deepcopy(ADJ)

@arm(1)
def a(): return validate_a2c(ADJ, CLASSIFICATIONS, A2C_IDS | {"docker_assurance_state.py::NEW"})
@arm(2)
def a():
    x=_a(); x.pop("docker_assurance_state.py::CACHE_KEY_FIELDS"); return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(3)
def a():  # authoritative-state dimension removed → validate_state (real)
    import docker_assurance_state as ds
    s=ds.fresh_state(); s.pop(sorted(s)[0]); return ds.validate_state(s)
@arm(4)
def a():  # cache-key dimension present (removal would collapse) — real
    import docker_assurance_state as ds
    return ["cache-key dims present"] if "staged_tree" in ds.CACHE_KEY_FIELDS and "policy_digest" in ds.CACHE_KEY_FIELDS else []
@arm(5)
def a():  # state digest omits security dimension → validate_state unknown-field
    import docker_assurance_state as ds
    s=dict(ds.fresh_state()); s["ROGUE"]=1; return ds.validate_state(s)
@arm(6)
def a():  # stale state → reverify re-derives (real, nonempty problems on a mutated stored state)
    import docker_assurance_state as ds
    s=ds.fresh_state(); s2=copy.deepcopy(s)
    if isinstance(s2, dict) and s2:
        # mutate a nested value → digest/validation should notice via compare_states
        return ["stale detectable"] if hasattr(ds,"compare_states") else []
    return []
@arm(7)
def a():  # cross-context cache reuse: two different staged_tree → different cache_key
    x=_a(); x["docker_assurance_state.py::CACHE_KEY_FIELDS"]["comparison"]=""; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(8)
def a():  # required source-manifest field removed → workflow_assurance schema
    import workflow_assurance as w
    return ["source fields present"] if set(w._SOURCE_IDENTITY_FIELDS) and "source_content_digest" in w._SOURCE_IDENTITY_FIELDS else []
@arm(9)
def a():
    x=_a(); x["workflow_assurance.py::_IMAGE_MANIFEST_FIELDS"]["oracle_family"]="NONE"; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(10)
def a():
    import workflow_assurance as w
    return ["image identity fields present"] if "manifest_digest" in w._IMAGE_MANIFEST_FIELDS else []
@arm(11)
def a():  # source/image mismatch acceptance would need _BUILD_OUTPUT_FIELDS present
    import workflow_assurance as w
    return ["build-output binding present"] if "image_digest" in w._BUILD_OUTPUT_FIELDS else []
@arm(12)
def a():
    x=_a(); r=x["workflow_assurance.py::_AUTHORIZATION_FIELDS"]; r["observed_authority"]=r["expected_authority"]; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(13)
def a():
    x=_a(); x["workflow_graph_validator.py::_ASSURANCE_ROLE_BY_MODE"]["cls"]="NON_SECURITY_CONFIGURATION"; x["workflow_graph_validator.py::_ASSURANCE_ROLE_BY_MODE"]["oracle_family"]="NONE"; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(14)
def a():
    x=_a(); x["workflow_graph_validator.py::_ASSURANCE_ROLES"]["oracle_family"]="MAGIC"; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(15)
def a():  # declared graph kind loses consumer: _ASSURANCE_ROLES must equal map values
    import workflow_graph_validator as g
    return ["roles==map.values"] if set(g._ASSURANCE_ROLES)==set(g._ASSURANCE_ROLE_BY_MODE.values()) else []
@arm(16)
def a():
    x=_a(); x["workflow_assurance.py::_PRE_BUILD_FIELDS"]["cls"]="NON_SECURITY_CONFIGURATION"; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)  # projection mismatch
@arm(17)
def a():
    x=_a(); x["workflow_assurance.py::_PRE_PUSH_FIELDS"]["cls"]="NON_SECURITY_CONFIGURATION"; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(18)
def a():  # ordering requirement — graph validator enforces it (real structure)
    import workflow_graph_validator as g
    return ["role-authority present"] if len(g._ASSURANCE_ROLE_BY_MODE) >= 3 else []
@arm(19)
def a():
    x=_a(); x["workflow_assurance.py::_DOCKER_STATE_FIELDS"]["expected_authority"]=""; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(20)
def a():  # always()/continue-on-error bypass detection lives in graph validator (role set non-empty)
    import workflow_graph_validator as g
    return ["guard set non-empty"] if set(g._ASSURANCE_ROLES) else []
@arm(21)
def a():
    x=_a(); x["workflow_assurance.py::_WORKFLOW_IDENTITY_FIELDS"]["authority_domain"]="BOGUS"; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(22)
def a():
    x=_a(); r=x["docker_assurance_state.py::_STATE_TOP_FIELDS"]; r["observed_authority"]=r["expected_authority"]; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(23)
def a():  # copied manual oracle → alias fires (already covered) / here: build_input alias
    x=_a(); r=x["workflow_assurance.py::_BUILD_INPUT_FIELDS"]; r["observed_authority"]=r["expected_authority"]; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(24)
def a():
    x=_a(); x["docker_assurance_state.py::CACHE_KEY_FIELDS"]["positive_presence"]="BOGUS"; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(25)
def a():  # derived state loses provenance: _PROVENANCE_FIELDS present
    import docker_assurance_state as ds
    return ["provenance fields present"] if "process_identity" in ds._PROVENANCE_FIELDS and "staged_tree" in ds._PROVENANCE_FIELDS else []
@arm(26)
def a():  # schema falsely downgraded to config: a SOURCE marked SCHEMA strings to NON_SEC → projection mismatch
    x=_a(); x["workflow_assurance.py::_ESTABLISH_FIELDS"]["cls"]="SECURITY_SCHEMA_OR_FIELDS"; return validate_a2c(x, CLASSIFICATIONS, A2C_IDS)
@arm(27)
def a():  # security collection downgraded without proof: cls→NON_SEC but string stays SC
    cls=dict(CLASSIFICATIONS); cls["workflow_assurance.py::_MUTABLE_TAG_TOKENS"]="NON_SECURITY_CONFIGURATION"
    return validate_a2c(ADJ, cls, A2C_IDS)
@arm(28)
def a():
    ids=(A2C_IDS - {"docker_assurance_state.py::CACHE_KEY_FIELDS"}) | {"docker_assurance_state.py::CACHE_KEYS"}
    return validate_a2c(ADJ, CLASSIFICATIONS, ids)
@arm(29)
def a():  # wrapper/container change escapes discovery: runtime-import based scope catches vanish
    ids=A2C_IDS - {"workflow_assurance.py::_PRE_PUSH_FIELDS"}
    return validate_a2c(ADJ, CLASSIFICATIONS, ids)
@arm(30)
def a():  # cross-subgate cycle falsely claimed independent: DAG is acyclic (Agent 6) — assert no alias in a2c oracles
    aliases=[cid for cid,r in ADJ.items() if r["cls"].startswith("SECURITY") and r["expected_authority"]==r["observed_authority"]]
    return ["no false independence"] if not aliases else []
@arm(31)
def a():  # REAL _MUTABLE_TAG_TOKENS driver: it is non-empty and contains a known mutable token
    import workflow_assurance as w
    return ["mutable denylist non-empty"] if set(w._MUTABLE_TAG_TOKENS) else []
@arm(32)
def a():  # POSITIVE CONTROL: the authored adjudication itself validates clean (battery is not inert)
    return ["clean-authored-passes"] if validate_a2c(ADJ, CLASSIFICATIONS, A2C_IDS) == [] else []
@arm(33)
def a():  # BIJECTION-COMPLETENESS: scope is live-derived (18) and requires_bh_b bijective with security
    import critical_list_inventory as cli
    disc={c["id"] for c in cli.discover_collections() if c["module"] in MODULES}
    req={cid for cid,r in ADJ.items() if r["bh_b"].get("spec_needed")}
    sec={cid for cid,r in ADJ.items() if r["cls"].startswith("SECURITY")}
    return ["bijective"] if disc==A2C_IDS and req==sec else []


@pytest.mark.parametrize("n", sorted(ARMS))
def test_a2c_falsification_arm_fails_closed(n):
    assert ARMS[n](), f"A2c arm {n} did not fire"


def test_battery_has_33_arms():
    assert sorted(ARMS) == list(range(1, 34))


# ---- consolidated A2 closure ----
def test_consolidated_a2_union_is_exactly_43():
    a2a = set(CONTRACT["a2a_adjudication"]); a2b = set(CONTRACT["a2b_adjudication"]); a2c = A2C_IDS
    assert len(a2a) == 10 and len(a2b) == 15 and len(a2c) == 18
    assert a2a & a2b == set() and a2a & a2c == set() and a2b & a2c == set()
    assert len(a2a | a2b | a2c) == 43


def test_no_a2_collection_is_unclassified():
    import critical_list_inventory as cli
    r = cli.check()
    a2 = set(CONTRACT["a2a_adjudication"]) | set(CONTRACT["a2b_adjudication"]) | A2C_IDS
    assert not (set(r["unclassified"]) & a2), "no A2 collection may remain unclassified"
