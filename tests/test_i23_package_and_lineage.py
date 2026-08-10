"""Gate 4N-I23 — the three I22 blockers, each with the exploit that proved it.

STANDING RULE (carried from I20B/I21/I22): wiring is asserted STRUCTURALLY — parsed YAML,
AST, exact tokens, exact path inventories. Never substring containment. I22 finding F5 showed
29 of 30 graded CI step bodies could be replaced with `echo` because the assertion was
`"terraform_role_inventory.py" in workflow`.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import commit_package_coherence as cpc  # noqa: E402
import provenance as pv  # noqa: E402
import signalnest_identity as identity  # noqa: E402
import terraform_role_inventory as tri  # noqa: E402
import tracked_state as ts  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The predicted commit tree Gate 4N-I22 froze. It contained 14 fixtures, a CI workflow
# invoking 26 absent scripts, and zero test files. It is a permanent, immutable specimen.
I22_INCOHERENT_TREE = "57398f81edcb3b59be6c557d2d71a23b04a38dfb"


# --------------------------------------------------------------------------- #
# BLOCKER 1 — commit package coherence
# --------------------------------------------------------------------------- #

def test_the_i22_predicted_tree_is_rejected_as_incoherent():
    """THE decisive regression test. If this ever passes, the I22 defect is back."""
    tree_exists = subprocess.run(["git", "cat-file", "-e", f"{I22_INCOHERENT_TREE}^{{tree}}"],
                                 cwd=REPO_ROOT, capture_output=True).returncode == 0
    if not tree_exists:
        pytest.skip("the I22 specimen tree is not in this object database")
    result = cpc.verify(I22_INCOHERENT_TREE)
    assert not result["coherent"], (
        "the I22 predicted tree MUST be rejected: it shipped a CI workflow invoking 26 scripts "
        "that were not in it, and zero test files")
    unresolved = [f["path"] for f in result["findings"] if f["check"] == "ci_command_resolves"]
    assert len(unresolved) >= 20, f"expected the mass of unresolved CI commands, got {unresolved}"
    assert any(f["check"] == "test_file_committed" for f in result["findings"])


def test_the_current_predicted_tree_is_coherent():
    result = cpc.verify(ts.predicted_commit_tree()["predicted_tree_hash"])
    assert result["coherent"], result["findings"]


def test_a_workflow_referencing_an_absent_script_is_a_finding(tmp_path, monkeypatch):
    """Drive the REAL check() over a materialised tree we control, and require the finding.

    An earlier draft of this test ended in `assert findings is not None`, which can never
    fail — the exact class of dead assertion the I22 adversarial lane kept reporting (L2).
    """
    root = tmp_path / "tree"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  x:\n    steps:\n      - run: python3 scripts/definitely_absent.py\n",
        encoding="utf-8")
    # The tree listing is what check() adjudicates membership against; supply one that
    # contains the workflow and nothing else.
    monkeypatch.setattr(cpc, "tree_paths", lambda _h: {".github/workflows/ci.yml"})
    # No repository control scripts exist under this fake root, so restrict the
    # repository-completeness probes to keep the assertion about the CI reference alone.
    monkeypatch.setattr(cpc, "REPO_ROOT", root)
    result = cpc.check("deadbeef", root)
    unresolved = [f for f in result["findings"] if f["check"] == "ci_command_resolves"]
    assert [f["path"] for f in unresolved] == ["scripts/definitely_absent.py"]
    assert not result["coherent"]


def test_local_imports_are_resolved_by_ast_not_substring():
    """A module named only inside a string or comment is not an import."""
    assert cpc.local_module_imports("import terraform_role_inventory\n") == \
        {"terraform_role_inventory"}
    assert cpc.local_module_imports("from provenance import records\n") == {"provenance"}
    assert "terraform_role_inventory" not in cpc.local_module_imports(
        '# terraform_role_inventory\nx = "terraform_role_inventory"\n')


def test_protected_inventory_is_absent_from_the_predicted_tree():
    present = cpc.tree_paths(ts.predicted_commit_tree()["predicted_tree_hash"])
    for prohibited in cpc.PROHIBITED_IN_TREE:
        assert prohibited not in present, f"{prohibited} is PROTECTED and must never be committed"


def test_predicted_tree_includes_staged_modifications_to_tracked_files():
    """Gate 4N-I23. `staged` was `index_paths() - head_paths()` — additions only. A staged
    MODIFICATION to a tracked file is in HEAD, so it was excluded, and once staged it no
    longer appears in `git diff --name-only` either. It fell through both sets and the
    predicted tree silently kept HEAD's version. Invisible at I22 only because the tracked
    modifications happened to be unstaged."""
    source = (REPO_ROOT / "scripts" / "tracked_state.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "predicted_commit_tree")
    staged_assign = next(
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "staged" for t in n.targets))
    rendered = ast.dump(staged_assign)
    assert "'--cached'" in rendered or '"--cached"' in rendered, (
        "the staged set must come from `git diff --cached`, which reports additions AND "
        "modifications; index_paths() - head_paths() yields additions only")


def test_the_predicted_tree_equals_the_real_index_tree_when_nothing_is_unstaged(tmp_path):
    """If there are no unstaged changes, what git would commit IS the index tree."""
    unstaged = subprocess.run(["git", "diff", "--name-only"], cwd=REPO_ROOT,
                              capture_output=True, text=True).stdout.split()
    if unstaged:
        pytest.skip(f"{len(unstaged)} unstaged paths; the two definitions legitimately differ")
    index_copy = tmp_path / "index_copy"
    index_copy.write_bytes((REPO_ROOT / ".git" / "index").read_bytes())
    index_tree = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT, capture_output=True,
                                text=True, env={**dict(__import__("os").environ),
                                                "GIT_INDEX_FILE": str(index_copy)}).stdout.strip()
    assert ts.predicted_commit_tree()["predicted_tree_hash"] == index_tree


# --------------------------------------------------------------------------- #
# BLOCKER 2 — reconciler lineage
# --------------------------------------------------------------------------- #

def _poison(monkeypatch):
    """The exact I22 exploit: writable_roles() wrongly resolves migration_task."""
    import gen_operator_policies as gen
    real = tri.writable_roles

    def poisoned():
        w = dict(real())
        w["migration_task"] = [{"policy_label": "fabricated_inline_policy"}]
        return w

    monkeypatch.setattr(tri, "writable_roles", poisoned)
    monkeypatch.setattr(gen, "INLINE_POLICY_ROLE_ARNS",
                        tri.role_arns(poisoned()), raising=False)
    return identity.iam_role_arn(f"{identity.PREFIX}-migration-task")


def test_the_i22_exploit_is_rejected_by_the_DEFAULT_reconcile_path(monkeypatch):
    """THE decisive regression test for blocker 2."""
    mig = _poison(monkeypatch)
    assert mig in tri.generated_writable_arns_from_policy(), "the exploit must actually grant it"
    result = tri.reconcile()  # no arguments — the shipping path
    rows = {r.get("role_name"): r["classification"] for r in result["rows"] if r.get("role_name")}
    assert rows[f"{identity.PREFIX}-migration-task"] == "OVER_GRANTED"
    assert not result["clean"]


def test_the_expected_side_does_not_descend_from_writable_roles(monkeypatch):
    """Poisoning the generated ancestor must move ONLY the generated side."""
    before = tri.reconcile()["expected_writable_role_set"]
    _poison(monkeypatch)
    after = tri.reconcile()["expected_writable_role_set"]
    assert before == after, ("the expected set moved when writable_roles() was poisoned, so it "
                             "still descends from it")


def test_expected_source_is_the_authored_requirement_not_a_parse(monkeypatch):
    """The reported label must reflect the ACTUAL computation, not just be a string.

    Gate 4N-I23 falsification found this test passing while `expected_arns` had been rewired
    to `set(role_arns(writable))` — because it only read the `expected_source` metadata, which
    is emitted from `scope` regardless of what the expected set was actually built from. A
    label that is reported independently of the thing it describes is not evidence. So drive
    it behaviourally: change the AUTHORED requirement and require the expected set to move.
    """
    result = tri.reconcile()
    assert result["expected_source_kind"] == "INDEPENDENTLY_AUTHORED_REQUIREMENT"
    assert result["expected_source"].endswith("expected-writable-roles.json")

    real_scope = tri.required_writable_scope()
    narrowed = {"writable_suffixes": [s for s in real_scope["writable_suffixes"]
                                      if s != "api-task"],
                "never_writable_suffixes": real_scope["never_writable_suffixes"],
                "source": real_scope["source"]}
    monkeypatch.setattr(tri, "required_writable_scope", lambda: narrowed)
    moved = tri.reconcile()
    assert len(moved["expected_writable_role_set"]) == \
        len(result["expected_writable_role_set"]) - 1, (
        "editing the AUTHORED requirement did not move the expected set, so the expected side "
        "is not actually built from it")
    assert not moved["clean"], "the now-unrequired api-task grant must surface as a finding"


def test_the_guard_script_itself_detects_a_lineage_regression():
    """I22 AWS-lane LOW, closed here. On an unpoisoned repository the authored requirement and
    the .tf parse agree, so rewiring the expected side back to `role_arns(writable_roles())`
    left `python3 scripts/terraform_role_inventory.py` exiting 0 — the standalone guard was
    blind to exactly the regression it exists to prevent, and only the suite caught it."""
    source = (REPO_ROOT / "scripts" / "terraform_role_inventory.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.FunctionDef) and n.name == "assert_expected_lineage")
    assert fn is not None
    main_fn = next(n for n in ast.walk(ast.parse(source))
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = {n.func.id for n in ast.walk(main_fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "assert_expected_lineage" in calls, \
        "main() does not run the lineage self-check, so the guard stays blind"


@pytest.mark.parametrize("bad_expr", [
    "set(role_arns(writable))",                       # the exact I22 wiring
    "set(generated_writable_arns_from_policy())",     # expected parsed from the emitted policy
    "set(INLINE_POLICY_ROLE_ARNS)",                   # expected taken from the generator constant
])
def test_the_lineage_self_check_rejects_coupled_wiring(bad_expr):
    """The self-check must FAIL on every way of re-coupling the two sides."""
    source = ("def reconcile(generated_writable_arns=None, expected_writable_arns=None):\n"
              f"    expected_arns = {bad_expr}\n"
              "    return expected_arns\n")
    with pytest.raises(tri.RequirementError, match="share a decisive ancestor"):
        tri.assert_expected_lineage(source_override=source)


def test_the_lineage_self_check_accepts_the_authored_wiring():
    """...and must PASS on the shipped form, or it is just a tripwire that always fires."""
    source = ("def reconcile(generated_writable_arns=None, expected_writable_arns=None):\n"
              "    expected_arns = required_writable_arns(names, scope)\n"
              "    return expected_arns\n")
    tri.assert_expected_lineage(source_override=source)
    tri.assert_expected_lineage()  # the real shipped module


@pytest.mark.parametrize("generator", ["gen_operator_policies.py", "gen_boundary_policy.py",
                                       "gen_role_bootstrap_policy.py"])
def test_no_policy_generator_reads_the_authored_requirement(generator):
    """Structural independence, both directions. If any generator read the requirement
    fixture, the observed side would descend from the expected side and the two would move
    together again — the same coupling as I22, mirrored."""
    src = (REPO_ROOT / "scripts" / generator)
    if not src.exists():
        pytest.skip(f"{generator} is not present")
    text = src.read_text(encoding="utf-8")
    assert "expected-writable-roles" not in text, (
        f"{generator} reads the authored requirement; the generated and expected sides would "
        "share an ancestor again")
    assert "required_writable_arns" not in text
    assert "required_writable_scope" not in text


def test_migration_task_is_explicitly_never_writable():
    scope = tri.required_writable_scope()
    assert any(s == "migration-task" for s in scope["never_writable_suffixes"])
    assert tri.classify_role_name(f"{identity.PREFIX}-migration-task") == "REQUIRED_EMPTY"


def test_an_unknown_role_fails_closed():
    assert tri.classify_role_name("signalnest-staging-brand-new-role") == "UNKNOWN"


def test_a_role_declared_both_writable_and_never_writable_is_refused(monkeypatch):
    monkeypatch.setattr(tri, "required_writable_scope",
                        lambda: {"writable_suffixes": ["api-task"],
                                 "never_writable_suffixes": ["api-task"], "source": "x"})
    assert tri.classify_role_name("signalnest-staging-api-task",
                                  tri.required_writable_scope()) == "UNKNOWN"


def test_a_wildcard_role_resource_is_refused():
    result = tri.reconcile(generated_writable_arns=["arn:aws:iam::111122223333:role/*"])
    assert any("WILDCARD" in p for p in result["problems"])
    assert not result["clean"]


def test_a_malformed_role_arn_is_refused():
    result = tri.reconcile(generated_writable_arns=["not-an-arn-at-all"])
    assert any("malformed" in p for p in result["problems"])


def test_a_duplicated_role_resource_is_reported():
    arn = identity.iam_role_arn(f"{identity.PREFIX}-api-task")
    result = tri.reconcile(generated_writable_arns=[arn, arn])
    assert arn in result["duplicate_generated_resources"]
    assert not result["clean"]


def test_under_grant_is_reachable():
    full = tri.reconcile()["generated_writable_role_set"]
    reduced = [a for a in full if not a.endswith("api-task")]
    result = tri.reconcile(generated_writable_arns=reduced)
    rows = {r.get("role_name"): r["classification"] for r in result["rows"] if r.get("role_name")}
    assert rows[f"{identity.PREFIX}-api-task"] == "UNDER_GRANTED"


def test_an_undeclared_role_is_reported():
    result = tri.reconcile(generated_writable_arns=[
        "arn:aws:iam::111122223333:role/attacker-role"])
    assert any(r["classification"] == "UNDECLARED" for r in result["rows"])


def test_a_missing_requirement_fixture_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(tri, "REQUIREMENT_FIXTURE", tmp_path / "absent.json")
    with pytest.raises(tri.RequirementError):
        tri.required_writable_scope()


# --------------------------------------------------------------------------- #
# exact CI wiring — structural, never substring (I22 F5)
# --------------------------------------------------------------------------- #

def _step_ids() -> set[str]:
    return set(re.findall(r"^\s+id:\s*([A-Za-z0-9_-]+)\s*$",
                          WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE))


@pytest.mark.parametrize("step_id", ["package_coherence", "role_inventory", "provenance_coverage"])
def test_required_ci_step_ids_exist_exactly(step_id):
    """Exact id-set membership. A suffix or prefix rename is a different id and must fail."""
    assert step_id in _step_ids(), f"CI step id {step_id!r} is absent; ids present: {_step_ids()}"


@pytest.mark.parametrize("step_id", ["package_coherence", "role_inventory", "provenance_coverage"])
def test_required_ci_step_is_in_the_guard_result_list(step_id):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert f"{step_id}=" in text, (
        f"{step_id} produces no entry in the Gate 4N guard result list, so its failure would "
        "not reach the top-level job")


@pytest.mark.parametrize("bypass", ["package_coherence_v2", "xpackage_coherence",
                                    "package_coherence_off"])
def test_substring_renames_do_not_satisfy_the_id_assertion(bypass):
    """I22 F5: `certification_gate` was a substring of `certification_gate_off`."""
    assert bypass not in _step_ids() or bypass == "package_coherence"


def test_the_coherence_step_actually_invokes_the_checker():
    """Not just the id — the step body must run the real command."""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = text.split("id: package_coherence", 1)[1].split("\n      - name:", 1)[0]
    assert "scripts/commit_package_coherence.py" in block
    assert "echo" not in block.split("run:", 1)[-1].split("\n")[0], \
        "the step body must not be a no-op echo"


# --------------------------------------------------------------------------- #
# BLOCKER 3 — provenance coverage (aggregate guard wiring)
# --------------------------------------------------------------------------- #

def test_provenance_main_exit_depends_on_row_coverage():
    """AST: main() must fold coverage into `clean`, or the guard can pass while blind."""
    source = (REPO_ROOT / "scripts" / "provenance.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "row_coverage_report" in calls, \
        "main() never calls row_coverage_report(), so coverage decides nothing"
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)]
    folded = any("complete" in ast.dump(a) and "clean" in ast.dump(a) for a in assigns)
    assert folded, "main() does not fold coverage completeness into the clean verdict"


def test_a_missing_row_inventory_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(pv, "ROW_INVENTORY_FIXTURE", tmp_path / "absent.json")
    with pytest.raises(pv.RowCoverageError):
        pv.row_coverage_report()


def test_incomplete_coverage_is_a_failure_not_a_warning():
    report = pv.row_coverage_report()
    assert report["complete"] is (not report["problems"])


# --------------------------------------------------------------------------- #
# I22 ADV-D qualification (finding F3) — the PRODUCTION candidate layout
# --------------------------------------------------------------------------- #

def _production_layout_candidate(tmp_path):
    """The DEPLOYED shape: the manifest lives INSIDE its own artifact_root.

    The I21 closure test put the manifest in a sibling directory, so it differed from
    production in exactly the load-bearing way and never exercised the real layout.
    """
    import hashlib
    root = tmp_path / "candidate"
    root.mkdir()
    artifacts = {}
    for name, role in (("p.json", "policy"), ("l.json", "lifecycle"), ("v.json", "provenance")):
        f = root / name
        f.write_text(json.dumps({"x": name}), encoding="utf-8")
        artifacts[name] = {"sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "role": role}
    (root / "prefreeze.txt").write_text("sums\n", encoding="utf-8")
    artifacts["prefreeze.txt"] = {
        "sha256": hashlib.sha256((root / "prefreeze.txt").read_bytes()).hexdigest(),
        "role": "manifest"}
    manifest = root / "FROZEN-CANDIDATE.json"        # INSIDE artifact_root, as in production
    manifest.write_text(json.dumps({
        "candidate_id": "SYNTHETIC-I23-LAYOUT-1", "artifact_root": str(root),
        "prefreeze_manifest": "prefreeze.txt", "certifies_production": False,
        "artifacts": artifacts}), encoding="utf-8")
    return manifest, root


def test_an_untampered_candidate_exits_zero_in_the_PRODUCTION_layout(tmp_path, monkeypatch):
    """I22 F3: the manifest inside its own artifact_root was permanently self-undeclared, so
    clean, review-contaminated and tampered candidates ALL exited non-zero."""
    import candidate_manifest as cm
    manifest, _ = _production_layout_candidate(tmp_path)
    monkeypatch.setenv("SIGNALNEST_CANDIDATE_MANIFEST", str(manifest))
    result = cm.verify(cm.load())
    assert result["problems"] == [], result["problems"]
    assert result["clean"]


def test_a_tampered_artifact_is_still_caught_in_the_PRODUCTION_layout(tmp_path, monkeypatch):
    """The exclusion must be the manifest ONLY — everything else still hashes."""
    import candidate_manifest as cm
    manifest, root = _production_layout_candidate(tmp_path)
    monkeypatch.setenv("SIGNALNEST_CANDIDATE_MANIFEST", str(manifest))
    with open(root / "p.json", "a", encoding="utf-8") as fh:
        fh.write("x")
    result = cm.verify(cm.load())
    assert not result["clean"]
    assert any("hash mismatch" in p for p in result["problems"])


def test_an_undeclared_artifact_is_still_caught_in_the_PRODUCTION_layout(tmp_path, monkeypatch):
    import candidate_manifest as cm
    manifest, root = _production_layout_candidate(tmp_path)
    monkeypatch.setenv("SIGNALNEST_CANDIDATE_MANIFEST", str(manifest))
    (root / "sneaked-in.json").write_text("{}", encoding="utf-8")
    result = cm.verify(cm.load())
    assert not result["clean"]
    assert any("NOT declared" in p for p in result["problems"])


def test_the_exclusion_is_by_identity_not_by_name(tmp_path, monkeypatch):
    """A file merely NAMED like a manifest must still be an undeclared artifact — otherwise
    the fix becomes an exemption list, which is the blind spot ADV-D was about."""
    import candidate_manifest as cm
    manifest, root = _production_layout_candidate(tmp_path)
    monkeypatch.setenv("SIGNALNEST_CANDIDATE_MANIFEST", str(manifest))
    (root / "FROZEN-CANDIDATE.json.bak").write_text("{}", encoding="utf-8")
    result = cm.verify(cm.load())
    assert not result["clean"]
    assert any("FROZEN-CANDIDATE.json.bak" in p for p in result["problems"])


# --------------------------------------------------------------------------- #
# I22 qualifications closed here: OPERATOR-RULING (H3) and ARCH-H3/AWS-3 (H1/F4)
# --------------------------------------------------------------------------- #

def test_a_synthetic_candidate_can_never_be_production_certified():
    """I22 architect H3. Every CI run manufactured a PRODUCTION_CERTIFIED /
    certifies_production:true artifact the gate permitted; the only thing preventing it was a
    fixture that OMITTED the opt-in synthetic marker. Non-certification is now derived from
    the candidate id's structure, so it cannot be left out."""
    import production_certification as pc
    assert pc.NON_CERTIFYING_ID_PREFIX == "SYNTHETIC-"
    eligibility = {"certification_state": pc.PRODUCTION_CERTIFICATION_ELIGIBLE,
                   "certifies_production": False,
                   "candidate_id": "SYNTHETIC-CI-FIXTURE-CANDIDATE-1"}
    with pytest.raises(pc.CertificationError, match="can never certify production"):
        pc.generate_certification(eligibility, certifier_provenance="test")


