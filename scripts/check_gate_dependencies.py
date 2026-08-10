#!/usr/bin/env python3
"""Gate-guard dependency contract — Gate 4N-I26C.

Every dependency the Gate 4N guards import must be DECLARED in scripts/requirements-gate.txt
and IMPORTABLE at the point the guards run. Both directions matter:

  declared but not importable   the install step did not run, or ran after a consumer
  importable but not declared   the guard works on a machine that happens to have the package
                                and fails on a clean runner — which is exactly how the I26B
                                guards were shipped

Absence fails LOUDLY here rather than surfacing as three guards quietly exiting 2 "refused",
which reads like a policy decision instead of a missing package.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "scripts" / "requirements-gate.txt"

# Distribution name -> the module it provides. Only needed where they differ.
MODULE_OF = {"PyYAML": "yaml"}

# Modules the standard library provides; never expected in the contract.
def _is_stdlib(name: str) -> bool:
    return name in getattr(sys, "stdlib_module_names", set())


def declared() -> dict[str, str]:
    if not CONTRACT.exists():
        raise SystemExit(f"the gate dependency contract is missing: {CONTRACT}")
    out = {}
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([\w.]+)$", line)
        if not match:
            raise SystemExit(
                f"unpinned or malformed dependency line: {line!r}. Every gate dependency is "
                "pinned to an exact version, like every other toolchain pin in this repository.")
        out[match.group(1)] = match.group(2)
    return out


WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def gate_guard_scripts() -> list[Path]:
    """Scripts the Gate 4N job runs under the SYSTEM python3, plus their local import closure.

    SCOPE IS DERIVED, NOT LISTED. A first version of this checker scanned every file under
    scripts/ and reported `httpx` as an undeclared gate dependency — but smoke_http.py runs in
    the integration-smoke job, which installs apps/api[dev] and therefore has its own contract.
    Scoping by "which scripts does the bare-python3 job actually invoke" is the real question,
    and it is answered from the workflow rather than from an exemption list that would go stale.

    A regex is used here deliberately: this reads COMMAND TOKENS, not block-scalar structure,
    and it must work BEFORE PyYAML is installed — a dependency checker that needs the dependency
    it checks for cannot report its absence.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    invoked = set(re.findall(r"(?<!\.)\bpython3?\s+(scripts/[A-Za-z0-9_\-]+\.py)", text))
    closure: set[Path] = set()

    def walk(path: Path) -> None:
        if path in closure or not path.exists():
            return
        closure.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            return
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for name in names:
                local = REPO_ROOT / "scripts" / f"{name}.py"
                if local.exists():
                    walk(local)

    for rel in sorted(invoked):
        walk(REPO_ROOT / rel)
    return sorted(closure)


def third_party_imports() -> set[str]:
    """Third-party modules the GATE GUARD CLOSURE imports. Neither stdlib nor local."""
    local = {p.stem for p in (REPO_ROOT / "scripts").glob("*.py")}
    found: set[str] = set()
    for script in gate_guard_scripts():
        try:
            tree = ast.parse(script.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for name in names:
                if name not in local and not _is_stdlib(name):
                    found.add(name)
    return found


def check() -> dict:
    contract = declared()
    modules = {MODULE_OF.get(dist, dist.lower()): dist for dist in contract}
    problems = []

    for module, dist in modules.items():
        if importlib.util.find_spec(module) is None:
            problems.append(
                f"{dist} is DECLARED in the gate dependency contract but NOT IMPORTABLE. The "
                "install step has not run, or it runs after a guard that needs it.")

    for imported in sorted(third_party_imports()):
        if imported not in modules:
            problems.append(
                f"a guard script imports {imported!r}, which the dependency contract does not "
                "declare. It works here only because this machine happens to have it; on a "
                "clean runner the guard would refuse.")

    return {"contract": str(CONTRACT.relative_to(REPO_ROOT)),
            "declared": contract, "importable": {m: importlib.util.find_spec(m) is not None
                                                 for m in modules},
            "gate_guard_closure": [str(p.relative_to(REPO_ROOT)) for p in gate_guard_scripts()],
            "third_party_imports_found": sorted(third_party_imports()),
            "problems": problems, "clean": not problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = check()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for dist, version in result["declared"].items():
            print(f"  {dist}=={version}")
        for problem in result["problems"]:
            print(f"    {problem}", file=sys.stderr)
        print("GATE DEPENDENCIES:", "satisfied" if result["clean"] else "UNSATISFIED")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
