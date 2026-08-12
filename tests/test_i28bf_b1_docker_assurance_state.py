"""Gate 4N-I28BF-B1 — the authoritative Docker assurance state (Layers A + B).

Proves the state is complete, deterministic, canonically ordered, digest-bound, deep-frozen, and
fail-closed on missing / unknown / malformed / stale fields, and that it binds the identities the
prior per-site state did not: the authorization pair and both universe digests.
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


def _plain():
    return das._thaw(das.fresh_state())


# ===================================================================== completeness & identities
def test_fresh_state_is_complete_and_valid():
    assert das.validate_state(das.fresh_state()) == []


def test_state_binds_the_authorization_pair_the_prior_per_site_state_did_not():
    auth = _plain()["authorization"]
    assert auth["issuance"] == "2026-08-12T05:00:00Z"
    assert auth["expiry"] == "2026-08-13T03:00:00Z"
    assert auth["duration_seconds"] == 79200
    assert auth["pair_digest"] == ca.digest(
        {k: auth[k] for k in ("issuance", "expiry", "duration_seconds")})


def test_state_binds_both_universe_digests_and_reconciles_them():
    uni = _plain()["universe"]
    assert uni["reconciliation"] == "AGREE"
    assert uni["production_universe_digest"] and uni["independent_universe_digest"]
    assert uni["site_count"] == 50 and uni["load_bearing_count"] == 47
    assert uni["expected_positive"] is True


def test_the_workflow_marker_is_the_exact_deferred_string():
    assert _plain()["aggregate"]["workflow_coverage"] == das.WORKFLOW_COVERAGE_MARKER
    assert "I28BG" in das.WORKFLOW_COVERAGE_MARKER


# ===================================================================== determinism & canonicalization
def test_digest_is_stable_across_independent_fresh_derivations():
    assert das.state_digest(das.fresh_state()) == das.state_digest(das.fresh_state())


def test_semantically_equal_ordering_yields_the_same_digest():
    s = _plain()
    reordered = copy.deepcopy(s)
    # Reverse the per-site tuple order; canonicalisation must make the digest identical.
    reordered["per_site"] = list(reversed(list(reordered["per_site"])))
    assert das.state_digest(reordered) == das.state_digest(s), (
        "canonical ordering must make a reordered-but-equal state digest-identical")


def test_a_semantic_change_moves_the_digest():
    s = _plain()
    changed = copy.deepcopy(s)
    changed["per_site"][0]["decision"] = "FAIL"
    assert das.state_digest(changed) != das.state_digest(s)


def test_authoritative_state_is_deep_frozen():
    from types import MappingProxyType
    a = das.authoritative_state()
    assert isinstance(a, MappingProxyType)
    with pytest.raises(TypeError):
        a["schema_version"] = "x"


# ===================================================================== fail-closed refusals
@pytest.mark.parametrize("label,mutate", [
    ("stale schema version", lambda s: s.update({"schema_version": "old"})),
    ("unknown top field", lambda s: s.update({"surprise": 1})),
    ("missing top field", lambda s: s.pop("universe")),
    ("auth digest wrong", lambda s: s["authorization"].update({"pair_digest": "forged"})),
    ("auth field removed", lambda s: s["authorization"].pop("pair_digest")),
    ("stale policy schema", lambda s: s["policy"].update({"policy_schema_version": "old"})),
    ("policy digest empty", lambda s: s["policy"].update({"policy_digest": ""})),
    ("stale parser schema", lambda s: s["parser"].update({"parser_schema_version": "old"})),
    ("stale position schema", lambda s: s["parser"].update({"source_position_version": "old"})),
    ("universe disagree", lambda s: s["universe"].update({"reconciliation": "DISAGREE"})),
    ("not positive", lambda s: s["universe"].update({"expected_positive": False})),
    ("empty universe", lambda s: s["universe"].update({"site_ids": (), "site_count": 0})),
    ("duplicate site id", lambda s: s["per_site"].append(dict(s["per_site"][0]))),
    ("site position removed", lambda s: s["per_site"][0].update({"position": ""})),
    ("workflow marker omitted", lambda s: s["aggregate"].pop("workflow_coverage")),
    ("workflow marker forced PASS", lambda s: s["aggregate"].update({"workflow_coverage": "PASS"})),
    ("aggregate forced clean over fail",
     lambda s: (s["per_site"][0].update({"decision": "FAIL"}),
                s["aggregate"].update({"docker_aggregate": True, "docker_per_site_layer": True}))),
    ("parser untrustworthy", lambda s: s["parser"].update({"parser_untrustworthy": ("x",)})),
], ids=lambda x: x if isinstance(x, str) else "")
def test_validate_state_fails_closed(label, mutate):
    s = _plain()
    mutate(s)
    assert das.validate_state(s), f"{label} must be refused"


def test_validate_rejects_a_non_mapping():
    assert das.validate_state(["not", "a", "dict"])
    assert das.validate_state(None)


def test_authoritative_state_raises_when_invalid(monkeypatch):
    monkeypatch.setattr(das, "fresh_state", lambda: {"schema_version": "old"})
    with pytest.raises(das.DockerAssuranceError):
        das.authoritative_state()
