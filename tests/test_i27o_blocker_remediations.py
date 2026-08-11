#!/usr/bin/env python3
"""GATE 4N-I27O — the four canonical High blockers, pinned so they cannot return.

Each group states the defect it closes, replays the EXACT canary that demonstrated it, and
asserts the property that makes the canary refuse — never merely that today's values pass.

The shape shared by all four defects: a control whose SCOPE was a literal the control itself
owned. Membership in the list WAS the justification, so widening the list widened the control's
own idea of what is acceptable and nothing objected. Every fix below replaces "is it in the
list?" with a question the list cannot answer about itself.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# The unapproved identifier the I27M bypass planted. It is ASSEMBLED rather than written as a
# literal for a reason worth stating: leak_scan scans this file too, and an unapproved account
# id spelled out here would be a real finding. Adding it to the approved registry to silence
# that would be exactly the widening this whole remediation exists to refuse — so the canary
# stays unapproved and the test carries it in pieces.
UNAPPROVED_ACCOUNT = "".join(str(d) for d in (2, 1, 0, 9, 8, 7, 6, 5, 4, 3, 2, 1))

import ci_invocation_model as inv  # noqa: E402
import leak_scan  # noqa: E402
import protected_inventory as pi  # noqa: E402


# =====================================================================================
# BLOCKER 1 (dependent residual) — ci_invocation_model.py::DATA_CONSUMERS
# =====================================================================================
#
# THE DEFECT. `invoked_targets` harvested repository paths from EVERY token of a command, so
# `echo python3 scripts/allow_model.py` yielded scripts/allow_model.py as an "invocation". The
# only thing preventing that was `echo` appearing in the hand-written DATA_CONSUMERS set; empty
# the set and the echo substitution satisfies the must-invoke contract.

ECHO_SUBSTITUTION = "echo python3 scripts/allow_model.py"
REAL_INVOCATION = "python3 scripts/allow_model.py"


def test_a_real_invocation_is_recognised():
    assert "scripts/allow_model.py" in inv.invoked_targets(REAL_INVOCATION)


def test_echo_substitution_yields_no_invocation():
    assert inv.invoked_targets(ECHO_SUBSTITUTION) == set()


def test_echo_substitution_is_rejected_even_with_DATA_CONSUMERS_emptied(monkeypatch):
    """THE CANARY. This is the whole repair: the verdict must not depend on that set."""
    monkeypatch.setattr(inv, "DATA_CONSUMERS", set())
    assert inv.invoked_targets(ECHO_SUBSTITUTION) == set(), (
        "emptying DATA_CONSUMERS restored the echo-substitution bypass; the invocation "
        "decision is still resting on a hand-authored list of command names")


@pytest.mark.parametrize("body", [
    "printf '%s' 'python3 scripts/allow_model.py'",
    "cat scripts/allow_model.py",
    "my_wrapper scripts/allow_model.py",          # a command nobody enumerated
    ": scripts/allow_model.py",
])
def test_no_unrecognised_command_can_claim_an_invocation(body, monkeypatch):
    """A longer list would be the same defect. An UNKNOWN command proves nothing, so it
    yields nothing — including `my_wrapper`, which no list would have contained."""
    monkeypatch.setattr(inv, "DATA_CONSUMERS", set())
    assert "scripts/allow_model.py" not in inv.invoked_targets(body)


def test_interpreter_script_arguments_are_still_invocations():
    for body in ("python3 scripts/allow_model.py", "bash scripts/run-tests-api.sh",
                 "python3 -m pytest tests/ -q"):
        assert inv.invoked_targets(body), f"{body!r} lost its invocation"


def test_pytest_target_paths_are_executable_positions():
    assert "tests/" in inv.invoked_targets("python3 -m pytest tests/ -q")
    assert "PYTEST" in inv.invoked_targets("python3 -m pytest tests/ -q")


def test_the_module_argument_of_a_non_runner_is_not_harvested():
    """`python -m json.tool scripts/x.py` does not RUN scripts/x.py."""
    assert "scripts/allow_model.py" not in inv.invoked_targets(
        "python3 -m json.tool scripts/allow_model.py")


def test_every_graded_step_still_satisfies_the_contract():
    """The repair must not break the forty-four steps it protects."""
    result = inv.check()
    assert result["clean"], result["problems"]
    assert result["graded_in_workflow"] == result["graded_in_contract"] == 45  # +1: Gate 4N-I28BH-B added the security_collection_assurance graded step


# =====================================================================================
# BLOCKER 2 — allow_model.py::PERMITTED_WILDCARDS
# =====================================================================================


def _allow_model():
    import allow_model
    return allow_model


def test_the_current_permitted_wildcard_set_is_independently_justified():
    _allow_model().require_independently_justified_wildcards()


def test_budgets_star_is_refused():
    """THE CANARY: the exact I27K token, added to the list that used to justify it."""
    am = _allow_model()
    with pytest.raises(am.WildcardJustificationError, match="cannot be justified"):
        am.require_independently_justified_wildcards(
            {**am.PERMITTED_WILDCARDS, "budgets:*": "probe justification"})


@pytest.mark.parametrize("token", ["s3:*", "iam:*", "kms:*", "unknownsvc:*", "sts:Get*"])
def test_no_service_wildcard_can_be_justified(token):
    am = _allow_model()
    with pytest.raises(am.WildcardJustificationError):
        am.require_independently_justified_wildcards({**am.PERMITTED_WILDCARDS,
                                                      token: "probe"})


def test_an_exact_action_the_contract_requires_is_accepted():
    am = _allow_model()
    am.require_independently_justified_wildcards(
        {"sts:GetCallerIdentity": "required by the closure contract"})


def test_an_exact_action_the_contract_does_not_classify_is_refused():
    am = _allow_model()
    with pytest.raises(am.WildcardJustificationError, match="not a justification"):
        am.require_independently_justified_wildcards(
            {"budgets:SomeUnlistedAction": "asserted, but nothing external says so"})


def test_an_entry_with_no_stated_reason_is_refused():
    am = _allow_model()
    with pytest.raises(am.WildcardJustificationError, match="no stated reason"):
        am.require_independently_justified_wildcards({"sts:GetCallerIdentity": "  "})


def test_the_justification_oracle_does_not_read_the_list_it_bounds():
    """A list compared against a copy of itself agrees with anything."""
    import ast

    am = _allow_model()
    tree = ast.parse(Path(am.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "require_independently_justified_wildcards")
    # The parameter defaulting to the module constant is the ONE permitted reference; the
    # decision itself must come from classify()/FORBIDDEN_CAPABILITIES.
    decision = [n for n in ast.walk(func) if isinstance(n, ast.Call)]
    called = {getattr(c.func, "id", getattr(c.func, "attr", "")) for c in decision}
    assert "classify" in called, "the wildcard decision no longer consults the closure contract"


# =====================================================================================
# BLOCKER 3 — protected_inventory.py::SELF_ATTESTING_FIELDS
# =====================================================================================

def _fixture() -> dict:
    return json.loads(pi.SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))


def test_the_tracked_synthetic_fixture_is_not_self_attesting():
    assert pi.self_attesting_fields(_fixture()) == []


def test_a_document_carrying_its_own_sha256_is_refused(monkeypatch):
    """THE CANARY, with the original four-name list emptied."""
    monkeypatch.setattr(pi, "SELF_ATTESTING_FIELDS", ())
    data = dict(_fixture(), sha256="0" * 64)
    assert pi.self_attesting_fields(data), "a self-attesting document validated cleanly"
    with pytest.raises(pi.InventoryError, match="own verification value"):
        pi._validate(data, tier=pi.TIER_SYNTHETIC)


@pytest.mark.parametrize("field", ["file_digest", "inventory_hash", "content_checksum",
                                   "body_signature", "fingerprint"])
def test_integrity_shaped_names_the_old_list_never_held_are_refused(field, monkeypatch):
    monkeypatch.setattr(pi, "SELF_ATTESTING_FIELDS", ())
    data = dict(_fixture(), **{field: "0" * 64})
    assert pi.self_attesting_fields(data), f"{field!r} was accepted"


def test_a_renamed_field_holding_the_documents_real_digest_is_refused(monkeypatch):
    """The name-independent half. Renaming is the obvious evasion, so the structural check
    recomputes the document's own canonical digest with the field removed."""
    monkeypatch.setattr(pi, "SELF_ATTESTING_FIELDS", ())
    import hashlib

    data = _fixture()
    data["provenance_marker"] = hashlib.sha256(pi.canonical_bytes(data)).hexdigest()
    offenders = pi.self_attesting_fields(data)
    assert any("own canonical digest" in o for o in offenders), offenders


