#!/usr/bin/env python3
"""Structural CI invocation model — Gate 4N-I24C, findings I24C-06 and I24C-07.

WHAT THIS REPLACES AND WHY.

Gate 4N-I23 asserted CI wiring like this:

    block = text.split("id: package_coherence", 1)[1].split("\\n      - name:", 1)[0]
    assert "scripts/commit_package_coherence.py" in block
    assert "echo" not in block.split("run:", 1)[-1].split("\\n")[0]

Both assertions are defeated, and the adversarial lane executed the defeat:

  * With `run: |` the first line after `run:` is " |", so the echo assertion inspects " |"
    and CANNOT FAIL. Eight of the thirty-three graded steps already use block scalars.
  * The command assertion is a bare substring, satisfied by the echo's OWN ARGUMENT — so
    `echo NO-OP scripts/commit_package_coherence.py` passes it.

Replacing package_coherence and containment with those echoes produced 1988 passed / 83
skipped — the frozen candidate's exact claimed result — with every guard at 0.

Worse (finding I24C-07): `policy_tests`, the ONLY step that runs pytest, was referenced by
ZERO tests. Echoing it changed nothing. Every mitigation elsewhere that reads "the suite
catches it" terminates at that one unguarded step.

THE CORRECTION, AND WHERE IT BELONGS. The adversarial lane's diagnosis is adopted verbatim:
`commit_package_coherence` requiring referenced paths to EXIST is that module doing its
stated job — presence in the commit. It is not an invocation oracle and must not be widened
into one. The defect is that NOTHING asserted invocation. So this module is new, separate,
and narrow: it answers "is this command actually EXECUTED by this step?" and nothing else.

FAIL-CLOSED PARSING. A graded step whose execution semantics cannot be determined is
UNKNOWN, and UNKNOWN is a finding. This module deliberately does not depend on PyYAML,
because the CI guards run under the system `python3` while the test suite runs under a
different interpreter; a control that is only available to the richer environment is the
class of defect this chain keeps removing.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONTRACT = REPO_ROOT / "tests" / "fixtures" / "ci-invocation-contract.json"

# Execution classifications. Only INVOKED counts as running a command.
INVOKED = "INVOKED"
ECHOED = "ECHOED"
COMMENTED = "COMMENTED"
ASSIGNED_NEVER_RUN = "ASSIGNED_NEVER_RUN"
DEAD_BRANCH = "DEAD_BRANCH"
AFTER_UNCONDITIONAL_EXIT = "AFTER_UNCONDITIONAL_EXIT"
MASKED = "MASKED"
DEFINED_NEVER_CALLED = "DEFINED_NEVER_CALLED"
HEREDOC_DATA = "HEREDOC_DATA"
UNKNOWN = "UNKNOWN"

NON_EXECUTING = {ECHOED, COMMENTED, ASSIGNED_NEVER_RUN, DEAD_BRANCH,
                 AFTER_UNCONDITIONAL_EXIT, MASKED, DEFINED_NEVER_CALLED, HEREDOC_DATA,
                 UNKNOWN}

# Commands that consume their arguments as DATA rather than executing them.
DATA_CONSUMERS = {"echo", "printf", "cat", "true", ":", "false", "test", "["}

# Operators that mask a failure, so a command under them cannot fail the step.
#
# GATE 4N-I26B, closing I26B-03. This tuple is NO LONGER AUTHORITATIVE. Gate 4N-I25's
# adversarial lane showed it was a four-string literal that caught `|| true` because the string
# was spelled here and missed `|| echo 'suite non-blocking'` because it was not — leaving pytest
# classified INVOKED, the model CLEAN, and the step exiting 0 whatever the tests did. A longer
# list has the same shape: recognising bad forms means the unrecognised form passes.
#
# scripts/failure_propagation.py now decides masking by asking whether a non-zero exit is
# PROVEN to reach the step's status, answering NO unless the structure shows otherwise, and
# failing closed on UNKNOWN. This tuple is retained ONLY as a fast pre-filter for the
# invocation classifier's own bookkeeping; it decides nothing on its own, and
# tests/test_i26b_eleven_findings.py asserts that the authoritative verdict comes from the
# structural analyser.
MASKING = ("|| true", "||true", "|| :", "continue-on-error")


class InvocationError(RuntimeError):
    """Fail-closed."""


# --------------------------------------------------------------------------- #
# structural step extraction (stdlib only, deliberately)
# --------------------------------------------------------------------------- #

_ID = re.compile(r"^(?P<indent>\s+)id:\s*(?P<id>[A-Za-z0-9_-]+)\s*$")


def _dedent_block(lines: list[str]) -> str:
    body = [ln for ln in lines if ln.strip()]
    if not body:
        return ""
    pad = min(len(ln) - len(ln.lstrip()) for ln in body)
    return "\n".join(ln[pad:] if len(ln) >= pad else ln for ln in lines)


def parse_steps(text: str | None = None) -> list[dict]:
    """Every step carrying an `id:`, with its `run` scalar recovered exactly.

    Block scalars (`run: |`) are recovered as their FULL multi-line content, which is the
    whole point: the I23 guard inspected the rendered " |" marker instead.
    """
    text = WORKFLOW.read_text(encoding="utf-8") if text is None else text
    lines = text.splitlines()
    steps: list[dict] = []

    for i, line in enumerate(lines):
        m = _ID.match(line)
        if not m:
            continue
        indent = len(m.group("indent"))
        step = {"id": m.group("id"), "name": None, "run": None, "form": None,
                "continue_on_error": False, "line": i + 1}

        # Scan the surrounding mapping at the same indent until the next list item.
        j = i - 1
        while j >= 0 and lines[j].strip() and not lines[j].lstrip().startswith("- "):
            j -= 1
        start = max(j, 0)
        k = start + 1
        while k < len(lines):
            ln = lines[k]
            if k > i and ln.strip().startswith("- name:"):
                break
            stripped = ln.strip()
            cur = len(ln) - len(ln.lstrip())
            if stripped.startswith("name:") and step["name"] is None and cur <= indent:
                step["name"] = stripped[5:].strip()
            if stripped.startswith("continue-on-error:"):
                step["continue_on_error"] = "true" in stripped.lower()
            if re.match(r"^run:\s*\|", stripped):
                block, k2 = [], k + 1
                while k2 < len(lines):
                    nxt = lines[k2]
                    if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= cur:
                        break
                    block.append(nxt)
                    k2 += 1
                step["run"] = _dedent_block(block)
                step["form"] = "block"
                k = k2
                continue
            if stripped.startswith("run:") and step["run"] is None:
                step["run"] = stripped[4:].strip()
                step["form"] = "single"
            k += 1
        steps.append(step)
    return steps


# --------------------------------------------------------------------------- #
# shell execution analysis
# --------------------------------------------------------------------------- #

def analyse_shell(script: str) -> list[dict]:
    """Classify every command position in a shell fragment.

    Conservative by construction: anything not confidently understood is UNKNOWN, and
    UNKNOWN is treated as non-executing for a graded step.
    """
    if script is None:
        return []
    results: list[dict] = []
    in_heredoc = None
    dead_depth = 0
    seen_unconditional_exit = False
    pending_continuation = ""

    for raw in script.splitlines():
        line = raw

        if in_heredoc is not None:
            if line.strip() == in_heredoc:
                in_heredoc = None
            else:
                results.append({"text": line.strip(), "class": HEREDOC_DATA, "argv": []})
            continue

        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            results.append({"text": stripped, "class": COMMENTED, "argv": []})
            continue

        hd = re.search(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?", stripped)
        if hd:
            in_heredoc = hd.group(1)

        if pending_continuation:
            stripped = pending_continuation + " " + stripped
            pending_continuation = ""
        if stripped.endswith("\\"):
            pending_continuation = stripped[:-1].strip()
            continue

        # dead branches
        if re.match(r"^if\s+false\b", stripped) or re.match(r"^if\s+\[\s+false\s+\]", stripped):
            dead_depth += 1
            results.append({"text": stripped, "class": DEAD_BRANCH, "argv": []})
            continue
        if stripped.startswith("fi") and dead_depth:
            dead_depth -= 1
            continue
        if dead_depth:
            results.append({"text": stripped, "class": DEAD_BRANCH, "argv": []})
            continue

        if re.match(r"^exit\s+0\s*$", stripped):
            seen_unconditional_exit = True
            continue
        if seen_unconditional_exit:
            results.append({"text": stripped, "class": AFTER_UNCONDITIONAL_EXIT, "argv": []})
            continue

        masked = any(tok in stripped for tok in MASKING)

        # Split on separators that start a NEW command position, KEEPING the separator so a
        # position's reachability can be judged. Gate 4N-I24C: `true || python3 scripts/x.py`
        # previously classified the right-hand side as INVOKED. A command after `||` runs only
        # if the left side FAILS, so it is conditional, and `true ||` makes it unreachable.
        # Conservatively, anything reachable only through `||` is not a guaranteed invocation.
        pieces = re.split(r"(&&|\|\||;|\|)", stripped)
        after_or = False
        for idx in range(0, len(pieces), 2):
            part = pieces[idx]
            sep_before = pieces[idx - 1] if idx > 0 else None
            if sep_before == "||":
                after_or = True
            seg = part.strip()
            if not seg:
                continue
            if after_or:
                results.append({"text": seg, "class": MASKED, "argv": []})
                continue
            # pure assignment with no command after it
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg) and not re.search(r"=\S*\s+\S", seg):
                results.append({"text": seg, "class": ASSIGNED_NEVER_RUN, "argv": []})
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*\(\)\s*\{?", seg):
                results.append({"text": seg, "class": DEFINED_NEVER_CALLED, "argv": []})
                continue
            try:
                argv = shlex.split(seg, comments=True)
            except ValueError:
                results.append({"text": seg, "class": UNKNOWN, "argv": []})
                continue
            if not argv:
                continue
            # strip leading env assignments (VAR=x cmd ...)
            head = 0
            while head < len(argv) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[head]):
                head += 1
            argv = argv[head:]
            if not argv:
                results.append({"text": seg, "class": ASSIGNED_NEVER_RUN, "argv": []})
                continue
            cmd = argv[0].lstrip("(").strip()
            if cmd in DATA_CONSUMERS:
                results.append({"text": seg, "class": ECHOED, "argv": argv})
                continue
            results.append({"text": seg, "class": MASKED if masked else INVOKED, "argv": argv})
    return results


INTERPRETERS = ("python", "python3", "bash", "sh", "zsh")

# GATE 4N-I27R. Flags after which the interpreter does NOT execute a trailing script path:
# it prints something (or runs inline source) and exits. A path following one of these is an
# argument, never an invocation. Enumerated deliberately and used only to REFUSE — the default
# for an unrecognised flag remains "keep looking for the script", and the fail-closed property
# comes from _executable_positions() yielding nothing when no script is found.
_NON_EXECUTING_FLAGS = frozenset({
    "--version", "-V", "-h", "--help", "--help-env", "--help-xoptions",
    "--help-all", "-VV",
})

# Flags whose NEXT token is their value, not the script. Without this `python3 -X dev app.py`
# would read `dev` as the script and credit no invocation — fail-closed, but a false negative
# that would surface as a spurious must-invoke finding.
_VALUE_TAKING_FLAGS = frozenset({"-X", "-W", "--check-hash-based-pycs"})


def _executable_positions(argv: list[str]) -> list[str]:
    """The tokens this command actually RUNS, as opposed to tokens it merely mentions.

    GATE 4N-I27O. This is the whole of the DATA_CONSUMERS repair, and it is an INVERSION.

    THE DEFECT. `invoked_targets` used to scan EVERY token of a command for anything shaped
    like a repository path. `echo python3 scripts/allow_model.py` therefore yielded the target
    `scripts/allow_model.py`, and the only thing standing between that and a satisfied
    must-invoke assertion was `echo` happening to appear in the hand-written DATA_CONSUMERS
    set. Empty that set and the echo substitution passes the contract — the exact defeat
    Gate 4N-I24C was created to stop, reachable again through a different door.

    A LONGER LIST IS THE SAME DEFECT. `printf`, `cat`, `:`, a shell function, a wrapper
    script — recognising commands that do NOT execute their arguments means the unrecognised
    one passes. So the question is inverted, the same way failure_propagation.py inverted the
    masking question: a path counts as invoked only when its position PROVES execution — it is
    the program itself, or the script argument of a recognised interpreter. Anything else,
    including any command this module has never heard of, yields NO target and the must-invoke
    assertion fails closed.
    """
    if not argv:
        return []
    program = argv[0]
    out = [program]
    base = program.rsplit("/", 1)[-1]
    if base not in INTERPRETERS and not base.startswith("python"):
        return out
    index = 1
    while index < len(argv):
        token = argv[index]
        # GATE 4N-I27R. An OPTION-TERMINATING or INLINE-SOURCE flag means the interpreter never
        # runs the trailing path. Gate 4N-I27Q's adversarial lane defeated the must-invoke
        # contract with `python3 --version scripts/leak_scan.py`: the old loop skipped every
        # '-'-prefixed token as "a flag, not the script" and harvested the path behind it,
        # while `python3 --version <anything>` prints a version and exits 0 having executed
        # nothing. `-c` is the same shape — the code comes from the argument, and any path
        # after it is sys.argv data, not a script.
        if token in _NON_EXECUTING_FLAGS or token.startswith("--version="):
            return out
        if token in ("-c",):
            return out
        if token == "-m":                     # `python -m pytest` runs the MODULE
            if index + 1 >= len(argv):
                return out
            module = argv[index + 1]
            out.append(f"MODULE:{module}")
            if module.rsplit("/", 1)[-1] != "pytest":
                return out
            # A test RUNNER's positional arguments are the paths it executes, so they are
            # executable positions too — `python -m pytest tests/` really does run tests/.
            # This is deliberately limited to the runner: for any other module the arguments
            # are data and are not harvested.
            for rest in argv[index + 2:]:
                if not rest.startswith("-"):
                    out.append(rest)
            return out
        if token == "-":                      # `python3 - <<'PY'` runs inline source
            out.append("INLINE")
            return out
        if token in _VALUE_TAKING_FLAGS:      # e.g. `-X dev` — the value is not the script
            index += 2
            continue
        if token.startswith("-"):             # an interpreter flag, not the script
            index += 1
            continue
        out.append(token)                     # the script the interpreter executes
        return out
    return out


def invoked_targets(script: str) -> set[str]:
    """Repository paths this fragment actually EXECUTES (directly or as an interpreter arg).

    A target is recorded only from an EXECUTABLE POSITION (see _executable_positions); a path
    that appears merely as an argument to some other command is data, not an invocation.
    """
    targets: set[str] = set()
    for c in analyse_shell(script):
        if c["class"] != INVOKED:
            continue
        for tok in _executable_positions(c["argv"]):
            if tok == "INLINE":
                # An inline interpreter heredoc (`python3 - <<'PY'`) is a real invocation even
                # though it names no repository path. Without this the certification gate would
                # look unguarded when it is not.
                targets.add("PYTHON_INLINE")
                continue
            module = tok[len("MODULE:"):] if tok.startswith("MODULE:") else None
            name = module if module is not None else tok
            plain = name.lstrip("./")
            if plain.startswith(("scripts/", "tests/", "apps/", "infra/")):
                targets.add(plain)
            elif name == "pytest" or name.rsplit("/", 1)[-1] == "pytest":
                targets.add("PYTEST")
            elif name.rsplit("/", 1)[-1] == "tofu":
                targets.add("TOFU")
    return targets


# --------------------------------------------------------------------------- #
# the authored contract — the INDEPENDENT oracle for invocation
# --------------------------------------------------------------------------- #

def contract() -> dict:
    if not CONTRACT.exists():
        raise InvocationError(
            f"the authored CI invocation contract is absent: {CONTRACT}. Absence must never be "
            "read as 'every step is fine'.")
    doc = json.loads(CONTRACT.read_text(encoding="utf-8"))
    steps = doc.get("graded_steps")
    if not isinstance(steps, dict) or not steps:
        raise InvocationError("the contract declares no graded steps")
    return doc


# --------------------------------------------------------------------------- #
# GATE 4N-I28Y: which tests a graded command RUNS is part of its contract
# --------------------------------------------------------------------------- #
#
# Gate 4N-I28X proved that `must_invoke` alone is not a contract over a test command. Adding
# `--deselect <node id>` to the graded pytest step satisfied `must_invoke: [PYTEST, tests/]`
# perfectly while removing the entire assertion-control system from the run. The step still
# invoked pytest; it just no longer ran the controls.
#
# So a spec may now also state which options MUST be present (`required_options`, each a token
# sequence such as ["-p", "pytest_session_guard"]) and which MUST NOT (`forbidden_options`).
# Equivalent orderings and joined forms (`-ppytest_session_guard`, `--deselect=x`) are handled,
# because a contract defeated by a space would be theatre.
#
# This is a NECESSARY condition, not a sufficient one: the final authority on which tests ran is
# the session guard observing the real session. Both exist because they fail differently — the
# contract catches the workflow edit before it runs, the guard catches whatever reaches the run.

def _pytest_options(commands: list[dict]) -> list[list[str]]:
    """The option tokens of each INVOKED pytest run, from AFTER the pytest token.

    Slicing matters: `python -m pytest` carries an interpreter `-m` that has nothing to do with
    marker selection, and a contract that could not tell those apart would refuse its own
    workflow. Only what follows the pytest token selects tests.
    """
    out = []
    for c in commands:
        if c["class"] != INVOKED:
            continue
        argv = c["argv"]
        head = 0
        while head < len(argv) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[head]):
            head += 1                                   # leading VAR=value assignments
        idx = None
        if head < len(argv) and argv[head].rsplit("/", 1)[-1] == "pytest":
            idx = head                                  # pytest run directly
        else:
            for i in range(head, len(argv) - 1):
                if argv[i] == "-m" and argv[i + 1] == "pytest":
                    idx = i + 1                         # python -m pytest
                    break
        if idx is None:
            # `pip install pytest` names pytest as DATA, not as the program being run. Treating
            # it as a pytest invocation would demand the guard plugin of an installer.
            continue
        out.append(argv[idx + 1:])
    return out


def _has_sequence(argv: list[str], seq: list[str]) -> bool:
    """True when `seq` appears in argv, spaced or joined (`-p x` or `-px`)."""
    n = len(seq)
    for i in range(len(argv) - n + 1):
        if argv[i:i + n] == seq:
            return True
    if n == 2:
        joined = seq[0] + seq[1]
        if joined in argv:
            return True
        if f"{seq[0]}={seq[1]}" in argv:
            return True
    return n == 1 and seq[0] in argv


def _option_problems(sid: str, spec: dict, commands: list[dict]) -> list[str]:
    required = [list(o) if isinstance(o, (list, tuple)) else [o]
                for o in (spec.get("required_options") or [])]
    forbidden = list(spec.get("forbidden_options") or [])
    if not required and not forbidden:
        return []
    problems = []
    runs = _pytest_options(commands)
    if not runs:
        problems.append(f"{sid}: no INVOKED pytest command was found, so its option contract "
                        "cannot be satisfied")
        return problems
    for argv in runs:
        for seq in required:
            if not _has_sequence(argv, seq):
                problems.append(
                    f"{sid}: the graded pytest command does not carry the required option "
                    f"{' '.join(seq)!r}. Without it the mandatory assurance controls are not "
                    "observed and their removal would be silent (Gate 4N-I28X).")
        for opt in forbidden:
            hit = any(a == opt or a.startswith(opt + "=") or
                      (len(opt) == 2 and opt.startswith("-") and a.startswith(opt) and a != opt)
                      for a in argv)
            if hit:
                problems.append(
                    f"{sid}: the graded pytest command carries the selection-altering option "
                    f"{opt!r}. Which tests run is part of this contract: {opt!r} can remove a "
                    "mandatory control while the step still 'invokes pytest'.")
    return problems


def check(text: str | None = None) -> dict:
    doc = contract()
    required = doc["graded_steps"]
    steps = {s["id"]: s for s in parse_steps(text)}
    problems: list[str] = []
    rows = []

    for sid, spec in sorted(required.items()):
        step = steps.get(sid)
        if step is None:
            problems.append(f"{sid}: graded step is ABSENT from the workflow")
            rows.append({"id": sid, "present": False, "class": "ABSENT"})
            continue
        if step["continue_on_error"]:
            problems.append(f"{sid}: continue-on-error is set, so its failure cannot fail the job")
        commands = analyse_shell(step["run"])
        targets = invoked_targets(step["run"])
        wanted = set(spec.get("must_invoke", []))
        missing = sorted(wanted - targets)
        for w in missing:
            classes = sorted({c["class"] for c in commands
                              if any(w in a for a in c["argv"]) or w in c["text"]})
            why = f" (present but classified {classes})" if classes else " (absent entirely)"
            problems.append(f"{sid}: must invoke {w!r} and does not{why}")
        problems += _option_problems(sid, spec, commands)
        rows.append({"id": sid, "present": True, "form": step["form"],
                     "invoked": sorted(targets), "required": sorted(wanted),
                     "missing": missing,
                     "commands": [{"class": c["class"], "text": c["text"][:90]} for c in commands]})

    # every graded id in the workflow must be in the contract — a NEW step with no
    # invocation assertion is exactly finding I24C-07's shape.
    for sid in sorted(steps):
        if sid not in required:
            problems.append(f"{sid}: workflow declares a graded step the contract does not cover")

    # GATE 4N-I24C: a step whose OUTCOME never reaches the guard result list cannot fail the
    # job, so proving it invokes the right command is not enough. A substring-preserving
    # rename of the guard entry (`package_coherence=` -> `package_coherence_x=`) leaves the
    # step running and its result unread.
    workflow_text = WORKFLOW.read_text(encoding="utf-8") if text is None else text
    for sid in sorted(required):
        if sid not in steps:
            continue
        entry = f'"{sid}=${{{{ steps.{sid}.outcome }}}}"'
        if entry not in workflow_text:
            problems.append(
                f"{sid}: its outcome is not read by the guard result list "
                f"(expected the exact entry {entry}), so its failure would not fail the job")

    return {"contract": str(CONTRACT), "graded_in_contract": len(required),
            "graded_in_workflow": len(steps),
            "multiline_steps": sum(1 for s in steps.values() if s["form"] == "block"),
            "rows": rows, "problems": problems, "clean": not problems}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = check()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  graded steps: workflow {result['graded_in_workflow']} / "
              f"contract {result['graded_in_contract']}  "
              f"(multiline run|: {result['multiline_steps']})")
        for p in result["problems"]:
            print(f"    {p}", file=sys.stderr)
        print("CI INVOCATION:", "clean" if result["clean"] else "findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
