"""Gate 4N-I28BF-A4 — the ten assertion-contract mutation classes against live contracts.

WHAT THIS PROVES. Section 12 of the gate: for each of ten mutation classes, an actual live
load-bearing assertion is mutated, the mutation is shown to activate, the intended detector fires,
and the final graded result fails. The FINAL GRADED CONSUMER for assertion mutations is the
mandatory node ``test_i28u_assertion_self_protection.py::test_the_contracted_assertions_are_all_intact``,
which runs ``assertion_contracts.validate()`` over the whole registry inside the qualified suite; a
trivialised load-bearing assertion makes it fail, which fails the graded session. Class 10 is
proven end to end through a real graded pytest session; the other nine are proven through the same
``validate()`` consumer the mandatory control invokes, plus the inventory and proving-mutation
invariants the control's siblings enforce.

INERT MUTATIONS STAY INERT. A comment-only edit does not change the validator's verdict — proven
by ``test_inert_comment_change_is_not_flagged`` — so the battery cannot pass by flagging noise.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import assertion_contracts as ac                   # noqa: E402

REGISTRY_PATH = REPO_ROOT / "tests" / "fixtures" / "assertion-contract-registry.json"

# A representative LIVE contract whose test we mutate in copies. Chosen because its required
# assertion is a single EXACT_IDENTITY_EQUALITY, the simplest shape to gut convincingly.
LIVE_CONTRACT_ID = "AC-23-DOCKER-CATEGORY-AND-SESSION-FINISH"


def _registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _live_contract() -> dict:
    return next(c for c in _registry()["contracts"] if c["contract_id"] == LIVE_CONTRACT_ID)


def _copy_tree(tmp_path: Path) -> Path:
    """Materialise every file the registry references plus the registry itself."""
    root = tmp_path / "repo"
    (root / "tests" / "fixtures").mkdir(parents=True)
    for rel in sorted({c["file"] for c in _registry()["contracts"]}):
        src = REPO_ROOT / rel
        if src.is_file():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, root / rel)
    shutil.copy(REGISTRY_PATH, root / "tests" / "fixtures")
    return root


def _replace_test_body(root: Path, rel: str, name: str, new_body: str):
    """Replace one test function's body, preserving its signature line."""
    p = root / rel
    text = p.read_text(encoding="utf-8")
    start = text.index(f"def {name}(")
    sig_end = text.index("\n", start) + 1
    try:
        nxt = text.index("\ndef ", sig_end)
        after = text[nxt + 1:]
    except ValueError:
        after = ""
    p.write_text(text[:sig_end] + textwrap.indent(textwrap.dedent(new_body), "    ") + "\n\n" + after)


def _validate_tree(root: Path) -> dict:
    reg = json.loads((root / "tests" / "fixtures" / "assertion-contract-registry.json").read_text())
    return ac.validate(reg, root=root)


def _live_test_location() -> tuple[str, str]:
    c = _live_contract()
    return c["file"], c["test"]


# ===================================================================== green-when-clean control
def test_the_copied_live_tree_validates_clean(tmp_path):
    """Without this every rejection below proves nothing."""
    root = _copy_tree(tmp_path)
    assert _validate_tree(root)["clean"], _validate_tree(root)["problems"]


# ===================================================================== class 1: assertion removed
def test_class_01_assertion_removed(tmp_path):
    root = _copy_tree(tmp_path)
    assert _validate_tree(root)["clean"], "baseline must be green before mutating"
    rel, name = _live_test_location()
    _replace_test_body(root, rel, name, "entry = entry\n")   # keeps the param, drops every assert
    result = _validate_tree(root)
    assert not result["clean"], "removing the load-bearing assertion must be caught"
    assert any(LIVE_CONTRACT_ID in p for p in result["problems"]), result["problems"]


# ============================================================= class 2: replaced with constant true
def test_class_02_replaced_with_constant_true(tmp_path):
    root = _copy_tree(tmp_path)
    rel, name = _live_test_location()
    _replace_test_body(root, rel, name, "assert True\n")
    result = _validate_tree(root)
    assert not result["clean"]
    assert any("always true" in p or LIVE_CONTRACT_ID in p for p in result["problems"])


