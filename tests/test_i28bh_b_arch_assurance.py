"""Gate 4N-I28BH-B-ARCHITECTURAL-ADJUDICATION — the property-specific security-collection assurance.

WHAT THIS PROVES. Every SECURITY_CRITICAL collection now carries a load-bearing, non-circular,
fail-closed assurance control MATCHING THE PROPERTY THAT CAN BE PROVEN FOR IT (membership where an
independent oracle exists; reviewed integrity / exclusion-policy / cross-domain / generated-contract /
runtime-invariant where it does not). This file is the FALSIFICATION MATRIX: for every control class
it exhibits a mutation that MUST flip the control RED, plus the self-attestation and root-of-trust
attacks the design must defeat. A control that cannot be made to fail is not a control — so each
class is exercised in both directions (clean baseline ACCEPT, mutated REFUSED_*).

None of these tests mutate a repository file: live-collection drift is simulated with monkeypatch,
registry/pin/ledger tampering with in-memory copies.
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


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _two_live_review_pins():
    """Two collection ids that are review-pinned with the default live-import source (so a
    monkeypatched drift is observable), preferring a plain set/tuple collection."""
    reg = _load("security-assurance-registry.json")["assurance"]
    pins = _load("review-pin-registry.json")["pins"]
    live = [cid for cid, p in sorted(pins.items())
            if p.get("source", "live") == "live"
            and reg.get(cid, {}).get("assurance_kind") == "AUTHORED_SOURCE_OF_TRUTH_INTEGRITY"]
    return live[0], live[1]


_REVIEW_CID, _OTHER_CID = _two_live_review_pins()


@pytest.fixture(scope="module")
def registry():
    return _load("security-assurance-registry.json")


@pytest.fixture(scope="module")
def pins():
    return _load("review-pin-registry.json")


@pytest.fixture(scope="module")
def ledger():
    return _load("review-record-ledger.json")


# ===================================================================== BASELINE (both green)
def test_inventory_is_complete_zero_ungoverned():
    result = cli.check()
    assert result["clean"], result["problems"][:5]
    assert result["security_ungoverned"] == [], result["security_ungoverned"]
    assert result["unclassified"] == []
    assert result["stale_classifications"] == []
    assert result["stale_assurance"] == []
    assert result["security_critical_count"] == 178  # BH-C: +2 discovery-recovered SECURITY roots


def test_every_assignment_accepts_at_baseline():
    result = sca.assess()
    assert result["clean"], result["problems"][:5]
    assert result["assigned"] == result["accepted"] == 178  # BH-C: +2


def test_assignment_covers_exactly_the_security_collections(registry):
    inv = cli.check()
    assured = set(registry["assurance"])
    security = set(inv["security_critical"])
    assert assured == security, {"only_assured": sorted(assured - security),
                                 "only_security": sorted(security - assured)}


# ===================================================================== CLOSED DISPATCH, NO DEFAULT
def test_unknown_assurance_kind_fails_closed(registry, pins, ledger, monkeypatch):
    bad = copy.deepcopy(registry)
    cid = next(iter(bad["assurance"]))
    bad["assurance"][cid] = {"assurance_kind": "TOTALLY_MADE_UP", "control": "review_pin"}
    _run_with(monkeypatch, bad, pins, ledger)
    result = sca.assess()
    assert not result["clean"]
    assert any(r["collection"] == cid and r["verdict"] == "REFUSED_UNKNOWN_KIND"
               for r in result["rows"])


def test_inventory_rejects_a_kind_outside_the_closed_enum():
    assert "TOTALLY_MADE_UP" not in cli.ASSURANCE_KINDS
    assert set(cli.ASSURANCE_KINDS) == set(sca._HANDLERS)   # inventory and dispatch agree


# ===================================================================== REVIEW-PIN (authored integrity)
def test_review_pin_drifts_red_when_the_collection_changes(pins, ledger, monkeypatch):
    cid = _REVIEW_CID
    module_file, attr = cid.split("::")
    module_name = module_file[:-3] if module_file.endswith(".py") else module_file
    import importlib
    module = importlib.import_module(module_name)
    original = getattr(module, attr)
    if isinstance(original, dict):
        mutated = dict(original); mutated["__injected__"] = "attacker"
    elif isinstance(original, (list, tuple)):
        mutated = type(original)(list(original) + ["__injected__"])
    else:
        mutated = set(original) | {"__injected__"}
    monkeypatch.setattr(module, attr, mutated)
    pin = pins["pins"][cid]
    value, err = rpc._load_for_pin(cid, pin)
    verdict = rpc.verify_pin(cid, pin, value, ledger, err)["verdict"]
    assert verdict == rpc.REFUSED_DIGEST_DRIFT


def test_self_deriving_pin_is_refused(pins, ledger):
    cid = _REVIEW_CID
    pin = dict(pins["pins"][cid], recompute=True)
    value, err = rpc._load_for_pin(cid, pin)
    assert rpc.verify_pin(cid, pin, value, ledger, err)["verdict"] == rpc.REFUSED_SELF_DERIVING_PIN


def test_copied_pin_is_refused_on_identity(pins, ledger):
    cid = _REVIEW_CID
    other = _OTHER_CID
    pin = dict(pins["pins"][cid])
    value, err = rpc._load_for_pin(cid, pin)
    # verify against the WRONG subject -> misbound identity
    assert rpc.verify_pin(other, pin, value, ledger, err)["verdict"] == rpc.REFUSED_MISBOUND_IDENTITY


def test_stale_review_record_reds_every_pin_that_cites_it(pins, ledger):
    cid = _REVIEW_CID
    pin = pins["pins"][cid]
    superseded = copy.deepcopy(ledger)
    for rec in superseded["review_records"].values():
        rec["status"] = "SUPERSEDED"
    value, err = rpc._load_for_pin(cid, pin)
    assert rpc.verify_pin(cid, pin, value, superseded, err)["verdict"] == rpc.REFUSED_STALE_REVIEW


def test_unknown_review_record_is_a_self_minted_approval(pins):
    cid = _REVIEW_CID
    pin = dict(pins["pins"][cid], review_record_id="REV-I-MINTED-THIS")
    value, err = rpc._load_for_pin(cid, pin)
    assert rpc.verify_pin(cid, pin, value, {"review_records": {}}, err)["verdict"] \
        == rpc.REFUSED_UNKNOWN_REVIEW


def test_missing_pin_fails_closed(ledger):
    cid = _REVIEW_CID
    assert rpc.verify_pin(cid, None, set(), ledger)["verdict"] == rpc.REFUSED_MISSING_PIN


def test_embedded_callable_is_content_pinned_by_code_not_address(pins, ledger):
    # ACTOR_RULES carries policy lambdas; the pin must be stable across processes yet move when the
    # lambda BODY changes. Baseline ACCEPT was already proven; here a code change must RED.
    cid = "role_bootstrap_lifecycle.py::ACTOR_RULES"
    import role_bootstrap_lifecycle as rb
    pin = pins["pins"][cid]
    good = rpc.canonical_digest(cid, rb.ACTOR_RULES, pin["ordered"])
    assert good == pin["reviewed_digest"]              # deterministic, matches baseline
    tampered = copy.deepcopy(dict(rb.ACTOR_RULES))
    first = next(iter(tampered))
    tampered[first] = dict(tampered[first]); tampered[first]["policy"] = lambda ctx: True  # changed
    assert rpc.canonical_digest(cid, tampered, pin["ordered"]) != pin["reviewed_digest"]


# ===================================================================== EXCLUSION-POLICY
def test_exclusion_d1_unjustified_member_fails_closed(registry, pins, ledger, monkeypatch):
    cid = "allow_model.py::EXEMPTIONS"
    entry = registry["assurance"][cid]
    assert entry["subtype"] == "D1"
    import allow_model
    injected = copy.deepcopy(dict(allow_model.EXEMPTIONS))
    injected["attacker_operator"] = {"iam:*": "unauthorised"}
    monkeypatch.setattr(allow_model, "EXEMPTIONS", injected)
    ctx = {"pins": pins, "ledger": ledger, "consumers": {}}
    # digest drift fires first (a real change), which is itself fail-closed for D1.
    verdict = sca._h_exclusion(cid, entry, ctx)["verdict"]
    assert verdict in ("REFUSED_DIGEST_DRIFT", "REFUSED_UNJUSTIFIED_MEMBER")


def test_exclusion_d2_member_outside_ceiling_fails_closed(registry, pins, ledger, monkeypatch):
    cid = "gen_readonly_verifier_policy.py::AUDIT_READS"
    entry = registry["assurance"][cid]
    assert entry["subtype"] == "D2" and entry["ceiling_relation"] == "SUBSET_OF_CEILING"
    import gen_readonly_verifier_policy as g
    monkeypatch.setattr(g, "AUDIT_READS", tuple(g.AUDIT_READS) + ("iam:DeleteRole",))
    ctx = {"pins": pins, "ledger": ledger, "consumers": {}}
    assert sca._h_exclusion(cid, entry, ctx)["verdict"] == "REFUSED_EXCEEDS_CEILING"


def test_exclusion_d2_forbidden_member_intersects_mustnot(registry, pins, ledger, monkeypatch):
    cid = "gen_bootstrap_operator_policy.py::BOUNDARY_ATTACH_ACTIONS"
    entry = registry["assurance"][cid]
    assert entry["subtype"] == "D2" and entry["ceiling_relation"] == "DISJOINT_FROM_MUSTNOT"
    import gen_bootstrap_operator_policy as g, must_not_contract
    forbidden = next(iter(must_not_contract.FORBIDDEN_CAPABILITIES))
    live = sca._live_members(cid)
    monkeypatch.setattr(g, cid.split("::")[1], tuple(live) + (forbidden,))
    ctx = {"pins": pins, "ledger": ledger, "consumers": {}}
    assert sca._h_exclusion(cid, entry, ctx)["verdict"] == "REFUSED_INTERSECTS_MUSTNOT"


# ===================================================================== CROSS-DOMAIN / GENERATED / RUNTIME
def test_cross_domain_drift_fails_closed(registry, pins, ledger, monkeypatch):
    cid = "deny_triangulation.py::PROTECTED_RESOURCE"
    entry = registry["assurance"][cid]
    assert entry["control"] == "cross_domain"
    import deny_triangulation
    monkeypatch.setattr(deny_triangulation, "PROTECTED_RESOURCE",
                        tuple(sca._live_members(cid)) + ("arn:aws:iam::999:not-in-owner",))
    ctx = {"pins": pins, "ledger": ledger, "consumers": {}}
    assert sca._h_cross_domain(cid, entry, ctx)["verdict"] == "REFUSED_CROSS_DOMAIN_DRIFT"


@pytest.mark.parametrize("cid,module_name,attr", [
    ("deny_triangulation.py::PROTECTED_RESOURCE", "deny_triangulation", "PROTECTED_RESOURCE"),
    ("docker_boundary.py::LOAD_BEARING_CLASSIFICATIONS", "docker_boundary", "LOAD_BEARING_CLASSIFICATIONS"),
])
def test_cross_domain_subset_member_DROP_fails_closed(cid, module_name, attr, registry, pins, ledger,
                                                      monkeypatch):
    """The adversarial-review escape: a SUBSET_OF_OWNER curated list can shrink toward empty and stay
    a subset. The review-pin drift backstop must catch member-REMOVAL (the documented failure mode)."""
    import importlib
    entry = registry["assurance"][cid]
    assert entry["control"] == "cross_domain" and entry["relation"] == "SUBSET_OF_OWNER"
    assert cid in pins["pins"], "a SUBSET row must carry a review-pin drop backstop"
    module = importlib.import_module(module_name)
    live = getattr(module, attr)
    one = next(iter(live))
    shrunk = {one: live[one]} if isinstance(live, dict) else type(live)([one]) \
        if isinstance(live, (list, tuple)) else frozenset({one})
    monkeypatch.setattr(module, attr, shrunk)
    ctx = {"pins": pins, "ledger": ledger, "consumers": {}}
    assert sca._h_cross_domain(cid, entry, ctx)["verdict"] == rpc.REFUSED_DIGEST_DRIFT


def test_generated_mismatch_fails_closed(registry, pins, ledger, monkeypatch):
    cid = "gen_operator_policies.py::REFRESH_CLOSURE"
    entry = registry["assurance"][cid]
    assert entry["control"] == "generated"
    import gen_operator_policies as go
    injected = copy.deepcopy(dict(go.REFRESH_CLOSURE))
    injected["attacker_group"] = ["iam:PassRole"]
    monkeypatch.setattr(go, "REFRESH_CLOSURE", injected)
    ctx = {"pins": pins, "ledger": ledger, "consumers": {}}
    assert sca._h_generated(cid, entry, ctx)["verdict"] == "REFUSED_GENERATOR_MISMATCH"


def test_runtime_invariant_violation_fails_closed(registry, pins, ledger, monkeypatch):
    cid = "collection_completeness.py::RESOLVERS"
    entry = registry["assurance"][cid]
    assert entry["control"] == "runtime"
    import collection_completeness as cc
    monkeypatch.setattr(cc, "RESOLVERS", tuple(cc.RESOLVERS) + ("unregistered_ghost_kind",))
    ctx = {"pins": pins, "ledger": ledger, "consumers": {}}
    assert sca._h_runtime(cid, entry, ctx)["verdict"] == "REFUSED_INVARIANT_VIOLATED"


def test_delegated_membership_unwired_fails_closed(registry, pins, ledger):
    cid = "cache_authority.py::CLASSIFICATIONS"       # a class-A membership row
    entry = dict(registry["assurance"][cid])
    assert entry["assurance_kind"] == "INDEPENDENT_MEMBERSHIP_COMPLETENESS"
    ctx = {"pins": pins, "ledger": ledger, "consumers": {}}   # empty consumer map -> unwired
    assert sca._h_delegated(cid, entry, ctx)["verdict"] == "REFUSED_UNWIRED"


# ===================================================================== ROOT OF TRUST
def test_tampering_the_assurance_registry_fails_root_of_trust(ledger, tmp_path, monkeypatch):
    tampered = _load("security-assurance-registry.json")
    cid = next(iter(tampered["assurance"]))
    tampered["assurance"][cid]["assurance_kind"] = "AUTHORED_SOURCE_OF_TRUTH_INTEGRITY"
    tampered["assurance"][cid]["_smuggled"] = True
    path = tmp_path / "security-assurance-registry.json"
    path.write_text(json.dumps(tampered))
    # BH-C: root-of-trust now iterates the _GOVERNED_FILES map, so redirect that entry (not sca.REGISTRY).
    monkeypatch.setitem(sca._GOVERNED_FILES, "security-assurance-registry.json", path)
    problems = sca._root_of_trust(ledger)
    assert any("security-assurance-registry.json" in p for p in problems)


def test_ledger_with_no_governed_files_is_ungoverned():
    assert sca._root_of_trust({"review_records": {}})


def test_ledger_bad_status_fails_closed():
    led = _load("review-record-ledger.json")
    led["review_records"]["REV-BOGUS"] = {"status": "SELF_APPROVED"}
    problems = sca._root_of_trust(led)
    assert any("SELF_APPROVED" in p for p in problems)


# ===================================================================== helpers
def _run_with(monkeypatch, registry, pins, ledger, tmp_path=None):
    """Point the module's fixtures at in-memory copies via temp files."""
    import tempfile
    for attr, doc in (("REGISTRY", registry), ("PIN_REGISTRY", pins), ("REVIEW_LEDGER", ledger)):
        t = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(doc, t); t.close()
        monkeypatch.setattr(sca, attr, Path(t.name))
