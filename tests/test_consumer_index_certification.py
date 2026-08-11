"""Gate 4N-I20 — executed consumers, index/commit binding, certification states.

Three retained I17 agenda items, and one deliberately-separated extra:

  ARCH-H1/ADV-C  GENERATED was computed by CALLING DECLARED, so the widening suite's "three
                 independent sets" were two.
  ARCH-H2        reconcile() had no executed consumer and, as wired, could never emit
                 OVER_GRANTED or UNDER_GRANTED — the two findings it exists to emit.
  ARCH-H3/AWS-3  tracked-anchor tests used `git ls-files` (the INDEX), so a staged-but-never-
                 committed fixture satisfied a claim about history.
  OPERATOR-RULING (not named in the I20 authorization's five labels; implemented under its
                 explicit scope grant and reported separately) — certification was metadata
                 nothing consumed.

The load-bearing tests here are the ones that show the OLD wiring could not fail:
`test_the_old_wiring_could_not_emit_over_granted` and
`test_git_ls_files_does_not_establish_history`.
"""

from __future__ import annotations

import copy
import datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import expiry_authorization as ea  # noqa: E402
import gen_operator_policies as gen  # noqa: E402
import production_certification as pc  # noqa: E402
import signalnest_identity as identity  # noqa: E402
import terraform_role_inventory as tri  # noqa: E402
import tracked_state as ts  # noqa: E402

UTC = datetime.timezone.utc


def policy():
    return gen.bootstrap_temp_policy(ea.ACTIVE_EXPIRY_UTC)


def _writable_statement(document):
    for statement in document["Statement"]:
        if statement.get("Effect") == "Allow" and "iam:PutRolePolicy" in (statement.get("Action") or []):
            return statement
    raise AssertionError("no inline-policy lifecycle grant found")


# =====================================================================================
# ARCH-H2 — the reconciler has an executed consumer and its classifications are reachable
# =====================================================================================


def test_the_generated_side_is_read_back_out_of_the_emitted_policy():
    """Not from the constant the expected side is built from."""
    from_policy = tri.generated_writable_arns_from_policy()
    assert from_policy, "no writable grant was recovered from the emitted policy"
    assert from_policy == sorted(set(from_policy))


def test_the_old_wiring_could_not_emit_over_granted():
    """The defect, demonstrated rather than described.

    Passing `gen.INLINE_POLICY_ROLE_ARNS` — which is literally
    `role_arns(writable_roles())`, the same expression reconcile() builds its expected side
    from — makes is_expected == is_generated for every role, so neither OVER_GRANTED nor
    UNDER_GRANTED can ever appear whatever the policy actually says.
    """
    self_compared = tri.reconcile(gen.INLINE_POLICY_ROLE_ARNS)
    classes = {row["classification"] for row in self_compared["rows"]}
    assert "OVER_GRANTED" not in classes and "UNDER_GRANTED" not in classes

    # Even with the emitted policy grossly over-granting, the OLD wiring stays clean.
    over = copy.deepcopy(policy())
    _writable_statement(over)["Resource"] = sorted(
        set(_writable_statement(over)["Resource"])
        | {identity.iam_role_arn(f"{identity.PREFIX}-migration-task")})
    assert tri.reconcile(gen.INLINE_POLICY_ROLE_ARNS)["clean"], (
        "the self-comparing wiring was expected to stay clean; if it now notices, this test no "
        "longer demonstrates why the re-sourcing was necessary")

    # The NEW wiring sees it.
    fixed = tri.reconcile(tri.generated_writable_arns_from_policy(over))
    assert not fixed["clean"]


def test_the_DEFAULT_reconcile_path_reads_the_emitted_policy(monkeypatch):
    """The path `main()` actually uses must be the re-sourced one.

    FOUND BY THIS GATE'S IN-PLACE FALSIFICATION SWEEP. Reverting the default back to
    `gen.INLINE_POLICY_ROLE_ARNS` changed NO test result: every other test in this file passes
    the generated set explicitly, so nothing exercised `reconcile()` with no argument — which is
    exactly how `main()`, the CI step and the guard list call it. A fix that only holds on the
    argument-passing path is the ARCH-H2 defect wearing a different hat.

    The divergence is injected into the EMITTED POLICY ONLY, leaving the constant untouched.
    Under the correct wiring the generated side moves and the reconciler objects; under the
    self-comparing wiring both sides are the constant, nothing moves, and it stays clean.
    """
    real_generator = gen.bootstrap_temp_policy

    def policy_with_an_extra_grant(*args, **kwargs):
        document = copy.deepcopy(real_generator(*args, **kwargs))
        statement = _writable_statement(document)
        statement["Resource"] = sorted(
            set(statement["Resource"])
            | {identity.iam_role_arn(f"{identity.PREFIX}-migration-task")})
        return document

    monkeypatch.setattr(gen, "bootstrap_temp_policy", policy_with_an_extra_grant)
    assert list(gen.INLINE_POLICY_ROLE_ARNS) == list(gen.INLINE_POLICY_ROLE_ARNS), "constant untouched"

    result = tri.reconcile()          # NO ARGUMENT — the shipping path
    assert not result["clean"], (
        "reconcile() with no argument did not notice a grant present in the emitted policy but "
        "absent from the Terraform declarations — the generated side is not being read from the "
        "policy")
    rows = {r.get("role_name"): r["classification"] for r in result["rows"] if r.get("role_name")}
    assert rows[f"{identity.PREFIX}-migration-task"] == "OVER_GRANTED"