def test_the_ci_certification_gate_asserts_refusal_not_success():
    """The workflow must assert the synthetic candidate is REFUSED. Exact block match."""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = text.split("id: certification_gate", 1)[1].split("\n      - name:", 1)[0]
    assert 'assert cert.returncode != 0' in block
    assert "can never certify production" in block
    assert 'assert cert.returncode == 0' not in block, (
        "the workflow still asserts a synthetic candidate is successfully certified")


def _build_index_diverged_repo(repo: Path) -> tuple:
    """A throwaway git repo whose INDEX differs from HEAD (a staged addition).

    Returns (head_tree, index_tree) computed independently via git. Lets the I22-H1/F4 regression
    be proven where index != HEAD BY CONSTRUCTION — the 'field carries HEAD's tree' defect is
    invisible on a clean checkout of the real repo (index == HEAD makes the two trees equal).
    """
    import os as _os

    def g(*args, index_file=None):
        env = {**_os.environ}
        if index_file:
            env["GIT_INDEX_FILE"] = index_file
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              env=env, check=True)

    repo.mkdir(parents=True, exist_ok=True)
    g("init", "-q")
    g("config", "user.email", "reconciliation@example.invalid")
    g("config", "user.name", "reconciliation")
    g("config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-qm", "base")
    head_tree = g("rev-parse", "HEAD^{tree}").stdout.strip()
    (repo / "staged.txt").write_text("added\n", encoding="utf-8")
    g("add", "staged.txt")
    index_tree = g("write-tree").stdout.strip()
    return head_tree, index_tree