def test_an_unrelated_hex_value_is_not_mistaken_for_self_attestation():
    data = dict(_fixture(), some_opaque_id="a" * 64)
    assert pi.self_attesting_fields(data) == []


def test_a_missing_required_field_still_fails_closed():
    data = {k: v for k, v in _fixture().items() if k != "trails"}
    with pytest.raises(pi.InventoryError, match="missing required field"):
        pi._validate(data, tier=pi.TIER_SYNTHETIC)


def test_the_tier_1_path_still_loads():
    loaded = pi.load({"SIGNALNEST_ANCHOR_TIER": pi.TIER_SYNTHETIC})
    assert loaded.tier == pi.TIER_SYNTHETIC
    assert loaded.certifies_production is False


def test_protected_tier_without_evidence_remains_explicit():
    """Tier-2 evidence is unavailable to this chain and is never fabricated."""
    with pytest.raises(pi.InventoryError):
        pi.load({"SIGNALNEST_ANCHOR_TIER": pi.TIER_PROTECTED})


# =====================================================================================
# BLOCKER 4 — leak_scan.py::ALLOWED_ACCOUNTS
# =====================================================================================


def test_every_permitted_account_is_independently_registered():
    leak_scan.require_registered_allowed_accounts()


def test_the_registry_and_the_scanner_agree_in_both_directions():
    assert set(leak_scan.approved_accounts()) == set(leak_scan.ALLOWED_ACCOUNTS)


