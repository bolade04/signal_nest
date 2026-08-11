"""Gate 4N-I27L — the real repository verdict must reach process exit.

Two graded Tier-1 guards computed a real repository verdict, PRINTED the failure, and then
returned the verdict of a self-authored mechanism probe instead:

    trust_validator.py   ci.yml `trust`   -- printed `unauthorized service principal`, exited 0
    external_anchor.py   ci.yml `anchor`  -- printed `MISMATCH ... != anchor ...`, exited 0

Both are graded steps named as validators, so each was credited in CI with enforcing a domain
it could not fail on. These tests pin the corrected contract: success requires the real
verdict AND the mandatory mechanism probe, combined with `and`, never `or`.

Every CLI case runs the EXACT graded invocation -- `python3 scripts/<module>.py` with no
arguments under TIER_1_SYNTHETIC. Calling the helper directly would re-open the precise gap
the defect lived in.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
ENV = {**os.environ, "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC", "PYTHONDONTWRITEBYTECODE": "1"}

TRUST_ALLOW = 'ALLOWED_SERVICE_PRINCIPALS = {"ecs-tasks.amazonaws.com"}'
TRUST_ALLOW_BROKEN = 'ALLOWED_SERVICE_PRINCIPALS = {"NOTHING-MATCHES"}'
TRUST_PROBE_DEF = "def _mechanism_probe_rejects_a_foreign_account() -> bool:"
ANCHOR_PROBE_DEF = "def _mechanism_probe_detects_a_foreign_account(result: dict) -> bool:"


@pytest.fixture
def sandbox(tmp_path):
    """A disposable copy of everything the two graded commands actually read.

    `infra/` matters: trust_validator resolves the permitted GitHub identity from
    `infra/aws/variables.tf`, and without it the guard fail-closes with exit 2. A red
    baseline would let these tests pass for the wrong reason.
    """
    root = tmp_path / "repo"
    shutil.copytree(SCRIPTS, root / "scripts")
    shutil.copytree(REPO / "tests" / "fixtures", root / "tests" / "fixtures")
    for extra in (".github", "infra"):
        src = REPO / extra
        if src.exists():
            shutil.copytree(src, root / extra,
                            ignore=shutil.ignore_patterns(".terraform", "*.tfstate*"))
    return root


def test_the_sandbox_reproduces_a_clean_baseline_for_both_guards(sandbox):
    """Baseline first. If either guard is red here, every result below is meaningless."""
    for module in ("trust_validator.py", "external_anchor.py"):
        code, out = run_graded(sandbox, module)
        assert code == 0, f"{module} is not green on an unmodified sandbox:\n{out}"


def run_graded(root: Path, module: str) -> tuple[int, str]:
    """The exact graded CI invocation: bare, no arguments, Tier 1."""
    proc = subprocess.run([sys.executable, f"scripts/{module}"], cwd=root, env=ENV,
                          capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def break_real_trust(root: Path) -> None:
    """A REAL violation: a tracked role's trust policy names an unpermitted principal."""
    p = root / "scripts" / "trust_validator.py"
    text = p.read_text()
    assert text.count(TRUST_ALLOW) == 1
    p.write_text(text.replace(TRUST_ALLOW, TRUST_ALLOW_BROKEN))


def break_real_anchor(root: Path) -> None:
    """A REAL violation: an account-bearing identity names a foreign account."""
    p = root / "scripts" / "gen_boundary_policy.py"
    text = p.read_text()
    decl = re.search(r"^POLICY_ARN\s*=\s*(.+)$", text, re.M)
    assert decl
    p.write_text(text.replace(
        decl.group(0),
        'POLICY_ARN = "arn:aws:iam::999988887777:policy/signalnest-w0-boundary"'))


def break_probe(root: Path, module: str, signature: str) -> None:
    p = root / "scripts" / module
    text = p.read_text()
    assert text.count(signature) == 1
    p.write_text(text.replace(signature, signature + "\n    return False  # forced probe failure"))


# --------------------------------------------------------------------------- trust validator
def test_trust_real_pass_mechanism_pass_exits_zero(sandbox):
    code, out = run_graded(sandbox, "trust_validator.py")
    assert code == 0, out


def test_trust_real_fail_mechanism_pass_exits_nonzero(sandbox):
    """The demonstrated defect. Before I27L this exited 0 while printing the failure."""
    break_real_trust(sandbox)
    code, out = run_graded(sandbox, "trust_validator.py")
    assert "unauthorized service principal" in out, out
    assert code != 0, "a real trust-policy failure must fail the graded step"


def test_trust_real_pass_mechanism_fail_exits_nonzero(sandbox):
    break_probe(sandbox, "trust_validator.py", TRUST_PROBE_DEF)
    code, out = run_graded(sandbox, "trust_validator.py")
    assert code != 0, out
    assert "mechanism FAILED" in out, out