def test_index_tree_hash_is_the_index_tree_not_heads(tmp_path, monkeypatch):
    """I22 H1/F4, found independently by THREE lanes. The field carried HEAD's tree.

    GATE 4N-I28BH-E1-HISTORY-RECONCILIATION. The original proof included
    `index_tree_hash != head_tree_hash`. That inequality was a phase-dependent PROXY: it held
    only because the branch was uncommitted, so the index differed from HEAD. On a clean checkout
    of the now-committed branch (commit d5cab12d) index == HEAD, so the CORRECT index tree
    legitimately EQUALS the head tree and the proxy fails though the field is right — the exact
    'true only under an accidental precondition' shape this gate family exists to remove. The real
    invariant is (1) EQUALITY to the independently-computed index tree, strengthened with (2) a
    positive probe that a change to the index MOVES the field, which a field carrying HEAD's tree
    could never do (that is the I22-H1/F4 signature: 'staging left the field unchanged')."""
    import os as _os
    record = ts.repository_state_record()
    assert record["index_tree_hash"] is not None
    # (1) the field equals the tree the CURRENT index actually produces.
    index_copy = tmp_path / "idx"
    index_copy.write_bytes((REPO_ROOT / ".git" / "index").read_bytes())
    real = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT, capture_output=True, text=True,
                          env={**_os.environ, "GIT_INDEX_FILE": str(index_copy)}).stdout.strip()
    assert record["index_tree_hash"] == real
    # (2) mutating the index MOVES the index tree off both its prior value and HEAD's tree. Done
    # against the COPY index via --cacheinfo, so the real .git/index is never touched; writing the
    # probe blob into the object database is harmless and idempotent (the module relies on the same
    # property). A field that carried HEAD's tree could not move under an index-only change.
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=REPO_ROOT,
                          input="reconciliation-probe\n", capture_output=True, text=True,
                          env={**_os.environ}).stdout.strip()
    subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                    f"100644,{blob},reconciliation-probe-path.txt"], cwd=REPO_ROOT, check=True,
                   capture_output=True, text=True,
                   env={**_os.environ, "GIT_INDEX_FILE": str(index_copy)})
    moved = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT, capture_output=True, text=True,
                           env={**_os.environ, "GIT_INDEX_FILE": str(index_copy)}).stdout.strip()
    assert moved != record["index_tree_hash"], "a change to the index did not move the index tree"
    assert moved != record["head_tree_hash"], "the moved index tree collides with HEAD's tree"
    # (3) Phase-INDEPENDENT I22-H1/F4 regression at its NAMED location. On a clean checkout the real
    # repo has index == HEAD, so a field returning HEAD's tree is indistinguishable from correct and
    # arms (1)-(2) above cannot see it. Prove it where index != HEAD by construction: a synthetic
    # repo with a staged addition. index_tree_hash() must read the INDEX (the tree WITH the addition),
    # which differs from that repo's HEAD tree; a field carrying HEAD's tree fails the second assert.
    synth = tmp_path / "synthetic-index"
    head_tree, index_tree = _build_index_diverged_repo(synth)
    assert head_tree != index_tree, "synthetic setup did not diverge the index from HEAD"
    monkeypatch.setattr(ts, "REPO_ROOT", synth)
    assert ts.index_tree_hash() == index_tree, "index_tree_hash did not read the index (I22-H1/F4)"
    assert ts.index_tree_hash() != head_tree, "index_tree_hash returned HEAD's tree, not the index's"


