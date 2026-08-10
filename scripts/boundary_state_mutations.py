#!/usr/bin/env python3
"""Falsification harness for the authoritative boundary-state model (Gate 4N-I16, Phase E).

WHY THIS EXISTS. Gate 4N-I15 shipped a Stage-A guard that reported safety it did not
measure, and the whole tftest suite stayed green. A guard is only worth its runtime if
removing it turns something red, so this harness removes each load-bearing clause in turn
and requires a NAMED oracle to fail.

THE NAMED-ORACLE RULE. Each mutation declares which oracle must catch it. "Some test went
red" is a weaker claim than it looks: a mutation that breaks an unrelated suite would
satisfy it while the guard under test remained vacuous. Naming the oracle also documents
WHERE the protection lives, which is how the reader learns that two of these mutations are
structurally undetectable by plan behaviour (see BYPASS NOTE below).

BYPASS NOTE — an honest limitation, stated rather than engineered around. Mutations that
repoint a role from `local.effective_permissions_boundary` to the raw
`var.role_permissions_boundary_arn` CANNOT be caught by any plan-time assertion, because the
two expressions differ only in states the state model now rejects outright:
    disabled + non-null ARN -> INVALID_PARTIAL_BOOTSTRAP (rejected)
    required + null ARN     -> INVALID_PARTIAL_BOOTSTRAP (rejected)
In every LEGAL state the raw variable and the derived local are equal, so no plan can
distinguish them. Those two mutations are therefore caught by a SOURCE-STRUCTURAL oracle
(tests/test_boundary_durability.py), and that is a genuinely weaker guarantee than the
executed ones. It is recorded here rather than left for a reviewer to discover.

Mutations are applied to the real working tree and restored in a `finally`, with byte
identity verified by SHA-256 before and after. Nothing here calls AWS; `tofu test` runs
offline against a mocked provider with no backend.

Usage:
    python3 scripts/boundary_state_mutations.py [--json]
Exit: 0 iff every mutation is caught by its declared oracle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

READER_VARS = "infra/aws/modules/revision_reader/variables.tf"
READER_IAM = "infra/aws/modules/revision_reader/iam.tf"
IAM_MAIN = "infra/aws/modules/iam/main.tf"

# --- oracles -------------------------------------------------------------------------------


def _tofu(module: str) -> tuple[bool, str]:
    proc = subprocess.run(["tofu", "test"], cwd=REPO_ROOT / "infra/aws/modules" / module,
                          capture_output=True, text=True)
    return proc.returncode == 0, proc.stdout[-400:] + proc.stderr[-400:]


def _pytest_interpreter() -> str:
    """The interpreter that can actually import pytest.

    `sys.executable` is NOT safe here: on this host the default python3 has no pytest, so a
    pytest oracle invoked through it exits non-zero for a reason that has nothing to do with
    the mutation — every mutation would read as CAUGHT. A harness whose oracle fails for the
    wrong reason is indistinguishable from one that works, which is why the baseline control
    in run() checks the UNMUTATED tree first.
    """
    import shutil
    for candidate in (sys.executable, shutil.which("python3"), shutil.which("python"),
                      "/opt/miniconda3/bin/python3"):
        if not candidate:
            continue
        probe = subprocess.run([candidate, "-c", "import pytest"], capture_output=True)
        if probe.returncode == 0:
            return candidate
    raise SystemExit("no interpreter on PATH can import pytest; cannot run the pytest oracle")


def _pytest(target: str) -> tuple[bool, str]:
    proc = subprocess.run([_pytest_interpreter(), "-m", "pytest", target, "-q", "--no-header"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    return proc.returncode == 0, proc.stdout[-400:]


ORACLES = {
    "tofu:revision_reader": lambda: _tofu("revision_reader"),
    "tofu:iam": lambda: _tofu("iam"),
    "pytest:boundary_durability": lambda: _pytest("tests/test_boundary_durability.py"),
}

# --- mutations -----------------------------------------------------------------------------
#
# `old` must appear EXACTLY ONCE in the file. A mutation whose anchor matches zero times is
# a silently-skipped mutation, which is the same failure this harness exists to prevent.

MUTATIONS = [
    {
        "id": "revert_stage_a_to_the_superseded_arn_signal",
        "why": "THE Gate 4N-I15 defect, reintroduced verbatim: gate Stage A on the ARN "
               "variable instead of the mode the roles actually consume.",
        "file": READER_VARS, "oracle": "tofu:revision_reader",
        "old": 'condition     = !var.publication_bootstrap_enabled || var.role_boundary_mode == "required"',
        "new": "condition     = !var.publication_bootstrap_enabled || var.role_permissions_boundary_arn != null",
    },
    {
        "id": "ignore_bootstrap_enabled_in_the_stage_a_rule",
        "why": "Make the Stage-A bootstrap rule vacuous, so a dark state may create "
               "protected roles. Targets the SINGLE encoding of that rule — an earlier "
               "draft encoded it twice and neither copy could then be falsified.",
        "file": READER_VARS, "oracle": "tofu:revision_reader",
        "old": 'condition     = !var.publication_bootstrap_enabled || var.role_boundary_mode == "required"',
        "new": 'condition     = true || var.role_boundary_mode == "required"',
    },
    {
        "id": "accept_disabled_mode_with_a_non_null_arn",
        "why": "Remove the coherence axis in the reader module — the direction Gate "
               "4N-I15 had no check for at all.",
        "file": READER_IAM, "oracle": "tofu:revision_reader",
        "old": 'condition     = local.boundary_state != "INVALID_PARTIAL_BOOTSTRAP"',
        "new": "condition     = true",
    },
    {
        "id": "classify_required_with_a_null_arn_as_enforced",
        "why": "Corrupt the state model itself so required+null reads as BOUNDARY_ENFORCED "
               "— the Gate 4N-I7 removal defect, reachable again through the classifier.",
        "file": READER_IAM, "oracle": "tofu:revision_reader",
        "old": 'var.role_boundary_mode == "required" && var.role_permissions_boundary_arn != null\n    ? "BOUNDARY_ENFORCED"',
        "new": 'var.role_boundary_mode == "required"\n    ? "BOUNDARY_ENFORCED"',
    },
    {
        "id": "remove_the_exact_boundary_name_comparison",
        "why": "Drop the ceiling-identity axis so any policy ARN is accepted.",
        "file": READER_IAM, "oracle": "tofu:revision_reader",
        "old": 'condition     = !local.boundary_enforced || var.role_permissions_boundary_arn == null || can(regex("policy/signalnest-staging-role-boundary$", var.role_permissions_boundary_arn))',
        "new": 'condition     = !local.boundary_enforced || var.role_permissions_boundary_arn == null || can(regex("^arn:", var.role_permissions_boundary_arn))',
    },
    {
        "id": "weaken_ceiling_identity_to_syntax_only",
        "why": "Accept anything ARN-SHAPED. A wrong ceiling is not distinguishable from "
               "the right one by shape, which is the entire point of the name check.",
        "file": IAM_MAIN, "oracle": "tofu:iam",
        "old": 'condition     = !local.boundary_enforced || var.role_permissions_boundary_arn == null || can(regex("policy/signalnest-staging-role-boundary$", var.role_permissions_boundary_arn))',
        "new": 'condition     = !local.boundary_enforced || var.role_permissions_boundary_arn == null || can(regex("^arn:aws:iam::[0-9]{12}:policy/", var.role_permissions_boundary_arn))',
    },
    {
        "id": "guard_only_the_reader_module_not_the_iam_module",
        "why": "Protect one module and leave the other open. Both modules must carry the "
               "same state model or the composition has a soft side.",
        "file": IAM_MAIN, "oracle": "tofu:iam",
        "old": 'condition     = local.boundary_state != "INVALID_PARTIAL_BOOTSTRAP"',
        "new": "condition     = true",
    },
    {
        "id": "bypass_the_publisher_role_to_the_raw_variable",
        "why": "Repoint the publisher role at the raw ARN variable, bypassing the mode. "
               "STRUCTURAL oracle only — see BYPASS NOTE in the module docstring.",
        "file": READER_IAM, "oracle": "pytest:boundary_durability",
        "old": '  permissions_boundary = local.effective_permissions_boundary\n  name                 = "${var.name_prefix}-revision-reader-publisher"',
        "new": '  permissions_boundary = var.role_permissions_boundary_arn\n  name                 = "${var.name_prefix}-revision-reader-publisher"',
    },
    {
        "id": "bypass_one_reader_role_to_the_raw_variable",
        "why": "Same bypass on a single reader role — the one-role-drifts case. STRUCTURAL "
               "oracle only.",
        "file": IAM_MAIN, "oracle": "pytest:boundary_durability",
        "old": '  name                 = "${var.name_prefix}-ecs-execution"\n  permissions_boundary = local.effective_permissions_boundary',
        "new": '  name                 = "${var.name_prefix}-ecs-execution"\n  permissions_boundary = var.role_permissions_boundary_arn',
    },
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run() -> dict:
    touched = sorted({m["file"] for m in MUTATIONS})
    before = {f: _sha(REPO_ROOT / f) for f in touched}
    originals = {f: (REPO_ROOT / f).read_text(encoding="utf-8") for f in touched}

    rows: list[dict] = []
    try:
        # Positive control FIRST. If the unmutated tree is not green, every "caught" below
        # is meaningless — the oracle would be failing for a reason unrelated to the mutation.
        baseline = {}
        for name, fn in ORACLES.items():
            passed, _ = fn()
            baseline[name] = passed

        for mutation in MUTATIONS:
            path = REPO_ROOT / mutation["file"]
            text = originals[mutation["file"]]
            occurrences = text.count(mutation["old"])
            if occurrences != 1:
                rows.append({**{k: mutation[k] for k in ("id", "file", "oracle", "why")},
                             "anchor_occurrences": occurrences, "caught": False,
                             "detail": "anchor did not match exactly once; mutation NOT applied"})
                continue
            try:
                path.write_text(text.replace(mutation["old"], mutation["new"]),
                                encoding="utf-8")
                passed, detail = ORACLES[mutation["oracle"]]()
            finally:
                path.write_text(text, encoding="utf-8")
            rows.append({**{k: mutation[k] for k in ("id", "file", "oracle", "why")},
                         "anchor_occurrences": 1, "caught": not passed,
                         "detail": "" if not passed else f"ORACLE STAYED GREEN: {detail}"})
    finally:
        for f, text in originals.items():
            (REPO_ROOT / f).write_text(text, encoding="utf-8")

    after = {f: _sha(REPO_ROOT / f) for f in touched}
    restored = before == after

    uncaught = [r["id"] for r in rows if not r["caught"]]
    return {
        "mutations": len(rows), "rows": rows,
        "baseline_oracles_green": baseline,
        "uncaught": uncaught,
        "files_restored_byte_identical": restored,
        "structural_only_oracles": sorted(
            {r["id"] for r in rows if r["oracle"].startswith("pytest")}),
        "clean": not uncaught and restored and all(baseline.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        for row in result["rows"]:
            mark = "CAUGHT " if row["caught"] else "MISSED "
            print(f"  {mark} {row['id']:52s} <- {row['oracle']}")
            if row["detail"]:
                print(f"      {row['detail']}", file=sys.stderr)
        print(f"  baseline oracles green: {result['baseline_oracles_green']}")
        print(f"  files restored byte-identical: {result['files_restored_byte_identical']}")
        print("BOUNDARY MUTATIONS: clean" if result["clean"] else "BOUNDARY MUTATIONS: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
