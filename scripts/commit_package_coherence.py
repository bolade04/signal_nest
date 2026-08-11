#!/usr/bin/env python3
"""Semantic coherence of the PREDICTED COMMIT TREE — Gate 4N-I23, blocker 1.

WHY THIS EXISTS. Gate 4N-I22 froze a candidate whose predicted commit tree was
verified eleven times and never once *read*. The tree hash was stable across every
phase, so every immutability check passed — while the tree itself contained 14
fixture files, a CI workflow invoking 26 scripts that were not in it, and ZERO of
the 45 control scripts and ZERO of the 41 test files. The commit would have been
titled "finalize W0 verification controls" and would have contained no verification
controls.

THE LESSON, STATED SO IT CANNOT BE RE-LEARNED: verifying that an artifact has not
CHANGED is not verifying that it is CORRECT. A hash comparison answers "is this the
same as before?"; it is silent on "is this internally consistent and complete?".
Those feel like the same activity because both are mechanical and both go green.

So this module never compares a hash. It materialises the predicted tree and asks
whether the package can stand on its own: does every command the committed workflow
invokes exist in the commit; does every committed module's local imports resolve
inside the commit; does every fixture a committed test reads exist in the commit;
does every committed fixture have a reader.

IT MUST RUN AGAINST A MATERIALISED TREE, NOT THE WORKING TREE. Checking the working
tree and assuming equivalence is precisely the I22 defect: the working tree is richer
than the commit, so every check passes there and none of them mean anything. The
materialisation is `git archive <tree>` into a temporary directory; the real worktree
and the real index are never touched.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/ci.yml"

# A path referenced by a committed workflow or a committed module must exist in the
# commit. These are the roots that carry executable content.
SOURCE_ROOTS = ("scripts/", "tests/", "apps/", "infra/")

# Protected external evidence must never enter the tree. Absence is the assertion.
PROHIBITED_IN_TREE = (
    "infra/aws/live-resource-inventory.json",
    "infra/aws/cloudfront-expected.json",
)


class CoherenceError(RuntimeError):
    """The predicted commit package is not self-consistent."""


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd or REPO_ROOT),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise CoherenceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def materialize(tree_hash: str, dest: Path) -> Path:
    """Extract the predicted tree into dest. The real worktree/index are untouched.

    `git archive` reads the object database directly, so this works for a tree
    written by tracked_state.predicted_commit_tree() even though no commit points
    at it.
    """
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "archive", "--format=tar", tree_hash],
                          cwd=str(REPO_ROOT), capture_output=True)
    if proc.returncode != 0:
        raise CoherenceError(f"cannot materialize tree {tree_hash}: "
                             f"{proc.stderr.decode(errors='replace').strip()}")
    # GATE 4N-I28AK, closing ADV-I28AJ-01. This used to invoke a bare "tar", resolved through
    # PATH at call time and bound by nothing — so a fake tar earlier on PATH decided what this
    # function extracted, and therefore what the coherence checks below read. The executable-trust
    # layer now resolves and validates tar once, and this invokes THAT absolute path rather than
    # resolving the name again.
    import external_executable_trust as _eet
    tar_argv, tar_env = _eet.tar_invocation(["-x", "-C", str(dest)])
    tar = subprocess.run(tar_argv, input=proc.stdout, capture_output=True, env=tar_env)
    if tar.returncode != 0:
        raise CoherenceError(f"tar extraction failed: "
                             f"{tar.stderr.decode(errors='replace').strip()}")
    return dest


def tree_paths(tree_hash: str) -> set[str]:
    return set(_git("ls-tree", "-r", "--name-only", tree_hash).split("\n")) - {""}


# --------------------------------------------------------------------------- #
# reference extraction
# --------------------------------------------------------------------------- #

_PATH_RE = re.compile(r"(?:scripts|tests|apps|infra)/[A-Za-z0-9_./-]+"
                      r"\.(?:py|sh|json|txt|ya?ml|tf|hcl|pem|ini|cfg|toml)")


def referenced_paths(text: str) -> set[str]:
    """Every repository path a file mentions, by extension. Deliberately broad:
    a false positive is a path we then require to exist, which is the safe
    direction. A false NEGATIVE is what I22 shipped."""
    return set(_PATH_RE.findall(text))


def workflow_command_paths(workflow_text: str) -> set[str]:
    """Repository paths the workflow actually invokes or reads."""
    return {p for p in referenced_paths(workflow_text)
            if p.startswith(SOURCE_ROOTS)}


def local_module_imports(source: str) -> set[str]:
    """Top-level module names imported by a Python source file, via AST.

    AST, not regex: a mention inside a string or comment is not an import, and an
    import inside a function still binds at call time. Both matter — the I22 CI
    wiring test failed exactly because it matched a substring rather than parsing.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CoherenceError(f"unparseable committed python: {exc}") from exc
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


# --------------------------------------------------------------------------- #
# the checks
# --------------------------------------------------------------------------- #

