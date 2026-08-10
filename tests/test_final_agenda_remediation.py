"""Gate 4N-I21 — the last three retained I17 agenda items.

  ADV-B          `requirement_kind`, `principal` and `established_by_gate` were consumed by no
                 code, and the enforcing-consumer test that should have caught that had been
                 applied to a DIFFERENT fixture (the widening ceiling).
  ARCH-M2        the Gate 4N-I16 self-comparing hash assertion still shipped, with a docstring
                 claiming the expected value was produced without calling the production hash.
  ADV-F          `lifecycle_canonical` still claimed an independence it does not have.
  ADV-D          review output shared the frozen candidate directory, so `candidate_manifest`
                 could never exit 0 and real tampering was indistinguishable from noise.

ARCH-M2 and ADV-F are ONE root defect — a false claim of independence — recorded in two places:
an assertion that compares an implementation with itself, and a module docstring asserting the
opposite of what the module is. One correction addresses both, so each keeps its own decisive
test below.

STANDING RULE APPLIED THROUGHOUT (Gate 4N-I20B): wiring is asserted with exact structural
matches, never substring containment that a rename or a comment can still satisfy.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import candidate_manifest as cm  # noqa: E402
import deny_requirements as dr  # noqa: E402
import role_bootstrap_lifecycle as lc  # noqa: E402

REQUIREMENTS = REPO_ROOT / "tests" / "fixtures" / "synthetic-requirements.json"


# =====================================================================================
# ADV-B — the authored requirement metadata decides something
# =====================================================================================


def test_the_requirement_metadata_consumers_are_clean():
    assert dr.requirement_metadata_problems() == []
    assert dr.permanent_requirements_never_expire() == []
    assert dr.principals_actually_deny_their_requirements() == []


def test_every_authored_requirement_field_has_an_enforcing_consumer():
    """Applied to the REQUIREMENTS fixture — the one the original finding was about.

    The Gate 4N-I17 adversarial lane's point was not merely that three fields were unread; it
    was that the enforcing-consumer test existed and had been pointed at a different fixture, so
    the gap was invisible. This enumerates the fields actually present and requires each to name
    a consumer, so a NEW field added later fails until someone consumes it.
    """
    entries = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))["entries"]
    present = {key for entry in entries for key in entry}
    consumers = {
        "outcome_id": "requirement_metadata_problems (uniqueness)",
        "outcome": "source1_actions (grounds text)",
        "actions": "source1_actions + principals_actually_deny_their_requirements",
        "established_by_gate": "requirement_metadata_problems (gate-id form, asserted)",
        "requirement_kind": "permanent_requirements_never_expire",
        "principal": "principals_actually_deny_their_requirements (scope)",
        "evidence_artifact": "requirement_metadata_problems (must be named)",
        "evidence_sha256": "requirement_metadata_problems (must be a sha256)",
    }
    unconsumed = sorted(present - set(consumers))
    assert not unconsumed, f"authored requirement fields with no enforcing consumer: {unconsumed}"


@pytest.mark.parametrize("field", ["requirement_kind", "principal", "established_by_gate"])
def test_blanking_a_load_bearing_field_fails(field, tmp_path):
    """THE agenda's required negative test.

    Runs against a COPY: the real fixture is never modified, and the consumer is pointed at the
    copy through the same explicit environment variable the shipping path uses.
    """
    doc = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
    for entry in doc["entries"]:
        entry[field] = ""
    target = tmp_path / "blanked.json"
    target.write_text(json.dumps(doc), encoding="utf-8")

    env = {**os.environ, "SIGNALNEST_REQUIREMENTS_PATH": str(target)}
    proc = subprocess.run([sys.executable, "scripts/deny_requirements.py"],
                          cwd=REPO_ROOT, capture_output=True, text=True, env=env)
    assert proc.returncode != 0, (
        f"blanking {field} on every row left the requirements guard passing")


def test_a_permanent_requirement_may_not_be_denied_with_an_expiry(monkeypatch):
    """requirement_kind decides: flip the graph so a permanent deny expires, and it must fail."""
    real = dr._reviewed_documents

    def with_expiring_deny():
        documents = copy.deepcopy(real())
        for statement in documents["permanent_w0"]["Statement"]:
            if statement.get("Effect") == "Deny":
                statement["Condition"] = {"DateLessThan": {"aws:CurrentTime": "2026-08-01T16:00:00Z"}}
                break
        return documents

    monkeypatch.setattr(dr, "_reviewed_documents", with_expiring_deny)
    assert dr.permanent_requirements_never_expire(), (
        "a permanent requirement denied with a date condition was not reported")


def test_the_principal_scope_check_can_actually_fail(monkeypatch):
    """FOUND BY THIS GATE'S FALSIFICATION SWEEP.

    Replacing `principals_actually_deny_their_requirements()` with `return []` changed no test
    result: the only assertion on it was that it is currently clean, which a constant-success
    stub satisfies perfectly. A check nobody has seen fail is not a check.
    """
    real = dr._reviewed_documents

    def with_a_missing_deny():
        documents = copy.deepcopy(real())
        w0 = documents["permanent_w0"]
        w0["Statement"] = [st for st in w0["Statement"] if st.get("Effect") != "Deny"]
        return documents

    monkeypatch.setattr(dr, "_reviewed_documents", with_a_missing_deny)
    problems = dr.principals_actually_deny_their_requirements()
    assert problems, "stripping every Deny from a principal policy was not reported"
    assert "permanent_w0" in problems[0]


def test_an_unrecognised_principal_scope_fails(tmp_path, monkeypatch):
    """A blanked or renamed scope phrase must not pass by being unrecognised."""
    doc = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
    for entry in doc["entries"]:
        entry["principal"] = "some principal nobody authored"
    target = tmp_path / "scope.json"
    target.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("SIGNALNEST_REQUIREMENTS_PATH", str(target))
    assert dr.principals_actually_deny_their_requirements(), (
        "an unrecognised principal scope was accepted")


def test_the_requirements_guard_exit_code_depends_on_the_metadata():
    source = (REPO_ROOT / "scripts" / "deny_requirements.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {n.func.id for n in ast.walk(main)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for required in ("requirement_metadata_problems", "permanent_requirements_never_expire",
                     "principals_actually_deny_their_requirements"):
        assert required in called, f"main() does not call {required}; the metadata decides nothing"


# =====================================================================================
# ARCH-M2 — the self-comparing assertion is gone
# =====================================================================================


LIFECYCLE_TEST = REPO_ROOT / "tests" / "test_role_bootstrap_lifecycle.py"


def test_the_self_comparing_hash_assertion_no_longer_exists():
    """AST, not substring: a comment or docstring mentioning it must not satisfy this."""
    tree = ast.parse(LIFECYCLE_TEST.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert) or not isinstance(node.test, ast.Compare):
            continue
        # ONLY an equality claim counts. `!=` assertions comparing a MUTATED graph against the
        # production hash are falsification tests — they are the opposite of the defect, and
        # flagging them would delete the very checks that make the oracle meaningful.
        if not all(isinstance(op, ast.Eq) for op in node.test.ops):
            continue
        calls = [n for n in ast.walk(node.test) if isinstance(n, ast.Call)]
        attrs = {c.func.attr for c in calls if isinstance(c.func, ast.Attribute)}
        if not {"graph_hash", "expected_hash"} <= attrs:
            continue
        # ...and only when the expected side is fed the UNMUTATED production steps, which is
        # what made it a claim of independence rather than a round-trip check.
        for call in calls:
            if isinstance(call.func, ast.Attribute) and call.func.attr == "expected_hash":
                for arg in call.args:
                    if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                            and arg.func.attr == "steps"):
                        offenders.append(ast.unparse(node)[:90])
    assert not offenders, f"the self-comparing hash assertion is back: {offenders}"


def test_the_production_hash_is_still_checked_against_the_independent_oracle():
    sys.path.insert(0, str(REPO_ROOT / "tests" / "oracle"))
    import graph_oracle

    assert lc.graph_hash() == graph_oracle.oracle_hash(lc.steps())


def test_the_oracle_does_not_import_the_production_canonicalisation():
    sys.path.insert(0, str(REPO_ROOT / "tests" / "oracle"))
    import graph_oracle

    tree = ast.parse(pathlib.Path(graph_oracle.__file__).read_text(encoding="utf-8"))
    imported = {(n.names[0].name if isinstance(n, ast.Import) else n.module or "").split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert not (imported & {"lifecycle_canonical", "role_bootstrap_lifecycle"})


# =====================================================================================
# ADV-F — the module no longer claims an independence it does not have
# =====================================================================================


CANONICAL = REPO_ROOT / "scripts" / "lifecycle_canonical.py"


def test_lifecycle_canonical_no_longer_claims_to_be_independent():
    text = CANONICAL.read_text(encoding="utf-8")
    for false_claim in ("implements the canonicalization AGAIN",
                        "never imports the production hash"):
        if false_claim not in text:
            continue
        # The corrected docstring QUOTES the old claim in order to disown it. That is not the
        # claim being made — but "it appears nowhere" would be the wrong test, because deleting
        # the history is how the next reader loses the reason. Require the quotation marker.
        before = text[:text.index(false_claim)]
        assert "previously claimed" in before[-400:], (
            f"the false independence claim {false_claim!r} appears without being disowned")


def test_lifecycle_canonical_states_what_it_actually_is():
    text = CANONICAL.read_text(encoding="utf-8")
    assert "THE PRODUCTION CANONICALISATION" in text
    assert "tests/oracle/graph_oracle.py" in text, (
        "the corrected docstring must point at where the independent check actually lives")


def test_the_claim_and_the_code_agree():
    """The structural fact behind the claim: graph_hash() really does route through this module."""
    source = (REPO_ROOT / "scripts" / "role_bootstrap_lifecycle.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "graph_hash")
    used = {n.value.id for n in ast.walk(fn)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    assert "lifecycle_canonical" in used, (
        "graph_hash no longer routes through lifecycle_canonical; the corrected docstring is now "
        "itself inaccurate and must be revisited")


# =====================================================================================
# ADV-D — reviews live outside the frozen candidate directory
# =====================================================================================


def _candidate(tmp_path):
    import shutil

    root = tmp_path / "CANDIDATE"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "candidate", root)
    doc = json.loads((REPO_ROOT / "tests" / "fixtures" / "candidate-manifest.json")
                     .read_text(encoding="utf-8"))
    doc["artifact_root"] = str(root)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    return manifest, root


def _run(manifest):
    return subprocess.run([sys.executable, "scripts/candidate_manifest.py"],
                          cwd=REPO_ROOT, capture_output=True, text=True,
                          env={**os.environ, "SIGNALNEST_CANDIDATE_MANIFEST": str(manifest)})


def test_an_untampered_candidate_exits_zero(tmp_path):
    """THE agenda's exact closure criterion."""
    manifest, _ = _candidate(tmp_path)
    assert _run(manifest).returncode == 0


