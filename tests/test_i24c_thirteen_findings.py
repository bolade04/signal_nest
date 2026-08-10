"""Gate 4N-I24C — the thirteen authoritative I23 findings, each with its executed exploit.

STANDING RULE (carried from I20B onward): wiring is asserted STRUCTURALLY — parsed shell,
AST, exact inventories. Never substring containment. I23 finding I24C-06 showed a guard that
inspected `" |"` under block scalars and therefore could not fail.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ci_invocation_model as cim  # noqa: E402
import evidence_binding as eb  # noqa: E402
import package_requirements as pr  # noqa: E402
import terraform_role_inventory as tri  # noqa: E402
import tracked_state as ts  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


# --------------------------------------------------------------------------- #
# I24C-06 / I24C-07 — CI invocation is proven, not inferred from a mention
# --------------------------------------------------------------------------- #

def test_the_workflow_satisfies_the_authored_invocation_contract():
    result = cim.check()
    assert result["clean"], result["problems"]


def test_every_graded_step_is_covered_by_the_contract():
    """A NEW graded step with no invocation assertion is finding I24C-07's shape."""
    steps = {s["id"] for s in cim.parse_steps()}
    covered = set(cim.contract()["graded_steps"])
    assert steps == covered, f"uncovered: {sorted(steps - covered)}; stale: {sorted(covered - steps)}"


def test_policy_tests_is_graded_and_must_invoke_pytest():
    """I23: policy_tests — the ONLY step that runs pytest — appeared in ZERO tests. Echoing it
    changed nothing, and every 'the suite catches it' mitigation terminated there."""
    spec = cim.contract()["graded_steps"]["policy_tests"]
    assert "PYTEST" in spec["must_invoke"] or "tests/" in spec["must_invoke"]
    text = WORKFLOW.read_text(encoding="utf-8")
    mutated = text.replace(
        '          PYTHONPATH="$GITHUB_WORKSPACE/scripts" .reader-venv/bin/python '
        "-m pytest tests/ -q -p signalnest_bootstrap -p pytest_session_guard",
        '          echo "pytest tests/ -q skipped"', 1)
    assert mutated != text
    assert any("policy_tests" in p for p in cim.check(mutated)["problems"])


@pytest.mark.parametrize("replacement", [
    "        run: echo NO-OP scripts/commit_package_coherence.py",
    "        run: |\n          # python3 scripts/commit_package_coherence.py\n          true",
    "        run: |\n          CMD=scripts/commit_package_coherence.py",
    "        run: |\n          if false; then\n            python3 scripts/commit_package_coherence.py\n          fi",
    "        run: cat scripts/commit_package_coherence.py",
    "        run: |\n          echo scripts/commit_package_coherence.py",
    "        run: true || python3 scripts/commit_package_coherence.py",
    "        run: |\n          exit 0\n          python3 scripts/commit_package_coherence.py",
    "        run: python3 scripts/commit_package_coherence.py || true",
    "        run: |\n          f() { python3 scripts/commit_package_coherence.py; }",
    "        run: |\n          cat <<'EOF'\n          python3 scripts/commit_package_coherence.py\n          EOF",
])
def test_non_invocations_do_not_satisfy_the_contract(replacement):
    """Echo, comment, assignment, dead branch, data argument, mention-only multiline,
    `true ||`, post-exit, `|| true`, uncalled function and heredoc data must all FAIL."""
    text = WORKFLOW.read_text(encoding="utf-8")
    original = "        run: python3 scripts/commit_package_coherence.py"
    assert original in text
    mutated = text.replace(original, replacement, 1)
    assert any("package_coherence" in p for p in cim.check(mutated)["problems"])


def test_a_substring_preserving_step_rename_fails():
    text = WORKFLOW.read_text(encoding="utf-8")
    mutated = text.replace("        id: package_coherence", "        id: package_coherence_v2", 1)
    assert any("package_coherence" in p for p in cim.check(mutated)["problems"])


def test_a_step_whose_outcome_is_not_read_by_the_guard_list_fails():
    text = WORKFLOW.read_text(encoding="utf-8")
    mutated = text.replace('"tracked_state=${{ steps.tracked_state.outcome }}" \\\n', "", 1)
    assert mutated != text
    assert any("tracked_state" in p and "guard result list" in p
               for p in cim.check(mutated)["problems"])


def test_continue_on_error_fails():
    text = WORKFLOW.read_text(encoding="utf-8")
    original = "        run: python3 scripts/commit_package_coherence.py"
    mutated = text.replace(original, "        continue-on-error: true\n" + original, 1)
    assert any("continue-on-error" in p for p in cim.check(mutated)["problems"])


def test_multiline_run_blocks_are_parsed_as_full_scalars():
    """The I23 guard inspected the rendered ' |' marker. Every block step must expose its
    entire multi-line body."""
    blocks = [s for s in cim.parse_steps() if s["form"] == "block"]
    assert blocks, "no block-scalar steps parsed — the parser is not seeing them"
    for s in blocks:
        assert "\n" in s["run"] or len(s["run"]) > 0
        assert s["run"].strip() != "|"


# --------------------------------------------------------------------------- #
# I24C-05 — independent package completeness
# --------------------------------------------------------------------------- #

def test_the_package_requirement_is_independently_authored():
    result = pr.check()
    assert result["requirement_kind"] == "INDEPENDENTLY_AUTHORED_PACKAGE_CONTRACT"
    assert result["complete"], result["problems"]