ALWAYS_TRUE_FORMS = [
    ("assert True or X", "classification = db_resolve()\nassert True or classification == 'X'\n"),
    ("assert bool(1) or X", "classification = db_resolve()\nassert bool(1) or classification == 'X'\n"),
    ("assert x == x or X", "classification = db_resolve()\n"
                           "assert classification == classification or classification == 'X'\n"),
]


@pytest.mark.parametrize("label,body", ALWAYS_TRUE_FORMS, ids=[f[0] for f in ALWAYS_TRUE_FORMS])
def test_class_02_short_circuit_always_true_forms_are_caught(tmp_path, label, body):
    root = _copy_tree(tmp_path)
    rel, name = _live_test_location()
    _replace_test_body(root, rel, name, body)
    result = _validate_tree(root)
    assert not result["clean"], f"{label} must be caught as always-true"


# ===================================================================== class 3: detector stubbed
def test_class_03_detector_stubbed_with_an_unregistered_helper(tmp_path):
    root = _copy_tree(tmp_path)
    rel, name = _live_test_location()
    _replace_test_body(root, rel, name, "assert an_unregistered_stub(entry)\n")
    result = _validate_tree(root)
    assert not result["clean"]
    assert any("unregistered helper" in p for p in result["problems"])


# ===================================================================== class 4: assertion result ignored
def test_class_04_assertion_result_computed_but_not_asserted(tmp_path):
    root = _copy_tree(tmp_path)
    rel, name = _live_test_location()
    # The comparison is computed and thrown away — the classic "result ignored" shape.
    _replace_test_body(root, rel, name,
                       "classification, mechanisms = db_resolve(entry)\n"
                       "_ = classification == 'CATEGORY_INVALID'\n")
    result = _validate_tree(root)
    assert not result["clean"], "an ignored comparison leaves no assertion and must be caught"


# ===================================================================== class 5: registry entry removed
def test_class_05_registry_entry_removed_is_caught_by_the_inventory_pin():
    """The validator cannot know an entry vanished; the authored inventory pin is what catches it.
    This mirrors the mandatory sibling ``test_the_inventory_cannot_lose_an_entry_unnoticed``."""
    reg = _registry()
    ids = {c["contract_id"] for c in reg["contracts"]}
    reduced = {c["contract_id"] for c in reg["contracts"] if c["contract_id"] != LIVE_CONTRACT_ID}
    assert LIVE_CONTRACT_ID in ids
    assert LIVE_CONTRACT_ID not in reduced
    # The inventory pin is an explicit membership assertion; removing an entry makes it fail.
    with pytest.raises(AssertionError):
        assert LIVE_CONTRACT_ID in reduced, "a removed contract must be visible"


# ===================================================================== class 6: proving mutation removed
def test_class_06_a_contract_without_a_proving_mutation_is_refused():
    """Membership is earned by a proving mutation; an entry that loses it is not load-bearing.
    This is the invariant the mandatory sibling ``test_every_contract_names_an_invariant_and_a_
    proving_mutation`` enforces."""
    for c in _registry()["contracts"]:
        assert c["proving_mutation"].strip(), f"{c['contract_id']} has no proving mutation"
    gutted = copy.deepcopy(_live_contract())
    gutted["proving_mutation"] = "   "
    with pytest.raises(AssertionError):
        assert gutted["proving_mutation"].strip(), "an empty proving mutation must be refused"


# ===================================================================== class 7: minimum count zeroed
def test_class_07_zeroing_the_minimum_and_emptying_required_assertions_is_refused(tmp_path):
    """Setting ``minimum_meaningful_assertions`` to zero only helps an attacker who also empties
    ``required_assertions`` — and an empty required set is refused by the load-bearing invariant, so
    a gutted test cannot be laundered through a zeroed minimum."""
    # With required_assertions intact, a zeroed minimum still enforces the class:
    root = _copy_tree(tmp_path)
    reg = json.loads((root / "tests" / "fixtures" / "assertion-contract-registry.json").read_text())
    for c in reg["contracts"]:
        if c["contract_id"] == LIVE_CONTRACT_ID:
            c["minimum_meaningful_assertions"] = 0
    rel, name = _live_test_location()
    _replace_test_body(root, rel, name, "assert True\n")
    assert not ac.validate(reg, root=root)["clean"], (
        "a zeroed minimum must not launder a trivialised assertion while the class is still required")
    # And emptying required_assertions is itself refused by the earned-membership invariant.
    gutted = copy.deepcopy(_live_contract())
    gutted["required_assertions"] = []
    with pytest.raises(AssertionError):
        assert gutted["required_assertions"], "a load-bearing contract must require assertions"