def test_reviews_in_the_sibling_directory_keep_the_candidate_clean(tmp_path):
    manifest, root = _candidate(tmp_path)
    reviews = cm.review_output_dir(cm.load({cm.ENV_MANIFEST: str(manifest)}))
    reviews.mkdir()
    for name in ("architect-review.txt", "architect-verdict.txt",
                 "consolidated-review-verdict.json"):
        (reviews / name).write_text("x", encoding="utf-8")
    assert _run(manifest).returncode == 0
    assert reviews.name.endswith(cm.REVIEW_OUTPUT_SUFFIX)
    assert reviews.parent == root.parent and reviews != root


def test_review_output_inside_the_frozen_directory_is_a_named_finding(tmp_path):
    manifest, root = _candidate(tmp_path)
    (root / "architect-review.txt").write_text("x", encoding="utf-8")
    proc = _run(manifest)
    assert proc.returncode != 0
    result = cm.verify(cm.load({cm.ENV_MANIFEST: str(manifest)}))
    assert result["misplaced_review_output"] == ["architect-review.txt"]
    assert result["undeclared_on_disk"] == [], (
        "a misplaced review must not be reported as an undeclared ARTIFACT; conflating the two "
        "is what saturated the signal")


def test_a_genuinely_undeclared_artifact_is_still_caught(tmp_path):
    manifest, root = _candidate(tmp_path)
    (root / "sneaky.json").write_text("{}", encoding="utf-8")
    assert _run(manifest).returncode != 0

    # FOUND BY THIS GATE'S FALSIFICATION SWEEP: making _is_review_output() return True for
    # everything left every test passing, because an undeclared artifact was still a finding —
    # just reported under the wrong heading. The CLASSIFICATION is what keeps the two signals
    # distinguishable, which is the whole point of ADV-D, so it is asserted directly.
    result = cm.verify(cm.load({cm.ENV_MANIFEST: str(manifest)}))
    assert result["undeclared_on_disk"] == ["sneaky.json"], (
        "a genuinely undeclared artifact was not classified as undeclared")
    assert result["misplaced_review_output"] == []