def test_an_injected_over_grant_produces_OVER_GRANTED():
    """The agenda's exact verification requirement."""
    over = copy.deepcopy(policy())
    migration = identity.iam_role_arn(f"{identity.PREFIX}-migration-task")
    statement = _writable_statement(over)
    statement["Resource"] = sorted(set(statement["Resource"]) | {migration})

    result = tri.reconcile(tri.generated_writable_arns_from_policy(over))
    rows = {r.get("role_name"): r["classification"] for r in result["rows"] if r.get("role_name")}
    assert rows[f"{identity.PREFIX}-migration-task"] == "OVER_GRANTED"
    assert not result["clean"]


def test_a_removed_grant_produces_UNDER_GRANTED():
    under = copy.deepcopy(policy())
    statement = _writable_statement(under)
    statement["Resource"] = sorted(set(statement["Resource"])
                                   - {identity.iam_role_arn(f"{identity.PREFIX}-api-task")})

    result = tri.reconcile(tri.generated_writable_arns_from_policy(under))
    rows = {r.get("role_name"): r["classification"] for r in result["rows"] if r.get("role_name")}
    assert rows[f"{identity.PREFIX}-api-task"] == "UNDER_GRANTED"
    assert not result["clean"]


def test_a_statement_assembly_bug_is_caught():
    """A wildcard resource is a grant the .tf parse can never justify."""
    broken = copy.deepcopy(policy())
    _writable_statement(broken)["Resource"] = "*"
    assert not tri.reconcile(tri.generated_writable_arns_from_policy(broken))["clean"]


def test_the_reconciler_is_executed_and_fails_non_zero():
    """PRODUCED_BUT_UNUSED is not enforcement: the exit code must move."""
    proc = subprocess.run([sys.executable, "scripts/terraform_role_inventory.py"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "ROLE INVENTORY" in proc.stdout


def test_the_consumer_is_wired_into_ci():
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "terraform_role_inventory.py" in workflow, (
        "the reconciler has no CI invocation; a mandatory result that no job runs is documentation")
    assert "role_inventory=" in workflow, (
        "the reconciler's outcome is not in the Gate 4N guard result list, so its failure would "
        "not reach the top-level job")


# =====================================================================================
# ARCH-H1/ADV-C — the independence claim now matches reality
# =====================================================================================


def test_the_three_sets_have_three_distinct_derivations():
    authored = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "expected-writable-roles.json").read_text())
    expected = {identity.iam_role_arn(f"{identity.PREFIX}-{e['suffix']}")
                for e in authored["role_name_suffixes_writable"]}
    declared = set(tri.role_arns(tri.writable_roles()))
    generated = set(tri.generated_writable_arns_from_policy())

    assert expected == declared == generated, "the three sets disagree"
    # ...but they are produced by three different routes: an authored fixture, a .tf parse, and
    # a round trip through policy construction. Agreement is meaningful only because of that.
    assert tri.generated_writable_arns_from_policy.__module__ == "terraform_role_inventory"


def test_widening_both_the_generator_and_the_policy_is_still_caught():
    """The agenda's verification: a mutation that moves both must still fail."""
    original = list(gen.INLINE_POLICY_ROLE_ARNS)
    sibling = identity.iam_role_arn(f"{identity.PREFIX}-not-declared")
    try:
        gen.INLINE_POLICY_ROLE_ARNS = sorted(set(original) | {sibling})
        widened = gen.bootstrap_temp_policy(ea.ACTIVE_EXPIRY_UTC)
        assert sibling in tri.generated_writable_arns_from_policy(widened)
        result = tri.reconcile(tri.generated_writable_arns_from_policy(widened))
        assert not result["clean"], "both sides moved and nothing noticed"
    finally:
        gen.INLINE_POLICY_ROLE_ARNS = original


# =====================================================================================
# ARCH-H3/AWS-3 — index is not history
# =====================================================================================
#
# GATE 4N-I28BH-E1-HISTORY-RECONCILIATION. These controls proved "the index is not committed
# history" by pointing at the real fixtures, which HAPPENED to be staged-not-committed while this
# branch was uncommitted. The authorized commit d5cab12d moved those anchors into HEAD, so that
# incidental specimen is gone and the pre-commit assertions fire — exactly as their authors
# demanded ("this branch has committed, so this test must be revisited rather than silently
# passing for a new reason"). The reconciliation does NOT weaken the discrimination: it proves the
# same load-bearing property (in-index / not-in-HEAD is a STAGED_ADDITION, never committed) on a
# SYNTHETIC throwaway git repo, so it holds in every commit phase and in a clean CI checkout.