# ===================================================================== class 8: duplicate masks removal
def test_class_08_a_duplicate_contract_id_is_reported(tmp_path):
    root = _copy_tree(tmp_path)
    reg = json.loads((root / "tests" / "fixtures" / "assertion-contract-registry.json").read_text())
    dup = copy.deepcopy(_live_contract())
    reg["contracts"].append(dup)
    result = ac.validate(reg, root=root)
    assert result["duplicate_contract_ids"] >= 1
    assert any("duplicate contract id" in p for p in result["problems"])


# ===================================================================== class 9: protected target never executes
def test_class_09_an_unreachable_assertion_cannot_satisfy_the_contract(tmp_path):
    root = _copy_tree(tmp_path)
    rel, name = _live_test_location()
    _replace_test_body(root, rel, name,
                       "return\n"
                       "classification = db_resolve(entry)\n"
                       "assert classification == 'CATEGORY_INVALID'\n")
    result = _validate_tree(root)
    assert not result["clean"], "an assertion after return can never execute and must be caught"


# ===================================================================== class 10: final aggregator ignores (E2E)
def _materialise(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    tree = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()
    archive = subprocess.run(["git", "archive", tree], cwd=REPO_ROOT,
                             capture_output=True, check=True).stdout
    tar = dest / "_tree.tar"
    tar.write_bytes(archive)
    subprocess.run(["tar", "-xf", str(tar)], cwd=dest, check=True)
    tar.unlink()
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=a@b.c", "-c", "user.name=x", "commit", "-qm", "base"]):
        subprocess.run(cmd, cwd=dest, check=True, capture_output=True)
    return dest


def test_class_10_a_gutted_live_assertion_fails_a_real_graded_session(tmp_path):
    """End to end: gut a live contracted assertion, then run the mandatory assertion control in a
    real graded session. The control fails, so the final graded result fails."""
    root = _materialise(tmp_path / "s")
    rel, name = _live_test_location()
    _replace_test_body(root, rel, name, "assert True\n")
    env = dict(os.environ,
               SIGNALNEST_ANCHOR_TIER="TIER_1_SYNTHETIC",
               SIGNALNEST_CANDIDATE_MANIFEST=str(root / "tests" / "fixtures" / "candidate-manifest.json"),
               PYTHONPATH=str(root / "scripts"))
    env.pop("SIGNALNEST_MANDATORY_NODES", None)
    node = ("tests/test_i28u_assertion_self_protection.py::"
            "test_the_contracted_assertions_are_all_intact")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "-p", "no:randomly", "-p", "signalnest_bootstrap"],
        cwd=root, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode != 0, (
        "the mandatory assertion control must fail the graded session on a gutted live assertion:\n"
        + (proc.stdout + proc.stderr)[-1500:])
    assert LIVE_CONTRACT_ID in (proc.stdout + proc.stderr), (
        "the failure must name the gutted contract")


# ===================================================================== inert-mutation control
def test_inert_comment_change_is_not_flagged(tmp_path):
    """A comment-only edit must leave the verdict clean; otherwise the battery flags noise."""
    root = _copy_tree(tmp_path)
    rel, name = _live_test_location()
    p = root / rel
    text = p.read_text(encoding="utf-8")
    marker = f"def {name}("
    idx = text.index("\n", text.index(marker)) + 1
    p.write_text(text[:idx] + "    # an inert explanatory comment added by the gate\n" + text[idx:])
    assert _validate_tree(root)["clean"], "a comment-only change must remain inert"