def check(tree_hash: str, root: Path) -> dict:
    """Run every coherence check against the MATERIALISED tree at `root`."""
    present = tree_paths(tree_hash)
    findings: list[dict] = []

    def fail(kind: str, detail: str, **extra):
        findings.append({"check": kind, "detail": detail, **extra})

    # 1/5. every path the committed workflow invokes exists in the commit.
    wf = root / WORKFLOW
    workflow_refs: set[str] = set()
    if not wf.exists():
        fail("workflow_present", f"{WORKFLOW} is not in the predicted tree")
    else:
        workflow_refs = workflow_command_paths(wf.read_text(encoding="utf-8"))
        for ref in sorted(workflow_refs):
            if ref not in present:
                fail("ci_command_resolves",
                     f"{WORKFLOW} references {ref}, which is NOT in the commit",
                     path=ref)

    # 2. every guard-list script referenced by the workflow's result aggregation.
    #    A guard whose script is absent is a step that cannot run.
    guard_scripts = {r for r in workflow_refs if r.startswith("scripts/")}
    for g in sorted(guard_scripts):
        if g not in present:
            fail("guard_command_resolves", f"guard script {g} absent from commit",
                 path=g)

    # 3. every local module imported by a committed .py resolves inside the commit.
    committed_py = sorted(p for p in present if p.endswith(".py"))
    local_names = {Path(p).stem for p in present
                   if p.endswith(".py") and "/" in p}
    for rel in committed_py:
        f = root / rel
        if not f.exists():
            continue
        for name in local_module_imports(f.read_text(encoding="utf-8")):
            # only adjudicate names that look like OUR modules: a name that exists
            # as a module somewhere in the repo but not in the commit is the defect.
            in_repo = (REPO_ROOT / "scripts" / f"{name}.py").exists() or \
                      (REPO_ROOT / "tests" / f"{name}.py").exists()
            if in_repo and name not in local_names:
                fail("local_import_resolves",
                     f"{rel} imports local module '{name}' which is NOT in the commit",
                     path=rel, module=name)

    # 4/14. fixtures: every fixture a committed file reads must exist, and every
    #       committed fixture must have at least one committed reader.
    fixtures_in_tree = {p for p in present if p.startswith("tests/fixtures/")}
    fixture_readers: dict[str, list[str]] = {f: [] for f in fixtures_in_tree}
    # .json and .txt are scanned too: a fixture named inside a committed MANIFEST
    # fixture is genuinely consumed (fixture -> manifest -> test). Gate 4N-I23 first
    # flagged tests/fixtures/candidate/synthetic-prefreeze.txt as an orphan purely
    # because this scan skipped JSON. The fix is to follow the real consumer chain,
    # not to add an exemption list — an exemption would become the next blind spot,
    # which is the ADV-D lesson.
    for rel in sorted(p for p in present
                      if p.endswith((".py", ".yml", ".yaml", ".sh", ".json", ".txt"))):
        f = root / rel
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for ref in referenced_paths(text):
            if ref.startswith("tests/fixtures/"):
                if ref not in present:
                    fail("fixture_reference_resolves",
                         f"{rel} reads {ref}, which is NOT in the commit",
                         path=rel, fixture=ref)
                else:
                    fixture_readers[ref].append(rel)
        # also catch basename-only fixture references (open(FIXTURES / "x.json"))
        for fx in fixtures_in_tree:
            if Path(fx).name in text and rel != fx:
                fixture_readers[fx].append(rel)

    for fx, readers in sorted(fixture_readers.items()):
        if not readers:
            fail("fixture_has_consumer",
                 f"{fx} is committed but NO committed file reads it (orphan)",
                 fixture=fx)

    # 15. protected external evidence must be absent.
    for prohibited in PROHIBITED_IN_TREE:
        if prohibited in present:
            fail("protected_inventory_absent",
                 f"PROTECTED external evidence {prohibited} is IN the commit",
                 path=prohibited)

    # 6/7/11/12/13. the package must be able to run its own verification: if the
    # repository has a control script or test that the commit lacks, the commit
    # cannot verify itself. This is the direct I22 refutation.
    repo_scripts = {f"scripts/{p.name}" for p in (REPO_ROOT / "scripts").glob("*.py")}
    repo_tests = {f"tests/{p.name}" for p in (REPO_ROOT / "tests").glob("test_*.py")}
    for missing in sorted(repo_scripts - present):
        fail("control_script_committed",
             f"control script {missing} exists in the repository but NOT in the commit",
             path=missing)
    for missing in sorted(repo_tests - present):
        fail("test_file_committed",
             f"test file {missing} exists in the repository but NOT in the commit",
             path=missing)
    for required in ("tests/conftest.py", "tests/oracle/graph_oracle.py"):
        if (REPO_ROOT / required).exists() and required not in present:
            fail("required_support_committed",
                 f"{required} is required to run the suite and is NOT in the commit",
                 path=required)

    return {
        "tree_hash": tree_hash,
        "paths_in_tree": len(present),
        "workflow_referenced_paths": len(workflow_refs),
        "committed_python": len(committed_py),
        "fixtures_in_tree": len(fixtures_in_tree),
        "orphan_fixtures": sum(1 for r in fixture_readers.values() if not r),
        "findings": findings,
        "coherent": not findings,
    }


def verify(tree_hash: str) -> dict:
    """Materialise the predicted tree in a temp dir and check it. Never touches the
    real worktree or index."""
    with tempfile.TemporaryDirectory(prefix="i23-predicted-tree-") as tmp:
        root = materialize(tree_hash, Path(tmp))
        return check(tree_hash, root)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tree", help="predicted commit tree hash "
                                   "(default: compute from the current index)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    tree = args.tree
    if not tree:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import tracked_state
        tree = tracked_state.predicted_commit_tree()["predicted_tree_hash"]

    result = verify(tree)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"predicted tree {result['tree_hash']}  "
              f"{result['paths_in_tree']} paths")
        print(f"  workflow-referenced paths : {result['workflow_referenced_paths']}")
        print(f"  committed python          : {result['committed_python']}")
        print(f"  fixtures / orphaned       : {result['fixtures_in_tree']} / "
              f"{result['orphan_fixtures']}")
        for f in result["findings"]:
            print(f"  [{f['check']}] {f['detail']}")
        print("COMMIT PACKAGE COHERENCE:",
              "coherent" if result["coherent"] else
              f"{len(result['findings'])} findings")
    return 0 if result["coherent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
