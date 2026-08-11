#!/usr/bin/env python3
"""Executable startup-file policy for the graded assurance run (Gate 4N-I28AG).

THE DEFECT THIS CLOSES. Gate 4N-I28AF finding ADV-I28AF-02. Gate 4N-I28AE's bootstrap RECORDS the
interpreter-startup surface — sitecustomize and usercustomize residency and origin, PYTHONPATH,
meta_path, path_hooks, .pth-derived entries — and its own docstring says, accurately, that this is
"evidence, not a verdict". Nothing adjudicated it. The single consumer,
test_a13_the_startup_surface_is_recorded_as_evidence, asserts that the FIELDS are present, which a
hostile surface satisfies, and commit_package_coherence.py contains no reference to startup of any
kind, which is why coherence reported COHERENT for a staged attacker file. Eight of the fifteen
startup mechanisms the gate enumerates were EVIDENCE_ONLY or ungoverned.

WHAT THIS DOES. Every applicable startup mechanism is classified by an AUTHORED policy as exactly
one of REQUIRED_AND_BOUND, ALLOWED_AND_BOUND, PROHIBITED or NOT_APPLICABLE, and this module
adjudicates the observed surface against it. An allowed file is bound by the sha256 of its CONTENT,
not by its path, so an approved startup file that changes is refused. A prohibited mechanism that is
present is refused. A mechanism the policy does not classify is refused — silence is not permission.

WHY AN UNCLASSIFIED MECHANISM IS FATAL RATHER THAN EVIDENCE. That downgrade is precisely what
I28AF found: a mechanism recorded and not judged is a mechanism that does not constrain anything.
A future Python that adds a new startup hook should break this control loudly rather than inherit
its silence, so an observed mechanism with no policy entry fails closed.

TRUST ROOT. Everything the policy binds must live under the repository root, or under the running
interpreter's own prefix for third-party machinery pytest legitimately needs. A startup file
resolving outside both is refused, which is what makes PYTHONPATH injection and PATH shadowing
detectable rather than merely visible.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import sysconfig
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "tests" / "fixtures" / "startup-policy.json"

REQUIRED_AND_BOUND = "REQUIRED_AND_BOUND"
ALLOWED_AND_BOUND = "ALLOWED_AND_BOUND"
PROHIBITED = "PROHIBITED"
NOT_APPLICABLE = "NOT_APPLICABLE"
DISPOSITIONS = (REQUIRED_AND_BOUND, ALLOWED_AND_BOUND, PROHIBITED, NOT_APPLICABLE)


class StartupPolicyError(RuntimeError):
    """Fail closed. A startup surface this policy cannot adjudicate is never accepted."""


def _interpreter_roots() -> tuple:
    roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    for key in ("stdlib", "purelib", "platlib", "scripts"):
        try:
            path = sysconfig.get_path(key)
        except (KeyError, OSError):
            continue
        if path:
            roots.add(Path(path).resolve())
    return tuple(sorted(roots))


def in_trust_root(path) -> bool:
    """Inside the repository, or inside the running interpreter's own installation."""
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        return True
    return any(root == resolved or root in resolved.parents for root in _interpreter_roots())


