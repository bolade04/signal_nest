#!/usr/bin/env python3
"""Evidence provenance and summary freshness — Gate 4N-I24C, findings I24C-01, -10 and -11.

THREE DEFECTS, ONE ROOT.

I24C-01 (I23 C1, CRITICAL). The frozen candidate bound tree `86b693da`, while every
tree-bearing artifact in the package adjudicated the superseded `9984d4ee`. Phase artifacts
were generated, remediation continued — moving the tree — and the freeze happened without
regenerating them. The only attestation for the frozen tree was the manifest attesting to
itself, which is exactly the pattern `production_certification.SELF_ATTESTING_FIELDS` forbids.
Adversarial E8 sharpened it: NOTHING in the repository bound the frozen candidate at all, so
altering the index between freeze and commit was caught by no automated control.

I24C-10. Hash-pinned artifacts did not document the state they were pinned against — six
independent signals (1982 vs 1988 tests, 27 vs 29 guards, 40 vs 42 workflow references, 522
vs 525 leak-scan files, a staged-diff mismatch, 108 vs 120 additions). Worse,
`equals_real_index_tree: true` was a HARD-CODED LITERAL that never computed anything: a
boolean asserted rather than measured, inside an artifact whose only job is to record a
measurement.

I24C-11 (GATE-STOPPING; it stopped the first I24 attempt). The `carried_to_i24` summary was
authored at five delivered lanes. The sixth lane then delivered X1, X2 and X4, which were
appended to `decisive_defects` — and the summary was never regenerated. A CRITICAL and a
GATE-STOPPING finding were therefore missing from the scope handed to the next gate. That is
C1 committed inside the document that records C1.

THE CONTRACT. Evidence must declare the object it describes, a consumer must verify that
declaration against live state, and a summary must be derivable only when its inputs are
complete. A summary that numerically agrees while omitting a later CRITICAL finding is still
stale, so completeness is checked against the defect set, not against a count.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

BINDING_FIELDS = ("head", "index_tree_hash", "predicted_commit_tree_hash",
                  "staged_diff_sha256", "workflow_sha256")

# The key a bound artifact carries. Named once so discovery and verification cannot drift.
BINDING_KEY = "_binding"


class EvidenceError(RuntimeError):
    """Fail-closed."""


def _git(*args: str) -> str:
    p = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        raise EvidenceError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def current_binding() -> dict:
    """The live values every decisive artifact must declare. MEASURED, never asserted."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import tracked_state
    staged = subprocess.run(["git", "diff", "--cached"], cwd=REPO_ROOT, capture_output=True)
    return {
        "head": _git("rev-parse", "HEAD").strip(),
        "index_tree_hash": tracked_state.index_tree_hash(),
        "predicted_commit_tree_hash":
            tracked_state.predicted_commit_tree()["predicted_tree_hash"],
        "staged_diff_sha256": hashlib.sha256(staged.stdout).hexdigest(),
        "workflow_sha256": hashlib.sha256(
            (REPO_ROOT / ".github/workflows/ci.yml").read_bytes()).hexdigest(),
    }


def bind(document: dict) -> dict:
    """Stamp an artifact with the object it describes. Refuses to stamp a self-attesting field."""
    if BINDING_KEY in document:
        raise EvidenceError("document is already bound; rebinding would hide a regeneration")
    for forbidden in ("equals_real_index_tree", "is_current", "is_fresh"):
        if forbidden in document:
            raise EvidenceError(
                f"{forbidden!r} is a self-asserted freshness claim. Gate 4N-I23 shipped "
                "`equals_real_index_tree: true` as a hard-coded literal that never computed "
                "anything. Freshness is decided by the VERIFIER, never declared by the artifact.")
    out = dict(document)
    out[BINDING_KEY] = current_binding()
    return out


def verify(document: dict, *, what: str = "artifact") -> list[str]:
    """Is this artifact describing the CURRENT object? Superseded-tree evidence fails here."""
    problems: list[str] = []
    binding = document.get(BINDING_KEY)
    if not isinstance(binding, dict):
        return [f"{what}: carries no _binding, so it does not say which object it describes; "
                "an unbound artifact can never be shown to be current"]
    live = current_binding()
    for field in BINDING_FIELDS:
        declared, actual = binding.get(field), live[field]
        if declared is None:
            problems.append(f"{what}: _binding omits {field}")
        elif declared != actual:
            problems.append(
                f"{what}: {field} declares {str(declared)[:12]}… but the current value is "
                f"{actual[:12]}… — this evidence describes a SUPERSEDED object")
    return problems


