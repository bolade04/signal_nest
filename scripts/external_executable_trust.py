#!/usr/bin/env python3
"""Trust policy for the external executables the assurance path invokes (Gate 4N-I28AI).

THE DEFECT THIS CLOSES. Gate 4N-I28AH finding ADV-I28AH-02. `startup_policy` resolved `git` and
`bash` with `shutil.which`, recorded the resolved path, and — because `allow_system_executables`
was true — skipped the trust-root refusal. Nothing ever compared the recorded path to an
expectation, so recording was evidence, not a control. Measured: a fake `git` placed earlier on
PATH resolved and the policy still reported `clean=True`. The same held for `bash`.

WHY THE CLASSIFICATION IS "APPROVED_PATH_SET_AND_CONTENT_BOUND" AND NOT "EXACT_PATH_AND_CONTENT".
Pinning a repository constant to the sha256 of `/usr/bin/git` would bind this package to one
machine image: the CI runner's git is a different binary from a developer's, so a pinned digest
would fail everywhere except where it was generated, and the predictable response would be to
delete the check. What is enforced instead:

  * resolution must land in an AUTHORED approved path set — a temp-directory shadow is refused,
    which is the actual attack;
  * the resolved path must be a real file, executable, and not a symlink out of the approved set
    (symlinks are resolved before the membership test, so a name that looks approved is not);
  * the CONTENT digest is captured at establish() and re-compared at session finish, so a binary
    swapped mid-run is caught even though its digest was never pinned in the repository.

That is weaker than a repository-pinned digest and stronger than path observation. Saying so
plainly matters more than claiming a stronger property: this is session-consistent content binding
over an approved path set, not cryptographic pinning to an authored constant.

CONFIGURATION IS PART OF THE EXECUTABLE'S BEHAVIOUR. A bound `git` binary whose behaviour is
steered by config is not trustworthy, so `git_invocation()` returns an environment with system and
global config neutralised, hooks and pager and editor and credential helpers and external diff
disabled, and no config-include path inherited. `bash_invocation()` returns `--noprofile --norc`
with `BASH_ENV`/`ENV` removed. `check()` additionally REFUSES the session when a variable that can
change what a read-only command reports is present — `GIT_CONFIG*`, `GIT_DIR`, `GIT_WORK_TREE`,
`GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_EXTERNAL_DIFF`, `BASH_ENV`, `ENV` and the rest of
FATAL_STEERING_VARIABLES — because a variable that must be neutralised at every call site is better
refused once. Variables that only reach interactive operations are stripped and RECORDED, not
refused; see the comment on NEUTRALIZED_STEERING_VARIABLES for why that split exists.

HONEST RESIDUAL. Call sites in this repository invoke bare `"git"` and `"bash"` and let PATH
resolve them. That stays safe by induction rather than by construction: this module refuses the
session when PATH resolution does not land on the approved binary, so by the time any call site
runs, a bare name can only resolve to the binary that was validated. `git_invocation()` and
`bash_invocation()` expose the validated absolute path for call sites that want construction
instead of induction; migrating the existing call sites is not required to close ADV-I28AH-02 and
is deliberately left out of this gate's scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "tests" / "fixtures" / "executable-trust-policy.json"

EXACT_PATH_AND_CONTENT_BOUND = "EXACT_PATH_AND_CONTENT_BOUND"
APPROVED_PATH_SET_AND_CONTENT_BOUND = "APPROVED_PATH_SET_AND_CONTENT_BOUND"
POLICY_TRUSTED_WITH_EXPLICIT_JUSTIFICATION = "POLICY_TRUSTED_WITH_EXPLICIT_JUSTIFICATION"
PROHIBITED = "PROHIBITED"
NOT_APPLICABLE = "NOT_APPLICABLE"
CURRENT_INTERPRETER_IDENTITY_BOUND = "CURRENT_INTERPRETER_IDENTITY_BOUND"
UNREACHABLE_FROM_GRADED_ROOTS = "UNREACHABLE_FROM_GRADED_ROOTS"
REACHABLE_NOT_EXERCISED_IN_GRADED_PATH = "REACHABLE_NOT_EXERCISED_IN_GRADED_PATH"
# GATE 4N-I28AS. An executable whose identity is a CHAIN rather than a file — npm is a wrapper for a
# JavaScript entrypoint executed by a separately-resolved Node — is adjudicated by `npm_authority`,
# which binds the whole chain. Delegation is explicit so that "this file's digest" can never again
# stand in for "this toolchain's identity".
TOOLCHAIN_IDENTITY_DELEGATED = "TOOLCHAIN_IDENTITY_DELEGATED"
CLASSIFICATIONS = (EXACT_PATH_AND_CONTENT_BOUND, APPROVED_PATH_SET_AND_CONTENT_BOUND,
                   CURRENT_INTERPRETER_IDENTITY_BOUND, UNREACHABLE_FROM_GRADED_ROOTS,
                   POLICY_TRUSTED_WITH_EXPLICIT_JUSTIFICATION,
                   REACHABLE_NOT_EXERCISED_IN_GRADED_PATH, TOOLCHAIN_IDENTITY_DELEGATED,
                   PROHIBITED, NOT_APPLICABLE)

# Environment that steers git or bash behaviour without changing either binary.
#
# The split is deliberate and was forced by measurement. A first version refused ANY steering
# variable and immediately failed on an honest developer machine, where an IDE exports GIT_EDITOR
# and GIT_ASKPASS — the "control you must disable to use it" failure this chain has hit before.
# What matters is whether a variable can change what the READ-ONLY commands this repository runs
# (show, ls-tree, diff, archive, rev-parse, remote get-url) actually report.
#
# FATAL: redirects the repository, the object store, the config, or the shell's startup, so it
# changes the ANSWER a read-only command gives.
FATAL_STEERING_VARIABLES = (
    "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT",
    "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_TEMPLATE_DIR", "GIT_NAMESPACE", "GIT_ATTR_NOSYSTEM",
    "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS",
)
# NEUTRALIZED: only reached by interactive operations (editing a message, prompting for a
# credential, paging output). No assurance command performs any of those. They are stripped from
# every invocation environment anyway, and recorded rather than refused. The claim that they cannot
# influence a read-only command is proven by an executable test, not asserted.
NEUTRALIZED_STEERING_VARIABLES = (
    "GIT_PAGER", "GIT_EDITOR", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS",
    "GIT_TERMINAL_PROMPT",
)
STEERING_VARIABLES = FATAL_STEERING_VARIABLES + NEUTRALIZED_STEERING_VARIABLES


class ExecutableTrustError(RuntimeError):
    """Fail closed. An executable whose identity cannot be established is never invoked."""


def load_policy(path: Path | None = None) -> dict:
    p = path or POLICY
    if not p.is_file():
        raise ExecutableTrustError(
            f"the executable trust policy is missing at {p}. Without it every required executable "
            "would be PATH-discovered, which is the Gate 4N-I28AH defect.")
    doc = json.loads(p.read_text(encoding="utf-8"))
    executables = doc.get("executables")
    if not isinstance(executables, dict) or not executables:
        raise ExecutableTrustError("the executable trust policy governs no executable")
    for name, entry in executables.items():
        if entry.get("classification") not in CLASSIFICATIONS:
            raise ExecutableTrustError(
                f"{name}: classification {entry.get('classification')!r} is not one of "
                f"{CLASSIFICATIONS}")
        if entry["classification"] == APPROVED_PATH_SET_AND_CONTENT_BOUND \
                and not entry.get("approved_paths"):
            raise ExecutableTrustError(f"{name}: an approved path set must not be empty")
    return doc


def _digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve(name: str, entry: dict) -> dict:
    """Resolve one executable and record everything the policy needs to judge it."""
    found = shutil.which(name)
    record: dict = {"executable": name, "which": found,
                    "classification": entry["classification"]}
    if found is None:
        # GATE 4N-I28AO. Some bound executables legitimately do not exist on every machine that
        # runs the assurance suite — `docker` is absent from this development host and present on
        # the CI runner. An entry may declare `required_present: false`, which means "bind it
        # WHENEVER it resolves". That is fail-closed in the direction that matters: a shadow at an
        # unapproved path is still refused. Absence is not a bypass, because the steps that use the
        # executable fail outright without it. The default remains required_present = true, so no
        # existing entry is weakened.
        if entry.get("required_present", True):
            record["problem"] = f"{name}: required executable is not resolvable on PATH"
        else:
            record["absent_and_not_required"] = True
        return record
    raw = Path(found)
    real = raw.resolve()
    record.update({"resolved_path": str(real), "is_symlink": raw.is_symlink(),
                   "symlink_target": str(real) if raw.is_symlink() else None})
    try:
        st = raw.stat()
    except OSError as exc:
        record["problem"] = f"{name}: cannot stat {found} ({exc})"
        return record
    record["mode"] = oct(stat.S_IMODE(st.st_mode))
    if not os.access(found, os.X_OK):
        record["problem"] = f"{name}: {found} is not executable"
        return record

    # GATE 4N-I28AS removed the `approved_path_prefixes` allowance entirely.
    #
    # Gate 4N-I28AP finding ADV-I28AP-02: a prefix match set `approved = []`, SKIPPING the
    # membership test below, so any file beneath an approved installation root was accepted and the
    # attacker's digest was recorded as the bound content. Six attacker layouts under
    # `~/.nvm/versions/node/` were accepted with trust clean and the bootstrap established.
    #
    # npm was the mechanism's only user, so deleting it makes the defect UNREACHABLE rather than
    # merely unused, and a policy still carrying prefixes is refused in load_policy(). npm is now
    # adjudicated by `npm_authority`, which binds the installation tree, the Node<->npm
    # relationship and the manager metadata instead of a path shape.
    approved = [Path(p) for p in entry.get("approved_paths") or []]
    # Membership is tested on the RESOLVED target, so a symlink named to look approved does not
    # pass, and a shadow in a temporary directory is refused whatever it is called.
    if approved and real not in [p.resolve() for p in approved if p.exists()]:
        record["problem"] = (
            f"{name}: resolved to {real}, which is not in the approved path set "
            f"{[str(p) for p in approved]}. A binary earlier on PATH cannot stand in for the "
            "approved one.")
        return record
    try:
        record["content_sha256"] = _digest(real)
    except OSError as exc:
        record["problem"] = f"{name}: cannot read {real} for hashing ({exc})"
        return record
    if entry.get("capture_version", True):
        try:
            proc = subprocess.run([str(real), "--version"], capture_output=True, text=True,
                                  timeout=15, env={"PATH": "/usr/bin:/bin"})
            record["version"] = (proc.stdout or proc.stderr).strip().splitlines()[0][:120]
        except (OSError, subprocess.SubprocessError, IndexError):
            record["version"] = None
    return record


def snapshot() -> dict:
    """Identity of every governed executable right now, plus the PATH that produced it."""
    doc = load_policy()
    out = {"path_env": os.environ.get("PATH", ""), "executables": {}}
    for name, entry in sorted(doc["executables"].items()):
        if entry["classification"] in (NOT_APPLICABLE, PROHIBITED,
                                       UNREACHABLE_FROM_GRADED_ROOTS):
            continue
        out["executables"][name] = resolve(name, entry)
    return out


def check(policy: dict | None = None) -> dict:
    doc = policy if policy is not None else load_policy()
    problems: list[str] = []
    records = {}

    # GATE 4N-I28AS. The prefix allowance is retired, and a policy still carrying one is refused
    # rather than silently ignored — an entry whose stated basis no code implements would read as
    # authorization to anyone auditing the policy.
    for name, entry in sorted(doc["executables"].items()):
        if entry.get("approved_path_prefixes"):
            problems.append(
                f"{name}: approved_path_prefixes is retired (Gate 4N-I28AS, closing ADV-I28AP-02). "
                "A path prefix authorizes every file beneath a directory, which is how an attacker "
                "script inside an approved installation root was accepted with its own digest "
                "recorded as the bound identity.")

    for name, entry in sorted(doc["executables"].items()):
        cls = entry["classification"]
        if cls == NOT_APPLICABLE:
            if not entry.get("why_not_applicable"):
                problems.append(f"{name}: NOT_APPLICABLE must be justified")
            continue
        if cls == REACHABLE_NOT_EXERCISED_IN_GRADED_PATH:
            # RETIRED at Gate 4N-I28AM, and refused rather than quietly accepted so it cannot creep
            # back. Gate 4N-I28AL finding ADV-I28AL-02: this classification meant "unbound unless
            # runtime tracing happens to observe it later", and the tracing never ran in a graded
            # session — the bootstrap calls check() with no trace, so the contradiction loop
            # iterated an empty set every time. A trust decision that trails execution is not a
            # trust decision.
            problems.append(
                f"{name}: REACHABLE_NOT_EXERCISED_IN_GRADED_PATH is retired. A statically "
                "reachable executable must be bound BEFORE graded work, proven unreachable by an "
                "executable precondition, or prohibited.")
            continue
        if cls == UNREACHABLE_FROM_GRADED_ROOTS:
            # Section 7 option B: an EXECUTABLE precondition, not an authored assertion. The claim
            # is that no release command root can reach this executable, and it is re-derived here
            # on every check so it fails closed the moment a graded root starts naming it.
            problems.extend(_graded_reachability_problems(name, entry))
            continue

        if cls == TOOLCHAIN_IDENTITY_DELEGATED:
            # The whole chain, adjudicated where the chain is understood. Its problems are surfaced
            # here so a session refuses at the same boundary as any other untrusted executable.
            import npm_authority

            delegate = entry.get("delegated_to")
            if delegate != "npm_authority":
                problems.append(
                    f"{name}: TOOLCHAIN_IDENTITY_DELEGATED must name the layer that adjudicates it; "
                    f"delegated_to={delegate!r} is not a layer this module knows.")
                continue
            outcome = npm_authority.verify()
            records[name] = {"executable": name, "classification": cls,
                             "delegated_to": delegate,
                             "family": outcome.get("family"),
                             "authority_model": outcome.get("authority_model"),
                             "canonical_npm": outcome["chain"].get("canonical_npm"),
                             "canonical_node": outcome["chain"].get("canonical_node"),
                             "npm_sha256": outcome["chain"].get("npm_sha256")}
            problems.extend(f"{name}: {p}" for p in outcome["problems"])
            continue

        if cls == CURRENT_INTERPRETER_IDENTITY_BOUND:
            resolved = shutil.which(name)
            records[name] = {"executable": name, "classification": cls,
                             "sys_executable": sys.executable, "which": resolved,
                             "matches_parent": resolved is not None
                             and Path(resolved).resolve() == Path(sys.executable).resolve()}
            if not entry.get("interpreter_rule"):
                problems.append(f"{name}: an interpreter classification must state its rule")
            # WHAT IS ENFORCED, and what deliberately is not.
            #
            # The rule is that a child Python process must BE sys.executable. It is NOT that no
            # other python may exist on PATH: a first version refused whenever a PATH-resolved
            # interpreter differed from the running one, and on this machine `python` is conda's
            # while pytest runs under the framework build — so it refused an entirely honest
            # session. That is the "control you must disable to use it" failure this chain has hit
            # before, and a control that must be switched off is not a control.
            #
            # So the PATH-resolved interpreter is RECORDED as evidence, and what fails closed is
            # the thing that actually matters: the running interpreter must be usable as the child
            # interpreter, because that is what every graded invocation uses.
            if not sys.executable or not os.access(sys.executable, os.X_OK):
                problems.append(
                    f"{name}: sys.executable is {sys.executable!r}, which is not usable as a child "
                    "interpreter, so a Python subprocess would have to fall back to PATH")
            try:
                records[name]["content_sha256"] = _digest(Path(sys.executable).resolve())
            except OSError as exc:
                problems.append(f"{name}: cannot hash the running interpreter ({exc})")
            continue
        if cls == PROHIBITED:
            if shutil.which(name) is not None and entry.get("refuse_if_present", True):
                problems.append(f"{name}: a PROHIBITED executable is resolvable")
            continue
        record = resolve(name, entry)
        records[name] = record
        if "problem" in record:
            problems.append(record["problem"])
        elif cls == POLICY_TRUSTED_WITH_EXPLICIT_JUSTIFICATION and not entry.get("why_trusted"):
            problems.append(f"{name}: POLICY_TRUSTED requires an explicit justification")

    fatal = sorted(v for v in FATAL_STEERING_VARIABLES if os.environ.get(v))
    neutralized = sorted(v for v in NEUTRALIZED_STEERING_VARIABLES if os.environ.get(v))
    if fatal:
        problems.append(
            f"behaviour-steering environment variable(s) {fatal} are set. Each redirects the "
            "repository, the object store, the config or the shell startup, so it changes the "
            "answer a read-only assurance command gives. A bound binary whose behaviour is "
            "redirected by configuration is not a bound behaviour.")

    return {"clean": not problems, "problems": problems, "executables": records,
            "steering_variables_fatal": fatal,
            "steering_variables_neutralized": neutralized,
            "path_env_sha256": hashlib.sha256(
                os.environ.get("PATH", "").encode()).hexdigest()[:32],
            "policy_sha256": hashlib.sha256(
                POLICY.read_bytes() if POLICY.is_file() else b"").hexdigest()}


def _graded_reachability_problems(name: str, entry: dict) -> list:
    """Verify that nothing reachable from a release command root names this executable."""
    declared = list(entry.get("call_site_modules") or [])
    if not declared:
        return [f"{name}: UNREACHABLE_FROM_GRADED_ROOTS must declare its call_site_modules"]
    try:
        import executable_inventory
        import site_taxonomy
        roots = {r["module"] for r in site_taxonomy.release_roots()}
        sites = {i["module"] for i in executable_inventory.static_inventory()["invocations"]
                 if i.get("executable") == name}
    except Exception as exc:                       # derivation unavailable -> refuse, never assume
        return [f"{name}: cannot derive graded reachability ({exc}); the unreachability claim "
                "cannot be checked, so it is refused rather than trusted"]
    problems = []
    undeclared = sorted(sites - set(declared))
    if undeclared:
        problems.append(
            f"{name}: call site(s) {undeclared} are not declared in call_site_modules, so the "
            "unreachability claim no longer describes the code")
    graded = sorted(sites & roots)
    if graded:
        problems.append(
            f"{name}: now reachable from release command root(s) {graded}. The "
            "UNREACHABLE_FROM_GRADED_ROOTS precondition has failed; it must be bound instead.")
    return problems


def compare(before: dict, after: dict) -> list:
    """Differences between two snapshots. Used by the session-finish re-verification."""
    problems = []
    if before.get("path_env") != after.get("path_env"):
        problems.append("PATH changed after initial verification, so executable resolution is no "
                        "longer the resolution that was validated")
    for name, prior in (before.get("executables") or {}).items():
        now = (after.get("executables") or {}).get(name)
        if now is None:
            problems.append(f"{name}: was resolvable at initial verification and is not now")
            continue
        for field, label in (("resolved_path", "resolved path"),
                             ("content_sha256", "content digest"),
                             ("symlink_target", "symlink target"),
                             ("mode", "file mode")):
            if prior.get(field) != now.get(field):
                problems.append(
                    f"{name}: {label} changed after initial verification "
                    f"({prior.get(field)} -> {now.get(field)})")
    return problems


# --------------------------------------------------------------------------- invocation
def _hardened_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STEERING_VARIABLES}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",       # ignore /etc/gitconfig
        "GIT_CONFIG_GLOBAL": os.devnull,  # ignore ~/.gitconfig
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    env.update(extra or {})
    return env


def validated_path(name: str) -> str:
    record = resolve(name, load_policy()["executables"][name])
    if "problem" in record:
        raise ExecutableTrustError(record["problem"])
    return record["resolved_path"]


def git_invocation(args) -> tuple:
    """argv using the validated absolute git, plus an environment that cannot steer it."""
    argv = [validated_path("git"), "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat",
            "-c", "diff.external=", "-c", "credential.helper=", "--no-pager", *args]
    return argv, _hardened_env()


def tar_invocation(args) -> tuple:
    """argv using the validated absolute tar. Never re-resolved through PATH after validation."""
    return [validated_path("tar"), *args], _hardened_env()


def bash_invocation(args) -> tuple:
    """argv using the validated absolute bash, with startup files disabled."""
    return [validated_path("bash"), "--noprofile", "--norc", *args], _hardened_env()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Adjudicate external executable trust.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--snapshot", action="store_true")
    args = ap.parse_args(argv)
    if args.snapshot:
        print(json.dumps(snapshot(), indent=1, sort_keys=True))
        return 0
    try:
        result = check()
    except ExecutableTrustError as exc:
        print(f"    {exc}", file=sys.stderr)
        print("EXTERNAL EXECUTABLE TRUST: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for p in result["problems"]:
            print(f"    {p}")
    print("EXTERNAL EXECUTABLE TRUST: " + ("clean" if result["clean"] else "PROBLEMS"))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