def test_trust_both_fail_exits_nonzero_and_reports_both(sandbox):
    break_real_trust(sandbox)
    break_probe(sandbox, "trust_validator.py", TRUST_PROBE_DEF)
    code, out = run_graded(sandbox, "trust_validator.py")
    assert code != 0
    assert "unauthorized service principal" in out
    assert "mechanism FAILED" in out


# --------------------------------------------------------------------------- external anchor
def test_anchor_real_pass_mechanism_pass_exits_zero(sandbox):
    code, out = run_graded(sandbox, "external_anchor.py")
    assert code == 0, out


def test_anchor_real_fail_mechanism_pass_exits_nonzero(sandbox):
    """The demonstrated defect. Before I27L this exited 0 while printing the mismatch."""
    break_real_anchor(sandbox)
    code, out = run_graded(sandbox, "external_anchor.py")
    assert "MISMATCH" in out, out
    assert code != 0, "a real account mismatch must fail the graded step"


def test_anchor_real_pass_mechanism_fail_exits_nonzero(sandbox):
    break_probe(sandbox, "external_anchor.py", ANCHOR_PROBE_DEF)
    code, out = run_graded(sandbox, "external_anchor.py")
    assert code != 0, out
    assert "mechanism FAILED" in out, out


def test_anchor_both_fail_exits_nonzero_and_reports_both(sandbox):
    break_real_anchor(sandbox)
    break_probe(sandbox, "external_anchor.py", ANCHOR_PROBE_DEF)
    code, out = run_graded(sandbox, "external_anchor.py")
    assert code != 0
    assert "MISMATCH" in out
    assert "mechanism FAILED" in out


# --------------------------------------------------------------------------- shared contract
@pytest.mark.parametrize("module,breaker,marker", [
    ("trust_validator.py", break_real_trust, "unauthorized service principal"),
    ("external_anchor.py", break_real_anchor, "MISMATCH"),
])
def test_a_printed_failure_can_never_accompany_exit_zero(sandbox, module, breaker, marker):
    """The defect in one sentence: it printed the failure and exited 0."""
    breaker(sandbox)
    code, out = run_graded(sandbox, module)
    assert marker in out
    assert code != 0, f"{module} printed a real failure and still exited 0"


@pytest.mark.parametrize("module,verdict_prefix", [
    ("trust_validator.py", "TRUST VALIDATOR:"),
    ("external_anchor.py", "EXTERNAL ANCHOR:"),
])
def test_verdict_line_never_claims_success_when_exit_is_nonzero(sandbox, module, verdict_prefix):
    break_probe(sandbox, module,
                TRUST_PROBE_DEF if "trust" in module else ANCHOR_PROBE_DEF)
    code, out = run_graded(sandbox, module)
    verdicts = [l for l in out.splitlines() if l.startswith(verdict_prefix)]
    assert verdicts, out
    assert code != 0
    assert "mechanism verified" not in verdicts[-1], verdicts[-1]


# --------------------------------------------------- structural failure-propagation guard
#
# Narrow by construction: it inspects exactly the two tier-gated branches this gate fixed,
# rather than introducing a general static-analysis framework.
PROPAGATION_TARGETS = [
    ("trust_validator.py", "_mechanism_probe_rejects_a_foreign_account"),
    ("external_anchor.py", "_mechanism_probe_detects_a_foreign_account"),
]


def _tier_branch_return(module: str) -> ast.AST:
    """The `return` that decides the exit inside the `not certifies_production` branch."""
    tree = ast.parse((SCRIPTS / module).read_text())
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    for node in ast.walk(main):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "certifies_production" in test and test.startswith("not "):
            returns = [n for n in ast.walk(node) if isinstance(n, ast.Return) and n.value is not None]
            assert returns, f"{module}: tier-1 branch has no return"
            return node, returns[-1]
    raise AssertionError(f"{module}: no `not ... certifies_production` branch found")


@pytest.mark.parametrize("module,probe", PROPAGATION_TARGETS)
def test_tier1_success_requires_real_result_and_mechanism_conjunction(module, probe):
    branch, ret = _tier_branch_return(module)
    branch_src = ast.unparse(branch)

    # the mechanism probe is still invoked -- the fix must not delete it
    assert probe in branch_src, f"{module}: mandatory mechanism probe no longer runs"

    # the real verdict is read in this branch
    assert re.search(r"result\[[\"']clean[\"']\]", branch_src), \
        f"{module}: tier-1 branch never reads the real verdict"

    # success is a CONJUNCTION of both, not a disjunction and not either one alone
    conj = [n for n in ast.walk(branch)
            if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.And)]
    joined = [ast.unparse(n) for n in conj]
    assert any("clean" in j and ("detected" in j or probe in j) for j in joined), \
        f"{module}: real verdict and mechanism result are not combined with `and`: {joined}"
    assert not any(isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or)
                   and "clean" in ast.unparse(n) for n in ast.walk(branch)), \
        f"{module}: real verdict combined with `or` -- either alone can grant success"

    # the deciding return is not a bare mechanism result
    ret_src = ast.unparse(ret)
    assert ret_src != "0 if detected else 1", \
        f"{module}: tier-1 return is mechanism-only again -- the I27K defect is back"
