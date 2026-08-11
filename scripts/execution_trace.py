#!/usr/bin/env python3
"""Reliable execution tracing for repository scripts — Gate 4N-I28O, Phase N.

THE DEFECT THIS CLOSES. Gate 4N-I28L ran a global trace across every guard, saw 219 frames where
single guards alone produce a hundred each, and recorded the instrument as UNRELIABLE. It refused
to rest any finding on it, which was right — but the instrument was not at fault. `runpy.run_path`
reports `co_filename` exactly as it was passed, so a script started as `scripts/leak_scan.py` has a
RELATIVE filename, and the filter required the substring `/scripts/`. Every script frame was
discarded and the trace reported a quiet, empty world.

Gate 4N-I28N corrected the filter and the same guards produced 641 frame identities. That
correction is what found both of I28N's findings, so the difference between the two filters is the
difference between seeing a defect and certifying around it.

WHAT THIS MODULE GUARANTEES.

* a frame belongs to the repository when its filename RESOLVES inside the repository, whether it
  arrived relative, absolute, or through a symlinked or cloned path;
* identities are QUALIFIED — `Class.method`, `outer.inner`, `owner.Local.method` — so two
  functions with the same bare name never collapse into one;
* standard library and dependency frames are excluded by resolved boundary, never by a substring;
* the command root and its arguments are recorded with the frames they produced.

WHAT IT IS NOT. It is corroborating evidence, not the site oracle. The taxonomy decides membership
statically; this decides what actually ran.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

#: The failure this module exists to prevent, kept as an executable statement of the bug.
RELATIVE_FILENAME_DEFECT = {
    "cause": "runpy.run_path reports co_filename as given, so a relative invocation yields a "
             "relative filename",
    "old_filter": "'/scripts/' in filename",
    "old_result": "every script frame discarded",
    "corrected_filter": "resolve the filename against the working directory, then test "
                        "containment in the repository",
}

_TRACER = r'''
import json, sys, runpy
from pathlib import Path

REPO = Path(__REPO__)
sys.argv = __ARGV__
rows = []


def _profile(frame, event, arg):
    if event == "call":
        code = frame.f_code
        rows.append((code.co_filename, code.co_qualname, code.co_firstlineno))
    return None


sys.setprofile(_profile)
try:
    runpy.run_path(__SCRIPT__, run_name="__main__")
except BaseException:
    pass
finally:
    sys.setprofile(None)

seen = {}
for filename, qualname, lineno in rows:
    try:
        resolved = Path(filename).resolve()
    except (OSError, ValueError):
        continue
    try:
        relative = resolved.relative_to(REPO)
    except ValueError:
        continue                      # stdlib, dependency, or anything outside the repository
    if relative.parts[0] != "scripts":
        continue
    identity = relative.name + "::" + qualname.replace(".<locals>", "")
    seen.setdefault(identity, {"identity": identity, "path": str(relative),
                               "first_line": lineno})
print("TRACE" + json.dumps(sorted(seen)))
'''


def trace_command(script: str, argv: list[str] | None = None, *, cwd: Path | None = None,
                  repo: Path | None = None, env: dict | None = None,
                  timeout: int = 600) -> dict:
    """Run one repository command and return the qualified identities it executed."""
    argv = list(argv or [])
    cwd = Path(cwd or REPO_ROOT)
    repo = Path(repo or cwd)
    source = (_TRACER.replace("__ARGV__", json.dumps([f"scripts/{script}", *argv]))
              .replace("__SCRIPT__", json.dumps(f"scripts/{script}"))
              .replace("__REPO__", json.dumps(str(Path(repo).resolve()))))
    environment = {**os.environ, "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC"}
    if env:
        environment.update(env)
    try:
        proc = subprocess.run([sys.executable, "-c", source], cwd=cwd, capture_output=True,
                              text=True, env=environment, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"command": f"{script} {' '.join(argv)}".strip(), "executed": [],
                "complete": False, "why": "timeout"}
    rows = [line for line in proc.stdout.splitlines() if line.startswith("TRACE")]
    return {"command": f"{script} {' '.join(argv)}".strip(), "script": script, "argv": argv,
            "executed": json.loads(rows[-1][5:]) if rows else [],
            "complete": bool(rows), "exit": proc.returncode}


def dispatched_overrides(results: list[dict], *, prefixes=("visit_",),
                         names=frozenset({"generic_visit"})) -> list[str]:
    """Framework-protocol overrides these runs actually executed."""
    out = set()
    for result in results:
        for identity in result["executed"]:
            leaf = identity.rsplit(".", 1)[-1]
            if leaf.startswith(prefixes) or leaf in names:
                out.add(identity)
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", action="append", default=[],
                        help="a repository command, e.g. 'leak_scan.py' or 'x.py state'")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = []
    for command in args.command:
        parts = command.split()
        results.append(trace_command(parts[0], parts[1:]))
    payload = {"results": results, "dispatched_overrides": dispatched_overrides(results),
               "relative_filename_defect": RELATIVE_FILENAME_DEFECT}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for result in results:
            print(f"  {result['command']:52s} frames={len(result['executed'])}")
        for override in payload["dispatched_overrides"]:
            print(f"    dispatched override {override}")
    return 0 if all(r["complete"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