def test_broadening_the_scanner_literal_alone_is_refused(monkeypatch):
    """THE CANARY: the exact I27M bypass — add the planted id to ALLOWED_ACCOUNTS."""
    monkeypatch.setattr(leak_scan, "ALLOWED_ACCOUNTS",
                        frozenset(leak_scan.ALLOWED_ACCOUNTS | {UNAPPROVED_ACCOUNT}))
    with pytest.raises(leak_scan.AccountRegistryError, match="no registry entry"):
        leak_scan.require_registered_allowed_accounts()


def test_removing_a_registered_account_from_the_scanner_is_refused(monkeypatch):
    monkeypatch.setattr(leak_scan, "ALLOWED_ACCOUNTS",
                        frozenset(set(leak_scan.ALLOWED_ACCOUNTS) - {"000000000000"}))
    with pytest.raises(leak_scan.AccountRegistryError, match="does not permit"):
        leak_scan.require_registered_allowed_accounts()


def test_an_absent_registry_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(leak_scan, "APPROVED_ACCOUNT_REGISTRY", tmp_path / "absent.json")
    with pytest.raises(leak_scan.AccountRegistryError, match="ABSENT"):
        leak_scan.approved_accounts()


@pytest.mark.parametrize("entry,match", [
    ({"account_id": UNAPPROVED_ACCOUNT, "classification": "SYNTHETIC_FOREIGN_ACCOUNT",
      "provenance": "short"}, "no stated provenance"),
    ({"account_id": UNAPPROVED_ACCOUNT, "classification": "PRODUCTION_ACCOUNT",
      "provenance": "a real account someone wanted to allow through the scan"},
     "not one of the non-live classes"),
    ({"account_id": "nope", "classification": "SYNTHETIC_FOREIGN_ACCOUNT",
      "provenance": "an entry whose account id is not twelve digits at all"},
     "no valid account_id"),
])
def test_a_registry_entry_without_real_justification_is_refused(entry, match, monkeypatch,
                                                                tmp_path):
    doc = {"approved_accounts": [entry]}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(leak_scan, "APPROVED_ACCOUNT_REGISTRY", path)
    with pytest.raises(leak_scan.AccountRegistryError, match=match):
        leak_scan.approved_accounts()


def test_the_registry_is_not_derived_from_the_scanner_literal():
    """If the registry were generated from ALLOWED_ACCOUNTS it would agree by construction."""
    import ast

    tree = ast.parse(Path(leak_scan.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "approved_accounts")
    names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    assert "ALLOWED_ACCOUNTS" not in names, (
        "approved_accounts() reads the literal it is supposed to bound")


def test_a_planted_unapproved_identifier_is_still_detected(tmp_path):
    """The scanner must keep doing its actual job."""
    assert leak_scan.scan_text(f"account {UNAPPROVED_ACCOUNT} appears here"), \
        "an unapproved identifier is no longer reported"
    assert leak_scan.scan_text("account " + sorted(leak_scan.ALLOWED_ACCOUNTS)[0]
                               + " appears here") == [], \
        "a registered placeholder is now falsely reported"


def test_the_graded_containment_command_is_clean_on_the_real_tree():
    proc = subprocess.run([sys.executable, "scripts/leak_scan.py"], cwd=REPO_ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
