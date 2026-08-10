"""Gate 4N-I28I, root cause RC-3 — security relevance decided by invocation, not by name.

Gate 4N-I28G finding ADV-03. `tests/fixtures/critical-list-contract.json` states its rule plainly:

    SECURITY_CRITICAL when a module-level constant in a guard script names a SCOPE the guard
    enforces (allowed / required / expected / denied / permitted / covered).

That is a rule about the NAME, and in `scripts/leak_scan.py` it inverted two gradings at once:
`SKIP_DIRS` — which `candidate_files()` uses to decide what is scanned at all — was graded
NON_SECURITY_CONFIGURATION with the reason "not an enforced scope", while `SCAN_SUFFIXES`, which
nothing references, was graded SECURITY_CRITICAL_LIST because its name reads like a scope.
`EXCLUDED_PATH_PARTS` was in neither list.

That misclassification is why nobody pinned `SKIP_DIRS`, and why one line added to it could make
80 files vanish from the scan with a planted identifier inside them. RC-3 is the architectural
parent of RC-2.

The expectations below are derived from the CALL GRAPH by `scripts/enforcement_path.py` and
compared against the contract. Nothing here consults a constant's name to decide what it is.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import enforcement_path as ep          # noqa: E402
import critical_list_inventory as cli  # noqa: E402

CONTRACT = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "critical-list-contract.json").read_text(encoding="utf-8"))
CLASSIFICATIONS = CONTRACT["classifications"]

INVENTORY = ep.enforcement_inventory("leak_scan.py")


# =====================================================================================
# The derivation itself.
# =====================================================================================

def test_the_entry_points_are_reachable_and_the_walk_completes():
    reach = INVENTORY["reachability"]
    assert reach["entry_points"], "no entry point found; reachability would be vacuous"
    assert reach["analysis_complete"], (
        f"unresolved calls to functions defined in this module: {reach['unresolved_calls']}; "
        "an incomplete walk must not be reported as a clean inventory")


def test_the_scan_gates_are_actually_reachable():
    """If the gating functions were unreachable the whole inventory would be meaningless."""
    reachable = set(INVENTORY["reachability"]["reachable"])
    for gate in ("candidate_files", "is_scannable", "scan_text"):
        assert gate in reachable, f"{gate} is not reachable from an entry point"


# =====================================================================================
# The property: classification must follow enforcement, in both directions.
# =====================================================================================

LOAD_BEARING_SCAN_CONSTANTS = ["leak_scan.py::SKIP_DIRS", "leak_scan.py::EXCLUDED_PATH_PARTS"]


@pytest.mark.parametrize("control", LOAD_BEARING_SCAN_CONSTANTS)
def test_a_constant_that_gates_the_scan_is_security_critical(control):
    """THE ADV-03 DEFECT, forward direction: SKIP_DIRS decides what is examined."""
    record = INVENTORY["controls"][control]
    assert record["load_bearing"], f"{control} is no longer on a gating path — re-derive"
    assert CLASSIFICATIONS.get(control) == cli.SECURITY_CRITICAL, (
        f"{control} is referenced by {record['gating_callers']}, which decide what the scanner "
        f"examines, but the contract grades it {CLASSIFICATIONS.get(control)!r}")


def test_a_dead_constant_is_not_graded_security_critical():
    """THE ADV-03 DEFECT, reverse direction: SCAN_SUFFIXES is referenced by nothing."""
    for control, record in INVENTORY["controls"].items():
        if record["dead"]:
            assert CLASSIFICATIONS.get(control) != cli.SECURITY_CRITICAL, (
                f"{control} is referenced by no function at all, yet is graded "
                "SECURITY_CRITICAL — that is a grade earned by its name, not its effect")


#: The contract covers COLLECTION constants. Regexes, paths and integers are load-bearing too but
#: are outside its domain, so the assertion below is scoped to what the contract is for — widening
#: it to every upper-case name would report a gap the contract never claimed to close.
COLLECTION_IDS = {c["id"] for c in cli.discover_collections()}


def test_every_gating_collection_appears_in_the_contract_at_all():
    """`EXCLUDED_PATH_PARTS` was in neither list — absence is its own failure mode."""
    missing = [c for c, r in INVENTORY["controls"].items()
               if r["load_bearing"] and r["gating_callers"]
               and c in COLLECTION_IDS and c not in CLASSIFICATIONS]
    assert not missing, f"gating collections absent from the contract: {missing}"


def test_classification_is_not_predicted_by_the_name_alone():
    """The guard on the guard: if name shape still predicted the grading perfectly, nothing
    would have changed. SKIP_DIRS reads like configuration and is security-critical;
    SCAN_SUFFIXES reads like an enforced scope and is not."""
    skip = CLASSIFICATIONS.get("leak_scan.py::SKIP_DIRS")
    suffixes = CLASSIFICATIONS.get("leak_scan.py::SCAN_SUFFIXES")
    assert skip == cli.SECURITY_CRITICAL, skip
    assert suffixes != cli.SECURITY_CRITICAL, suffixes


def test_a_renamed_enforcing_helper_keeps_its_classification(tmp_path):
    """A newly introduced or renamed helper must be classified by what it DOES.

    The inventory is derived per run from the call graph, so a constant that starts gating the
    scan becomes load-bearing without anyone editing a list of names.
    """
    module = tmp_path / "guard_probe.py"
    module.write_text(
        "FILTER = {'a'}\n"
        "UNUSED = {'b'}\n"
        "def _renamed_helper(p):\n"
        "    return p not in FILTER\n"
        "def check():\n"
        "    return _renamed_helper('x')\n"
        "def main():\n"
        "    return check()\n", encoding="utf-8")
    original = ep.SCRIPTS
    try:
        ep.SCRIPTS = tmp_path
        ep.SCAN_DOMAIN_GATES["guard_probe.py"] = ("_renamed_helper",)
        inv = ep.enforcement_inventory("guard_probe.py")
    finally:
        ep.SCRIPTS = original
        ep.SCAN_DOMAIN_GATES.pop("guard_probe.py", None)
    assert inv["controls"]["guard_probe.py::FILTER"]["load_bearing"], (
        "a constant reached through a renamed helper must still be load-bearing")
    assert inv["controls"]["guard_probe.py::UNUSED"]["dead"], (
        "a constant nothing references must be reported dead")
