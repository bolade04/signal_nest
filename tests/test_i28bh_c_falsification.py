"""Gate 4N-I28BH-C — independent falsification regressions.

BH-C attacked the certified BH-B assurance architecture with 14 independent adversaries. The bounded
false-passes they confirmed are pinned here so they cannot silently reopen. Each test drives a real
mutation and asserts the control now fails closed. All mutations are in-memory (monkeypatch); no repo
file is modified.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import critical_list_inventory as cli          # noqa: E402
import review_pin_control as rpc               # noqa: E402
import security_collection_assurance as sca    # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _ctx():
    return {"pins": json.loads((FIXTURES / "review-pin-registry.json").read_text()),
            "ledger": json.loads((FIXTURES / "review-record-ledger.json").read_text()),
            "consumers": json.loads((FIXTURES / "critical-list-contract.json").read_text())
            .get("completeness_consumers", {})}


def _reg():
    return json.loads((FIXTURES / "security-assurance-registry.json").read_text())["assurance"]


def _verdict(cid, handler):
    return handler(cid, _reg()[cid], _ctx())["verdict"]


# ===================================================================== baseline
def test_baseline_clean_after_bh_c():
    assert sca.assess()["clean"]
    inv = cli.check()
    assert inv["clean"] and inv["security_ungoverned"] == [] and inv["unclassified"] == []


# ===================================================================== F4 GENERATED tautology
def test_f4_generated_closure_injection_fails_closed(monkeypatch):
    cid = "gen_operator_policies.py::REFRESH_CLOSURE"
    import gen_operator_policies as go
    injected = copy.deepcopy(dict(go.REFRESH_CLOSURE))
    injected["star_regional"] = list(injected["star_regional"]) + ["iam:PassRole"]
    monkeypatch.setattr(go, "REFRESH_CLOSURE", injected)
    assert _verdict(cid, sca._h_generated) == rpc.REFUSED_DIGEST_DRIFT


def test_f4_generated_requires_a_drop_backstop_pin():
    # A FLATTEN generated row with no pin must refuse rather than accept the tautology.
    cid = "gen_operator_policies.py::REFRESH_CLOSURE"
    ctx = _ctx()
    ctx["pins"]["pins"].pop(cid, None)
    assert sca._h_generated(cid, _reg()[cid], ctx)["verdict"] == "REFUSED_NO_DROP_BACKSTOP"


# ===================================================================== F2 PARTITION circular universe
def test_f2_partition_deny_drop_fails_closed(monkeypatch):
    cid = "gen_boundary_policy.py::IAM_ADMIN_DENIED"
    import gen_boundary_policy as gbp
    shrunk = [a for a in gbp.IAM_ADMIN_DENIED][1:]
    monkeypatch.setattr(gbp, "IAM_ADMIN_DENIED", shrunk)
    assert _verdict(cid, sca._h_delegated) == rpc.REFUSED_DIGEST_DRIFT


# ===================================================================== F1 delegated identity confusion
def test_f1_delegated_wrong_consumer_fails_closed():
    cid = "cache_authority.py::CLASSIFICATIONS"
    entry = dict(_reg()[cid], consumer_ref="leak_scan.py::DECISIONS")
    assert sca._h_delegated(cid, entry, _ctx())["verdict"] == "REFUSED_MISWIRED_CONSUMER"


# ===================================================================== P1 DISPUTED runtime drop
def test_p1_disputed_context_emptied_fails_closed(monkeypatch):
    cid = "iam_eval.py::DISPUTED_RUNTIME_CONTEXT"
    import iam_eval
    monkeypatch.setattr(iam_eval, "DISPUTED_RUNTIME_CONTEXT", {})
    assert _verdict(cid, sca._h_runtime) == rpc.REFUSED_DIGEST_DRIFT


# ===================================================================== F5a canonicalization fidelity
def test_f5a_container_type_is_not_erased():
    cid = "x.py::Y"
    assert rpc.canonical_digest(cid, frozenset({"a", "b"}), False) != \
        rpc.canonical_digest(cid, set({"a", "b"}), False)
    assert rpc.canonical_digest(cid, ("a", "b"), True) != rpc.canonical_digest(cid, ["a", "b"], True)


def test_f5a_live_frozenset_to_set_reds(monkeypatch):
    import docker_boundary as db
    cid = "docker_boundary.py::TRUST_BOUNDARIES"
    if not isinstance(getattr(db, "TRUST_BOUNDARIES", None), frozenset):
        pytest.skip("TRUST_BOUNDARIES is not a frozenset on this tree")
    monkeypatch.setattr(db, "TRUST_BOUNDARIES", set(db.TRUST_BOUNDARIES))
    assert _verdict(cid, sca._h_review_pin) == rpc.REFUSED_DIGEST_DRIFT


def test_f5c_nested_lambda_constant_change_reds():
    f_allow = lambda: "ALLOW"   # noqa: E731
    f_deny = lambda: "DENY"     # noqa: E731
    assert rpc._canonical_member(f_allow) != rpc._canonical_member(f_deny)


# ===================================================================== F3/F7 root-of-trust governance
@pytest.mark.parametrize("govfile", ["readonly-verifier-ceiling.json", "critical-list-contract.json",
                                      "security-assurance-registry.json", "review-pin-registry.json"])
def test_f3_f7_every_governance_map_is_root_pinned(govfile, tmp_path, monkeypatch):
    tampered = json.loads((FIXTURES / govfile).read_text())
    tampered["__smuggled__"] = True
    p = tmp_path / govfile
    p.write_text(json.dumps(tampered))
    monkeypatch.setitem(sca._GOVERNED_FILES, govfile, p)
    problems = sca._root_of_trust(_ctx()["ledger"])
    assert any(govfile in prob for prob in problems)


# ===================================================================== P6 dispatch robustness
@pytest.mark.parametrize("bad_kind", [["a"], {"k": 1}, None, "", "authored_source_of_truth_integrity",
                                      " AUTHORED_SOURCE_OF_TRUTH_INTEGRITY", "FUTURE_KIND"])
def test_p6_malformed_kind_fails_closed_not_crash(bad_kind, tmp_path, monkeypatch):
    reg = json.loads((FIXTURES / "security-assurance-registry.json").read_text())
    cid = next(iter(reg["assurance"]))
    reg["assurance"][cid] = {"assurance_kind": bad_kind, "control": "review_pin"}
    p = tmp_path / "security-assurance-registry.json"
    p.write_text(json.dumps(reg))
    monkeypatch.setattr(sca, "REGISTRY", p)
    # governed_files digest will now mismatch; we only assert the dispatch does not raise and the row
    # is refused (no default PASS, no TypeError crash on an unhashable kind).
    result = sca.assess()
    row = next(r for r in result["rows"] if r["collection"] == cid)
    assert row["verdict"] == "REFUSED_UNKNOWN_KIND"


def test_inventory_and_dispatch_kinds_agree():
    assert set(cli.ASSURANCE_KINDS) == set(sca._HANDLERS)


# ===================================================================== F8 discovery extension
def test_f8_derived_collections_are_discovered():
    discovered = {c["id"] for c in cli.discover_collections()}
    # collections assigned via a helper call / binop / comprehension must now be visible
    for cid in ("must_not_contract.py::FORBIDDEN_CAPABILITIES",
                "gen_operator_policies.py::PERMANENT_DENY",
                "signalnest_identity.py::ALL_ROLE_NAMES"):
        assert cid in discovered, f"{cid} escaped discovery"


def test_f8_new_security_roots_are_governed():
    reg = _reg()
    for cid in ("must_not_contract.py::FORBIDDEN_CAPABILITIES",
                "gen_operator_policies.py::PERMANENT_DENY"):
        assert cid in reg and reg[cid]["assurance_kind"] == "AUTHORED_SOURCE_OF_TRUTH_INTEGRITY"


# ===================================================================== AH-2 every runtime predicate has an arm
@pytest.mark.parametrize("invariant,cid,mutate,module,attr", [
    ("RESOLVERS_DISPATCH_LIVENESS", "collection_completeness.py::RESOLVERS",
     lambda v: tuple(v) + ("ghost_kind",), "collection_completeness", "RESOLVERS"),
    ("DISCOVERERS_EMIT_LIVENESS", "mutation_discovery.py::DISCOVERERS",
     lambda v: tuple(v) + ("ghost",), "mutation_discovery", "DISCOVERERS"),
    ("SCAN_DECISION_VALUES_IN_DOMAIN", "leak_scan.py::SCAN_DECISIONS",
     lambda v: {"f": ("BOGUS", "r")}, "leak_scan", "SCAN_DECISIONS"),
    ("MAPPING_VALUES_ALL_NONEMPTY", "leak_scan.py::SKIPPED_WITH_REASON",
     lambda v: {"f": ""}, "leak_scan", "SKIPPED_WITH_REASON"),
])
def test_ah2_runtime_predicate_is_load_bearing(invariant, cid, mutate, module, attr, monkeypatch):
    import importlib
    m = importlib.import_module(module)
    monkeypatch.setattr(m, attr, mutate(getattr(m, attr)))
    entry = {"assurance_kind": "RUNTIME_INVARIANT_ASSURANCE", "control": "runtime", "invariant": invariant}
    v = sca._h_runtime(cid, entry, _ctx())["verdict"]
    assert v.startswith("REFUSED"), f"{invariant} accepted a violating state: {v}"