def load_policy(path: Path | None = None) -> dict:
    p = path or POLICY
    if not p.is_file():
        raise StartupPolicyError(
            f"the startup policy is missing at {p}. It is the authored statement of which startup "
            "mechanisms may influence a graded run; without it every mechanism would be "
            "unclassified, which this control treats as fatal.")
    doc = json.loads(p.read_text(encoding="utf-8"))
    mechanisms = doc.get("mechanisms")
    if not isinstance(mechanisms, dict) or not mechanisms:
        raise StartupPolicyError("the startup policy classifies no mechanism; an empty policy "
                                 "would adjudicate nothing")
    for name, entry in mechanisms.items():
        disposition = entry.get("disposition")
        if disposition not in DISPOSITIONS:
            raise StartupPolicyError(
                f"{name}: disposition {disposition!r} is not one of {DISPOSITIONS}")
        if disposition in (REQUIRED_AND_BOUND, ALLOWED_AND_BOUND) and not entry.get("why_allowed"):
            raise StartupPolicyError(f"{name}: an allowed mechanism must state why_allowed")
        if disposition == NOT_APPLICABLE:
            if not entry.get("why_not_applicable"):
                raise StartupPolicyError(
                    f"{name}: NOT_APPLICABLE must be justified, not asserted — state "
                    "why_not_applicable with the evidence")
            # A mechanism excused from enforcement must be excused by something executable. This
            # is the difference between this policy and the evidence-only surface it replaces:
            # NOT_APPLICABLE is a claim, and a claim needs a test that fails when it stops being
            # true. PYTHONSTARTUP is the worked example — it provably does not execute for a
            # non-interactive `python -m pytest`, so refusing it would break honest operator
            # environments, but if a future CPython changes that the named test must break.
            if not entry.get("proof_test"):
                raise StartupPolicyError(
                    f"{name}: NOT_APPLICABLE must name the proof_test that would fail if the "
                    "mechanism ever became applicable")
    return doc


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------- observation
def observe() -> dict:
    """The startup surface actually in force. Facts only; adjudication happens in check()."""
    cwd = str(Path.cwd())
    pythonpath = [e for e in os.environ.get("PYTHONPATH", "").split(os.pathsep) if e]
    # Only the conftests pytest can actually reach for the graded invocation. That command is
    # `pytest tests/`, so the collection roots are the repository root (pytest walks up to it for
    # rootdir conftests) and tests/ itself. A conftest vendored inside a virtualenv or node_modules
    # is never loaded by that run, and pinning it would bind content this control does not consume
    # — a pin that means nothing is worse than no pin, because it reads as coverage.
    conftests = []
    root_level = REPO_ROOT / "conftest.py"
    if root_level.is_file():
        conftests.append("conftest.py")
    tests_dir = REPO_ROOT / "tests"
    if tests_dir.is_dir():
        conftests.extend(
            str(p.relative_to(REPO_ROOT)) for p in tests_dir.rglob("conftest.py")
            if not any(part in (".venv", "venv", "site-packages", "node_modules", ".git")
                       for part in p.parts))
    conftests = sorted(conftests)
    plugins = []
    pytest_module = sys.modules.get("pytest")
    if pytest_module is not None:
        for name, module in sorted(sys.modules.items()):
            origin = getattr(module, "__file__", None)
            if origin and name.startswith("pytest_"):
                plugins.append({"module": name, "origin": origin})
    return {
        "sitecustomize": {"resident": "sitecustomize" in sys.modules,
                          "origin": getattr(sys.modules.get("sitecustomize"), "__file__", None)},
        "usercustomize": {"resident": "usercustomize" in sys.modules,
                          "origin": getattr(sys.modules.get("usercustomize"), "__file__", None)},
        "pth_entries": [p for p in sys.path if p.endswith(".pth")],
        "PYTHONSTARTUP": os.environ.get("PYTHONSTARTUP"),
        "PYTHONPATH": pythonpath,
        "PYTHONHOME": os.environ.get("PYTHONHOME"),
        "BASH_ENV": os.environ.get("BASH_ENV"),
        "ENV": os.environ.get("ENV"),
        "PYTEST_ADDOPTS": os.environ.get("PYTEST_ADDOPTS"),
        "PYTEST_PLUGINS": os.environ.get("PYTEST_PLUGINS"),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"),
        "cwd": cwd,
        "cwd_is_repo_root": Path(cwd).resolve() == REPO_ROOT,
        "meta_path": [f"{type(f).__module__}.{type(f).__name__}" for f in sys.meta_path],
        "path_hooks": [getattr(h, "__name__", type(h).__name__) for h in sys.path_hooks],
        "conftest_files": conftests,
        "pytest_plugin_modules": plugins,
        "flags": {"no_site": sys.flags.no_site == 1, "no_user_site": sys.flags.no_user_site == 1,
                  "isolated": sys.flags.isolated == 1},
    }