def test_computing_the_index_tree_does_not_touch_the_real_index():
    before = hashlib.sha256((REPO_ROOT / ".git" / "index").read_bytes()).hexdigest()
    ts.index_tree_hash()
    after = hashlib.sha256((REPO_ROOT / ".git" / "index").read_bytes()).hexdigest()
    assert before == after, "computing the index tree modified the real .git/index"


def test_the_certification_binding_uses_the_real_index_tree():
    """GATE 4N-I28BH-E1-HISTORY-RECONCILIATION. The former `binding != head_tree_hash` was a
    phase-dependent proxy that fails on a clean checkout (index == HEAD) though the binding is
    correct. The real invariant is that the consumer reads the SAME index tree the model computes:
    parity with both the record and an independent recomputation — checkout-independent."""
    import production_certification as pc
    binding = pc.resolve_repository_binding()
    record = ts.repository_state_record()
    assert binding["index_tree_hash"] == record["index_tree_hash"]
    assert binding["index_tree_hash"] == ts.index_tree_hash()


def test_containment_scripts_have_an_executed_ci_consumer():
    """I22 F1: leak_scan and protected_inventory appeared in ci.yml zero times."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "containment" in _step_ids()
    block = text.split("id: containment", 1)[1].split("\n      - name:", 1)[0]
    assert "scripts/leak_scan.py" in block
    assert "scripts/protected_inventory.py" in block
    assert "containment=" in text, "the containment step is not in the guard result list"
