"""Gate 4N-I28BH-E1 — collection_completeness applicability aligned with the approved assurance taxonomy.

WHAT THIS PINS. collection_completeness demands membership/partition completeness ONLY from the
collections whose primary assurance class is one whose property IS completeness (MEMBERSHIP,
PARTITION), deriving that set from the AUTHORITATIVE assurance registry — not from a second list. The
other SECURITY collections are governed by security_collection_assurance and are NOT reported as
"uncovered". This is a semantic alignment with the BH-B architectural adjudication, NOT a weakening:
every SECURITY collection is still owned by exactly one graded validator, and no identity may escape
by changing kind, losing its consumer/pin, or moving to an easier control. All mutations in-memory.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import collection_completeness as cc          # noqa: E402
import critical_list_inventory as cli          # noqa: E402
import security_collection_assurance as sca    # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"
REG = json.loads((FIXTURES / "security-assurance-registry.json").read_text())["assurance"]


# ===================================================================== baseline: green + accurate
def test_completeness_is_green_under_applicability_semantics():
    r = cc.check()
    assert r["clean"], r["problems"][:5]
    assert r["completeness_ungoverned"] == []
    assert r["completeness_applicable"] == 32           # MEMBERSHIP 25 + PARTITION 7
    assert r["completeness_governed"] == r["completeness_applicable"]
    assert r["non_completeness_assurance_owned_elsewhere"] == r["security_total"] - r["completeness_applicable"]


def test_applicability_is_derived_from_the_registry_not_a_duplicate_list():
    inv = cli.check()
    critical = set(inv["security_critical"])
    applicable, registry = cc.completeness_applicable(critical)
    expected = {cid for cid in critical
                if REG[cid]["assurance_kind"] in cc.COMPLETENESS_REQUIRING_KINDS}
    assert applicable == expected
    # the requiring kinds are exactly membership + partition
    assert set(cc.COMPLETENESS_REQUIRING_KINDS) == {"INDEPENDENT_MEMBERSHIP_COMPLETENESS",
                                                    "PARTITION_RELATION_ASSURANCE"}


# ===================================================================== NOT a skip=pass
def test_a_membership_collection_without_a_consumer_still_fails(monkeypatch):
    original = cc.specs
    def dropped():
        d = dict(original())
        victim = next(k for k in d if REG.get(k, {}).get("assurance_kind")
                      == "INDEPENDENT_MEMBERSHIP_COMPLETENESS")
        d.pop(victim)
        return d
    monkeypatch.setattr(cc, "specs", dropped)
    r = cc.check()
    assert not r["clean"]
    assert r["completeness_ungoverned"], "an applicable collection losing its consumer must be RED"


def test_a_partition_collection_without_a_consumer_still_fails(monkeypatch):
    original = cc.specs
    def dropped():
        d = dict(original())
        victim = next(k for k in d if REG.get(k, {}).get("assurance_kind")
                      == "PARTITION_RELATION_ASSURANCE")
        d.pop(victim)
        return d
    monkeypatch.setattr(cc, "specs", dropped)
    assert not cc.check()["clean"]


# ===================================================================== cross-validator totality
def test_every_security_collection_is_owned_by_exactly_one_validator():
    inv = cli.check()
    critical = set(inv["security_critical"])
    applicable, registry = cc.completeness_applicable(critical)
    recognized = set(sca._HANDLERS)
    # every SECURITY collection has a recognized assignment
    for cid in critical:
        kind = registry.get(cid, {}).get("assurance_kind")
        assert kind in recognized, f"{cid}: kind {kind!r} owned by no validator"
    # applicable -> owned by completeness (has a spec); non-applicable -> owned by sca (accepted)
    declared = set(cc.specs())
    assert applicable <= declared, sorted(applicable - declared)
    res = sca.assess()
    accepted = {r["collection"] for r in res["rows"] if r["verdict"] == "ACCEPT"}
    non_applicable = critical - applicable
    assert non_applicable <= accepted, sorted(non_applicable - accepted)
    # partition of ownership: no overlap gap, union is the whole SECURITY set
    assert applicable | non_applicable == critical


def test_a_security_collection_with_no_assignment_fails_completeness(monkeypatch):
    # simulate an unassigned SECURITY collection: registry missing an entry -> owned by NEITHER -> RED
    inv = cli.check()
    victim = next(iter(inv["security_critical"]))
    stripped = copy.deepcopy(json.loads((FIXTURES / "security-assurance-registry.json").read_text()))
    stripped["assurance"].pop(victim, None)
    import tempfile
    p = Path(tempfile.NamedTemporaryFile("w", suffix=".json", delete=False).name)
    p.write_text(json.dumps(stripped))
    monkeypatch.setattr(cc, "ASSURANCE_REGISTRY", p)
    r = cc.check()
    assert not r["clean"]
    assert any("NO assurance assignment" in prob or "owned by NEITHER" in prob for prob in r["problems"])


def test_an_unknown_assurance_kind_fails_completeness_ownership(monkeypatch):
    inv = cli.check()
    victim = next(iter(inv["security_critical"]))
    doc = copy.deepcopy(json.loads((FIXTURES / "security-assurance-registry.json").read_text()))
    doc["assurance"][victim]["assurance_kind"] = "MADE_UP_KIND"
    import tempfile
    p = Path(tempfile.NamedTemporaryFile("w", suffix=".json", delete=False).name)
    p.write_text(json.dumps(doc))
    monkeypatch.setattr(cc, "ASSURANCE_REGISTRY", p)
    r = cc.check()
    assert not r["clean"]
    assert any("unknown assurance kind" in prob for prob in r["problems"])


def test_missing_registry_fails_closed(monkeypatch):
    monkeypatch.setattr(cc, "ASSURANCE_REGISTRY", REPO_ROOT / "does-not-exist.json")
    with pytest.raises(cc.CompletenessError):
        cc.check()
