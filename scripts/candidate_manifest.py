#!/usr/bin/env python3
"""Explicit frozen-candidate discovery (Gate 4N-I16, Defect 4).

THE DEFECT. `tests/test_stamped_artifact_bytes.py` hard-coded
`~/.signalnest/generated/4n-i10`, and `scripts/verify_artifacts.py` hard-coded `4n-i8`. Both
paths still existed on the developer host, so neither skipped: the byte-level suite reported
24 passing assertions about a candidate five gates old, and a repository-wide search for the
gate under review returned nothing at all. The suite whose stated purpose was "load the exact
bytes reviewers will inspect" was loading bytes reviewers inspected two gates earlier.

A CHECK WHOSE TARGET IS A CONSTANT SILENTLY OUTLIVES THE OBJECT IT WAS WRITTEN TO CHECK.
That is the single sentence this module exists to make untrue.

THE RULE. There is exactly ONE way to name the candidate under test: the environment
variable SIGNALNEST_CANDIDATE_MANIFEST, pointing at a manifest that names its own candidate
ID, artifact root, and the expected hash of every artifact. There is:
  * no default;
  * no fallback to a home directory;
  * no "most recent directory" scan;
  * no "latest" symlink;
  * no gate-number constant anywhere in the discovery path.
An unset variable is an ERROR, not an invitation to guess. Guessing is what produced the
defect: a wrong guess and a right guess are indistinguishable to a green test.

TIERS. A manifest declares `certifies_production`. A synthetic fixture manifest must set it
false and is refused if it claims otherwise, so the tracked CI fixture can exercise the
MECHANISM without ever blessing a real candidate.

Usage:
    SIGNALNEST_CANDIDATE_MANIFEST=<path> python3 scripts/candidate_manifest.py [--json]
Exit: 0 iff the manifest resolves and every declared artifact matches its expected hash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_MANIFEST = "SIGNALNEST_CANDIDATE_MANIFEST"

REQUIRED_KEYS = ("candidate_id", "artifact_root", "prefreeze_manifest",
                 "certifies_production", "artifacts")
# GATE 4N-I24C, finding I24C-02. The I23 manifest declared role "evidence" for 31 of its
# 35 artifacts; the vocabulary did not contain it, so load() raised before verify() was
# ever reached and the frozen candidate could not be read by its own verifier (exit 2).
# "evidence" is a genuine role for gate artifacts, so the vocabulary was wrong, not the
# producer. Widening it is the fix at the SOURCE; the round-trip contract below is what
# stops a producer/loader disagreement recurring for any future role.
ARTIFACT_ROLES = ("policy", "lifecycle", "provenance", "manifest", "evidence", "other")

# A candidate id must be explicit and gate-scoped. The pattern is deliberately strict so a
# placeholder cannot masquerade as a real candidate.
import re  # noqa: E402

_CANDIDATE_RE = re.compile(r"^4N-I\d+-CANDIDATE-\d+$|^SYNTHETIC-[A-Z0-9-]+$")


class CandidateError(Exception):
    """Fail-closed. Never downgraded to a warning, never satisfied by a fallback."""


@dataclass(frozen=True)
class Candidate:
    manifest_path: Path
    candidate_id: str
    artifact_root: Path
    prefreeze_manifest: str
    certifies_production: bool
    artifacts: dict           # relative name -> {"sha256": ..., "role": ...}

    def path(self, name: str) -> Path:
        return self.artifact_root / name

    def redacted(self) -> dict:
        return {"candidate_id": self.candidate_id,
                "artifact_count": len(self.artifacts),
                "certifies_production": self.certifies_production,
                "prefreeze_manifest": self.prefreeze_manifest}


def _reject_latest(value: str, what: str) -> None:
    """'latest' is a moving target, which is the whole defect in one word."""
    lowered = str(value).lower()
    for token in ("latest", "current", "newest"):
        if token in lowered.split("/")[-1] or f"/{token}" in lowered:
            raise CandidateError(
                f"{what} contains {token!r}: a moving pointer cannot identify a frozen "
                "candidate. Name the candidate explicitly.")


def load(env: dict | None = None) -> Candidate:
    env = os.environ if env is None else env
    raw_path = env.get(ENV_MANIFEST)
    if not raw_path:
        raise CandidateError(
            f"{ENV_MANIFEST} is not set. The candidate under test must be named EXPLICITLY; "
            "there is no default and no discovery. Gate 4N-I15 shipped tests pointed at a "
            "candidate five gates old precisely because a constant stood in for this.")
    _reject_latest(raw_path, ENV_MANIFEST)

    manifest_path = Path(raw_path)
    if not manifest_path.is_absolute():
        raise CandidateError(f"{ENV_MANIFEST} must be an absolute path, got {raw_path!r}")
    if manifest_path.is_symlink():
        raise CandidateError(f"{ENV_MANIFEST} points at a SYMLINK: {manifest_path}. A frozen "
                             "candidate must be named directly, not through an indirection "
                             "that can be repointed after review.")
    if not manifest_path.exists():
        raise CandidateError(f"{ENV_MANIFEST} points at a missing file: {manifest_path}")

    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CandidateError(f"candidate manifest is not valid JSON: {exc}") from exc

    missing = [k for k in REQUIRED_KEYS if k not in doc]
    if missing:
        raise CandidateError(f"candidate manifest is missing required keys: {missing}")

    candidate_id = str(doc["candidate_id"])
    if not _CANDIDATE_RE.match(candidate_id):
        raise CandidateError(
            f"candidate_id {candidate_id!r} does not match the required form "
            "'4N-I<n>-CANDIDATE-<n>' (or 'SYNTHETIC-...' for a non-certifying fixture)")

    root = Path(doc["artifact_root"])
    if not root.is_absolute():
        root = (manifest_path.parent / root).resolve()
    _reject_latest(str(root), "artifact_root")
    if not root.is_dir():
        raise CandidateError(f"artifact_root is not a directory: {root}")

    artifacts = doc["artifacts"]
    if not isinstance(artifacts, dict) or not artifacts:
        raise CandidateError("the manifest declares no artifacts")
    for name, spec in artifacts.items():
        if not isinstance(spec, dict) or "sha256" not in spec:
            raise CandidateError(f"artifact {name!r} declares no expected sha256")
        if spec.get("role") not in ARTIFACT_ROLES:
            raise CandidateError(
                f"artifact {name!r} declares role {spec.get('role')!r}; expected one of "
                f"{ARTIFACT_ROLES}")

    certifies = bool(doc["certifies_production"])
    if candidate_id.startswith("SYNTHETIC-") and certifies:
        raise CandidateError(
            "a SYNTHETIC candidate manifest claimed production certification. A tracked "
            "fixture validates the MECHANISM and must never bless a real candidate.")

    return Candidate(manifest_path=manifest_path, candidate_id=candidate_id,
                     artifact_root=root, prefreeze_manifest=str(doc["prefreeze_manifest"]),
                     certifies_production=certifies, artifacts=artifacts)



# --- GATE 4N-I21, ADV-D: reviews live in a SEPARATE directory --------------------------------
#
# THE DEFECT. Post-freeze review artifacts (`*-review.txt`, `*-verdict.txt`, the consolidated
# verdict) were written INTO the frozen candidate directory. `verify()` reports any file on disk
# that the manifest does not declare, so a reviewed candidate could never exit 0 — and once the
# signal is permanently saturated, real tampering is indistinguishable from expected noise and
# exit 0 can never be a gate criterion. Gate 4N-I17's adversarial lane recorded exactly that.
#
# The correction is separation, not an exemption list: reviews go to a SIBLING directory, and a
# review-shaped file found inside the frozen directory is now itself a finding. An exemption
# list would have kept the two mixed and made the exemption the new blind spot.

REVIEW_OUTPUT_SUFFIX = "-reviews"

# Review outputs, by shape. Used only to DETECT misplacement — never to excuse it.
REVIEW_FILE_SUFFIXES = ("-review.txt", "-verdict.txt")
REVIEW_FILE_NAMES = frozenset({"consolidated-review-verdict.json", "GATE-STATUS.txt",
                               "SHA256SUMS-AUTHORITATIVE-FINAL.txt"})


def review_output_dir(candidate) -> Path:
    """The directory reviews MUST be written to: a sibling of the frozen artifact root."""
    root = candidate.artifact_root if hasattr(candidate, "artifact_root") else Path(candidate)
    return root.parent / f"{root.name}{REVIEW_OUTPUT_SUFFIX}"


def _is_review_output(name: str) -> bool:
    return name in REVIEW_FILE_NAMES or name.endswith(REVIEW_FILE_SUFFIXES)


def verify(candidate: Candidate) -> dict:
    """Every declared artifact must exist and hash EXACTLY. No artifact may be undeclared."""
    problems: list[str] = []
    checked = []
    for name, spec in sorted(candidate.artifacts.items()):
        target = candidate.path(name)
        if not target.exists():
            problems.append(f"{name}: declared by the manifest but MISSING from the candidate")
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != spec["sha256"]:
            problems.append(f"{name}: hash mismatch — expected {spec['sha256'][:16]}…, "
                            f"found {digest[:16]}…")
        checked.append({"name": name, "role": spec["role"], "sha256": digest,
                        "matches": digest == spec["sha256"]})

    declared = set(candidate.artifacts)
    on_disk = {p.name for p in candidate.artifact_root.iterdir() if p.is_file()}

    # GATE 4N-I23, closing the I22 ADV-D qualification (finding F3, raised independently by
    # the security and adversarial lanes). In the DEPLOYED layout the manifest lives INSIDE
    # its own artifact_root, so it appeared in `on_disk`, could never appear in `declared`
    # (a manifest cannot contain its own digest), and was reported as an undeclared artifact
    # forever. Clean, review-contaminated and genuinely tampered candidates therefore ALL
    # exited non-zero and only the message text distinguished them — the exact ADV-D failure
    # mode, reinstated at the exit-code level.
    #
    # The I21 closure test missed it because its fixture put the manifest OUTSIDE the
    # candidate directory, so the test layout differed from production in precisely the
    # load-bearing way. This excludes the manifest by IDENTITY (same resolved path), not by
    # name pattern — a name pattern would exempt any file someone chose to call a manifest.
    manifest_path = getattr(candidate, "manifest_path", None)
    self_named = None
    if manifest_path is not None:
        try:
            mp = Path(manifest_path).resolve()
            if mp.parent == candidate.artifact_root.resolve():
                self_named = mp.name
        except OSError:
            self_named = None
    if self_named:
        on_disk = on_disk - {self_named}
    undeclared = sorted(on_disk - declared)

    # GATE 4N-I21, ADV-D. Review output belongs in the sibling directory. Naming the violation
    # separately is what keeps the undeclared-artifact signal meaningful: a reviewed candidate
    # now exits 0, and a genuinely undeclared artifact is still a finding.
    misplaced = sorted(n for n in undeclared if _is_review_output(n))
    undeclared = [n for n in undeclared if not _is_review_output(n)]

    # An artifact present in the candidate but absent from the manifest is not neutral: the
    # manifest is what reviewers are handed, so an undeclared file is a file nobody reviewed.
    if undeclared:
        problems.append(f"artifacts present but NOT declared in the manifest: {undeclared}")
    if misplaced:
        problems.append(
            f"review output written INTO the frozen candidate directory: {misplaced}. Reviews "
            f"belong in {review_output_dir(candidate).name}; mixing them saturates the "
            "undeclared-artifact signal so real tampering cannot be distinguished from noise.")

    roles = {spec["role"] for spec in candidate.artifacts.values()}
    for required_role in ("policy", "lifecycle", "provenance"):
        if required_role not in roles:
            problems.append(f"the manifest declares no artifact with role {required_role!r}")

    return {
        "candidate_id": candidate.candidate_id,
        "artifact_root": str(candidate.artifact_root),
        "certifies_production": candidate.certifies_production,
        "declared": len(declared), "verified": sum(1 for c in checked if c["matches"]),
        "undeclared_on_disk": undeclared,
        "misplaced_review_output": misplaced,
        "review_output_dir": str(review_output_dir(candidate)),
        "artifacts": checked, "problems": problems, "clean": not problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        candidate = load()
    except CandidateError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("CANDIDATE MANIFEST: fail-closed")
        return 2
    result = verify(candidate)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        print(f"  candidate {result['candidate_id']}  "
              f"{result['verified']}/{result['declared']} artifacts byte-exact  "
              f"certifies_production={result['certifies_production']}")
        for problem in result["problems"]:
            print(f"    {problem}", file=sys.stderr)
        print("CANDIDATE MANIFEST: clean" if result["clean"]
              else "CANDIDATE MANIFEST: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