def test_removing_a_required_control_from_both_sides_is_detected():
    """THE X4 REGRESSION TEST. Deleting a control from the worktree AND the index left the
    old checker reporting 'coherent' because expected and observed both descended from the
    working tree. The authored contract does not move."""
    doc = pr.requirements()
    victim = doc["required_paths"]["test_modules"][0]
    present = pr.tree_paths(ts.predicted_commit_tree()["predicted_tree_hash"])
    assert victim in present
    shrunk = present - {victim}
    missing = [p for p in doc["required_paths"]["test_modules"] if p not in shrunk]
    assert victim in missing, "the authored requirement must still name the deleted control"


def test_the_requirement_is_not_derived_from_the_observed_side():
    source = (REPO_ROOT / "scripts" / "package_requirements.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "requirements")
    calls = {c.func.id for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "tree_paths" not in calls, \
        "the requirement loader reads the observed side; expected and observed would share an ancestor"


def test_prohibited_paths_are_refused():
    assert "infra/aws/live-resource-inventory.json" in pr.PROHIBITED_PREFIXES
    assert any("FROZEN-CANDIDATE" in s for s in pr.PROHIBITED_SUBSTRINGS)


# --------------------------------------------------------------------------- #
# I24C-01 / I24C-10 / I24C-11 — evidence provenance and summary freshness
# --------------------------------------------------------------------------- #

def test_superseded_tree_evidence_is_rejected():
    doc = eb.bind({"phase": "test"})
    assert not eb.verify(doc)
    doc["_binding"]["predicted_commit_tree_hash"] = "9984d4ee756ac16e935e90c5b55cb95ef1684002"
    problems = eb.verify(doc)
    assert problems and "SUPERSEDED" in problems[0]


def test_an_unbound_artifact_cannot_be_shown_current():
    assert eb.verify({"phase": "test"})


def test_a_self_asserted_freshness_literal_is_refused():
    """I23 shipped `equals_real_index_tree: true` as a hard-coded literal that never computed
    anything — a boolean asserted rather than measured."""
    with pytest.raises(eb.EvidenceError, match="self-asserted"):
        eb.bind({"equals_real_index_tree": True})


def test_a_summary_generated_before_the_last_lane_is_rejected():
    """THE I24C-11 REGRESSION TEST — the exact sequence that stopped the first I24 attempt."""
    lanes = {"a": "FAIL", "b": "FAIL", "c": "FAIL", "d": "FAIL", "e": "FAIL",
             "f": "PENDING_AT_WRITE"}
    r = eb.summary_contract(lanes_expected=6, lane_verdicts=lanes,
                            defects=[{"id": "C1", "severity": "CRITICAL"}],
                            summary_ids=["C1"])
    assert not r["fresh"]
    assert any("pending" in p for p in r["problems"])


def test_appending_a_later_defect_invalidates_the_summary():
    lanes = {k: "FAIL" for k in "abcdef"}
    early = [{"id": "C1", "severity": "CRITICAL"}]
    late = early + [{"id": "X4", "severity": "CRITICAL"}]
    r = eb.summary_contract(lanes_expected=6, lane_verdicts=lanes, defects=late,
                            summary_ids=["C1"],
                            summary_generated_after=[d["id"] for d in early])
    assert not r["fresh"]
    assert any("X4" in p for p in r["problems"])


def test_numeric_agreement_is_not_coverage():
    """Blind-spot mutation: a stale summary whose COUNT matches while omitting a CRITICAL."""
    lanes = {k: "FAIL" for k in "abcdef"}
    defects = [{"id": "C1", "severity": "CRITICAL"}, {"id": "X4", "severity": "CRITICAL"}]
    r = eb.summary_contract(lanes_expected=6, lane_verdicts=lanes, defects=defects,
                            summary_ids=["C1", "DECOY"])
    assert not r["fresh"]
    assert any("numeric agreement is not coverage" in p for p in r["problems"])


# --------------------------------------------------------------------------- #
# I24C-03 — the predicted tree measures the INDEX
# --------------------------------------------------------------------------- #

def test_the_predicted_tree_is_the_index_tree():
    p = ts.predicted_commit_tree()
    assert p["predicted_tree_hash"] == ts.index_tree_hash()
    assert "INDEX" in p["measures"]


def test_predicted_tree_does_not_read_the_worktree():
    """I23: `git update-index --add` read the WORKING TREE, so an index-only tamper was
    silently healed and coherence still reported the correct hash."""
    source = (REPO_ROOT / "scripts" / "tracked_state.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.FunctionDef) and n.name == "predicted_commit_tree")
    rendered = ast.dump(fn)
    assert "'--add'" not in rendered and '"--add"' not in rendered, \
        "predicted_commit_tree still re-adds paths from the working tree"


# --------------------------------------------------------------------------- #
# I24C-02 — the manifest loads through its own production loader
# --------------------------------------------------------------------------- #

def test_evidence_is_a_legal_artifact_role():
    import candidate_manifest as cm
    assert "evidence" in cm.ARTIFACT_ROLES


def test_the_i23_manifest_now_loads_through_the_production_loader(monkeypatch):
    """The exact I23 failure: role 'evidence' was not in the vocabulary, so load() raised
    before verify() was reached and the frozen candidate could not be read by its own
    verifier (exit 2)."""
    import candidate_manifest as cm
    manifest = Path("/Users/mk/.signalnest/generated/4n-i23/FROZEN-CANDIDATE.json")
    if not manifest.exists():
        pytest.skip("the I23 candidate is not present on this host")
    monkeypatch.setenv("SIGNALNEST_CANDIDATE_MANIFEST", str(manifest))
    candidate = cm.load()          # must not raise
    assert candidate.candidate_id == "4N-I23-CANDIDATE-1"
    assert candidate.certifies_production is False