# --------------------------------------------------------------------------- adjudication
def _check_file_mechanism(name, entry, origin, problems, bound):
    """One file-backed mechanism: prohibited-when-present, or allowed-and-content-bound."""
    disposition = entry["disposition"]
    if origin is None:
        if disposition == REQUIRED_AND_BOUND:
            problems.append(f"{name}: required startup file is absent")
        return
    if disposition == PROHIBITED:
        problems.append(
            f"{name}: a PROHIBITED startup mechanism is active (origin {origin}). It executes "
            "before the assurance code and can change the run without changing any executed "
            "module.")
        return
    if disposition == NOT_APPLICABLE:
        problems.append(
            f"{name}: classified NOT_APPLICABLE but observed active at {origin}. A mechanism the "
            "policy believes cannot occur has occurred, so the policy is wrong and this fails "
            "closed.")
        return
    if not in_trust_root(origin):
        problems.append(f"{name}: startup file {origin} resolves outside the authorized trust root")
        return
    expected = entry.get("sha256")
    if not expected:
        problems.append(f"{name}: allowed startup file has no pinned sha256, so its content is "
                        "unbound")
        return
    try:
        actual = _sha(origin)
    except OSError as exc:
        problems.append(f"{name}: allowed startup file could not be read ({exc})")
        return
    bound[name] = actual
    if actual != expected:
        problems.append(
            f"{name}: allowed startup file content does not match its pin "
            f"(pinned {expected[:16]}…, actual {actual[:16]}…)")


