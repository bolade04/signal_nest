#!/usr/bin/env python3
"""Verify a gate's artifact persistence contract (Gate 4N-I7, validator finding).

WHY THIS EXISTS. The Gate 4N-I7 persistence contract was assembled by appending to a
manifest with no consistency check of its own. A regenerated artifact appended a SECOND
entry instead of replacing the first, so the contract carried two conflicting sha256 values
for `deny-shadow-matrix.json` — one of them stale by the contract's own definition. The
accompanying SHA256SUMS.txt hid it, because its duplicate line happened to carry the
correct hash both times, so `shasum -c` reported 26/26 OK against 25 files.

That is the defect this whole gate is about, reproduced in the evidence layer: an artifact
whose purpose is to police staleness, silently self-contradictory. The validator lane found
it. A contract that cannot be checked mechanically is not a contract, so it is checked
mechanically here.

Six invariants, all fail-closed:

  I1  no artifact appears twice in the contract
  I2  artifact_count equals the number of entries AND the number of distinct artifacts
  I3  every entry's sha256 matches the file on disk
  I4  every producer hash matches the repository file it names — BOTH the contract's
      top-level `source_hashes` AND each entry's own `producer_hashes`. The first version
      checked only the top level; the validator lane proved a corrupted per-entry hash
      passed clean, so an artifact could name a producer whose recorded hash was wrong.
  5   every file on disk is either in the contract or an allowed unlisted file
      (reviewer verdicts are written after the contract is sealed, by design)
  I6  SHA256SUMS.txt has no duplicate lines and covers exactly the contract's artifacts

Usage:
    python3 scripts/verify_artifacts.py [--dir ~/.signalnest/generated/4n-i7]
Exit: 0 iff every invariant holds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# The CURRENT gate. Superseded gate directories are historical records: their producer
# hashes legitimately no longer match the repository, and asserting otherwise would force
# either rewriting history or freezing the code.
# GATE 4N-I16 DEFECT 4. This was `.../generated/"4n-i8"` with the comment "The CURRENT
# gate" — seven gates stale by the time anyone noticed, and this script is not wired into
# ci.yml either, so nothing surfaced the drift. The directory is now derived from the
# EXPLICIT candidate manifest; there is no gate number in this file.
def _default_dir():
    import candidate_manifest
    try:
        return candidate_manifest.load().artifact_root
    except candidate_manifest.CandidateError:
        return None


DEFAULT_DIR = _default_dir()
CONTRACT_NAME = "artifact-persistence-contract.json"
SUMS_NAME = "SHA256SUMS.txt"

# Written AFTER the contract is sealed. Listing them here is the honest statement that they
# are outside it — not an excuse for arbitrary unlisted files. Anything else on disk is an
# orphan and fails I5. (This list caught GATE-STATUS.txt the moment it was added, which is
# the behaviour it exists for.)
UNLISTED_SUFFIXES = ("-review.txt", "-verdict.txt")
UNLISTED_NAMES = frozenset({
    # The gate's terminal status. Written after review concludes, so by construction it
    # cannot be inside a contract sealed before review began.
    "GATE-STATUS.txt",
})


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(directory: Path) -> dict:
    findings: list[str] = []
    contract_path = directory / CONTRACT_NAME
    if not contract_path.exists():
        return {"clean": False, "findings": [f"{CONTRACT_NAME} missing"]}

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    entries = contract["artifacts"]

    # I1 no duplicates
    counts = Counter(e["artifact"] for e in entries)
    for name, count in sorted(counts.items()):
        if count > 1:
            hashes = sorted({e["sha256"] for e in entries if e["artifact"] == name})
            findings.append(
                f"I1 DUPLICATE: {name} appears {count}x with {len(hashes)} distinct "
                f"hashes {hashes} — at least one is stale by this contract's own definition")

    # I2 counts agree
    if contract.get("artifact_count") != len(entries):
        findings.append(f"I2 artifact_count={contract.get('artifact_count')} but "
                        f"{len(entries)} entries")
    if len(counts) != len(entries):
        findings.append(f"I2 {len(entries)} entries but only {len(counts)} distinct artifacts")

    # I3 entries match disk
    for entry in entries:
        path = directory / entry["artifact"]
        if not path.exists():
            findings.append(f"I3 MISSING: {entry['artifact']} is in the contract but not on disk")
            continue
        actual = sha256(path.read_bytes())
        if actual != entry["sha256"]:
            findings.append(f"I3 STALE: {entry['artifact']} contract={entry['sha256'][:16]}… "
                            f"disk={actual[:16]}…")

    # I4 producer hashes match the repository
    for producer, recorded in sorted(contract.get("source_hashes", {}).items()):
        source = REPO_ROOT / producer
        if not source.exists():
            findings.append(f"I4 MISSING PRODUCER: {producer}")
            continue
        actual = sha256(source.read_bytes())
        if actual != recorded:
            findings.append(f"I4 STALE PRODUCER: {producer} changed since the artifacts were "
                            f"generated (contract={recorded[:16]}… repo={actual[:16]}…) — "
                            f"every artifact it produced must be regenerated")

    # I4b per-entry producer hashes. Found by the signalnest-validator lane: the first
    # version validated only the top-level source_hashes dict, so a corrupted per-entry
    # producer_hashes value passed clean and an artifact could claim provenance it did not
    # have.
    top_level = contract.get("source_hashes", {})
    for entry in entries:
        for producer, recorded in sorted(entry.get("producer_hashes", {}).items()):
            source = REPO_ROOT / producer
            if not source.exists():
                findings.append(f"I4b MISSING PRODUCER: {entry['artifact']} names {producer}, "
                                f"which does not exist")
                continue
            if sha256(source.read_bytes()) != recorded:
                findings.append(f"I4b STALE: {entry['artifact']} records a hash for "
                                f"{producer} that does not match the repository")
            if producer in top_level and top_level[producer] != recorded:
                findings.append(f"I4b INCONSISTENT: {entry['artifact']} and source_hashes "
                                f"disagree about {producer}")
        for producer in entry.get("produced_by", []):
            if producer not in entry.get("producer_hashes", {}):
                findings.append(f"I4b UNHASHED: {entry['artifact']} lists producer "
                                f"{producer} with no recorded hash")

    # I5 no orphan files
    listed = set(counts)
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name in (CONTRACT_NAME, SUMS_NAME):
            continue
        if (path.name in listed or path.name in UNLISTED_NAMES
                or path.name.endswith(UNLISTED_SUFFIXES)):
            continue
        findings.append(f"I5 ORPHAN: {path.name} is on disk but in no contract entry")

    # I6 checksum manifest
    sums_path = directory / SUMS_NAME
    if not sums_path.exists():
        findings.append(f"I6 {SUMS_NAME} missing")
    else:
        lines = [ln for ln in sums_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        names = [ln.split(None, 1)[1].strip() for ln in lines]
        duplicated = sorted({n for n, c in Counter(names).items() if c > 1})
        if duplicated:
            findings.append(f"I6 DUPLICATE LINES in {SUMS_NAME}: {duplicated} — a duplicate "
                            f"line makes `shasum -c` report more OK results than there are files")
        expected = listed | {CONTRACT_NAME}
        if set(names) != expected:
            findings.append(f"I6 COVERAGE: only in {SUMS_NAME}: {sorted(set(names) - expected)}; "
                            f"only in the contract: {sorted(expected - set(names))}")

    return {"clean": not findings, "findings": findings,
            "entries": len(entries), "distinct": len(counts),
            "files_on_disk": sum(1 for p in directory.iterdir() if p.is_file())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = verify(Path(args.dir).expanduser())
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for finding in result["findings"]:
            print(f"  {finding}", file=sys.stderr)
        print(f"  entries={result.get('entries')} distinct={result.get('distinct')} "
              f"files={result.get('files_on_disk')}")
        print("ARTIFACT CONTRACT: clean" if result["clean"] else "ARTIFACT CONTRACT: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