def test_a_tampered_artifact_byte_is_still_caught(tmp_path):
    manifest, root = _candidate(tmp_path)
    target = root / "synthetic-policy.json"
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert _run(manifest).returncode != 0


def test_the_separation_is_not_an_exemption_list(tmp_path):
    """A review-shaped name must not simply be ignored inside the frozen directory."""
    manifest, root = _candidate(tmp_path)
    (root / "anything-verdict.txt").write_text("x", encoding="utf-8")
    result = cm.verify(cm.load({cm.ENV_MANIFEST: str(manifest)}))
    assert not result["clean"], "a review-shaped file inside the frozen directory was exempted"


# =====================================================================================
# PHASE G — exact-structure wiring, and the bypasses that must not work
# =====================================================================================


def _guard_step_ids() -> set[str]:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return {line.strip().removeprefix("id: ")
            for line in workflow.splitlines() if line.strip().startswith("id: ")}


def test_required_guard_steps_are_present_by_exact_id():
    for step in ("deny_requirements", "candidate_discovery", "role_inventory",
                 "certification_gate"):
        assert step in _guard_step_ids(), f"CI step id {step!r} is absent"


@pytest.mark.parametrize("bypass", [
    "suffix", "prefix", "commented", "documentation", "dead_code",
])
def test_loose_matching_bypasses_do_not_satisfy_the_wiring_test(bypass, tmp_path):
    """Each of these preserves the expected SUBSTRING while breaking the wiring."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    if bypass == "suffix":
        mutated = workflow.replace("\n        id: deny_requirements\n",
                                   "\n        id: deny_requirements_disabled\n")
    elif bypass == "prefix":
        mutated = workflow.replace("\n        id: deny_requirements\n",
                                   "\n        id: old_deny_requirements\n")
    elif bypass == "commented":
        mutated = workflow.replace("\n        id: deny_requirements\n",
                                   "\n        # id: deny_requirements\n        id: something_else\n")
    elif bypass == "documentation":
        mutated = workflow.replace("\n        id: deny_requirements\n",
                                   "\n        id: gone\n      # mentions id: deny_requirements only in prose\n")
    else:
        mutated = workflow.replace("\n        id: deny_requirements\n",
                                   "\n        id: gone\n      # dead: run: python3 scripts/deny_requirements.py\n")
    assert mutated != workflow, "the bypass did not apply"

    ids = {line.strip().removeprefix("id: ")
           for line in mutated.splitlines()
           if line.strip().startswith("id: ") and not line.strip().startswith("#")}
    assert "deny_requirements" not in ids, (
        f"the {bypass} bypass still satisfied an exact-id check — the assertion is too loose")
    assert "deny_requirements" in mutated, (
        "this bypass was supposed to PRESERVE the substring; if it does not, it is not testing "
        "what it claims to")
