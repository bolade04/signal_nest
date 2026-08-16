#!/usr/bin/env python3
"""Gate 4N-I28BH-A4-PARALLEL (6-agent) — review-packet-digest and reviewer-retrieval-state
collection classification and oracle adjudication (completes A4).

WHAT THIS PINS. The 5 collections owning the review-packet digest contract (review_packet_digest) and the
reviewer-lane retrieval state machine (reviewer_retrieval_state) are classified in
`critical-list-contract.json::a4_adjudication`:
  * review_packet_digest.py::REQUIRED_FIELDS   -> SECURITY_CRITICAL_SOURCE (FUNCTION_RESULT_KEYS): verify()
      rejects any digest record missing a member; dropping one is FAIL-OPEN (the Gate 4N-I27Y unlabelled-digest
      defect). Non-circular vs digests() output keys (minus informational raw_file_bytes).
  * reviewer_retrieval_state.py::STATES        -> SECURITY_CRITICAL_SOURCE (MODULE_CONSTANTS): closed enum;
      may_relaunch/is_lost_verdict fail closed on a non-member; the danger is ADD (an injected state passes).
  * reviewer_retrieval_state.py::NEVER_RELAUNCH-> SECURITY_CRITICAL_SOURCE (AUTHORED_CONTRACT): the sole guard
      against relaunching a completed/in-flight lane; dropping COMPLETED_WITH_ARTIFACT is a fail-open
      double-execution / verdict-overwrite (the single most dangerous mutation in the module).
  * reviewer_retrieval_state.py::TRANSITIONS   -> DOCUMENTATION_ONLY (A4-FIND-01): a declared state-transition
      table that NO production code enforces — classify() recomputes state from raw inputs and never consults
      it. Unlike TRANSFER_WORDS (which shadows the live _ACTIVE regex), it shadows no enforcer, so it carries no
      current security role; the latent risk is recorded as A4-FIND-01.
  * reviewer_retrieval_state.py::DEFINITIONS   -> DOCUMENTATION_ONLY (A4-OBS-01): prose keyed by state, read by
      no decision path.
A4-FIND-02: the composite reviewer_context binding ('artifact binds candidate+tree+raw digest') is a caller
responsibility with NO production caller — replay/wrong-candidate/wrong-version are bound only in prose.
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
ADJ = CONTRACT["a4_adjudication"]
CLASSIFICATIONS = CONTRACT["classifications"]
A4_IDS = set(ADJ)
MODULES = {"review_packet_digest.py", "reviewer_retrieval_state.py"}

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

# No A4 collection was previously SECURITY (all 5 were UNCLASSIFIED) -> no downward reclassifications.
PRE_A4_SECURITY_CRITICAL: frozenset = frozenset()
FORBIDDEN_OBSERVED = "copied from the authored"


def validate_a4(adj, classifications, present_ids) -> list:
    """A4 adjudication validator (same contract as validate_a3):
    (A) SECURITY_CRITICAL_SOURCE positive_presence must be INVALID_EMPTY;
    (B) a SECURITY observed_authority may not be a 'copied from the authored list' marker;
    (C) SECURITY_CRITICAL_DERIVED must carry oracle_family PROVENANCE_DERIVATION;
    (D) a PRE_A4_SECURITY_CRITICAL id now out of SECURITY must carry downgrade_proof (vacuous for A4).
    """
    problems = []
    for cid in sorted(present_ids - set(adj)):
        problems.append(f"{cid}: an A4 collection with NO adjudication")
    for cid in sorted(set(adj) - present_ids):
        problems.append(f"{cid}: adjudicated but not a present A4 collection")
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
        if cid in PRE_A4_SECURITY_CRITICAL and cls not in SECURITY and not rec.get("downgrade_proof"):
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
            if cls == "SECURITY_CRITICAL_SOURCE" and rec.get("positive_presence") != "INVALID_EMPTY":
                problems.append(f"{cid}: SECURITY_CRITICAL_SOURCE must be INVALID_EMPTY")
            if FORBIDDEN_OBSERVED in (rec.get("observed_authority") or "").lower():
                problems.append(f"{cid}: observed_authority is a copy of the authored list (self-comparison)")
            if cls == "SECURITY_CRITICAL_DERIVED" and rec.get("oracle_family") != "PROVENANCE_DERIVATION":
                problems.append(f"{cid}: SECURITY_CRITICAL_DERIVED must carry oracle_family PROVENANCE_DERIVATION")
        else:
            if rec.get("oracle_family") != "NONE":
                problems.append(f"{cid}: non-security class must carry oracle_family NONE")
    return problems


# ---------------------------------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------------------------------
def test_a4_scope_is_exactly_5():
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections() if c["module"] in MODULES}
    assert A4_IDS == disc, A4_IDS ^ disc
    assert len(ADJ) == 5


def test_a4_adjudication_well_formed_positive_control():
    assert validate_a4(ADJ, CLASSIFICATIONS, A4_IDS) == []


def test_a4_distribution():
    from collections import Counter
    d = Counter(r["cls"] for r in ADJ.values())
    assert d["SECURITY_CRITICAL_SOURCE"] == 3
    assert d["DOCUMENTATION_ONLY"] == 2
    assert sum(d.values()) == 5


def test_a4_string_projection_matches_classifications():
    for cid, rec in ADJ.items():
        assert CLASSIFICATIONS[cid] == STRING_OF[rec["cls"]]


def test_a1_a2_a3_non_regression():
    assert len(CONTRACT["a1a_adjudication"]) == 36 and len(CONTRACT["a1b_adjudication"]) == 22  # INFRA-9-B3 apply-identity: +2 (W0_APPLY_CLOSURE, W0_SCOPED_CAPABILITIES)
    assert len(CONTRACT["a1c_adjudication"]) == 23 and len(CONTRACT["a2a_adjudication"]) == 10
    assert len(CONTRACT["a2b_adjudication"]) == 15 and len(CONTRACT["a2c_adjudication"]) == 18
    assert len(CONTRACT["a3_adjudication"]) == 25


def test_remaining_unclassified_within_a5():
    # At A4 closure the residual unclassified set was exactly these 3 (the A5 scope). Gate 4N-I28BH-A5
    # then classifies them, emptying the set; this assertion is forward-stable — it requires only that no
    # collection outside the known A5 universe ever leaks in as unclassified.
    import critical_list_inventory as cli
    allids = {c["id"] for c in cli.discover_collections()}
    uncl = allids - set(CLASSIFICATIONS)
    A5 = {"cache_authority.py::CLASSIFICATIONS", "cache_authority.py::_PINS",
          "leak_scan.py::NON_LIVE_CLASSIFICATIONS"}
    assert uncl <= A5, uncl - A5


def test_bijection_completeness():
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections() if c["module"] in MODULES}
    assert set(ADJ) == disc and len(disc) == 5


def test_a4_bh_b_count_is_three():
    bhb = [k for k, r in ADJ.items() if r["bh_b"]["spec_needed"]]
    assert sorted(bhb) == ["review_packet_digest.py::REQUIRED_FIELDS",
                           "reviewer_retrieval_state.py::NEVER_RELAUNCH",
                           "reviewer_retrieval_state.py::STATES"]


# ---------------------------------------------------------------------------------------------------
# Metadata falsification battery — each arm MUST make validate_a4 return a non-empty problem list.
# ---------------------------------------------------------------------------------------------------
def _mut(fn):
    adj = copy.deepcopy(ADJ)
    cls = dict(CLASSIFICATIONS)
    present = set(A4_IDS)
    ctx = fn(adj, cls, present)
    if not (isinstance(ctx, tuple) and len(ctx) == 3):
        ctx = (adj, cls, present)
    return validate_a4(*ctx)


def _a(adj, key, field, value):
    adj[key][field] = value


REQ = "review_packet_digest.py::REQUIRED_FIELDS"
STATES = "reviewer_retrieval_state.py::STATES"
NREL = "reviewer_retrieval_state.py::NEVER_RELAUNCH"
TRANS = "reviewer_retrieval_state.py::TRANSITIONS"
DEFS = "reviewer_retrieval_state.py::DEFINITIONS"

ARMS = {
    "arm01_unclassified_addition": lambda a, c, p: (a, c, p | {"reviewer_retrieval_state.py::NEW_ENUM"}),
    "arm02_remove_classification": lambda a, c, p: (a.pop(REQ), (a, c, p))[1],
    "arm24_collection_rename": lambda a, c, p: (a, c, (p - {REQ}) | {"review_packet_digest.py::REQUIRED_FLDS"}),
    "arm_collection_move": lambda a, c, p: (a, c, (p - {STATES}) | {"review_packet_digest.py::STATES"}),
    "arm31_lost_oracle_source": lambda a, c, p: _a(a, REQ, "oracle_family", "NONE"),
    "arm_states_lost_oracle": lambda a, c, p: _a(a, STATES, "oracle_family", "NONE"),
    "arm_blank_independence": lambda a, c, p: _a(a, NREL, "independence", ""),
    "arm_blank_comparison": lambda a, c, p: _a(a, STATES, "comparison", ""),
    "arm_blank_expected": lambda a, c, p: _a(a, REQ, "expected_authority", ""),
    "arm21_expected_observed_alias": lambda a, c, p: _a(a, STATES, "observed_authority", a[STATES]["expected_authority"]),
    "arm22_copied_oracle": lambda a, c, p: _a(a, REQ, "observed_authority", "the tuple, copied from the authored list verbatim"),
    "arm23_false_empty_source": lambda a, c, p: _a(a, NREL, "positive_presence", "VALID_EMPTY"),
    "arm29_doc_oracle_leak": lambda a, c, p: _a(a, DEFS, "oracle_family", "MODULE_CONSTANTS"),
    "arm27_trans_oracle_and_domain": lambda a, c, p: [_a(a, TRANS, "oracle_family", "MAGIC"),
                                                      _a(a, TRANS, "authority_domain", "BOGUS")],
    "arm28_projection_mismatch": lambda a, c, p: c.__setitem__(STATES, "NON_SECURITY_CONFIGURATION"),
    "arm_doc_projection_mismatch": lambda a, c, p: c.__setitem__(TRANS, "SECURITY_CRITICAL_LIST"),
    "arm_derived_not_provenance": lambda a, c, p: [_a(a, NREL, "cls", "SECURITY_CRITICAL_DERIVED"),
                                                   _a(a, NREL, "oracle_family", "MODULE_CONSTANTS")],
}


@pytest.mark.parametrize("name", sorted(ARMS))
def test_metadata_arm_fires(name):
    assert _mut(ARMS[name]), f"{name}: falsification arm did not fire"


def test_arm30_a1_a2_a3_a4_ownership_disjoint():
    for blk in ("a1a_adjudication", "a1b_adjudication", "a1c_adjudication",
                "a2a_adjudication", "a2b_adjudication", "a2c_adjudication", "a3_adjudication"):
        assert A4_IDS.isdisjoint(set(CONTRACT[blk])), blk
    a3 = dict(CONTRACT["a3_adjudication"]); a3[REQ] = {"cls": "X"}
    assert not A4_IDS.isdisjoint(set(a3))


# ---------------------------------------------------------------------------------------------------
# REAL production detectors — the load-bearing behaviour each SOURCE underwrites.
# ---------------------------------------------------------------------------------------------------
def _packet(**over):
    p = {"candidate_id": "C1", "tree": "T1", "evidence_id": "E1", "reviewer": "R1", "version": 1, "body": "x"}
    p.update(over)
    return p


def test_required_fields_missing_is_fail_closed():
    """arm03: a digest record missing a REQUIRED_FIELDS member is refused (verify fail-closed)."""
    import review_packet_digest as rpd
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "pk.json"; pk = _packet(); path.write_bytes(rpd.serialize(pk))
        declared = rpd.digests(pk, raw=path.read_bytes())
        assert rpd.verify(path, declared)["both_recomputed_from_the_distributed_file"]
        for field in rpd.REQUIRED_FIELDS:
            bad = dict(declared); bad.pop(field)
            with pytest.raises(rpd.PacketDigestError):
                rpd.verify(path, bad)


def test_required_fields_equals_digest_keys_minus_bytecount():
    """arm04: REQUIRED_FIELDS == digests() emitted keys minus the informational raw_file_bytes."""
    import review_packet_digest as rpd
    emitted = set(rpd.digests(_packet()))
    assert set(rpd.REQUIRED_FIELDS) == emitted - {"raw_file_bytes"}


def test_packet_byte_change_breaks_raw_digest():
    """arm05/12: a changed distributed byte no longer matches the declared raw digest."""
    import review_packet_digest as rpd
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "pk.json"; pk = _packet(); path.write_bytes(rpd.serialize(pk))
        declared = rpd.digests(pk, raw=path.read_bytes())
        path.write_bytes(rpd.serialize(_packet(body="TAMPERED")))
        with pytest.raises(rpd.PacketDigestError):
            rpd.verify(path, declared)


def test_canonicalization_no_collision_across_types():
    """arm06: {'n':1} and {'n':'1'} are semantically distinct -> distinct canonical digests."""
    import review_packet_digest as rpd, hashlib
    a = hashlib.sha256(rpd.canonical_bytes({"n": 1})).hexdigest()
    b = hashlib.sha256(rpd.canonical_bytes({"n": "1"})).hexdigest()
    assert a != b


def test_reordered_packet_canonical_equal_raw_distinct():
    """arm07: key reordering collapses the CANONICAL digest (sort_keys) but not the RAW file digest."""
    import review_packet_digest as rpd, hashlib
    p1 = {"a": 1, "b": 2}; p2 = {"b": 2, "a": 1}
    assert rpd.canonical_bytes(p1) == rpd.canonical_bytes(p2)
    assert hashlib.sha256(rpd.serialize(p1)).hexdigest() != hashlib.sha256(rpd.serialize(p2)).hexdigest()


def test_wrong_candidate_packet_rejected():
    """arm08/13/14: a declared record for candidate C1 does not verify a file bound to C2."""
    import review_packet_digest as rpd
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "pk.json"
        path.write_bytes(rpd.serialize(_packet(candidate_id="C1")))
        declared = rpd.digests(_packet(candidate_id="C2"))
        with pytest.raises(rpd.PacketDigestError):
            rpd.verify(path, declared)


def test_raw_and_canonical_are_distinct_positive_control():
    """arm32: the two digests genuinely differ (the whole point of the two-digest contract)."""
    import review_packet_digest as rpd
    d = rpd.digests(_packet())
    assert d["review_packet_raw_file_sha256"] != d["review_packet_canonical_object_sha256"]


def test_classify_covers_full_cross_product_within_states():
    """arm15: every classify() outcome over the input cross-product is a declared STATES member."""
    import reviewer_retrieval_state as rrs
    seen = set()
    for alive in (True, False):
        for exit_ in (None, 0, 1):
            for art in (True, False):
                for tr in (True, False):
                    try:
                        seen.add(rrs.classify(process_alive=alive, process_exit=exit_,
                                              artifact_valid=art, transport_delivered=tr))
                    except rrs.RetrievalStateError:
                        pass
    assert seen and seen <= set(rrs.STATES)


def test_unknown_state_is_fail_closed():
    """arm16: an unknown state is refused by both membership guards."""
    import reviewer_retrieval_state as rrs
    with pytest.raises(rrs.RetrievalStateError):
        rrs.may_relaunch("BOGUS_PASS")
    with pytest.raises(rrs.RetrievalStateError):
        rrs.is_lost_verdict("BOGUS_PASS")


def test_only_completed_no_artifact_is_lost_verdict():
    """arm17/19: only COMPLETED_NO_ARTIFACT is a lost verdict; terminal FAILED / RUNNING are not."""
    import reviewer_retrieval_state as rrs
    assert rrs.is_lost_verdict("COMPLETED_NO_ARTIFACT") is True
    assert rrs.is_lost_verdict("FAILED") is False
    assert rrs.is_lost_verdict("TRANSPORT_UNDELIVERED") is False
    assert rrs.is_lost_verdict("RUNNING") is False


def test_missing_exit_is_fail_closed():
    """arm18: a not-running lane with no exit status cannot be classified (fail-closed)."""
    import reviewer_retrieval_state as rrs
    with pytest.raises(rrs.RetrievalStateError):
        rrs.classify(process_alive=False, process_exit=None, artifact_valid=False, transport_delivered=False)


def test_artifact_precedence_over_nonzero_exit():
    """A valid artifact outranks a non-zero exit -> COMPLETED_WITH_ARTIFACT (never FAILED)."""
    import reviewer_retrieval_state as rrs
    assert rrs.classify(process_alive=False, process_exit=7,
                        artifact_valid=True, transport_delivered=False) == "COMPLETED_WITH_ARTIFACT"


def test_never_relaunch_protects_completed_and_running():
    """arm25: may_relaunch is False for both protected states; dropping either is a fail-open."""
    import reviewer_retrieval_state as rrs
    assert rrs.may_relaunch("COMPLETED_WITH_ARTIFACT") is False
    assert rrs.may_relaunch("RUNNING") is False
    assert rrs.may_relaunch("FAILED") is True  # a genuine infra fault may be relaunched


def test_definitions_keys_cover_states():
    """arm26 / A4-OBS-01: DEFINITIONS documents exactly the STATES universe (no over/under-documentation)."""
    import reviewer_retrieval_state as rrs
    assert set(rrs.DEFINITIONS) == set(rrs.STATES)


def test_transitions_structural_shape_a4_find_01():
    """A4-FIND-01: TRANSITIONS is DOCUMENTATION_ONLY (unenforced), but its current structural shape is
    pinned so the finding is anchored to a positive artifact: keys==STATES, every target subset STATES,
    COMPLETED_WITH_ARTIFACT terminal. If a transition guard is ever wired, this is the coverage it owes."""
    import reviewer_retrieval_state as rrs
    assert set(rrs.TRANSITIONS) == set(rrs.STATES)
    assert all(set(v) <= set(rrs.STATES) for v in rrs.TRANSITIONS.values())
    assert rrs.TRANSITIONS["COMPLETED_WITH_ARTIFACT"] == ()


def test_transitions_unread_by_decisions_a4_find_01():
    """A4-FIND-01 evidence: mutating TRANSITIONS changes NO retrieval decision (it is emitted-only)."""
    import reviewer_retrieval_state as rrs
    saved = rrs.TRANSITIONS
    try:
        rrs.TRANSITIONS = {}  # obliterate the table
        # classify / may_relaunch / is_lost_verdict are unaffected — they never read TRANSITIONS
        assert rrs.classify(process_alive=True, process_exit=None,
                            artifact_valid=False, transport_delivered=False) == "RUNNING"
        assert rrs.may_relaunch("COMPLETED_WITH_ARTIFACT") is False
        assert rrs.is_lost_verdict("COMPLETED_NO_ARTIFACT") is True
    finally:
        rrs.TRANSITIONS = saved
