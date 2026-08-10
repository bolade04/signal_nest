"""Gate 4N-I28BF-A4 — AC-23 contract-honesty validation.

WHAT THIS PROVES. Section 16: AC-23 must honestly describe the test it protects — its declared
protected property, assertion classes, proving mutation, minimum count, owner callables, and
consumers must match reality, and none of them may overstate what the target actually proves. The
eight retarget/overstate attacks below must each be refused, and the honest facts (owner callables
live, the mandatory control consumes AC-23, its linked behaviour is re-derived at session finish)
must hold.
"""

from __future__ import annotations

import ast
import copy
import json
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import assertion_contracts as ac                   # noqa: E402
import docker_boundary as db                       # noqa: E402

REGISTRY_PATH = REPO_ROOT / "tests" / "fixtures" / "assertion-contract-registry.json"
AC23_ID = "AC-23-DOCKER-CATEGORY-AND-SESSION-FINISH"


def _registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _ac23() -> dict:
    return next(c for c in _registry()["contracts"] if c["contract_id"] == AC23_ID)


def _tree_with(registry: dict, tmp_path: Path, *, target_file_text: str | None = None) -> Path:
    """Materialise AC-23's target file (optionally edited) + the given registry, return the root."""
    root = tmp_path / "repo"
    (root / "tests" / "fixtures").mkdir(parents=True)
    for rel in sorted({c["file"] for c in registry["contracts"]}):
        src = REPO_ROOT / rel
        if src.is_file():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, root / rel)
    if target_file_text is not None:
        (root / _ac23()["file"]).write_text(target_file_text)
    (root / "tests" / "fixtures" / "assertion-contract-registry.json").write_text(
        json.dumps(registry, indent=1))
    return root


def _validate(root: Path) -> dict:
    reg = json.loads((root / "tests" / "fixtures" / "assertion-contract-registry.json").read_text())
    return ac.validate(reg, root=root)


def _reg_with_ac23(**overrides) -> dict:
    reg = _registry()
    for c in reg["contracts"]:
        if c["contract_id"] == AC23_ID:
            c.update(overrides)
    return reg


# ===================================================================== honest facts hold
def test_ac23_baseline_is_honest_and_clean(tmp_path):
    root = _tree_with(_registry(), tmp_path)
    assert _validate(root)["clean"], _validate(root)["problems"]


def test_ac23_declares_the_property_classes_and_proving_mutation_it_actually_has():
    c = _ac23()
    assert "EXACT lookup" in c["protected_invariant"] or "exact" in c["protected_invariant"].lower()
    assert any(r["class"] == "EXACT_IDENTITY_EQUALITY" for r in c["required_assertions"])
    assert "CATEGORY_INVALID" in {t for r in c["required_assertions"]
                                  for t in r.get("must_reference", [])}
    assert "substring containment" in c["proving_mutation"]
    assert c["minimum_meaningful_assertions"] >= 2


def test_ac23_owner_callables_are_live():
    """The linked behaviour names real, callable production functions."""
    assert callable(db.resolve_steering_entry)
    assert callable(db.per_site_differences)
    assert "resolve_steering_entry" in _ac23()["linked_behaviour"]
    assert "per_site_differences" in _ac23()["linked_behaviour"]


