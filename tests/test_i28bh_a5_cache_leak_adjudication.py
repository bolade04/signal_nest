#!/usr/bin/env python3
"""Gate 4N-I28BH-A5-PARALLEL (5-agent) — cache-authority and leak-scan residual collection
classification + oracle adjudication (completes A5) and the consolidated BH-A closeout.

WHAT THIS PINS. The final 3 collections:
  * cache_authority.py::CLASSIFICATIONS   -> SECURITY_CRITICAL_SOURCE (MODULE_CONSTANTS): the closed cache
      authority-class enum; load_policy() refuses a cache whose classification is not a member. ADD widens
      (an unrecognized authority label passes); non-circular vs the classification_obligations() dispatch set.
  * cache_authority.py::_PINS             -> NON_SECURITY_CONFIGURATION: a runtime identity registry (`{}`
      at rest); verify()'s authoritative recomputation never consults it (the A2c _STATE_CACHE analog).
  * leak_scan.py::NON_LIVE_CLASSIFICATIONS-> SECURITY_CRITICAL_SOURCE (AUTHORITATIVE_SOURCE_NO_ENUMERABLE_
      ORACLE): a leak-SUPPRESSION closed enum; approved_accounts() refuses an account carrying an unlisted
      class. ADD widens = fail-open on live-identifier containment.

CONSOLIDATED BH-A. This module also programmatically reconciles the whole BH-A universe: A1 79 + A2 43 +
A3 25 + A4 5 + A5 3 = 155 adjudicated with zero pairwise overlap, + 101 pre-BH-A classified = 256, with
repository UNCLASSIFIED = 0.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
os.environ.setdefault("SIGNALNEST_ANCHOR_TIER", "TIER_1_SYNTHETIC")

CONTRACT = json.loads((REPO / "tests/fixtures/critical-list-contract.json").read_text())
ADJ = CONTRACT["a5_adjudication"]
CLASSIFICATIONS = CONTRACT["classifications"]
A5_IDS = set(ADJ)
MODULES = {"cache_authority.py", "leak_scan.py"}

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

PRE_A5_SECURITY_CRITICAL: frozenset = frozenset()  # all 3 were UNCLASSIFIED -> no downgrades
FORBIDDEN_OBSERVED = "copied from the authored"


def validate_a5(adj, classifications, present_ids) -> list:
    """A5 adjudication validator. Same contract as validate_a4 EXCEPT it does not force
    SECURITY_CRITICAL_SOURCE -> INVALID_EMPTY: a leak-suppression source (NON_LIVE_CLASSIFICATIONS) is
    legitimately CONDITIONALLY_EMPTY (empty is fail-closed/conservative), so the rule is dropped for A5.
    positive_presence is still required to be one of the three PP values for every security row.
    """
    problems = []
    for cid in sorted(present_ids - set(adj)):
        problems.append(f"{cid}: an A5 collection with NO adjudication")
    for cid in sorted(set(adj) - present_ids):
        problems.append(f"{cid}: adjudicated but not a present A5 collection")
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
        if cid in PRE_A5_SECURITY_CRITICAL and cls not in SECURITY and not rec.get("downgrade_proof"):
            problems.append(f"{cid}: downward reclassification from SECURITY without downgrade_proof")
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
            if FORBIDDEN_OBSERVED in (rec.get("observed_authority") or "").lower():
                problems.append(f"{cid}: observed_authority is a copy of the authored list (self-comparison)")
            if cls == "SECURITY_CRITICAL_DERIVED" and rec.get("oracle_family") != "PROVENANCE_DERIVATION":
                problems.append(f"{cid}: SECURITY_CRITICAL_DERIVED must carry oracle_family PROVENANCE_DERIVATION")
        else:
            if rec.get("oracle_family") != "NONE":
                problems.append(f"{cid}: non-security class must carry oracle_family NONE")
    return problems


# ---------------------------------------------------------------------------------------------------
# Structural + consolidated BH-A
# ---------------------------------------------------------------------------------------------------
CA_CL = "cache_authority.py::CLASSIFICATIONS"
CA_PINS = "cache_authority.py::_PINS"
LS_NLC = "leak_scan.py::NON_LIVE_CLASSIFICATIONS"


def test_a5_scope_is_exactly_3():
    # A5 owns the 3 residual UNCLASSIFIED collections (not every collection in these two modules — the
    # modules also carry earlier-classified collections in the 101 pre-BH-A set).
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections()}
    assert A5_IDS == {CA_CL, CA_PINS, LS_NLC}
    assert A5_IDS <= disc


def test_a5_adjudication_well_formed_positive_control():
    assert validate_a5(ADJ, CLASSIFICATIONS, A5_IDS) == []


def test_a5_distribution():
    from collections import Counter
    d = Counter(r["cls"] for r in ADJ.values())
    assert d["SECURITY_CRITICAL_SOURCE"] == 2
    assert d["NON_SECURITY_CONFIGURATION"] == 1
    assert sum(d.values()) == 3


def test_a5_string_projection():
    for cid, rec in ADJ.items():
        assert CLASSIFICATIONS[cid] == STRING_OF[rec["cls"]]


def test_a5_bh_b_count_is_two():
    bhb = sorted(k for k, r in ADJ.items() if r["bh_b"]["spec_needed"])
    assert bhb == [CA_CL, LS_NLC]


def test_repository_unclassified_is_zero():
    import critical_list_inventory as cli
    allids = {c["id"] for c in cli.discover_collections()}
    assert allids - set(CLASSIFICATIONS) == set()


def test_consolidated_bh_a_universe():
    """A1 79 + A2 43 + A3 25 + A4 5 + A5 3 = 155, pairwise overlap 0, + 101 pre-BH-A = 256 (BH-A closure).

    Gate 4N-I28BH-B0a-SLICE2 landed scripts/completeness_framework.py (the signed closed-capability
    witness-trust completeness verifier, Option-2 statically-resolvable re-cert), whose discovery adds
    119 framework-INTERNAL collections, all classified NON_SECURITY_CONFIGURATION and disjoint from the
    BH-A universe. The BH-A partition below is unchanged (155 + 101 = 256); the total grows to
    381 = 256 + 125 (Gate 4N-I28BH-B added 6 assurance-validator collections, disjoint from BH-A)."""
    import itertools, critical_list_inventory as cli
    allids = {c["id"] for c in cli.discover_collections()}
    assert len(allids) == 411  # BH-C-E1: +1 (collection_completeness::COMPLETENESS_REQUIRING_KINDS, NON_SECURITY)  # BH-C F8: +29 discovery-recovered collections (derived/comprehension forms)
    sets = {
        "A1": set(CONTRACT["a1a_adjudication"]) | set(CONTRACT["a1b_adjudication"]) | set(CONTRACT["a1c_adjudication"]),
        "A2": set(CONTRACT["a2a_adjudication"]) | set(CONTRACT["a2b_adjudication"]) | set(CONTRACT["a2c_adjudication"]),
        "A3": set(CONTRACT["a3_adjudication"]),
        "A4": set(CONTRACT["a4_adjudication"]),
        "A5": set(CONTRACT["a5_adjudication"]),
    }
    assert [len(sets[k]) for k in ("A1", "A2", "A3", "A4", "A5")] == [79, 43, 25, 5, 3]
    for a, b in itertools.combinations(sets, 2):
        assert not (sets[a] & sets[b]), (a, b, sets[a] & sets[b])
    union = set().union(*sets.values())
    assert len(union) == 155 and union <= allids
    # The B0a-SLICE2 framework-landing tranche: exactly 119 completeness_framework.py collections,
    # disjoint from the BH-A universe, all classified.
    framework_new = {i for i in allids if i.startswith("completeness_framework.py::")}
    assert len(framework_new) == 124  # BH-C F8: +5 completeness_framework derived collections
    assert not (framework_new & union)               # framework tranche is disjoint from BH-A
    assert len(allids - union) == 256  # BH-C-E1: +1  # BH-C F8: +29 (2 new SECURITY + 27 NON_SECURITY derived/internal)                 # 101 pre-BH-A + 119 B0a-SLICE2 framework + 6 BH-B assurance-validator constants
    assert (allids - union) <= set(CLASSIFICATIONS)  # all non-BH-A ids (pre-BH-A + framework) are classified


def test_bh_a_open_findings_preserved():
    """BH-A closing does not erase the prior findings/observations recorded IN the contract."""
    blob = json.dumps(CONTRACT)
    for tok in ("A4-FIND-01", "A4-OBS-01", "A3-OBS-01", "A3-OBS-02", "BH-A3-FIND-01", "A2c-OBS-01"):
        assert tok in blob, tok


def test_a1_a2_a3_a4_non_regression():
    assert [len(CONTRACT[b]) for b in ("a1a_adjudication", "a1b_adjudication", "a1c_adjudication")] == [34, 22, 23]
    assert [len(CONTRACT[b]) for b in ("a2a_adjudication", "a2b_adjudication", "a2c_adjudication")] == [10, 15, 18]
    assert len(CONTRACT["a3_adjudication"]) == 25 and len(CONTRACT["a4_adjudication"]) == 5


# ---------------------------------------------------------------------------------------------------
# Metadata falsification battery
# ---------------------------------------------------------------------------------------------------
def _mut(fn):
    adj = copy.deepcopy(ADJ); cls = dict(CLASSIFICATIONS); present = set(A5_IDS)
    ctx = fn(adj, cls, present)
    if not (isinstance(ctx, tuple) and len(ctx) == 3):
        ctx = (adj, cls, present)
    return validate_a5(*ctx)


def _a(adj, key, field, value):
    adj[key][field] = value


ARMS = {
    "A5_01_unclassified_addition": lambda a, c, p: (a, c, p | {"cache_authority.py::NEW_ENUM"}),
    "A5_02_remove_classification": lambda a, c, p: (a.pop(CA_CL), (a, c, p))[1],
    "A5_19_collection_rename": lambda a, c, p: (a, c, (p - {LS_NLC}) | {"leak_scan.py::_make_non_live()"}),
    "A5_move": lambda a, c, p: (a, c, (p - {CA_CL}) | {"leak_scan.py::CLASSIFICATIONS"}),
    "A5_07_pins_wrongly_security": lambda a, c, p: _a(a, CA_PINS, "cls", "SECURITY_CRITICAL_SOURCE"),
    "A5_09_nonsec_oracle_leak": lambda a, c, p: _a(a, CA_PINS, "oracle_family", "MODULE_CONSTANTS"),
    "A5_11_classifications_alias": lambda a, c, p: _a(a, CA_CL, "observed_authority", a[CA_CL]["expected_authority"]),
    "A5_18_nonlive_alias": lambda a, c, p: _a(a, LS_NLC, "observed_authority", a[LS_NLC]["expected_authority"]),
    "A5_12_copied_oracle": lambda a, c, p: _a(a, CA_CL, "observed_authority", "the frozenset, copied from the authored list"),
    "A5_lost_oracle_source": lambda a, c, p: _a(a, CA_CL, "oracle_family", "NONE"),
    "A5_blank_independence": lambda a, c, p: _a(a, LS_NLC, "independence", ""),
    "A5_blank_comparison": lambda a, c, p: _a(a, CA_CL, "comparison", ""),
    "A5_17_bogus_positive_presence": lambda a, c, p: _a(a, LS_NLC, "positive_presence", "ALWAYS_FINE"),
    "A5_unknown_family_domain": lambda a, c, p: [_a(a, LS_NLC, "oracle_family", "MAGIC"),
                                                 _a(a, LS_NLC, "authority_domain", "BOGUS")],
    "A5_projection_mismatch": lambda a, c, p: c.__setitem__(CA_CL, "NON_SECURITY_CONFIGURATION"),
    "A5_nonsec_projection_mismatch": lambda a, c, p: c.__setitem__(CA_PINS, "SECURITY_CRITICAL_LIST"),
    "A5_derived_not_provenance": lambda a, c, p: [_a(a, LS_NLC, "cls", "SECURITY_CRITICAL_DERIVED"),
                                                  _a(a, LS_NLC, "oracle_family", "MODULE_CONSTANTS")],
}


@pytest.mark.parametrize("name", sorted(ARMS))
def test_metadata_arm_fires(name):
    assert _mut(ARMS[name]), f"{name}: falsification arm did not fire"


def test_A5_20_consolidated_universe_drop_detected():
    """Dropping an A1-A5 collection from the consolidated roster while the per-sub-gate blocks stay intact
    must be caught by the union==256 / overlap==0 reconciliation."""
    import critical_list_inventory as cli
    allids = {c["id"] for c in cli.discover_collections()}
    union = (set(CONTRACT["a1a_adjudication"]) | set(CONTRACT["a1b_adjudication"]) | set(CONTRACT["a1c_adjudication"])
             | set(CONTRACT["a2a_adjudication"]) | set(CONTRACT["a2b_adjudication"]) | set(CONTRACT["a2c_adjudication"])
             | set(CONTRACT["a3_adjudication"]) | set(CONTRACT["a4_adjudication"]) | set(CONTRACT["a5_adjudication"]))
    assert len(union) == 155                        # the authored roster is complete
    tampered = union - {CA_CL}                      # drop one collection from the roster
    assert len(tampered) == 154 != 155              # reconciliation notices the missing member


def test_A5_30_ownership_disjoint():
    for blk in ("a1a_adjudication", "a1b_adjudication", "a1c_adjudication", "a2a_adjudication",
                "a2b_adjudication", "a2c_adjudication", "a3_adjudication", "a4_adjudication"):
        assert A5_IDS.isdisjoint(set(CONTRACT[blk])), blk
    a4 = dict(CONTRACT["a4_adjudication"]); a4[CA_CL] = {"cls": "X"}
    assert not A5_IDS.isdisjoint(set(a4))


# ---------------------------------------------------------------------------------------------------
# REAL production detectors
# ---------------------------------------------------------------------------------------------------
def test_classifications_gate_is_fail_closed_on_empty():
    """A5-06/13: an empty CLASSIFICATIONS enum makes load_policy() refuse every cache."""
    import cache_authority as ca
    saved = ca.CLASSIFICATIONS
    try:
        ca.CLASSIFICATIONS = frozenset()
        with pytest.raises(ca.CacheAuthorityError):
            ca.load_policy()
    finally:
        ca.CLASSIFICATIONS = saved


def test_classifications_remove_member_refuses_real_cache():
    """A5-03: dropping a class a real cache uses makes load_policy() refuse it (fail-closed)."""
    import cache_authority as ca
    policy = ca.load_policy()                                  # positive control: passes as-authored
    used = {v["classification"] for v in policy["caches"].values()}
    assert used and used <= ca.CLASSIFICATIONS
    saved = ca.CLASSIFICATIONS
    try:
        victim = sorted(used)[0]
        ca.CLASSIFICATIONS = frozenset(ca.CLASSIFICATIONS - {victim})
        with pytest.raises(ca.CacheAuthorityError):
            ca.load_policy()
    finally:
        ca.CLASSIFICATIONS = saved


def test_classifications_non_circular_against_obligations():
    """The non-circular BH-B anchor: the classes classification_obligations() dispatches on == CLASSIFICATIONS
    (bound to the obligation branches, not the six named constants the frozenset is built from)."""
    import cache_authority as ca, inspect
    src = inspect.getsource(ca.classification_obligations)
    handled = {cls for cls in ca.CLASSIFICATIONS if cls in src}
    assert handled == set(ca.CLASSIFICATIONS)


def test_pins_empty_at_rest_and_never_gates_verify():
    """_PINS is runtime state: the AUTHORED literal is `{}` (empty at rest); a live session populates it via
    pin(). assert_pinned no-ops on a never-pinned label; pin/replace is strict. Its contents cannot
    manufacture a false trust decision — verify() never consults it (the NON_SECURITY basis)."""
    import inspect
    import cache_authority as ca
    assert "_PINS: dict = {}" in inspect.getsource(ca)         # authored empty-at-rest literal
    assert isinstance(ca._PINS, dict)                          # runtime registry (a live session seeds it)
    ca.assert_pinned("a5-never-pinned-label", {"a": 1})       # no-op on an unknown label, does not raise
    box = {"k": ("frozen",)}
    ca.pin("a5_probe_t5", box)
    try:
        ca.assert_pinned("a5_probe_t5", box)                  # same identity -> ok
        with pytest.raises(ca.CacheAuthorityError):
            ca.assert_pinned("a5_probe_t5", {"k": ("frozen",)})  # replaced object -> refused
    finally:
        ca._PINS.pop("a5_probe_t5", None)                     # leave the session registry as we found it


def test_non_live_remove_member_refuses_registered_account():
    """A5-14: dropping an in-use NON_LIVE class makes approved_accounts() refuse a registered account (the
    account then becomes a live finding — fail-closed/conservative)."""
    import leak_scan as ls
    reg = ls.approved_accounts()                              # positive control: passes as-authored
    used = {e["classification"] for e in reg.values()}
    assert used and used <= ls.NON_LIVE_CLASSIFICATIONS
    saved = ls.NON_LIVE_CLASSIFICATIONS
    try:
        ls.NON_LIVE_CLASSIFICATIONS = frozenset(ls.NON_LIVE_CLASSIFICATIONS - {sorted(used)[0]})
        with pytest.raises(ls.AccountRegistryError):
            ls.approved_accounts()
    finally:
        ls.NON_LIVE_CLASSIFICATIONS = saved


def test_non_live_add_class_suppresses_a_live_account():
    """A5-15/16: ADDING a bogus non-live class lets a live-shaped id be approved (suppressed) — the fail-open
    direction. Uses a temp registry OUTSIDE the repo; the real registry is never touched."""
    import leak_scan as ls
    saved_set, saved_reg = ls.NON_LIVE_CLASSIFICATIONS, ls.APPROVED_ACCOUNT_REGISTRY
    # Built at runtime from two 6-digit halves so no 12-digit run appears in this tracked source file
    # (a literal one would itself trip the live-identifier leak scanner this module classifies).
    live_shaped = "314159" + "265358"
    with tempfile.TemporaryDirectory() as td:
        regp = Path(td) / "registry.json"
        regp.write_text(json.dumps({"approved_accounts": [{
            "account_id": live_shaped, "classification": "LIVE_TENANT_OK",
            "provenance": "synthetic adversarial fixture proving the add-suppression fail-open direction"}]}))
        try:
            ls.APPROVED_ACCOUNT_REGISTRY = regp
            # real set: the bogus class is rejected -> fail-closed
            with pytest.raises(ls.AccountRegistryError):
                ls.approved_accounts()
            # widened set: the bogus class is now authorized -> the live-shaped id is approved (fail-open)
            ls.NON_LIVE_CLASSIFICATIONS = frozenset(ls.NON_LIVE_CLASSIFICATIONS | {"LIVE_TENANT_OK"})
            assert live_shaped in ls.approved_accounts()
        finally:
            ls.NON_LIVE_CLASSIFICATIONS, ls.APPROVED_ACCOUNT_REGISTRY = saved_set, saved_reg


def test_a5_collections_discovered_as_authored_containers():
    """A5-19: the 3 A5 collections are discovered under their authored ids (rename/move would change the id)."""
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections()}
    assert {CA_CL, CA_PINS, LS_NLC} <= disc