# --------------------------------------------------------------------------- #
# summary freshness  (I24C-11)
# --------------------------------------------------------------------------- #

def summary_contract(*, lanes_expected: int, lane_verdicts: dict,
                     defects: list[dict], summary_ids: list[str],
                     summary_generated_after: list[str] | None = None) -> dict:
    """A summary may be derived ONLY when every lane is final, and must cover every
    unresolved decisive defect. Appending a later defect invalidates it."""
    problems: list[str] = []

    pending = sorted(k for k, v in lane_verdicts.items()
                     if str(v).upper() in ("PENDING", "PENDING_AT_WRITE", "", "NONE"))
    if pending:
        problems.append(
            f"summary generated while {len(pending)} lane(s) were still pending: {pending}. "
            "The Gate 4N-I23 carry-forward list was authored at five of six lanes and never "
            "regenerated; the sixth lane's CRITICAL and GATE-STOPPING findings were lost.")
    if len(lane_verdicts) < lanes_expected:
        problems.append(f"only {len(lane_verdicts)} of {lanes_expected} lane verdicts present")

    defect_ids = [d["id"] for d in defects]
    missing = [i for i in defect_ids if i not in summary_ids]
    for m in missing:
        sev = next((d.get("severity", "?") for d in defects if d["id"] == m), "?")
        problems.append(f"decisive defect {m!r} ({sev}) is absent from the summary")

    # A summary whose COUNT agrees while its CONTENT omits a CRITICAL item is still stale.
    if len(summary_ids) == len(defect_ids) and missing:
        problems.append(
            "the summary count matches the defect count while omitting "
            f"{missing} — numeric agreement is not coverage")

    if summary_generated_after is not None:
        late = [i for i in defect_ids if i not in summary_generated_after]
        for lt in late:
            problems.append(
                f"defect {lt!r} was appended AFTER the summary was generated; the summary is "
                "invalidated and must be regenerated")

    return {"lanes_expected": lanes_expected, "lanes_present": len(lane_verdicts),
            "pending_lanes": pending, "defects": len(defect_ids),
            "summary_entries": len(summary_ids), "missing_from_summary": missing,
            "problems": problems, "fresh": not problems}