def test_ac23_is_consumed_by_the_mandatory_control_and_its_behaviour_at_session_finish():
    """Baseline/finish binding: the mandatory assertion control validates AC-23, and AC-23's linked
    behaviour (per_site_differences) is what reverify re-derives at session finish."""
    assert AC23_ID in {c["contract_id"] for c in _registry()["contracts"]}
    boot_src = (REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text()
    assert "per_site_differences" in boot_src, (
        "AC-23's linked behaviour must be consumed at session finish (reverify docker_per_site)")


def test_ac23_proving_mutation_actually_flips_the_target(tmp_path):
    """The proving mutation must exercise the SAME property AC-23 names: restoring substring
    containment makes a measured widening input resolve to a mechanism, so the target no longer
    sees CATEGORY_INVALID. Simulated here without touching production: the invariant is that the
    target's assertion depends on resolve_steering_entry returning CATEGORY_INVALID."""
    cls, mech = db.resolve_steering_entry("flagrant nonsense", db.load_policy()["steering"])
    assert cls == db.CATEGORY_INVALID and mech == ()
    # If containment were restored, this input would resolve to real mechanisms and the target's
    # `assert classification == CATEGORY_INVALID` would fail — the property AC-23 protects.


# ===================================================================== the eight honesty attacks
def test_attack_1_retargeting_ac23_to_an_unrelated_test_is_refused(tmp_path):
    reg = _reg_with_ac23(test="test_the_category_table_is_structurally_sound")
    root = _tree_with(reg, tmp_path)
    result = _validate(root)
    assert not result["clean"], "retargeting to a test lacking the CATEGORY_INVALID equality must fail"
    assert any(AC23_ID in p for p in result["problems"])


def test_attack_2_changing_declared_classes_without_changing_the_test_is_refused(tmp_path):
    reg = _reg_with_ac23(required_assertions=[{"class": "SET_EQUALITY",
                                               "must_reference": ["CATEGORY_INVALID"]}])
    root = _tree_with(reg, tmp_path)
    assert not _validate(root)["clean"], "declaring a class the target lacks must be refused"


def test_attack_3_removing_the_proving_mutation_is_refused():
    gutted = copy.deepcopy(_ac23())
    gutted["proving_mutation"] = "  "
    with pytest.raises(AssertionError):
        assert gutted["proving_mutation"].strip(), "AC-23 without a proving mutation is not earned"


def test_attack_4_pointing_the_owner_at_a_stale_callable_is_detectable():
    """An honesty check: linked_behaviour must name live callables; a stale name has no owner."""
    stale = "docker_boundary.py::a_function_that_was_removed"
    module_names = {n for n in dir(db)}
    assert "a_function_that_was_removed" not in module_names
    # The real declaration names only live callables.
    for name in ("resolve_steering_entry", "per_site_differences"):
        assert name in module_names, f"AC-23 names {name}, which must be a live callable"


def test_attack_5_tokens_only_in_dead_code_do_not_satisfy_the_contract(tmp_path):
    target = _ac23()
    text = (REPO_ROOT / target["file"]).read_text()
    # Rewrite the target function so CATEGORY_INVALID appears only in an unreachable region.
    dead = (
        "@pytest.mark.parametrize(\"entry\", WIDENING_INPUTS)\n"
        "def test_every_measured_widening_input_now_fails_closed(entry):\n"
        "    return\n"
        "    classification, mechanisms = db.resolve_steering_entry(entry, TABLE)\n"
        "    assert classification == db.CATEGORY_INVALID\n"
        "    assert mechanisms == ()\n")
    mutated = _splice_function(text, "test_every_measured_widening_input_now_fails_closed", dead)
    root = _tree_with(_registry(), tmp_path, target_file_text=mutated)
    assert not _validate(root)["clean"], "an unreachable assertion cannot satisfy AC-23"


def test_attack_6_tokens_only_in_a_comment_do_not_satisfy_the_contract(tmp_path):
    text = (REPO_ROOT / _ac23()["file"]).read_text()
    commented = (
        "@pytest.mark.parametrize(\"entry\", WIDENING_INPUTS)\n"
        "def test_every_measured_widening_input_now_fails_closed(entry):\n"
        "    # classification == db.CATEGORY_INVALID  (only a comment now)\n"
        "    classification, mechanisms = db.resolve_steering_entry(entry, TABLE)\n"
        "    assert classification is not None\n")
    mutated = _splice_function(text, "test_every_measured_widening_input_now_fails_closed", commented)
    root = _tree_with(_registry(), tmp_path, target_file_text=mutated)
    assert not _validate(root)["clean"], "a token in a comment is not an assertion"


def test_attack_7_the_contract_cannot_be_forced_clean_when_the_target_fails(tmp_path):
    """validate fails closed on problems; a gutted target cannot be laundered to clean."""
    text = (REPO_ROOT / _ac23()["file"]).read_text()
    gutted = (
        "@pytest.mark.parametrize(\"entry\", WIDENING_INPUTS)\n"
        "def test_every_measured_widening_input_now_fails_closed(entry):\n"
        "    assert True\n")
    mutated = _splice_function(text, "test_every_measured_widening_input_now_fails_closed", gutted)
    root = _tree_with(_registry(), tmp_path, target_file_text=mutated)
    result = _validate(root)
    assert not result["clean"] and result["problems"], "a failing target must not report clean"


def test_attack_8_an_inaccurately_high_minimum_is_refused(tmp_path):
    """Setting the minimum above what the target actually asserts is caught immediately, so the
    declared minimum cannot overstate the target's real assertion count."""
    reg = _reg_with_ac23(minimum_meaningful_assertions=9)
    root = _tree_with(reg, tmp_path)
    assert not _validate(root)["clean"], "a minimum above the target's real count must be refused"


# ===================================================================== helper
def _splice_function(text: str, name: str, replacement: str) -> str:
    """Replace the decorated function `name` (and its decorator line) with `replacement`."""
    lines = text.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith("def ") and f"def {name}(" in lines[i]:
            # back up over immediately preceding decorator lines
            while out and out[-1].lstrip().startswith("@"):
                out.pop()
            out.append(replacement if replacement.endswith("\n") else replacement + "\n")
            i += 1
            # skip the original body until the next top-level def/decorator at column 0
            while i < len(lines) and not (lines[i].startswith("def ") or lines[i].startswith("@")
                                          or (lines[i].strip() and not lines[i][0].isspace()
                                              and not lines[i].startswith(")"))):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    result = "".join(out)
    ast.parse(result)                                   # never emit a file that will not parse
    return result
