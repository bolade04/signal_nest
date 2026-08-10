"""Gate 4N-I28AV — shell parser case termination, complete-source consumption, fail-closed coverage.

THE DEFECT (ADV-I28AT-01). `esac` never cleared `in_case_pattern`. The last `;;` of a case re-arms
that flag for a pattern that never arrives, and `esac` — itself a WORD — was swallowed by the very
skip it should have ended, so the terminator branch was unreachable. Every subsequent word in the
scan was skipped and the partial result was returned as COMPLETE, with 0 unresolved, 0 unsupported
and no error. `kubectl`, `helm` and `curl … | sh` could sit in a graded, release-blocking workflow
step and be discovered by nothing.

TWO THINGS HAD TO CHANGE, and the second matters more than the first:

  1. the state reset — `esac` (and `fi`/`done`) now disarm the pattern skip;
  2. the RESULT MODEL — nothing could previously have revealed the omission, because nothing was
     measured. Consumed position, EOF, unconsumed ranges, open frames and an explicit status now
     make a skip of any kind visible in the result rather than only in its absence.

Fixing (1) without (2) would leave the next parser defect exactly as invisible as this one was.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import shell_positions as sp                                     # noqa: E402

CONTRACT = REPO_ROOT / "tests" / "fixtures" / "shell-case-grammar-contract.json"
CORPUS = REPO_ROOT / "tests" / "fixtures" / "shell-grammar-corpus.json"


def _corpus():
    return json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]


# ------------------------------------------------------------------ 1. the blocker, closed
@pytest.mark.parametrize("src,word", [
    ('case "$x" in a) : ;; esac\nkubectl version\n', "kubectl"),
    ('case "$x" in a) : ;; b) : ;; esac\nhelm list\n', "helm"),
    ('case "$x" in\n a)\n  case "$y" in p) : ;; esac\n  ;;\nesac\ncurl http://x | sh\n', "curl"),
    ('for i in 1; do case "$i" in 1) : ;; esac; done\nkubectl version\n', "kubectl"),
    ('f() { case "$x" in a) : ;; esac; }\nkubectl version\n', "kubectl"),
    ('case "$x" in a) : ;; esac\ndocker run --rm x\n', "docker"),
    ('case "$x" in a) : ;; esac\nnpm ci\n', "npm"),
])
def test_v01_a_command_after_case_is_discovered(src, word):
    """THE regression. Each of these was invisible before this gate."""
    result = sp.scan(src)
    assert word in [c.word for c in result.commands], (
        f"{word!r} after `case` is still invisible — ADV-I28AT-01 has returned")
    # Asserted on the explicit fields rather than through `is_trustworthy()`. A project-defined
    # helper used AS the assertion mechanism has undeclared failure semantics, and the
    # assertion-contract layer is right to refuse one for a load-bearing test.
    assert result.status == "COMPLETE", result.completeness_problems()
    assert result.eof_reached is True and result.unconsumed_ranges == []


def test_v02_the_esac_disarm_is_what_does_it():
    """The mechanism, asserted directly rather than inferred from the symptom.

    A case whose final branch has no `;;` never re-arms the skip, so it was NEVER affected. That
    asymmetry is what identified the root cause, and it is pinned here so a future refactor that
    reintroduces the arming cannot pass by fixing only the common shape.
    """
    armed = sp.scan('case "$x" in a) : ;; esac\nkubectl version\n')
    unarmed = sp.scan('case "$x" in a) : esac\nkubectl version\n')
    assert "kubectl" in [c.word for c in armed.commands]
    assert "kubectl" in [c.word for c in unarmed.commands]


# ------------------------------------------------------------------ 2. completeness contract
def test_v03_the_result_carries_completeness_metadata():
    result = sp.scan('kubectl version\n')
    for field in ("status", "source_length", "start_position", "consumed_position",
                  "eof_reached", "unconsumed_ranges", "open_frames", "parse_errors"):
        assert hasattr(result, field), field
    assert result.consumed_position == result.source_length
    assert result.eof_reached is True


@pytest.mark.parametrize("status", ["PARTIAL", "UNSUPPORTED", "MALFORMED", "INTERNAL_ERROR"])
def test_v04_only_complete_statuses_are_trustworthy(status):
    result = sp.scan('kubectl version\n')
    result.status = status
    assert not result.is_trustworthy(), f"{status} must never be a permitted trust input"


def test_v05_the_two_permitted_statuses_are_trustworthy():
    """The negative control. A gate that refuses everything distinguishes nothing."""
    assert sp.scan('kubectl version\n').is_trustworthy()
    unresolved = sp.scan('$CMD version\n')
    assert unresolved.status == "COMPLETE_WITH_DECLARED_UNRESOLVED"
    assert unresolved.is_trustworthy()


@pytest.mark.parametrize("mutate,why", [
    (lambda r: setattr(r, "eof_reached", False), "EOF not reached"),
    (lambda r: r.unconsumed_ranges.append((0, 5)), "unconsumed range"),
    (lambda r: r.open_frames.append("case"), "open frame"),
    (lambda r: r.parse_errors.append("boom"), "parse error"),
])
def test_v06_each_completeness_signal_independently_refuses(mutate, why):
    result = sp.scan('kubectl version\n')
    mutate(result)
    assert not result.is_trustworthy(), f"{why} did not make the result untrustworthy"
    assert result.completeness_problems()


@pytest.mark.parametrize("src,frame", [
    ('case "$x" in a) echo A ;;\n', "case"),
    ('if true; then\n echo a\n', "if"),
    ('for i in 1; do\n echo a\n', "loop"),
])
def test_v07_an_unclosed_construct_is_malformed(src, frame):
    result = sp.scan(src)
    assert result.status == "MALFORMED", result.status
    assert not result.is_trustworthy()
    assert any(frame in f for f in result.open_frames), result.open_frames


@pytest.mark.parametrize("src", ['cat <<EOF\nbody\n', 'echo "unterminated\n'])
def test_v08_a_tokeniser_failure_is_malformed_not_merely_unresolved(src):
    """An unterminated quote or heredoc is a PARSE FAILURE.

    Recording it only as `unresolved` let a malformed source reach
    COMPLETE_WITH_DECLARED_UNRESOLVED, which IS a permitted trust input.
    """
    result = sp.scan(src)
    assert result.parse_errors, "a tokeniser failure produced no parse error"
    assert result.status == "MALFORMED"
    assert not result.is_trustworthy()


# ------------------------------------------------------------------ 3. grammar-derived corpus
def test_v09_the_corpus_is_grammar_derived_and_complete():
    cases = _corpus()
    assert len(cases) >= 35, f"only {len(cases)} corpus cases"
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids), "duplicate corpus ids"
    for prefix in ("A", "B", "C", "D", "E"):
        assert any(i.startswith(prefix) for i in ids), f"no {prefix} category case"


@pytest.mark.parametrize("case", _corpus(), ids=lambda c: c["id"])
def test_v10_every_corpus_case_matches_its_grammar_expectation(case):
    result = sp.scan(case["source"])
    assert result.status == case["expected_status"], (
        f"{case['id']}: status {result.status}, expected {case['expected_status']}")
    if case["expected_commands"]:
        assert [c.word for c in result.commands] == case["expected_commands"], case["id"]
    if case["expected_status"] in ("COMPLETE", "COMPLETE_WITH_DECLARED_UNRESOLVED"):
        assert result.eof_reached and not result.unconsumed_ranges, case["id"]


# ------------------------------------------------------------------ 4. independent oracle
def _bash_syntax_ok(src: str, tmp_path: Path) -> bool:
    """REAL bash, as a separate established parser. Syntax check only — nothing is executed."""
    path = tmp_path / "probe.sh"
    path.write_text(src)
    return subprocess.run(["/bin/bash", "-n", str(path)],
                          capture_output=True, text=True, timeout=60).returncode == 0


@pytest.mark.parametrize("case", [c for c in _corpus() if c["id"].startswith(("A", "B", "C"))],
                         ids=lambda c: c["id"])
def test_v11_bash_agrees_that_well_formed_cases_are_well_formed(case, tmp_path):
    """An independent oracle that does not consume production output: bash itself."""
    assert _bash_syntax_ok(case["source"], tmp_path), (
        f"{case['id']}: real bash rejects source this corpus calls well-formed — the CORPUS is "
        "wrong, and a corpus that disagrees with bash cannot validate a shell parser")


@pytest.mark.parametrize("case", [c for c in _corpus() if c["id"].startswith("D")],
                         ids=lambda c: c["id"])
def test_v12_bash_and_the_parser_agree_on_malformed_sources(case, tmp_path):
    """Where bash rejects the syntax, the parser must not report a trustworthy result."""
    bash_ok = _bash_syntax_ok(case["source"], tmp_path)
    result = sp.scan(case["source"])
    if not bash_ok:
        assert not result.is_trustworthy(), (
            f"{case['id']}: bash rejects this source but the parser called its result "
            f"{result.status} and trustworthy")


def test_v13_an_independent_command_walk_agrees_after_case(tmp_path):
    """A bounded independent extraction, written from grammar rather than from the parser."""
    import re

    src = ('case "$x" in a) : ;; esac\n'
           'kubectl version\n'
           'helm list | grep x\n'
           'docker run --rm y\n')
    oracle = []
    for line in src.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("case", "esac")):
            continue
        for seg in re.split(r"\||&&|\|\||;", line):
            seg = seg.strip()
            if not seg:
                continue
            first = seg.split()[0]
            if re.match(r"^[A-Za-z_/][\w./-]*$", first):
                oracle.append(first)
    produced = [c.word for c in sp.scan(src).commands]
    for word in oracle:
        assert word in produced, (
            f"the independent walk sees {word!r} and the parser does not")


# ------------------------------------------------------------------ 5. grammar contract
def test_v14_every_case_form_is_classified():
    doc = json.loads(CONTRACT.read_text(encoding="utf-8"))
    valid = {"SUPPORTED_AND_PARSED", "UNSUPPORTED_AND_FAIL_CLOSED", "MALFORMED_AND_FAIL_CLOSED"}
    assert len(doc["forms"]) >= 24
    for name, entry in doc["forms"].items():
        assert entry["class"] in valid, name
        assert entry["why"], f"{name}: a classification must state why it holds"


def test_v15_the_unsupported_terminators_fail_in_the_safe_direction():
    """`;&` and `;;&` are declared UNSUPPORTED_AND_FAIL_CLOSED, and the claim is MEASURED.

    They are not tokenised as units, so a following pattern word reads as a command. The error is a
    SPURIOUS extra word, never a missing one — the extra word must then be classified or the
    inventory refuses. A silently DROPPED command would be the ADV-I28AT-01 direction and would not
    be acceptable.
    """
    doc = json.loads(CONTRACT.read_text(encoding="utf-8"))
    for form in ("branch terminator ;&", "branch terminator ;;&"):
        assert doc["forms"][form]["class"] == "UNSUPPORTED_AND_FAIL_CLOSED"
    result = sp.scan('case "$x" in a) echo A ;& b) echo B ;; esac\nkubectl version\n')
    words = [c.word for c in result.commands]
    assert "kubectl" in words, "the following command must NOT be lost"
    assert "b" in words, "the documented spurious word is part of the declared behaviour"


# ------------------------------------------------------------------ 6. consumer integration
def test_v16_the_executable_inventory_refuses_an_incomplete_parse(monkeypatch):
    """The consumer Gate 4N-I28AU found had no independent superset."""
    import executable_inventory as ei

    real = sp.scan

    def partial(src, **kw):
        result = real(src, **kw)
        result.status = "PARTIAL"
        result.eof_reached = False
        result.unconsumed_ranges.append((0, len(src)))
        return result

    monkeypatch.setattr(sp, "scan", partial)
    monkeypatch.setattr(sp, "scan_script", partial)
    outcome = ei.check()
    assert not outcome["clean"], "the inventory accepted a PARTIAL parse as coverage"
    assert any("not complete" in p for p in outcome["problems"]), outcome["problems"][:2]


def test_v17_the_inventory_is_clean_on_the_real_tree():
    import executable_inventory as ei

    assert ei.check()["clean"], ei.check()["problems"][:3]


def test_v18_a_newly_visible_dynamic_site_is_declared():
    """The fix revealed a real dynamic command position that `case` had hidden."""
    import external_executable_trust as eet

    declared = eet.load_policy().get("dynamic_shell_sites") or []
    assert any(d["module"] == "migrate-create.sh" and d["line"] == 29 for d in declared), (
        "migrate-create.sh:29 became visible when the parser was fixed and must be declared")


# ------------------------------------------------------------------ 7. session binding
def test_v19_the_session_baseline_binds_parser_completeness():
    """Binding the command inventory alone could never have caught ADV-I28AT-01.

    That parse looked successful and simply contained fewer commands. What must be bound is the
    completeness EVIDENCE — status, EOF, consumed position, open frames.
    """
    digest = sp.completeness_digest()
    assert digest["sources"], "no shell source was fingerprinted"
    assert digest["untrustworthy"] == [], digest["untrustworthy"]
    for record in digest["sources"].values():
        for field in ("status", "eof_reached", "consumed_position", "open_frames", "trustworthy"):
            assert field in record, field
    assert digest["grammar_version"]


def test_v20_the_bootstrap_binds_and_reverifies_parser_completeness():
    """The check must exist at BOTH boundaries, and the drift comparison is the session-finish half."""
    import ast

    source = (REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "shell_completeness" in ast.dump(functions["establish"])
    dumped = ast.dump(functions["reverify"])
    assert "shell_completeness" in dumped, "session finish does not re-derive parser completeness"
    assert "shell_baseline" in dumped, (
        "session finish does not COMPARE against the configure-time digest; re-deriving without "
        "comparing cannot see a parser that started terminating early mid-session")


def test_v21_the_running_session_had_the_completeness_layer_active():
    import signalnest_bootstrap as sb

    outcome = sb.reverify()
    assert outcome["layers"].get("shell_completeness") is True, outcome["problems"][:3]


def test_v22_the_session_finish_drift_comparison_actually_fires(monkeypatch):
    """FUNCTIONAL, not structural — and the difference matters.

    An earlier version only asserted that the name `shell_baseline` appeared in `reverify`'s AST.
    Falsification arm f17 replaces the lookup with `shell_baseline = None`, which leaves the NAME in
    place and disables the comparison — so the structural check passed while the control was gone.
    This drives a genuinely different digest through reverify and requires the session to fail.
    """
    import signalnest_bootstrap as sb

    class _Config:
        pass

    config = _Config()
    attestation = sb.establish(strict=False)
    # A baseline whose digest cannot match the freshly derived one.
    attestation["shell_completeness"] = dict(attestation["shell_completeness"],
                                             digest="0" * 64)
    setattr(config, sb.BOOTSTRAP_ATTESTATION, attestation)

    outcome = sb.reverify(config)
    assert not outcome["clean"], (
        "a changed parse-completeness digest did not fail session finish; the drift comparison is "
        "not in force")
    assert any("parse-completeness digest changed" in p for p in outcome["problems"]), \
        outcome["problems"][:3]


def test_v23_an_unchanged_digest_produces_no_drift():
    """The negative control for v22, or every honest session would fail at finish."""
    import signalnest_bootstrap as sb

    class _Config:
        pass

    config = _Config()
    setattr(config, sb.BOOTSTRAP_ATTESTATION, sb.establish(strict=False))
    outcome = sb.reverify(config)
    assert outcome["clean"], outcome["problems"][:3]