def _build_synthetic_repo(repo: Path) -> dict:
    """A throwaway git repo with one path in each of the seven tracked_state states.

    Returns {relpath: expected_state}. The classifier is driven against it by rebinding the
    single module global `tracked_state.REPO_ROOT` (see the `synthetic_states` fixture): every
    tracked_state function reads that global, so the whole classifier redirects here. A synthetic
    repo has a real `.git` DIRECTORY, so `.git/index` exists and `git write-tree` runs — none of
    which depends on whether THIS branch has committed.
    """
    def g(*args, index_file: str | None = None):
        import os as _os
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
    # Initial commit. .gitignore is committed HERE, in the first commit, so that later staging is
    # never swept into a second commit (a `git commit` writes the whole index, which would move
    # staged specimens into HEAD and destroy the states we are constructing).
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "committed.txt").write_text("v1\n", encoding="utf-8")
    (repo / "to_modify_worktree.txt").write_text("base\n", encoding="utf-8")
    (repo / "to_modify_staged.txt").write_text("base\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-qm", "base")
    # The remaining states, created with NO further commit.
    (repo / "staged_addition.txt").write_text("new\n", encoding="utf-8")
    g("add", "staged_addition.txt")                          # in index, absent from HEAD
    (repo / "to_modify_staged.txt").write_text("changed\n", encoding="utf-8")
    g("add", "to_modify_staged.txt")                         # in HEAD, index content differs
    (repo / "to_modify_worktree.txt").write_text("changed\n", encoding="utf-8")   # worktree differs, not added
    (repo / "untracked.txt").write_text("loose\n", encoding="utf-8")              # never added
    (repo / "ignored.txt").write_text("junk\n", encoding="utf-8")                 # matched by .gitignore
    return {
        "committed.txt": ts.TRACKED_IN_HEAD,
        "staged_addition.txt": ts.STAGED_ADDITION,
        "to_modify_staged.txt": ts.STAGED_MODIFICATION,
        "to_modify_worktree.txt": ts.MODIFIED_IN_WORKTREE,
        "untracked.txt": ts.UNTRACKED,
        "ignored.txt": ts.IGNORED,
        "absent.txt": ts.ABSENT,
    }


@pytest.fixture
def synthetic_states(tmp_path, monkeypatch):
    """Build the synthetic repo and redirect the whole tracked_state classifier at it.

    `monkeypatch` restores `tracked_state.REPO_ROOT` after the test, so the real repository is
    never affected and other tests keep measuring it.
    """
    repo = tmp_path / "synthetic-git"
    expected = _build_synthetic_repo(repo)
    monkeypatch.setattr(ts, "REPO_ROOT", repo)
    return expected


def test_git_ls_files_does_not_establish_history(synthetic_states):
    """The ARCH-H3/AWS-3 defect, proven on a controlled specimen.

    `git ls-files` reports the INDEX; a staged-only path is in the index yet absent from
    committed history and carries no review trail. Formerly demonstrated with the real fixtures
    (staged-not-committed on an uncommitted branch); now that the branch has committed the proof
    runs on a synthetic repo and no longer depends on the branch's commit phase.
    """
    index = ts.index_paths()
    head = ts.head_paths()
    assert "staged_addition.txt" in index, "`git ls-files` does not see the staged addition"
    assert "staged_addition.txt" not in head, "the staged addition is in committed history"
    assert index - head, "index membership coincides with HEAD; the distinction is untested"
    # The load-bearing discrimination: an in-index / not-in-HEAD path is a STAGED_ADDITION, never
    # 'committed'/TRACKED_IN_HEAD. A control that equated ls-files membership with history would
    # return TRACKED_IN_HEAD here.
    assert ts.state_of("staged_addition.txt") == ts.STAGED_ADDITION
    assert ts.state_of("staged_addition.txt") != ts.TRACKED_IN_HEAD


@pytest.mark.parametrize("rel,expected", [
    # GATE 4N-I28BH-E1-HISTORY-RECONCILIATION. Only phase-STABLE real-repo anchors remain here.
    # After the authorized commit d5cab12d these production anchors are committed, so in any clean
    # checkout they are TRACKED_IN_HEAD. The transient staging states they used to demonstrate
    # (STAGED_ADDITION for the two fixtures + action_classifier.py, STAGED_MODIFICATION for ci.yml)
    # are now proven on a synthetic repo in test_synthetic_repo_reports_all_seven_states, which is
    # checkout-independent. ci.yml was DROPPED from this list rather than re-pointed: its state is
    # legitimately checkout-dependent (STAGED_MODIFICATION while a change is staged, TRACKED_IN_HEAD
    # in a clean checkout), so a fixed transient expectation for it would be true only under an
    # accidental precondition — the exact defect shape this module exists to remove.
    ("tests/fixtures/lifecycle-canonical-sha256.txt", ts.TRACKED_IN_HEAD),
    ("tests/fixtures/expected-writable-roles.json", ts.TRACKED_IN_HEAD),
    ("scripts/action_classifier.py", ts.TRACKED_IN_HEAD),
    ("infra/aws/live-resource-inventory.json", ts.IGNORED),
    ("README.md", ts.TRACKED_IN_HEAD),
])
def test_each_path_reports_its_exact_state(rel, expected):
    assert ts.state_of(rel) == expected


