"""Current-candidate discovery and byte targeting (Gate 4N-I16, Defect 4, Phases L/M/N).

WHAT WENT WRONG. Two artifact checkers named their target with a constant:
`tests/test_stamped_artifact_bytes.py` pointed at `4n-i10` and `scripts/verify_artifacts.py`
at `4n-i8`. Both directories still existed, so neither skipped and neither failed; the
byte-level suite passed 24 assertions about a superseded candidate while a repository-wide
search for the gate under review returned nothing. Green meant "the old thing is still
fine", which is indistinguishable from "the new thing was checked".

WHAT THESE TESTS DO. They exercise the discovery CONTRACT rather than any particular
candidate: that a candidate must be named explicitly, that every declared artifact is
byte-verified, that an undeclared artifact is a finding, and — the part that would have
caught the original defect — that pointing the manifest at the WRONG candidate FAILS.

The mutations in the second half are the load-bearing half. Each one takes a valid manifest,
breaks exactly one thing, and requires the contract to reject it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import candidate_manifest as cm  # noqa: E402

FIXTURE_MANIFEST = REPO_ROOT / "tests" / "fixtures" / "candidate-manifest.json"


def _env(path) -> dict:
    return {cm.ENV_MANIFEST: str(path)}


@pytest.fixture()
def sandbox(tmp_path):
    """A writable copy of the tracked fixture candidate, so mutations touch no real file."""
    root = tmp_path / "candidate"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "candidate", root)
    doc = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    doc["artifact_root"] = str(root)
    manifest = tmp_path / "candidate-manifest.json"
    manifest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return manifest, root, doc


def _write(manifest: Path, doc: dict) -> Path:
    manifest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return manifest


# =====================================================================================
# PHASE M — the current candidate's exact bytes
# =====================================================================================


def test_the_tracked_fixture_manifest_resolves_and_verifies():
    candidate = cm.load(_env(FIXTURE_MANIFEST))
    result = cm.verify(candidate)
    assert result["clean"], result["problems"]
    assert result["declared"] == result["verified"] == 4


def test_the_fixture_declares_every_required_role():
    candidate = cm.load(_env(FIXTURE_MANIFEST))
    roles = {spec["role"] for spec in candidate.artifacts.values()}
    for required in ("policy", "lifecycle", "provenance"):
        assert required in roles


def test_a_synthetic_candidate_cannot_certify_production():
    candidate = cm.load(_env(FIXTURE_MANIFEST))
    assert candidate.candidate_id.startswith("SYNTHETIC-")
    assert candidate.certifies_production is False


def test_every_declared_artifact_is_byte_verified_not_merely_present(sandbox):
    manifest, root, doc = sandbox
    # Present but with different bytes: presence alone must not satisfy the contract.
    (root / "synthetic-policy.json").write_text('{"tampered": true}\n', encoding="utf-8")
    result = cm.verify(cm.load(_env(manifest)))
    assert not result["clean"]
    assert any("hash mismatch" in p for p in result["problems"])


def test_the_candidate_id_is_reported_so_a_reviewer_can_see_which_one_ran():
    candidate = cm.load(_env(FIXTURE_MANIFEST))
    assert candidate.redacted()["candidate_id"] == "SYNTHETIC-FIXTURE-1"


# =====================================================================================
# PHASE N — stale-candidate and mis-targeting mutations. Every one must FAIL.
# =====================================================================================


def test_n1_an_unset_manifest_variable_is_an_error_not_a_guess():
    with pytest.raises(cm.CandidateError) as exc:
        cm.load({})
    assert "not set" in str(exc.value)


def test_n2_pointing_at_a_superseded_candidate_directory_fails(sandbox, tmp_path):
    """THE ORIGINAL DEFECT: a manifest aimed at an older gate's artifacts."""
    manifest, root, doc = sandbox
    stale = tmp_path / "4n-i10"
    stale.mkdir()
    (stale / "synthetic-policy.json").write_text("{}\n", encoding="utf-8")
    doc["artifact_root"] = str(stale)
    result = cm.verify(cm.load(_env(_write(manifest, doc))))
    assert not result["clean"]


def test_n3_a_manifest_whose_id_says_i15_while_artifacts_are_i16_still_byte_checks(sandbox):
    """Changing ONLY the id must not change what is verified.

    The id is a label; the hashes are the evidence. A gate that trusted the id would accept
    relabelled bytes, so this asserts the byte check is independent of the name.
    """
    manifest, root, doc = sandbox
    doc["candidate_id"] = "4N-I15-CANDIDATE-1"
    doc["certifies_production"] = False
    candidate = cm.load(_env(_write(manifest, doc)))
    assert candidate.candidate_id == "4N-I15-CANDIDATE-1"
    assert cm.verify(candidate)["clean"]           # bytes still match
    (root / "synthetic-lifecycle.json").write_text("{}\n", encoding="utf-8")
    assert not cm.verify(cm.load(_env(manifest)))["clean"]   # and still catch a change


def test_n4_an_artifact_root_that_does_not_exist_fails(sandbox, tmp_path):
    manifest, root, doc = sandbox
    doc["artifact_root"] = str(tmp_path / "does-not-exist")
    with pytest.raises(cm.CandidateError):
        cm.load(_env(_write(manifest, doc)))