def check(policy: dict | None = None, *, surface: dict | None = None) -> dict:
    doc = policy if policy is not None else load_policy()
    obs = surface if surface is not None else observe()
    mechanisms = doc["mechanisms"]
    problems: list[str] = []
    inert: list[str] = []
    bound: dict = {}

    def entry(name):
        got = mechanisms.get(name)
        if got is None:
            problems.append(
                f"{name}: observed startup mechanism has NO policy classification. Silence is not "
                "permission — an unclassified mechanism fails closed.")
        return got

    # -- Python interpreter startup files
    for key in ("sitecustomize", "usercustomize"):
        spec = entry(key)
        if spec:
            _check_file_mechanism(key, spec, obs[key]["origin"] if obs[key]["resident"] else None,
                                  problems, bound)

    # -- environment-provided startup hooks: present-or-not against the disposition
    for key in ("PYTHONSTARTUP", "PYTHONHOME", "BASH_ENV", "ENV", "PYTEST_ADDOPTS",
                "PYTEST_PLUGINS"):
        spec = entry(key)
        if not spec:
            continue
        value = obs.get(key)
        if value and spec["disposition"] == PROHIBITED:
            problems.append(
                f"{key}: a PROHIBITED startup mechanism is set. It influences the graded run "
                "without appearing in any executed module.")
        elif value and spec["disposition"] == NOT_APPLICABLE:
            # Recorded, not refused — the disposition is backed by proof_test, which fails if the
            # mechanism ever becomes able to execute. Refusing a provably inert variable would be
            # the "control you must disable to use it" failure.
            inert.append(f"{key} is set but provably inert (see {spec['proof_test']})")
        elif not value and spec["disposition"] == REQUIRED_AND_BOUND:
            problems.append(f"{key}: required but not set")

    # -- .pth executable entries
    spec = entry("pth_files")
    if spec and obs["pth_entries"] and spec["disposition"] == PROHIBITED:
        problems.append(f"pth_files: PROHIBITED .pth-derived path entries are active: "
                        f"{obs['pth_entries'][:3]}")

    # -- PYTHONPATH: every entry must be inside the trust root
    spec = entry("PYTHONPATH")
    if spec:
        allowed = set(spec.get("allowed_relative_entries") or [])
        for raw in obs["PYTHONPATH"]:
            resolved = Path(raw).resolve()
            rel = str(resolved.relative_to(REPO_ROOT)) if (
                resolved == REPO_ROOT or REPO_ROOT in resolved.parents) else None
            if rel is not None and rel in allowed:
                continue
            if not in_trust_root(resolved):
                problems.append(
                    f"PYTHONPATH: entry {raw} resolves outside the authorized trust root, so it "
                    "can shadow a protected module with code the policy never approved")
            elif rel is not None:
                problems.append(
                    f"PYTHONPATH: repository entry {rel!r} is not in the policy's allowed set "
                    f"{sorted(allowed)}")

    # -- conftest files: the authored set, bound by content
    spec = entry("conftest")
    if spec:
        pinned = spec.get("files") or {}
        observed = set(obs["conftest_files"])
        for extra in sorted(observed - set(pinned)):
            problems.append(
                f"conftest: {extra} is present but not pinned by the policy. A conftest executes "
                "during collection and can remove or alter tests.")
        for missing in sorted(set(pinned) - observed):
            problems.append(f"conftest: pinned {missing} is absent")
        for rel in sorted(observed & set(pinned)):
            actual = _sha(REPO_ROOT / rel)
            bound[f"conftest:{rel}"] = actual
            if actual != pinned[rel]:
                problems.append(
                    f"conftest: {rel} content does not match its pin "
                    f"(pinned {pinned[rel][:16]}…, actual {actual[:16]}…)")

    # -- pytest plugin modules resolved from outside the trust root
    spec = entry("pytest_plugins")
    if spec:
        for plugin in obs["pytest_plugin_modules"]:
            if not in_trust_root(plugin["origin"]):
                problems.append(
                    f"pytest_plugins: {plugin['module']} loaded from {plugin['origin']}, outside "
                    "the authorized trust root")

    # -- PATH resolution of executables the assurance chain depends on
    spec = entry("PATH")
    if spec:
        for executable in spec.get("required_executables") or []:
            found = shutil.which(executable)
            if found is None:
                problems.append(f"PATH: required executable {executable!r} is not resolvable")
            elif not in_trust_root(found):
                bound[f"PATH:{executable}"] = found
                if not spec.get("allow_system_executables", False):
                    problems.append(
                        f"PATH: {executable!r} resolves to {found}, outside the authorized trust "
                        "root, so a shadowing binary could stand in for it")

    # -- every classified mechanism must be one this code actually adjudicates
    adjudicated = {"sitecustomize", "usercustomize", "PYTHONSTARTUP", "PYTHONHOME", "BASH_ENV",
                   "ENV", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "pth_files", "PYTHONPATH",
                   "conftest", "pytest_plugins", "PATH"}
    for name, spec in sorted(mechanisms.items()):
        if name in adjudicated:
            continue
        if spec["disposition"] != NOT_APPLICABLE:
            problems.append(
                f"{name}: the policy classifies this mechanism {spec['disposition']} but no "
                "executable check adjudicates it. A policy entry without enforcement is "
                "documentation, which is what ADV-I28AF-02 was.")

    return {"mechanisms": len(mechanisms), "problems": problems, "clean": not problems,
            "inert_observations": inert, "bound": bound, "surface": obs,
            "policy_sha256": hashlib.sha256(
                POLICY.read_bytes() if POLICY.is_file() else b"").hexdigest()}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Adjudicate the startup surface against the policy.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--observe", action="store_true", help="print the observed surface only")
    args = ap.parse_args(argv)
    if args.observe:
        print(json.dumps(observe(), indent=1, sort_keys=True))
        return 0
    try:
        result = check()
    except StartupPolicyError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("STARTUP POLICY: refused")
        return 2
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "surface"}, indent=2))
    else:
        print(f"  {result['mechanisms']} mechanism(s); problems {len(result['problems'])}")
        for p in result["problems"]:
            print(f"    {p}")
    print("STARTUP POLICY: " + ("clean" if result["clean"] else "PROBLEMS"))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