def bound_artifacts(root: Path) -> list[Path]:
    """Every artifact in `root` that CARRIES a binding, discovered rather than listed."""
    found = []
    for path in sorted(root.rglob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and BINDING_KEY in doc:
            found.append(path)
    return found


def verify_set(root: Path) -> dict:
    """Verify EVERY bound artifact under `root`. The default mode's real work.

    GATE 4N-I26B, closing the ARCH-C1 half of I26B-05. The graded CI step ran this module with
    NO arguments, and the no-argument branch returned `{"problems": [], "fresh": True}` — both
    literals — so the step exited 0 for every possible repository state. The guard existed, was
    graded, and could not fail. Reporting the current binding is a diagnostic; it was standing
    in for a verification that never happened.
    """
    artifacts = bound_artifacts(root)
    problems: list[str] = []
    for path in artifacts:
        doc = json.loads(path.read_text(encoding="utf-8"))
        problems.extend(f"{path.name}: {p}" for p in verify(doc, what=path.name))
    if not artifacts:
        # Absence is not freshness. An empty directory returning "fresh" is how a verifier
        # reports success for having found nothing to check.
        problems.append(
            f"no bound artifact found under {root}. An empty set is not a verified set; if "
            "there is genuinely nothing to verify, say so explicitly rather than exiting 0.")
    return {"root": str(root), "artifacts_verified": len(artifacts),
            "artifacts": [p.name for p in artifacts],
            "problems": problems, "fresh": not problems}


def mechanism_selftest() -> dict:
    """Prove the binding mechanism DETECTS, by making it detect. Runs anywhere, including CI.

    Every assertion here is a thing this module promises elsewhere in prose. Prose does not
    fail a build.
    """
    problems: list[str] = []

    fresh = bind({"kind": "selftest"})
    if verify(fresh, what="selftest"):
        problems.append("a freshly bound document did not verify as fresh")

    # Each binding field must be INDIVIDUALLY load-bearing. A tamper that the verifier does not
    # name is a field that could be dropped from BINDING_FIELDS without any test noticing.
    for field in BINDING_FIELDS:
        tampered = dict(fresh)
        tampered[BINDING_KEY] = {**fresh[BINDING_KEY], field: "0" * 40}
        found = verify(tampered, what="selftest")
        if not found:
            problems.append(f"tampering with {field} was NOT detected")
        elif not any(field in p for p in found):
            problems.append(f"tampering with {field} was detected but not attributed to it")

    unbound = verify({"kind": "selftest"}, what="selftest")
    if not unbound:
        problems.append("an UNBOUND document verified clean; absence of a binding must fail")

    try:
        bind(fresh)
        problems.append("rebinding an already-bound document was permitted")
    except EvidenceError:
        pass

    for forbidden in ("equals_real_index_tree", "is_current", "is_fresh"):
        try:
            bind({forbidden: True})
            problems.append(f"binding a document carrying {forbidden!r} was permitted")
        except EvidenceError:
            pass

    return {"mode": "mechanism selftest", "checks": 1 + 2 * len(BINDING_FIELDS) + 5,
            "binding_fields": list(BINDING_FIELDS),
            "problems": problems, "fresh": not problems}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", metavar="PATH", help="verify one bound artifact")
    ap.add_argument("--verify-set", metavar="DIR",
                    help="verify EVERY bound artifact under DIR (the default mode's target)")
    ap.add_argument("--summary", metavar="PATH",
                    help="check a consolidated summary against summary_contract()")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the binding mechanism DETECTS (the default; naming it "
                         "explicitly is what lets a structural check confirm the graded step "
                         "runs a verifying mode rather than a diagnostic one)")
    ap.add_argument("--binding-only", action="store_true",
                    help="print the current binding and exit 0. DIAGNOSTIC ONLY — this mode "
                         "verifies nothing and must never be what a graded step runs.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.verify:
        doc = json.loads(Path(args.verify).read_text(encoding="utf-8"))
        problems = verify(doc, what=Path(args.verify).name)
        result = {"artifact": args.verify, "problems": problems, "fresh": not problems}
    elif args.verify_set:
        result = verify_set(Path(args.verify_set))
    elif args.summary:
        # GATE 4N-I26B, closing I26B-07. summary_contract() shipped with ZERO production
        # callers — only tests referenced it — so the I24C-11 remediation guarded nothing that
        # runs. This is its caller, and the exit code depends on it.
        doc = json.loads(Path(args.summary).read_text(encoding="utf-8"))
        verdicts = {k: (v.get("verdict") if isinstance(v, dict) else v)
                    for k, v in (doc.get("lane_verdicts") or {}).items()}
        defects = doc.get("decisive_defects") or []
        contract = summary_contract(
            lanes_expected=int(doc.get("lanes_expected", 6)),
            lane_verdicts=verdicts, defects=defects,
            summary_ids=[d.get("id") for d in defects if isinstance(d, dict)],
            summary_generated_after=doc.get("summary_generated_after"))
        result = {"summary": args.summary, "problems": contract["problems"],
                  "fresh": not contract["problems"]}
    elif args.selftest:
        result = mechanism_selftest()
    elif args.binding_only:
        result = {"current_binding": current_binding(), "problems": [], "fresh": True,
                  "mode": "DIAGNOSTIC — nothing was verified"}
    else:
        # THE DEFAULT VERIFIES. The old default returned {"problems": [], "fresh": True} —
        # two literals — so the graded step exited 0 for every repository state.
        #
        # WHAT THE DEFAULT CAN HONESTLY CHECK IN CI. The bound artifacts live OUTSIDE the
        # repository, in the gate package, and are absent on a runner; ADV-Y11 is right that
        # artifact freshness is structurally a REVIEW-TIME control. What CI can decide, and
        # what nothing previously checked, is that the MECHANISM works: that a bound document
        # verifies fresh, that tampering with each binding field is DETECTED and named, and
        # that the refusals this module promises actually fire. That is a real failure mode, it
        # runs anywhere, and it is what the graded step now exercises.
        result = mechanism_selftest()

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if "current_binding" in result:
            for k, v in result["current_binding"].items():
                print(f"  {k:28s} {v}")
        for p in result["problems"]:
            print(f"    {p}", file=sys.stderr)
        print("EVIDENCE BINDING:", "fresh" if result["fresh"] else "STALE")
    return 0 if result["fresh"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
