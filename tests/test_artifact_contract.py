"""The artifact persistence contract must be mechanically checkable (Gate 4N-I7).

WHY THIS FILE EXISTS. The signalnest-validator lane found that the Gate 4N-I7 persistence
contract carried TWO conflicting sha256 values for `deny-shadow-matrix.json`. The manifest
was assembled by appending, so regenerating an artifact appended a second entry instead of
replacing the first, and one of the two was stale by the contract's own definition.
`SHA256SUMS.txt` concealed it: its duplicate line carried the correct hash both times, so
`shasum -c` reported 26 OK against 25 files.

That is this gate's own defect class reproduced in the evidence layer — the artifact whose
job is to police staleness, silently self-contradictory, with the obvious check passing.

`scripts/verify_artifacts.py` now enforces six fail-closed invariants. These tests build
synthetic contracts and require each invariant to actually FIRE, because a checker nobody
has seen fail is not a checker. They use only temporary directories, so they run anywhere
and do not depend on ~/.signalnest existing.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_artifacts  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(directory: Path, artifacts: dict[str, str], *, entries=None, sums=None,
          source_hashes=None, artifact_count=None) -> Path:
    """Write a synthetic gate directory. Defaults produce a VALID contract."""
    for name, body in artifacts.items():
        (directory / name).write_text(body)

    if entries is None:
        entries = [{"artifact": n, "sha256": sha(b.encode()), "produced_by": [],
                    "producer_hashes": {}} for n, b in sorted(artifacts.items())]
    contract = {
        "gate": "synthetic",
        "artifact_count": len(entries) if artifact_count is None else artifact_count,
        "source_hashes": source_hashes or {},
        "artifacts": entries,
    }
    text = json.dumps(contract, indent=2) + "\n"
    (directory / "artifact-persistence-contract.json").write_text(text)

    if sums is None:
        sums = [f"{sha(b.encode())}  {n}" for n, b in sorted(artifacts.items())]
        sums.append(f"{sha(text.encode())}  artifact-persistence-contract.json")
    (directory / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n")
    return directory


def test_a_correct_contract_is_clean(tmp_path):
    """Positive control. A checker that fails everything proves nothing."""
    build(tmp_path, {"a.json": "{}\n", "b.json": "[]\n"})
    result = verify_artifacts.verify(tmp_path)
    assert result["clean"], result["findings"]


def test_I1_a_duplicate_entry_is_caught(tmp_path):
    """THE defect the validator found, reproduced exactly."""
    artifacts = {"a.json": "{}\n"}
    entries = [
        {"artifact": "a.json", "sha256": sha(b"STALE"), "produced_by": [], "producer_hashes": {}},
        {"artifact": "a.json", "sha256": sha(b"{}\n"), "produced_by": [], "producer_hashes": {}},
    ]
    build(tmp_path, artifacts, entries=entries)
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I1 DUPLICATE") for f in result["findings"]), result["findings"]


def test_I1_fires_even_when_one_of_the_duplicates_is_correct(tmp_path):
    """The real case: `shasum -c` was green because a correct hash appeared twice."""
    artifacts = {"a.json": "{}\n"}
    good = {"artifact": "a.json", "sha256": sha(b"{}\n"), "produced_by": [], "producer_hashes": {}}
    build(tmp_path, artifacts, entries=[good, dict(good)])
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any("I1 DUPLICATE" in f for f in result["findings"])


def test_I2_a_wrong_artifact_count_is_caught(tmp_path):
    build(tmp_path, {"a.json": "{}\n"}, artifact_count=99)
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I2") for f in result["findings"])


def test_I3_an_edited_artifact_is_reported_stale(tmp_path):
    build(tmp_path, {"a.json": "{}\n"})
    (tmp_path / "a.json").write_text('{"edited": true}\n')
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I3 STALE") for f in result["findings"])


def test_I3_a_missing_artifact_is_caught(tmp_path):
    build(tmp_path, {"a.json": "{}\n"})
    (tmp_path / "a.json").unlink()
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I3 MISSING") for f in result["findings"])


def test_I4_a_changed_producer_invalidates_the_artifacts(tmp_path):
    """The whole point of recording producer hashes."""
    build(tmp_path, {"a.json": "{}\n"},
          source_hashes={"scripts/allow_model.py": sha(b"not the real file")})
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I4 STALE PRODUCER") for f in result["findings"])


def test_I4_passes_against_the_real_repository_files(tmp_path):
    real = REPO_ROOT / "scripts/allow_model.py"
    build(tmp_path, {"a.json": "{}\n"},
          source_hashes={"scripts/allow_model.py": sha(real.read_bytes())})
    assert verify_artifacts.verify(tmp_path)["clean"]


def test_I5_an_orphan_file_is_caught(tmp_path):
    build(tmp_path, {"a.json": "{}\n"})
    (tmp_path / "undeclared.json").write_text("{}\n")
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I5 ORPHAN") for f in result["findings"])


def test_I5_allows_review_and_verdict_files(tmp_path):
    """They are written by the review lanes AFTER the contract is sealed, by design."""
    build(tmp_path, {"a.json": "{}\n"})
    (tmp_path / "architect-review.txt").write_text("findings\n")
    (tmp_path / "architect-verdict.txt").write_text("PASS\n")
    assert verify_artifacts.verify(tmp_path)["clean"]


def test_I6_a_duplicate_checksum_line_is_caught(tmp_path):
    """`shasum -c` reports one OK per LINE, so a duplicate line inflates the count."""
    artifacts = {"a.json": "{}\n"}
    directory = build(tmp_path, artifacts)
    lines = (directory / "SHA256SUMS.txt").read_text().splitlines()
    (directory / "SHA256SUMS.txt").write_text("\n".join(lines + [lines[0]]) + "\n")
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I6 DUPLICATE LINES") for f in result["findings"])


def test_I6_a_checksum_manifest_missing_an_artifact_is_caught(tmp_path):
    artifacts = {"a.json": "{}\n", "b.json": "[]\n"}
    directory = build(tmp_path, artifacts)
    lines = [ln for ln in (directory / "SHA256SUMS.txt").read_text().splitlines()
             if "b.json" not in ln]
    (directory / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n")
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I6 COVERAGE") for f in result["findings"])


def test_a_missing_contract_is_not_silently_clean(tmp_path):
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]


def test_the_real_gate_directory_is_clean_if_it_exists():
    """Checks the actual evidence when it is present; skips in CI, where it is not.

    GATE 4N-I16 DEFECT 4: DEFAULT_DIR is now resolved from the explicit candidate manifest
    rather than a hard-coded gate number, so it is None when no candidate is declared. That
    is the correct fail-closed shape — but it must SKIP rather than raise, or the absence of
    a declared candidate would read as an infrastructure error instead of what it is.
    """
    directory = verify_artifacts.DEFAULT_DIR
    if directory is None:
        pytest.skip("no candidate declared via SIGNALNEST_CANDIDATE_MANIFEST")
    if not (directory / verify_artifacts.CONTRACT_NAME).exists():
        pytest.skip("gate artifact directory not present (expected in CI)")
    result = verify_artifacts.verify(directory)
    assert result["clean"], result["findings"]


# --- I4b: per-entry producer hashes (found by the signalnest-validator lane) -----------
#
# The first version of the checker validated only the contract's TOP-LEVEL `source_hashes`.
# The validator built a synthetic contract with a corrupted per-entry `producer_hashes`
# value and the checker reported it clean — so an artifact could claim provenance it did not
# have. These tests exist because that gap was demonstrated, not theorised.


def test_I4b_a_corrupted_per_entry_producer_hash_is_caught(tmp_path):
    entries = [{"artifact": "a.json", "sha256": sha(b"{}\n"),
                "produced_by": ["scripts/allow_model.py"],
                "producer_hashes": {"scripts/allow_model.py": sha(b"WRONG")}}]
    build(tmp_path, {"a.json": "{}\n"}, entries=entries)
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I4b STALE") for f in result["findings"]), result["findings"]


def test_I4b_a_producer_listed_without_a_hash_is_caught(tmp_path):
    entries = [{"artifact": "a.json", "sha256": sha(b"{}\n"),
                "produced_by": ["scripts/allow_model.py"], "producer_hashes": {}}]
    build(tmp_path, {"a.json": "{}\n"}, entries=entries)
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any(f.startswith("I4b UNHASHED") for f in result["findings"])


def test_I4b_disagreement_with_source_hashes_is_caught(tmp_path):
    real = sha((REPO_ROOT / "scripts/allow_model.py").read_bytes())
    entries = [{"artifact": "a.json", "sha256": sha(b"{}\n"),
                "produced_by": ["scripts/allow_model.py"],
                "producer_hashes": {"scripts/allow_model.py": real}}]
    build(tmp_path, {"a.json": "{}\n"}, entries=entries,
          source_hashes={"scripts/allow_model.py": sha(b"DIFFERENT")})
    result = verify_artifacts.verify(tmp_path)
    assert not result["clean"]
    assert any("I4b INCONSISTENT" in f or "I4 STALE PRODUCER" in f for f in result["findings"])


def test_I4b_a_correct_per_entry_producer_hash_is_clean(tmp_path):
    real = sha((REPO_ROOT / "scripts/allow_model.py").read_bytes())
    entries = [{"artifact": "a.json", "sha256": sha(b"{}\n"),
                "produced_by": ["scripts/allow_model.py"],
                "producer_hashes": {"scripts/allow_model.py": real}}]
    build(tmp_path, {"a.json": "{}\n"}, entries=entries,
          source_hashes={"scripts/allow_model.py": real})
    assert verify_artifacts.verify(tmp_path)["clean"]


def test_a_post_seal_status_file_is_allowed_but_arbitrary_files_are_not(tmp_path):
    """GATE-STATUS.txt is declared; anything else on disk must still fail I5."""
    build(tmp_path, {"a.json": "{}\n"})
    (tmp_path / "GATE-STATUS.txt").write_text("REMEDIATION REQUIRED\n")
    assert verify_artifacts.verify(tmp_path)["clean"]
    (tmp_path / "notes.txt").write_text("scratch\n")
    assert not verify_artifacts.verify(tmp_path)["clean"]
