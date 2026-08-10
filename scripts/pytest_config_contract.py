#!/usr/bin/env python3
"""Approved pytest CONFIGURATION contract (Gate 4N-I28Y).

THE DEFECT THIS CLOSES. One of the three Gate 4N-I28X bypasses needed no command-line change at
all: appending `collect_ignore = [...]` to `tests/conftest.py` removed both assertion-control
modules from the graded run, and every control stayed green. Selection can be altered from
configuration as easily as from argv, so constraining argv alone would have closed one door and
left the other open.

WHAT THIS DOES. It enumerates every configuration source that can affect collection or selection
in the graded session, hashes them against an authored baseline, and refuses shapes that remove
tests: `collect_ignore`, `collect_ignore_glob`, `pytest_ignore_collect`, `pytest_collection_modifyitems`
that deselects, module-level skip/xfail marks in mandatory files, and `addopts` carrying selection
flags.

WHY BOTH A HASH AND A SHAPE CHECK. The hash makes any change visible; the shape check names WHAT
is wrong when a change is legitimate but harmful. A hash alone would force a re-baseline on every
innocent edit and teach reviewers to re-baseline without reading. A shape check alone would miss
a novel mechanism nobody enumerated. Together, an unapproved edit is caught by the hash and a
dangerous edit is explained by the shape.

FAIL CLOSED. A configuration source that exists but is not in the baseline is a PROBLEM. So is a
baseline entry whose file has disappeared.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / "tests" / "fixtures" / "pytest-configuration-baseline.json"

# Every file that pytest reads for configuration in this repository's graded session.
CONFIG_CANDIDATES = (
    "pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg",
    "conftest.py", "tests/conftest.py",
)

# Names whose presence in a conftest removes tests from the session.
COLLECTION_REMOVERS = (
    "collect_ignore", "collect_ignore_glob", "pytest_ignore_collect",
    "pytest_collection_skip", "collect_ignore_regex",
)

# Options that change WHICH tests run when smuggled through `addopts`.
SELECTION_FLAGS = (
    "--deselect", "-k", "-m", "--ignore", "--ignore-glob", "--collect-only", "--co",
    "--confcutdir", "--pyargs", "--last-failed", "--lf", "--failed-first", "--ff",
    "--stepwise", "--sw", "-p no:",
)


class ConfigContractError(RuntimeError):
    """Fail closed."""


def load_baseline(path: Path | None = None) -> dict:
    p = path or BASELINE
    if not p.is_file():
        raise ConfigContractError(
            f"the approved pytest configuration baseline is missing at {p}. Without it any "
            "configuration file could silently remove mandatory tests from the graded session.")
    return json.loads(p.read_text(encoding="utf-8"))


def discovered_sources(root: Path | None = None) -> dict[str, str]:
    """Configuration files that exist right now, with their byte hashes."""
    base = root or REPO_ROOT
    out = {}
    for rel in CONFIG_CANDIDATES:
        p = base / rel
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _conftest_problems(rel: str, text: str, mandatory_files: set[str]) -> list[str]:
    problems = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"{rel}: does not parse ({exc}); refusing to treat it as harmless"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in COLLECTION_REMOVERS:
                    problems.append(
                        f"{rel}: defines {t.id!r}, which removes files from collection. This is "
                        "the exact Gate 4N-I28X bypass 1.")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name in COLLECTION_REMOVERS:
            problems.append(
                f"{rel}: defines the collection hook {node.name!r}, which can remove mandatory "
                "tests from the session")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name == "pytest_collection_modifyitems":
            src = ast.dump(node)
            if "deselect" in src or "items.clear" in text:
                problems.append(
                    f"{rel}: pytest_collection_modifyitems deselects items; deselection of a "
                    "mandatory node is refused")
    return problems


def _ini_problems(rel: str, text: str) -> list[str]:
    problems = []
    lowered = text.lower()
    if "addopts" in lowered:
        for line in text.splitlines():
            if "addopts" in line.lower():
                for flag in SELECTION_FLAGS:
                    if flag in line:
                        problems.append(
                            f"{rel}: addopts carries the selection-altering option {flag!r}; "
                            "configuration may not change which tests run")
    return problems


def _mandatory_files() -> set[str]:
    reg = REPO_ROOT / "tests" / "fixtures" / "mandatory-pytest-nodes.json"
    if not reg.is_file():
        return set()
    doc = json.loads(reg.read_text(encoding="utf-8"))
    return {n["node_id"].split("::")[0] for n in doc.get("mandatory_nodes", [])}


def _module_level_skip_problems(mandatory_files: set[str]) -> list[str]:
    """A mandatory file may not carry a module-level skip or xfail mark."""
    problems = []
    for rel in sorted(mandatory_files):
        p = REPO_ROOT / rel
        if not p.is_file():
            problems.append(f"{rel}: a mandatory test file is absent from the tree")
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append(f"{rel}: does not parse ({exc})")
            continue
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
                marks = ast.dump(node.value)
                for bad in ("skip", "xfail"):
                    if f"'{bad}'" in marks or f'"{bad}"' in marks or f"attr='{bad}'" in marks:
                        problems.append(
                            f"{rel}: module-level pytestmark applies {bad!r} to a mandatory "
                            "control file. This is the exact Gate 4N-I28X bypass 2.")
    return problems


def check(root: Path | None = None, baseline: dict | None = None) -> dict:
    base = baseline if baseline is not None else load_baseline()
    approved = base.get("approved_sources", {})
    found = discovered_sources(root)
    problems: list[str] = []

    for rel, digest in sorted(found.items()):
        if rel not in approved:
            problems.append(
                f"{rel}: a pytest configuration source exists that the approved baseline does "
                "not cover. An unapproved configuration file can change collection.")
        elif approved[rel] != digest:
            problems.append(
                f"{rel}: content changed ({digest[:16]} != approved {approved[rel][:16]}). A "
                "configuration change is legitimate, but it must be re-approved in the baseline "
                "so that a collection change cannot arrive unread.")
    for rel in sorted(approved):
        if rel not in found:
            problems.append(f"{rel}: approved configuration source is missing from the tree")

    mandatory_files = _mandatory_files()
    for rel in sorted(found):
        text = (root or REPO_ROOT).joinpath(rel).read_text(encoding="utf-8")
        if rel.endswith("conftest.py"):
            problems += _conftest_problems(rel, text, mandatory_files)
        else:
            problems += _ini_problems(rel, text)
    problems += _module_level_skip_problems(mandatory_files)

    return {
        "approved_sources": len(approved),
        "discovered_sources": found,
        "mandatory_files": sorted(mandatory_files),
        "problems": problems,
        "clean": not problems,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        result = check()
    except ConfigContractError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("PYTEST CONFIGURATION CONTRACT: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['approved_sources']} approved source(s); "
              f"problems {len(result['problems'])}")
        for p in result["problems"]:
            print(f"    {p}")
    print("PYTEST CONFIGURATION CONTRACT: " + ("clean" if result["clean"] else "PROBLEMS"))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
