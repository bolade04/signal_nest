"""Gate 4N-I28BF-B1 — the dedicated governed Docker assurance cache (Layer C).

Proves the cache is governed by cache_authority (inventoried, policy-declared, resettable,
deep-frozen), that its key binds the complete identity set, that its value validates independently,
that provenance distinguishes every path, and that correctness never depends on a cache hit.
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


@pytest.fixture(autouse=True)
def _clean_cache():
    das.reset_caches()
    yield
    das.reset_caches()


# ===================================================================== governance integration
def test_the_cache_is_discovered_and_classified_by_cache_authority():
    das.fresh_state()                                    # ensure module resident
    result = ca.verify()
    assert result["clean"], result["problems"]
    assert "docker_assurance_state._STATE_CACHE" in result["records"]["discovered"]


def test_the_cache_is_policy_declared_as_content_bound():
    doc = ca.load_policy()["caches"]["docker_assurance_state._STATE_CACHE"]
    assert doc["classification"] == ca.AUTHORITATIVE_CONTENT_BOUND_CACHE
    assert doc["content_binding"], "an authoritative content-bound cache must declare its binding"


def test_reset_empties_the_cache():
    das.store(das.fresh_state())
    assert das._STATE_CACHE
    das.reset_caches()
    assert das._STATE_CACHE == {}


def test_cache_authority_suspends_the_cache_during_fresh_derivation():
    das.store(das.fresh_state())
    # verify() derives the authoritative taxonomy with every classified cache suspended; it must
    # remain clean regardless of what our cache holds.
    assert ca.verify()["clean"]


def test_stored_and_returned_values_are_deep_frozen():
    from types import MappingProxyType
    kd = das.store(das.fresh_state())
    assert isinstance(das._STATE_CACHE[kd], MappingProxyType)
    state, tag = das.lookup(das.fresh_state())
    assert tag == "HIT" and isinstance(state, MappingProxyType)
    with pytest.raises(TypeError):
        state["schema_version"] = "x"


# ===================================================================== key contract
def test_key_binds_the_complete_identity_set():
    key = das.cache_key(das.fresh_state())
    assert set(key) == set(das.CACHE_KEY_FIELDS)
    for field in das.CACHE_KEY_FIELDS:
        assert key[field] not in (None, ""), field


def test_key_includes_authorization_tree_policy_and_both_universes():
    key = das.cache_key(das.fresh_state())
    for essential in ("authorization_pair_digest", "staged_tree", "policy_digest",
                      "category_table_digest", "production_universe_digest",
                      "independent_universe_digest", "assertion_contract_digest"):
        assert essential in key


@pytest.mark.parametrize("label,mutate", [
    ("unknown component", lambda k: k.update({"surprise": 1})),
    ("missing component", lambda k: k.pop("policy_digest")),
    ("empty component", lambda k: k.update({"staged_tree": ""})),
])
def test_validate_cache_key_fails_closed(label, mutate):
    key = das.cache_key(das.fresh_state())
    mutate(key)
    assert das.validate_cache_key(key), f"{label} must be refused"


def test_a_different_identity_produces_a_different_key_digest():
    s = das._thaw(das.fresh_state())
    base = das.cache_key_digest(s)
    for path in (("authorization", "pair_digest"), ("repository", "staged_tree"),
                 ("policy", "policy_digest")):
        mutated = copy.deepcopy(s)
        mutated[path[0]][path[1]] = "different-value"
        assert das.cache_key_digest(mutated) != base, f"{path} must change the key"


# ===================================================================== value contract
def test_a_valid_value_validates():
    kd = das.store(das.fresh_state())
    assert das.validate_cache_value(das._STATE_CACHE[kd], expected_key_digest=kd) == []


@pytest.mark.parametrize("label,mutate", [
    ("unknown value field", lambda v: v.update({"surprise": 1})),
    ("missing state", lambda v: v.pop("state")),
    ("stale cache schema", lambda v: v.update({"cache_schema_version": "old"})),
    ("bad validation status", lambda v: v.update({"validation_status": "SKIPPED"})),
    ("state digest mismatch", lambda v: v.update({"state_digest": "forged"})),
    ("key digest mismatch", lambda v: v.update({"cache_key_digest": "forged"})),
    ("provenance removed", lambda v: v.pop("provenance")),
])
def test_validate_cache_value_fails_closed(label, mutate):
    kd = das.store(das.fresh_state())
    v = das._thaw(das._STATE_CACHE[kd])                  # mutable copy
    mutate(v)
    assert das.validate_cache_value(v, expected_key_digest=kd), f"{label} must be refused"


def test_a_valid_key_never_excuses_an_invalid_value():
    kd = das.store(das.fresh_state())
    v = das._thaw(das._STATE_CACHE[kd])
    v["state"]["schema_version"] = "old"                 # value now carries an invalid state
    problems = das.validate_cache_value(v, expected_key_digest=kd)
    assert any("cached state is invalid" in p for p in problems)


# ===================================================================== freshness & provenance
def test_lookup_is_a_miss_after_reset():
    das.store(das.fresh_state())
    das.reset_caches()
    state, tag = das.lookup(das.fresh_state())
    assert state is None and tag == "MISS"


def test_a_cross_tree_value_filed_under_the_current_key_is_rejected():
    kd = das.store(das.fresh_state())
    # Forge a value whose state is from "another tree": its own key digest no longer equals kd.
    v = das._thaw(das._STATE_CACHE[kd])
    v["state"]["repository"]["staged_tree"] = "0" * 40
    das._STATE_CACHE[kd] = ca.deep_freeze(v)
    state, tag = das.lookup(das.fresh_state())
    assert state is None and tag.startswith("REJECTED"), tag


def test_provenance_records_origin_and_process():
    kd = das.store(das.fresh_state(), origin="cold")
    prov = das._thaw(das._STATE_CACHE[kd])["provenance"]
    assert prov["origin"] == "cold"
    assert prov["process_identity"] and prov["creation_utc"]
    assert prov["provenance_schema_version"] == das.PROVENANCE_SCHEMA_VERSION


def test_correctness_does_not_depend_on_a_cache_hit():
    """Cold (empty cache) must still yield a valid authoritative state."""
    das.reset_caches()
    assert das.validate_state(das.fresh_state()) == []
    assert das.authoritative_state()                     # no cache required