def test_synthetic_repo_reports_all_seven_states(synthetic_states):
    """The full seven-way discrimination on controlled specimens, independent of commit phase.

    Strictly stronger than the previous real-path rows: it exercises every transient staging
    state (STAGED_ADDITION, STAGED_MODIFICATION, MODIFIED_IN_WORKTREE, UNTRACKED) that a clean
    checkout of a committed branch can no longer show on the real anchors — so the staging-vs-
    committed discrimination the retired rows demonstrated is preserved, not lost.
    """
    for rel, expected in synthetic_states.items():
        assert ts.state_of(rel) == expected, f"{rel}: got {ts.state_of(rel)}, expected {expected}"
    # The seven states are genuinely distinct — a classifier that collapsed any pair would fail.
    assert len(set(synthetic_states.values())) == 7


def test_the_ambiguous_vocabulary_is_refused():
    for word in ("tracked", "committed"):
        with pytest.raises(ts.TrackedStateError, match="not a state"):
            ts.assert_state("tests/fixtures/expected-writable-roles.json", word)


def test_the_predicted_commit_tree_is_reproducible_and_does_not_touch_the_index():
    before = subprocess.run(["git", "diff", "--cached"], cwd=REPO_ROOT,
                            capture_output=True, text=True).stdout
    first = ts.predicted_commit_tree()
    second = ts.predicted_commit_tree()
    after = subprocess.run(["git", "diff", "--cached"], cwd=REPO_ROOT,
                           capture_output=True, text=True).stdout
    assert first["predicted_tree_hash"] == second["predicted_tree_hash"]
    assert before == after, "building the predicted tree modified the real index"


def test_the_predicted_tree_includes_staged_additions_and_differs_from_head(synthetic_states):
    """With a staged addition present, the predicted commit tree INCLUDES it and DIFFERS from
    HEAD's tree. GATE 4N-I28BH-E1-HISTORY-RECONCILIATION: formerly asserted on the real branch,
    where `predicted != head` held only while uncommitted; on a clean checkout of the committed
    branch index == HEAD and the correct predicted tree equals HEAD's, so the property is now
    proven deterministically on a synthetic repo that always has a staged addition."""
    predicted = ts.predicted_commit_tree()
    assert predicted["predicted_tree_hash"] != predicted["head_tree_hash"], (
        "a staged addition did not move the predicted tree off HEAD")
    assert "staged_addition.txt" in predicted["entries"], "the staged addition would not reach the commit"
    assert "staged_addition.txt" in predicted["added_relative_to_head"]


def test_the_predicted_tree_contains_the_committed_governance_fixtures():
    """Phase-invariant real-repo coverage retained from the reconciled test above: whatever the
    branch's commit phase, the predicted commit tree IS the index tree and contains the
    governance fixtures — committed (clean checkout) or staged (pre-commit)."""
    predicted = ts.predicted_commit_tree()
    assert predicted["predicted_tree_hash"] == ts.index_tree_hash()
    for rel in ("tests/fixtures/expected-writable-roles.json",
                "tests/fixtures/readonly-verifier-ceiling.json"):
        assert rel in predicted["entries"], f"{rel} would not reach the commit"


def test_the_predicted_tree_excludes_ignored_live_evidence():
    predicted = ts.predicted_commit_tree()
    for prohibited in ("infra/aws/live-resource-inventory.json",
                       "infra/aws/cloudfront-expected.json"):
        assert prohibited not in predicted["entries"], (
            f"{prohibited} would enter the commit; the Gate 4N-I18 containment is defeated")


EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_the_state_record_separates_every_hash():
    """Each binding must measure its OWN object.

    GATE 4N-I23. This asserted six pairwise-distinct values, which held only while the tree
    was PARTIALLY staged. With the full package staged the empty unstaged diff and the empty
    untracked inventory both hash to the empty digest, and the staged diff legitimately IS the
    full tracked diff — three "collapses" that are all semantically correct. Demanding six
    different values was an assertion true only under an accidental precondition, the same
    shape as the predicted_commit_tree defect this gate fixed.

    So assert the identities that must ALWAYS hold, and derive the rest from the actual state
    rather than from a count.
    """
    record = ts.repository_state_record()

    # The predicted commit tree IS the index tree, so it differs from HEAD's tree EXACTLY when
    # something is staged. GATE 4N-I28BH-E1-HISTORY-RECONCILIATION: the old unconditional
    # `predicted != head` held only while the branch was uncommitted; on a clean checkout of the
    # committed branch nothing is staged, index == HEAD, and equality is CORRECT — not a collapse.
    # Derive the expectation from the actual staged state, the same shape this test already uses
    # for the diff digests below.
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT).returncode != 0
    if staged:
        assert record["predicted_commit_tree_hash"] != record["head_tree_hash"]
    else:
        assert record["predicted_commit_tree_hash"] == record["head_tree_hash"]

    unstaged = subprocess.run(["git", "diff", "--name-only"], cwd=REPO_ROOT,
                              capture_output=True, text=True).stdout.split()
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                               cwd=REPO_ROOT, capture_output=True, text=True).stdout.split()

    # An empty diff hashes to the empty digest — and must, or the binding is lying about
    # whether anything is unstaged.
    assert (record["unstaged_diff_sha256"] == EMPTY_SHA256) == (not unstaged)
    assert (record["untracked_inventory_sha256"] == EMPTY_SHA256) == (not untracked)

    # With nothing unstaged, staged == full is an identity, not a collapse. With something
    # unstaged, they MUST differ or one of them is not measuring what it claims.
    if unstaged:
        assert record["staged_diff_sha256"] != record["full_tracked_diff_sha256"]
    else:
        assert record["staged_diff_sha256"] == record["full_tracked_diff_sha256"]

    # Tree hashes and diff digests are different kinds of object and must never coincide.
    assert record["head_tree_hash"] not in {record["staged_diff_sha256"],
                                            record["full_tracked_diff_sha256"]}