def test_n5_replacing_one_policy_with_stale_bytes_fails(sandbox):
    manifest, root, doc = sandbox
    (root / "synthetic-policy.json").write_text(
        '{"Version": "2012-10-17", "Statement": []}\n', encoding="utf-8")
    assert not cm.verify(cm.load(_env(manifest)))["clean"]


def test_n6_replacing_the_provenance_file_fails(sandbox):
    manifest, root, doc = sandbox
    (root / "synthetic-provenance.json").write_text('{"records": 99}\n', encoding="utf-8")
    assert not cm.verify(cm.load(_env(manifest)))["clean"]


def test_n7_deleting_the_lifecycle_artifact_fails(sandbox):
    manifest, root, doc = sandbox
    (root / "synthetic-lifecycle.json").unlink()
    result = cm.verify(cm.load(_env(manifest)))
    assert not result["clean"]
    assert any("MISSING" in p for p in result["problems"])


def test_n8_an_undeclared_artifact_appearing_in_the_candidate_fails(sandbox):
    """A file nobody declared is a file nobody reviewed."""
    manifest, root, doc = sandbox
    (root / "surprise-policy.json").write_text('{"extra": true}\n', encoding="utf-8")
    result = cm.verify(cm.load(_env(manifest)))
    assert not result["clean"]
    assert any("NOT declared" in p for p in result["problems"])


def test_n9_a_latest_symlink_is_refused(sandbox, tmp_path):
    manifest, root, doc = sandbox
    link = tmp_path / "latest-candidate-manifest.json"
    link.symlink_to(manifest)
    with pytest.raises(cm.CandidateError) as exc:
        cm.load(_env(link))
    assert "latest" in str(exc.value).lower() or "SYMLINK" in str(exc.value)


def test_n10_a_moving_pointer_in_the_artifact_root_is_refused(sandbox, tmp_path):
    manifest, root, doc = sandbox
    moving = tmp_path / "generated" / "latest"
    moving.mkdir(parents=True)
    doc["artifact_root"] = str(moving)
    with pytest.raises(cm.CandidateError) as exc:
        cm.load(_env(_write(manifest, doc)))
    assert "moving pointer" in str(exc.value)


def test_n11_a_relative_manifest_path_is_refused():
    with pytest.raises(cm.CandidateError) as exc:
        cm.load({cm.ENV_MANIFEST: "tests/fixtures/candidate-manifest.json"})
    assert "absolute" in str(exc.value)


def test_n12_a_synthetic_manifest_claiming_production_is_refused(sandbox):
    manifest, root, doc = sandbox
    doc["certifies_production"] = True
    with pytest.raises(cm.CandidateError) as exc:
        cm.load(_env(_write(manifest, doc)))
    assert "never bless a real candidate" in str(exc.value)


def test_n13_an_artifact_without_a_declared_hash_is_refused(sandbox):
    manifest, root, doc = sandbox
    doc["artifacts"]["synthetic-policy.json"] = {"role": "policy"}
    with pytest.raises(cm.CandidateError) as exc:
        cm.load(_env(_write(manifest, doc)))
    assert "no expected sha256" in str(exc.value)


def test_n14_a_missing_required_role_is_a_finding(sandbox):
    manifest, root, doc = sandbox
    doc["artifacts"]["synthetic-lifecycle.json"]["role"] = "other"
    result = cm.verify(cm.load(_env(_write(manifest, doc))))
    assert not result["clean"]
    assert any("role 'lifecycle'" in p for p in result["problems"])


def test_n15_a_malformed_candidate_id_is_refused(sandbox):
    manifest, root, doc = sandbox
    doc["candidate_id"] = "TBD"
    with pytest.raises(cm.CandidateError):
        cm.load(_env(_write(manifest, doc)))


# =====================================================================================
# The discovery path must contain NO gate-number constant. This is the regression guard
# for the defect itself rather than for any one symptom of it.
# =====================================================================================


def test_no_artifact_checker_resolves_its_target_from_a_gate_constant():
    """AST, not regex — and a PRECISE rule rather than a keyword ban.

    The first draft of this test was a line-level regex for a `4n-i<n>` literal. It
    immediately flagged its own mutation fixture on the line `stale = tmp_path / "4n-i10"`,
    which is a legitimate use: that line exists to BUILD a wrong target so the contract can
    reject it. This gate chain has now produced a regex scanner that flags its own rule
    declaration five separate times, so the remedy is the established one — parse, and state
    the rule you actually mean.

    THE RULE ACTUALLY MEANT: no module may RESOLVE A CANDIDATE PATH from a gate constant.
    A gate literal is an offender only when the same expression also reaches into the
    artifact store (`.signalnest`, `generated`, or a home directory). A gate literal joined
    to a pytest tmp_path is not resolving anything.
    """
    import ast
    import re

    gate_literal = re.compile(r"^4n-i\d+$", re.IGNORECASE)
    store_marker = re.compile(r"\.signalnest|generated|home\(\)")
    offenders = []

    for path in sorted((REPO_ROOT / "scripts").glob("*.py")) + \
            sorted((REPO_ROOT / "tests").glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                     # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Call, ast.BinOp)):
                continue
            source = ast.unparse(node)
            literals = [n.value for n in ast.walk(node)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and gate_literal.match(n.value)]
            if literals and store_marker.search(source):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {source[:100]}")

    assert not offenders, ("a candidate artifact path is resolved from a gate constant "
                           "again:\n  " + "\n  ".join(sorted(set(offenders))))
