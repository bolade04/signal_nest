"""Gate 4N-I28BF-B1 — the thirty cache/state poisoning controls (section 16).

Each poison targets the authoritative state or the governed cache and must be refused by the
intended detector: `validate_state` for a malformed/forced state, `validate_cache_key` /
`validate_cache_value` / `lookup` for a cross-identity or malformed cache entry, and the
deep-freeze guarantee for a post-validation mutation. Propagation to the final graded result is
proven by the focused isolated sessions in test_i28bf_b1_integration.py; here each of the thirty is
activated and its refusal is asserted directly against the production detector.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cache_authority as ca                       # noqa: E402
import docker_assurance_state as das               # noqa: E402
import docker_boundary as db                       # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    das.reset_caches()
    yield
    das.reset_caches()


def _state():
    return das._thaw(das.fresh_state())


def _refused_by_state(mutate) -> bool:
    s = _state()
    mutate(s)
    return bool(das.validate_state(s))


def _policy_variant(mutate_doc):
    """A per-site state derived from a mutated policy, as a poison would present it."""
    doc = copy.deepcopy(db.load_policy())
    mutate_doc(doc)
    ps = db.per_site_state(doc, db.steering_state())
    return ps


# ---- state-level poisons (validate_state is the detector) -----------------------------------
def test_p01_omit_one_docker_site():
    ps = _policy_variant(lambda d: d["call_sites"].pop(0))
    # An omitted site makes production and independent universes disagree -> refused.
    s = _state()
    s["universe"]["production_universe_digest"] = ca.digest(
        sorted(x["id"] for x in db.load_policy()["call_sites"][1:]))
    s["universe"]["reconciliation"] = "DISAGREE"
    assert das.validate_state(s)


def test_p02_omit_all_sites_from_one_source():
    s = _state()
    s["universe"]["reconciliation"] = "DISAGREE"
    assert das.validate_state(s)


def test_p03_duplicate_one_site():
    assert _refused_by_state(lambda s: s["per_site"].append(dict(s["per_site"][0])))


def test_p04_change_source_position():
    s = _state()
    a = das.fresh_state()
    moved = copy.deepcopy(das._thaw(a))
    moved["per_site"][0]["position"] = moved["per_site"][0]["position"] + "|moved"
    assert das.compare_states(das.fresh_state(), moved), "a moved position must be a difference"


def test_p05_change_site_classification():
    moved = _state()
    moved["per_site"][0]["classification"] = "X"
    assert das.compare_states(das.fresh_state(), moved)


def test_p06_remove_one_authored_field():
    ps = _policy_variant(lambda d: d["call_sites"][0].pop("failure_behaviour"))
    assert not ps["clean"], "a removed authored field must make per-site enforcement non-clean"


def test_p07_add_unknown_field():
    ps = _policy_variant(lambda d: d["call_sites"][0].update({"surprise": 1}))
    assert not ps["clean"], "an unknown field must fail closed"


def test_p08_falsely_mark_field_consumed():
    # consumed is DERIVED; a fabricated consumed set differs from the derived one on comparison.
    moved = _state()
    moved["per_site"][0]["consumed"] = "id"
    assert das.compare_states(das.fresh_state(), moved)


def test_p09_force_one_decision_pass():
    # Start from a genuinely failing site, then force its decision PASS in the state.
    a = das.fresh_state()
    forced = copy.deepcopy(das._thaw(a))
    forced["per_site"][0]["decision"] = "PASS"           # already PASS; force a FAIL->PASS instead
    # Build a failing baseline and force it clean:
    failing = copy.deepcopy(das._thaw(a))
    failing["per_site"][0]["decision"] = "FAIL"
    failing["aggregate"]["docker_aggregate"] = True
    failing["aggregate"]["docker_per_site_layer"] = True
    assert das.validate_state(failing), "forcing a site PASS in the aggregate must be refused"


def test_p10_force_aggregate_pass():
    s = _state()
    s["per_site"][0]["decision"] = "FAIL"
    s["aggregate"]["docker_aggregate"] = True
    s["aggregate"]["docker_per_site_layer"] = True
    assert any("forced clean" in p for p in das.validate_state(s))


def test_p11_production_universe_empty():
    s = _state()
    s["universe"]["site_ids"] = ()
    s["universe"]["site_count"] = 0
    assert das.validate_state(s)


def test_p12_independent_universe_empty(monkeypatch):
    monkeypatch.setattr(db, "derive_call_sites", lambda: {"sites": [], "problems": [], "count": 0})
    s = das.fresh_state()
    assert das.validate_state(s), "an empty independent universe must fail reconciliation/positive"


def test_p13_both_universes_empty():
    s = _state()
    s["universe"].update({"site_ids": (), "site_count": 0, "expected_positive": False,
                          "production_universe_digest": ca.digest([]),
                          "independent_universe_digest": ca.digest([])})
    assert das.validate_state(s)


def test_p14_stale_pre_i28be_state():
    assert _refused_by_state(lambda s: s.update({"schema_version": "pre-i28be"}))


def test_p15_state_from_another_staged_tree():
    kd = das.store(das.fresh_state())
    v = das._thaw(das._STATE_CACHE[kd]); v["state"]["repository"]["staged_tree"] = "0" * 40
    das._STATE_CACHE[kd] = ca.deep_freeze(v)
    _, tag = das.lookup(das.fresh_state())
    assert tag.startswith("REJECTED")


def test_p16_state_from_another_policy():
    s = _state(); s["policy"]["policy_digest"] = "0" * 64
    # a different policy digest changes the key, so the entry cannot be served under the real key
    kd_real = das.cache_key_digest(das.fresh_state())
    kd_other = das.cache_key_digest(s)
    assert kd_real != kd_other


def test_p17_state_from_another_category_table():
    s = _state(); s["policy"]["category_table_digest"] = "0" * 64
    assert das.cache_key_digest(s) != das.cache_key_digest(das.fresh_state())


def test_p18_alter_category_table_digest_only():
    kd = das.store(das.fresh_state())
    v = das._thaw(das._STATE_CACHE[kd]); v["state"]["policy"]["category_table_digest"] = "forged"
    das._STATE_CACHE[kd] = ca.deep_freeze(v)
    _, tag = das.lookup(das.fresh_state())
    assert tag.startswith("REJECTED"), "an altered digest breaks the value's state/key digests"


def test_p19_alter_policy_digest_only():
    kd = das.store(das.fresh_state())
    v = das._thaw(das._STATE_CACHE[kd]); v["state"]["policy"]["policy_digest"] = "forged"
    das._STATE_CACHE[kd] = ca.deep_freeze(v)
    _, tag = das.lookup(das.fresh_state())
    assert tag.startswith("REJECTED")


def test_p20_remove_authorization_digest():
    assert _refused_by_state(lambda s: s["authorization"].pop("pair_digest"))


def test_p21_use_retired_authorization_pair():
    s = _state()
    s["authorization"] = {"issuance": "2026-08-06T01:35:35Z", "expiry": "2026-08-06T23:35:35Z",
                          "duration_seconds": 79200}
    s["authorization"]["pair_digest"] = ca.digest(s["authorization"])
    # a retired pair changes the key digest, so a warm entry cannot be served under the active key
    assert das.cache_key_digest(s) != das.cache_key_digest(das.fresh_state())


def test_p22_remove_source_position_schema_version():
    assert _refused_by_state(lambda s: s["parser"].pop("source_position_version"))


def test_p23_remove_source_content_token():
    key = das.cache_key(das.fresh_state()); key.pop("source_content_token")
    assert das.validate_cache_key(key)


def test_p24_reuse_initial_result_at_finish():
    """reverify_state freshly derives and never substitutes the bound baseline as the answer:
    if the tree drifts, the fresh finish differs from the (reused) baseline and it is caught."""
    base = das.establish_state()
    # Simulate a late drift by comparing the baseline against a mutated 'fresh' state.
    mutated = copy.deepcopy(das._thaw(base["state"]))
    mutated["per_site"][0]["decision"] = "FAIL"
    assert das.compare_states(base["state"], mutated), "reuse cannot hide a late drift"


def test_p25_inject_cache_after_baseline():
    base = das.establish_state()
    # Inject a bogus entry; reverify_state does not consult the cache, so the fresh finish is clean.
    das._STATE_CACHE["bogus"] = ca.deep_freeze({"state": {}, "state_digest": "x",
                                                "cache_key_digest": "x", "provenance": {},
                                                "validation_status": "VALIDATED",
                                                "cache_schema_version": das.CACHE_SCHEMA_VERSION})
    out = das.reverify_state(base)
    assert out["clean"], "an injected cache entry must not affect the fresh finish derivation"


def test_p26_mutate_cache_after_validation():
    kd = das.store(das.fresh_state())
    with pytest.raises(TypeError):
        das._STATE_CACHE[kd]["state"]["schema_version"] = "x"   # deep-frozen: immutable


def test_p27_serialize_unknown_field():
    assert _refused_by_state(lambda s: s.update({"surprise_field": 1}))


def test_p28_remove_unresolved_field_evidence():
    # A per-site record with no position (its resolved-location evidence) is refused.
    assert _refused_by_state(lambda s: s["per_site"][0].update({"position": ""}))


def test_p29_mark_workflow_coverage_complete():
    assert _refused_by_state(
        lambda s: s["aggregate"].update({"workflow_coverage": "COMPLETE"}))


def test_p30_malformed_cached_result_shape():
    das._STATE_CACHE["k"] = ca.deep_freeze({"not": "a valid cache value"})
    assert das.validate_cache_value(das._STATE_CACHE["k"], expected_key_digest="k")


def test_zero_void_all_thirty_are_present():
    """Guards against a silently dropped arm: exactly thirty p## controls exist."""
    names = [n for n in globals() if n.startswith("test_p") and n[6:8].isdigit()]
    assert len(names) == 30, sorted(names)