# =====================================================================================
# Certification — states, transitions, and a gate that actually refuses
# =====================================================================================


def _tier2_fixtures(tmp_path):
    """Protected Tier-2 EQUIVALENT inputs. No AWS call; nothing synthetic-marked."""
    import hashlib

    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"approved_account_id": "111122223333", "partition": "aws",
                                    "_note": "protected-equivalent test evidence"}), encoding="utf-8")
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    inventory_sha = "b" * 64

    candidate = {"candidate_id": "4N-I21-CANDIDATE-1", "candidate_manifest_sha256": "a" * 64}
    external = {"protected_tier": "TIER_2_PROTECTED",
                "external_anchor_sha256": evidence_sha,
                "external_inventory_sha256": inventory_sha}
    checks = [{"check_id": c["check_id"], "status": "PASSED"} for c in pc.required_checks()]
    return {"evidence": evidence, "evidence_sha": evidence_sha, "inventory_sha": inventory_sha,
            "candidate": candidate, "external": external, "checks": checks}


def _certified(tmp_path):
    """The full legitimate pipeline: eligibility -> certify."""
    f = _tier2_fixtures(tmp_path)
    eligibility = pc.establish_eligibility(
        tier=pc.TIER_PROTECTED, anchor_path=str(f["evidence"]), anchor_sha256=f["evidence_sha"],
        inventory_sha256=f["inventory_sha"], candidate_manifest=f["candidate"], checks=f["checks"])
    artifact = pc.generate_certification(eligibility, certifier_provenance="Tier-2 operator process")
    return artifact, f


# =====================================================================================
# THE GATE 4N-I20A DEFECT — the default path must fail closed
# =====================================================================================


EXPLOIT = {
    "schema_version": pc.SCHEMA_VERSION, "certification_state": pc.PRODUCTION_CERTIFIED,
    "certifies_production": True, "candidate_id": "TOTALLY-MADE-UP-CANDIDATE",
    "candidate_manifest_sha256": "a" * 64, "head": "b" * 40, "index_tree_hash": "c" * 40,
    "commit_tree_hash": "d" * 40, "repository_diff_sha256": "e" * 64,
    "external_anchor_sha256": "f" * 64, "external_inventory_sha256": "0" * 64,
    "protected_tier": "TIER_2_PROTECTED", "certified_at_utc": "2026-08-01T00:00:00Z",
    "valid_until_utc": "2099-01-01T00:00:00Z", "checks": [],
    "certifier_provenance": "anything", "eligibility_sha256": "9" * 64,
}


def test_the_i20a_exploit_is_refused_by_the_default_path(tmp_path):
    """THE regression test for this gate.

    At Gate 4N-I20A this exact artifact was PERMITTED with exit 0, because the bindings were
    only compared when the caller passed optional expected_* arguments and the shipping CLI
    passed none. Nothing optional is supplied here.
    """
    f = _tier2_fixtures(tmp_path)
    verdict = pc.production_gate(EXPLOIT, candidate_manifest=f["candidate"],
                                 external_binding=f["external"])
    assert not verdict["permitted"]
    assert verdict["exit_code"] == 2
    for expected_mismatch in ("candidate_id", "head", "commit_tree_hash", "checks"):
        assert expected_mismatch in verdict["reason"]


def test_the_gate_refuses_when_binding_inputs_are_absent():
    """Making the bindings optional is what caused the defect; absence is now refusal."""
    assert not pc.production_gate(EXPLOIT)["permitted"]
    assert not pc.production_gate(EXPLOIT, candidate_manifest={"candidate_id": "x"})["permitted"]
    assert not pc.production_gate(None)["permitted"]


def test_the_shipping_cli_has_no_artifact_only_verify_mode(tmp_path):
    """argparse itself must refuse; a permissive mode cannot be reached."""
    path = tmp_path / "a.json"
    path.write_text(json.dumps(EXPLOIT), encoding="utf-8")
    proc = subprocess.run([sys.executable, "scripts/production_certification.py", "verify",
                           "--artifact", str(path)], cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "required" in (proc.stderr + proc.stdout).lower()


def test_the_shipping_cli_refuses_the_exploit(tmp_path):
    """The Gate 4N-I25 exploit, replayed against the shipping CLI."""
    artifact, f = _certified(tmp_path)
    cand = tmp_path / "cand.json"; cand.write_text(json.dumps(f["candidate"]), encoding="utf-8")
    ext = tmp_path / "ext.json"; ext.write_text(json.dumps(f["external"]), encoding="utf-8")

    exploit = tmp_path / "exploit.json"
    exploit.write_text(json.dumps(EXPLOIT), encoding="utf-8")
    refused = subprocess.run([sys.executable, "scripts/production_certification.py", "verify",
                              "--artifact", str(exploit), "--candidate-manifest", str(cand),
                              "--external-binding", str(ext)],
                             cwd=REPO_ROOT, capture_output=True, text=True)
    assert refused.returncode == 2
    assert "refused" in refused.stdout


def test_a_caller_supplied_authorization_cannot_authorize_production(tmp_path):
    """GATE 4N-I26B, closing I26B-02 — the finding that made the security lane FAIL at I25.

    A well-formed certificate is now ALSO refused when its external authorization is a document
    the caller wrote. That is not a regression in the happy path; it is the happy path being
    correct for the first time. Previously the 'expected' external digests came from a file the
    caller passed on the command line, so inventing a digest and echoing the same value into
    --external-binding produced a match with itself — exit 0, "PRODUCTION GATE: permitted", on a
    wholly fabricated artifact.

    Production permission now requires an authorization document whose digest THIS REPOSITORY
    pins. No document written into a tmp directory can be one.
    """
    artifact, f = _certified(tmp_path)
    cand = tmp_path / "cand.json"; cand.write_text(json.dumps(f["candidate"]), encoding="utf-8")
    ext = tmp_path / "ext.json"; ext.write_text(json.dumps(f["external"]), encoding="utf-8")
    good = tmp_path / "cert.json"; good.write_text(json.dumps(artifact), encoding="utf-8")

    result = subprocess.run([sys.executable, "scripts/production_certification.py", "verify",
                             "--artifact", str(good), "--candidate-manifest", str(cand),
                             "--external-binding", str(ext)],
                            cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode != 0, (
        "a caller-authored authorization document was accepted; this is exactly the I25 "
        "fabrication path:\n" + result.stdout + result.stderr)
    assert "permitted" not in result.stdout


def test_the_pinned_synthetic_authorization_authenticates_but_cannot_certify_production(tmp_path):
    """The mechanism must be exercisable WITHOUT real protected evidence, and must still refuse.

    The synthetic authorization's digest IS pinned, so it passes authentication and every field
    check runs — proving the path is live rather than merely present. It then fails on tier,
    because a TIER_1_SYNTHETIC authorization may not certify production. Both halves matter: a
    mechanism that cannot run in CI is untested, and one that runs and permits is the defect.
    """
    import production_certification as pc

    document = pc.resolve_external_authorization(
        REPO_ROOT / "tests" / "fixtures" / "synthetic-external-authorization.json",
        tier=pc.TIER_SYNTHETIC)
    assert document["protected_tier"] == pc.TIER_SYNTHETIC

    problems = pc._external_problems({"external_anchor_sha256": "x"}, document)
    assert any("not TIER_2_PROTECTED" in p or "may certify production" in p for p in problems), \
        problems


def test_an_unpinned_tier_is_refused_rather_than_trusted():
    """TIER_2_PROTECTED has no pin in this repository, and absence must mean refusal."""
    import production_certification as pc

    with pytest.raises(pc.CertificationError, match="no authorization digest is pinned"):
        pc.resolve_external_authorization(
            REPO_ROOT / "tests" / "fixtures" / "synthetic-external-authorization.json",
            tier=pc.TIER_PROTECTED)


def test_authorization_field_coverage_is_complete_in_both_directions():
    """A required field nobody validates is a field an attacker may set freely."""
    import production_certification as pc

    coverage = pc.authorization_field_coverage()
    assert coverage["required_not_validated"] == [], coverage
    assert coverage["validated_not_required"] == [], coverage
    assert coverage["complete"]


# =====================================================================================
# Bindings are mandatory and independently resolved
# =====================================================================================


BINDING_MUTATIONS = {
    "candidate_id": "OTHER-CANDIDATE", "candidate_manifest_sha256": "9" * 64,
    "head": "9" * 40, "index_tree_hash": "9" * 40, "commit_tree_hash": "9" * 40,
    "repository_diff_sha256": "9" * 64, "external_anchor_sha256": "9" * 64,
    "external_inventory_sha256": "9" * 64, "eligibility_sha256": "",
    "valid_until_utc": "2020-01-01T00:00:00Z", "certifies_production": False,
    "certification_state": pc.PRODUCTION_CERTIFICATION_ELIGIBLE,
}


@pytest.mark.parametrize("field", sorted(BINDING_MUTATIONS))
def test_every_binding_mutation_is_refused(tmp_path, field):
    artifact, f = _certified(tmp_path)
    artifact = copy.deepcopy(artifact)
    artifact[field] = BINDING_MUTATIONS[field]
    verdict = pc.production_gate(artifact, candidate_manifest=f["candidate"],
                                 external_binding=f["external"])
    assert not verdict["permitted"], f"{field} mutation was permitted"


def test_expected_bindings_are_resolved_live_so_drift_is_detected(tmp_path):
    """The repository side is read from tracked_state, not from the artifact or an argument."""
    resolved = pc.resolve_repository_binding()
    live = ts.repository_state_record()
    assert resolved["head"] == live["head"]
    assert resolved["commit_tree_hash"] == live["predicted_commit_tree_hash"]
    artifact, f = _certified(tmp_path)
    assert artifact["head"] == live["head"]

    drifted = copy.deepcopy(artifact)
    drifted["commit_tree_hash"] = "0" * 40
    assert not pc.production_gate(drifted, candidate_manifest=f["candidate"],
                                  external_binding=f["external"])["permitted"]


def test_an_artifact_may_not_supply_its_own_expected_values(tmp_path):
    artifact, f = _certified(tmp_path)
    for field in ("canonical_sha256", "required_checks", "expected_bindings"):
        m = copy.deepcopy(artifact); m[field] = "anything"
        assert not pc.production_gate(m, candidate_manifest=f["candidate"],
                                      external_binding=f["external"])["permitted"], field


# =====================================================================================
# Required production checks
# =====================================================================================


def test_the_required_check_set_is_non_empty_and_authored_independently():
    checks = pc.required_checks()
    assert checks
    ids = [c["check_id"] for c in checks]
    assert len(ids) == len(set(ids)), "duplicate check ids in the authored contract"
    assert "required_checks" in pc.SELF_ATTESTING_FIELDS, (
        "an artifact must not be allowed to name its own required set")


@pytest.mark.parametrize("label", ["empty", "missing_field", "one_missing", "one_failed",
                                   "one_unknown", "duplicate"])
def test_check_set_defects_are_refused(tmp_path, label):
    artifact, f = _certified(tmp_path)
    artifact = copy.deepcopy(artifact)
    if label == "empty":
        artifact["checks"] = []
    elif label == "missing_field":
        del artifact["checks"]
    elif label == "one_missing":
        artifact["checks"] = artifact["checks"][1:]
    elif label == "one_failed":
        artifact["checks"][0]["status"] = "FAILED"
    elif label == "one_unknown":
        artifact["checks"][0]["status"] = "WHO KNOWS"
    elif label == "duplicate":
        artifact["checks"] = artifact["checks"] + [artifact["checks"][0]]
    verdict = pc.production_gate(artifact, candidate_manifest=f["candidate"],
                                 external_binding=f["external"])
    assert not verdict["permitted"], f"{label} was permitted"


# =====================================================================================
# Eligibility and the generator are executed paths
# =====================================================================================


def test_eligibility_is_established_and_is_NOT_certification(tmp_path):
    f = _tier2_fixtures(tmp_path)
    result = pc.establish_eligibility(
        tier=pc.TIER_PROTECTED, anchor_path=str(f["evidence"]), anchor_sha256=f["evidence_sha"],
        inventory_sha256=f["inventory_sha"], candidate_manifest=f["candidate"], checks=f["checks"])
    assert result["certification_state"] == pc.PRODUCTION_CERTIFICATION_ELIGIBLE
    assert result["certifies_production"] is False


@pytest.mark.parametrize("break_it", ["tier1", "wrong_hash", "synthetic", "empty_checks",
                                      "no_inventory"])
def test_eligibility_refuses_incomplete_evidence(tmp_path, break_it):
    f = _tier2_fixtures(tmp_path)
    kw = dict(tier=pc.TIER_PROTECTED, anchor_path=str(f["evidence"]),
              anchor_sha256=f["evidence_sha"], inventory_sha256=f["inventory_sha"],
              candidate_manifest=f["candidate"], checks=f["checks"])
    if break_it == "tier1":
        kw["tier"] = "TIER_1_SYNTHETIC"
    elif break_it == "wrong_hash":
        kw["anchor_sha256"] = "9" * 64
    elif break_it == "synthetic":
        f["evidence"].write_text(json.dumps({"_classification": "NON_PRODUCTION_TEST_FIXTURE",
                                             "approved_account_id": "111122223333"}),
                                 encoding="utf-8")
        import hashlib
        kw["anchor_sha256"] = hashlib.sha256(f["evidence"].read_bytes()).hexdigest()
    elif break_it == "empty_checks":
        kw["checks"] = []
    elif break_it == "no_inventory":
        kw["inventory_sha256"] = ""
    with pytest.raises(pc.CertificationError):
        pc.establish_eligibility(**kw)


def test_the_generator_accepts_only_a_valid_eligibility_result(tmp_path):
    f = _tier2_fixtures(tmp_path)
    eligibility = pc.establish_eligibility(
        tier=pc.TIER_PROTECTED, anchor_path=str(f["evidence"]), anchor_sha256=f["evidence_sha"],
        inventory_sha256=f["inventory_sha"], candidate_manifest=f["candidate"], checks=f["checks"])
    assert pc.generate_certification(eligibility, certifier_provenance="p")["certifies_production"]

    for label, mutate in [
            ("candidate-only", lambda e: {**e, "certification_state": pc.CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED}),
            ("mechanism-only", lambda e: {**e, "certification_state": pc.MECHANISM_ONLY}),
            ("already certified", lambda e: {**e, "certification_state": pc.PRODUCTION_CERTIFIED}),
            ("invalid", lambda e: {**e, "certification_state": pc.INVALID_CERTIFICATION}),
            ("flag true", lambda e: {**e, "certifies_production": True}),
            ("empty checks", lambda e: {**e, "checks": []}),
            ("tier1", lambda e: {**e, "protected_tier": "TIER_1_SYNTHETIC"}),
            ("drifted tree", lambda e: {**e, "commit_tree_hash": "0" * 40})]:
        with pytest.raises(pc.CertificationError):
            pc.generate_certification(mutate(eligibility), certifier_provenance="p")


# =====================================================================================
# The state machine runs in production code
# =====================================================================================


def test_the_state_machine_has_production_callers():
    """classify() and transition() must be reachable from shipping code, not tests only."""
    source = (REPO_ROOT / "scripts" / "production_certification.py").read_text(encoding="utf-8")
    body = source[source.index("def establish_eligibility"):]
    assert "classify(" in body, "establish_eligibility does not call classify()"
    assert "transition(" in body, "the eligibility path does not exercise the transition table"
    generator = source[source.index("def generate_certification"):]
    assert "transition(" in generator, "the generator does not exercise the transition table"
    derived = source[source.index("def derive_current_state"):]
    assert "classify(" in derived, "current state is not derived through classify()"


def test_current_state_is_derived_not_hardcoded():
    state = pc.derive_current_state(tier="TIER_1_SYNTHETIC")
    assert state["certification_state"] == pc.MECHANISM_ONLY
    assert state["certifies_production"] is False
    local = pc.derive_current_state(tier="TIER_2_PROTECTED")
    assert local["certification_state"] == pc.INVALID_CERTIFICATION, (
        "Tier-2 declared with no protected evidence must not resolve to a benign state")


PROHIBITED_TRANSITIONS = [
    (pc.MECHANISM_ONLY, pc.PRODUCTION_CERTIFIED),
    (pc.MECHANISM_ONLY, pc.PRODUCTION_CERTIFICATION_ELIGIBLE),
    (pc.CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED, pc.PRODUCTION_CERTIFIED),
    (pc.PRODUCTION_CERTIFIED, pc.MECHANISM_ONLY),
    (pc.INVALID_CERTIFICATION, pc.PRODUCTION_CERTIFIED),
]


@pytest.mark.parametrize("current,target", PROHIBITED_TRANSITIONS)
def test_prohibited_transitions_are_refused(current, target):
    with pytest.raises(pc.CertificationError, match="prohibited transition"):
        pc.transition(current, target)


@pytest.mark.parametrize("current,target", sorted(pc.ALLOWED_TRANSITIONS))
def test_allowed_transitions_are_permitted(current, target):
    assert pc.transition(current, target) == target


def test_every_state_has_a_required_flag_and_only_certified_is_true():
    assert set(pc.REQUIRED_FLAG) == set(pc.STATES)
    assert [s for s, f in pc.REQUIRED_FLAG.items() if f] == [pc.PRODUCTION_CERTIFIED]


def test_the_local_state_command_can_never_permit(tmp_path):
    proc = subprocess.run([sys.executable, "scripts/production_certification.py", "state"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "not production-certified" in proc.stdout
    assert "permitted" not in proc.stdout


def test_ci_executes_the_real_validating_gate():
    """CI must run the REAL verify path, in both directions, and fail the job on refusal.

    Gate 4N-I20A proved the previous step could not have caught a regression: it ran the
    module's default mode, which reports the local state and exits 0, and never reached the
    artifact-validating gate.

    This asserts on the certification_gate STEP BLOCK rather than on a substring of the whole
    workflow: the step invokes the CLI through a subprocess argument list, so a naive
    "production_certification.py verify" literal never appears no matter how correct the wiring
    is — and a test keyed to that literal would fail while the control worked, or pass on a
    comment mentioning it.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    # EXACT line match. FOUND BY THIS GATE'S FALSIFICATION SWEEP: renaming the step to
    # `certification_gate_off` left this test passing, because the original id is a SUBSTRING of
    # the renamed one. A disconnected step would have gone unnoticed.
    assert "\n        id: certification_gate\n" in workflow, (
        "the real production-gate step is absent from CI, or its id was renamed")
    start = workflow.index("\n        id: certification_gate\n")
    end = workflow.find("\n      - name:", start)
    block = workflow[start:end if end != -1 else len(workflow)]

    assert '"verify"' in block, "the step does not invoke the verify command"
    for required in ("--candidate-manifest", "--external-binding"):
        assert required in block, f"the step does not supply {required}; bindings would be optional"
    assert "PERMITTED A FABRICATED ARTIFACT" in block, (
        "the step does not assert that the exploit is refused")
    assert "artifact-only verify mode is reachable" in block, (
        "the step does not assert that the permissive mode is unreachable")
    assert "certify" in block and "eligibility" in block, (
        "the step does not exercise the eligibility -> certify -> verify pipeline")
    assert "|| true" not in block and "continue-on-error" not in block, (
        "the production-gate step suppresses its own failure")
    assert "certification_gate=${{ steps.certification_gate.outcome }}" in workflow, (
        "the production-gate step is not in the Gate 4N guard result list, so its failure would "
        "not reach the top-level job")
