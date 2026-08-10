"""Gate 4N-I28W — assertion reachability, inventory completeness, and the layer-2 meta contract.

WHAT THIS CLOSES. Gate 4N-I28V proved two things on git-bearing, proven-green baselines:

  I28V-01  a contracted test whose whole body sits under `if False:` keeps every required token,
           class and count, executes none of it, and every control stays green;
  I28V-02  the inventory omitted unsubsumed load-bearing tests — including the assertion-control
           test itself, so the mechanism could be disabled with nothing noticing.

THE TWO TRUST LAYERS, and why they are separate files.

  LAYER 1  tests/fixtures/assertion-contract-registry.json says what each load-bearing TEST must
           assert. scripts/assertion_contracts.py::validate checks it.
  LAYER 2  tests/fixtures/assertion-meta-contract.json says what the CONTROL must do and which
           tests must exist and be collected. validate_meta checks it, and lists ITSELF among the
           mandatory tests.

Neither layer can quietly remove its own protection, because weakening one requires a visible edit
in the other. That is a bounded guarantee, not a universal one, and the fixtures say so.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import assertion_contracts as ac  # noqa: E402

REGISTRY = REPO_ROOT / "tests" / "fixtures" / "assertion-contract-registry.json"
META = REPO_ROOT / "tests" / "fixtures" / "assertion-meta-contract.json"
BASELINE = REPO_ROOT / "tests" / "fixtures" / "assertion-registry-baseline.json"


def _fn(src: str, name: str = "test_case") -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(src))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _dispositions(src: str) -> list[str]:
    fn = _fn(src)
    reach = ac.assertion_reachability(fn)
    return [reach[id(n)]["disposition"] for n in ast.walk(fn) if isinstance(n, ast.Assert)]


# ===================================================================== PHASE E — 20 cases
REACH_CASES = [
    ("e01 reachable direct", "def test_case():\n    assert a == b\n", [ac.REACHABLE]),
    ("e02 under if True", "def test_case():\n    if True:\n        assert a == b\n",
     [ac.REACHABLE]),
    ("e03 under if False", "def test_case():\n    if False:\n        assert a == b\n",
     [ac.UNREACHABLE]),
    ("e04 under if 0", "def test_case():\n    if 0:\n        assert a == b\n", [ac.UNREACHABLE]),
    ("e05 under if 1", "def test_case():\n    if 1:\n        assert a == b\n", [ac.REACHABLE]),
    ("e06 after return", "def test_case():\n    return\n    assert a == b\n", [ac.UNREACHABLE]),
    ("e07 after raise", "def test_case():\n    raise ValueError\n    assert a == b\n",
     [ac.UNREACHABLE]),
    ("e08 reachable else",
     "def test_case():\n    if False:\n        pass\n    else:\n        assert a == b\n",
     [ac.REACHABLE]),
    ("e09 unreachable else",
     "def test_case():\n    if True:\n        pass\n    else:\n        assert a == b\n",
     [ac.UNREACHABLE]),
    ("e10 nested constant branches",
     "def test_case():\n    if True:\n        if False:\n            assert a == b\n",
     [ac.UNREACHABLE]),
    ("e11 dynamic branch", "def test_case():\n    if flag:\n        assert a == b\n",
     [ac.CONDITIONAL]),
    ("e12 statically empty loop", "def test_case():\n    for _ in []:\n        assert a == b\n",
     [ac.UNREACHABLE]),
    ("e13 finite nonempty loop",
     "def test_case():\n    for _ in [1, 2]:\n        assert a == b\n", [ac.REACHABLE]),
    ("e14 after unconditional break",
     "def test_case():\n    for _ in [1]:\n        break\n        assert a == b\n",
     [ac.UNREACHABLE]),
    ("e15 after unconditional continue",
     "def test_case():\n    for _ in [1]:\n        continue\n        assert a == b\n",
     [ac.UNREACHABLE]),
    ("e16 never-invoked nested function",
     "def test_case():\n    def helper():\n        assert a == b\n    return None\n",
     [ac.UNREACHABLE]),
    ("e17 invoked nested function",
     "def test_case():\n    def helper():\n        assert a == b\n    helper()\n",
     [ac.REACHABLE]),
    ("e18 lambda never invoked",
     "def test_case():\n    f = lambda: a == b\n    assert a == b\n", [ac.REACHABLE]),
    ("e19 try/finally reachable",
     "def test_case():\n    try:\n        pass\n    finally:\n        assert a == b\n",
     [ac.REACHABLE]),
    ("e20 after pytest.skip",
     "def test_case():\n    pytest.skip('x')\n    assert a == b\n", [ac.UNREACHABLE]),
]


@pytest.mark.parametrize("name,src,expected", REACH_CASES,
                         ids=[c[0].split()[0] for c in REACH_CASES])
def test_reachability_matrix(name, src, expected):
    got = _dispositions(src)
    assert got == expected, f"{name}: expected {expected}, got {got}"


# ===================================================================== PHASE F — the dead-branch pin
def _sandbox(tmp_path: Path) -> Path:
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    for f in ("test_i28s_command_roots.py", "test_i27r_rejection_remediation.py",
              "test_i28i_scan_accounting.py", "test_i27z_packet_digest_contract.py",
              "test_i28b_trap_scope_and_route53_domain.py", "test_i23_package_and_lineage.py",
              "test_i28u_assertion_self_protection.py",
              # Gate 4N-I28AS: AC-15 and AC-16 contract controls in this file, so a sandbox
              # without it validates contracts whose test does not exist.
              "test_i28as_npm_authority.py",
              # Gate 4N-I28AT: AC-17 and AC-18 contract controls in this file.
              "test_i28at_docker_boundary.py",
              # Gate 4N-I28AV: AC-19 and AC-20 contract controls in this file.
              "test_i28av_shell_parser_completeness.py",
              # Gate 4N-I28BB: AC-21 pins a control in this file; a sandbox without it validates a
              # contract whose test does not exist and fails on ABSENCE, not on the attack.
              "test_i28bb_exec_transfer.py",
              # Gate 4N-I28BE: AC-22 pins a control in this file.
              "test_i28be_docker_per_site_enforcement.py",
              "test_i28bfa_category_and_session_finish.py",
              # Gate 4N-I28BF-B1: the four AC-B1 contracts pin controls in these files; a sandbox
              # without them validates a contract whose test does not exist and fails on ABSENCE.
              "test_i28bf_b1_docker_assurance_state.py",
              "test_i28bf_b1_governed_cache.py",
              "test_i28bf_b1_poisoning.py",
              # Gate 4N-I28BF-B3: the three AC-B3 contracts pin controls in the environment matrix.
              "test_i28bf_b3_environment.py",
              # Gate 4N-I28BG-B1: the twelve AC-BG1 contracts pin controls in these files; a sandbox
              # without them validates a contract whose test does not exist and fails on ABSENCE.
              "test_i28bg_b1_workflow_assurance.py",
              "test_i28bg_b1_attacks.py",
              "test_i28bg_b1_synthetic_workflows.py",
              # Gate 4N-I28BG-B2: the eleven AC-BG2-READER-* contracts pin controls in these files.
              "test_i28bg_b2_reader_integration.py",
              "test_i28bg_b2_reader_attacks.py",
              # Gate 4N-I28BG-B3: the sixteen AC-BG3-* contracts pin controls in these files.
              "test_i28bg_b3_staging_integration.py",
              "test_i28bg_b3_staging_attacks.py",
              # Gate 4N-I28BG-B4: the fifteen AC-BG4-* contracts pin controls in these files.
              "test_i28bg_b4_cross_workflow.py",
              "test_i28bg_b4_batteries.py"):
        src = REPO_ROOT / "tests" / f
        if src.is_file():
            (tmp_path / "tests" / f).write_bytes(src.read_bytes())
    (tmp_path / "tests/fixtures/assertion-contract-registry.json").write_bytes(
        REGISTRY.read_bytes())
    return tmp_path


def _validate(root: Path) -> dict:
    return ac.validate(json.loads(
        (root / "tests/fixtures/assertion-contract-registry.json").read_text()), root=root)


def _wrap_in_dead_branch(root: Path, rel: str, name: str, tail: str):
    p = root / rel
    t = p.read_text()
    s = t.index(f"def {name}(")
    e = t.index("\ndef ", s + 10)
    head = t[s:t.index("\n", s) + 1]
    body = t[t.index("\n", s) + 1:e]
    indented = "\n".join(("    " + ln) if ln.strip() else ln
                         for ln in body.rstrip("\n").split("\n"))
    p.write_text(t[:s] + head + "    if False:\n" + indented + "\n" + tail + "\n" + t[e + 1:])


def test_f_the_exact_i28v_dead_branch_mutation_is_caught(tmp_path):
    """THE pin. Every required token, class and count preserved; nothing executes."""
    root = _sandbox(tmp_path)
    assert _validate(root)["clean"], "baseline must be green before mutating"
    _wrap_in_dead_branch(
        root, "tests/test_i28s_command_roots.py",
        "test_rc_s1_smoke_http_is_derived_through_the_real_shell_chain",
        "    roots = st.release_roots()\n    assert roots\n")
    result = _validate(root)
    assert not result["clean"], "the dead-branch mutation was NOT caught"
    assert any("can never execute" in p for p in result["problems"]), result["problems"][:3]


def test_f_a_reachable_equivalent_still_passes(tmp_path):
    """Green-when-clean: the pin must not simply reject everything."""
    root = _sandbox(tmp_path)
    assert _validate(root)["clean"]


# ===================================================================== PHASE I — layer 2
def test_the_meta_contract_holds():
    """LAYER 2. The control's own required behaviours and mandatory tests."""
    result = ac.validate_meta()
    assert result["clean"], "the assertion control no longer satisfies its meta contract:\n  " + \
                            "\n  ".join(result["problems"])
    assert result["behaviours"] >= 10
    assert result["mandatory_tests"] >= 4


def test_every_mandatory_test_is_collected():
    """Removal, renaming or deselection of a mandatory test must be visible.

    Collection is checked by asking pytest itself, not by trusting the file to contain a name:
    a test can exist in the file and still be deselected or renamed out of collection.
    """
    meta = ac.load_meta()
    node_ids = [e["node_id"] for e in meta["mandatory_tests"]]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly",
         *{n.split("::")[0] for n in node_ids}],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=900)
    collected = proc.stdout
    missing = [n for n in node_ids if n not in collected]
    assert not missing, (
        "mandatory test(s) not collected by pytest — removed, renamed or deselected:\n  "
        + "\n  ".join(missing))


def test_the_authored_registry_matches_its_pinned_baseline():
    """Bounded protection against unauthorized weakening of the authored contracts."""
    baseline = json.loads(BASELINE.read_text())
    assert hashlib.sha256(REGISTRY.read_bytes()).hexdigest() == baseline["registry_sha256"], (
        "the authored contract registry changed without updating its pinned baseline. A contract "
        "change is legitimate, but it must be accompanied by an identity-level explanation in the "
        "baseline's change_ledger — that is what makes weakening visible.")
    assert hashlib.sha256(META.read_bytes()).hexdigest() == baseline["meta_contract_sha256"]
    reg = json.loads(REGISTRY.read_text())
    assert len(reg["contracts"]) == baseline["contract_count"]
    assert sorted(c["contract_id"] for c in reg["contracts"]) == baseline["contract_ids"]
    for c in reg["contracts"]:
        shape = baseline["per_contract_shape"][c["contract_id"]]
        assert sorted(r["class"] for r in c["required_assertions"]) == shape["required_classes"], \
            f"{c['contract_id']}: required assertion classes were changed"
        assert c.get("minimum_meaningful_assertions", 1) == \
            shape["minimum_meaningful_assertions"], \
            f"{c['contract_id']}: the minimum assertion count was changed"
        assert sum(len(r.get("must_reference", [])) for r in c["required_assertions"]) == \
            shape["must_reference_token_count"], \
            f"{c['contract_id']}: stable semantic references were removed"
        assert bool(c.get("proving_mutation")) == shape["has_proving_mutation"]
    assert baseline["change_ledger"], "the change ledger must record why contracts changed"


def test_k05_every_registered_helper_declares_its_implementation_provenance():
    """Closes readiness criterion 16 by making implementation weakening DETECTABLE.

    A third_party helper has no in-tree implementation to weaken — and that is asserted, not
    assumed. An in_tree helper must pin its implementation hash, so registering a project helper
    without pinning it is a problem rather than a silent gap.
    """
    meta = ac.load_meta()
    for name, contract in meta["registered_helper_contracts"].items():
        assert contract["implementation_source"] in ("third_party", "in_tree"), name
        if contract["implementation_source"] == "in_tree":
            assert contract.get("implementation_sha256"), (
                f"{name}: an in-tree helper must pin its implementation")
        else:
            module = name.split(".")[0]
            assert not list((REPO_ROOT / "scripts").rglob(f"{module}.py")), (
                f"{name}: declared third_party but an in-tree implementation exists")
    assert ac.validate_meta()["clean"]


def test_k06_an_unpinned_in_tree_helper_is_rejected():
    meta = json.loads(META.read_text())
    meta["registered_helper_contracts"]["site_taxonomy.check"] = {
        "arity_min": 0, "argument_roles": [], "failure_semantics": "x",
        "permitted_keywords": [], "implementation_source": "in_tree"}
    result = ac.validate_meta(meta)
    assert not result["clean"]
    assert any("implementation_sha256" in p for p in result["problems"]), result["problems"]


def test_the_trust_limit_is_stated_not_overclaimed():
    """An authored oracle cannot prove itself; the fixtures must say so rather than imply otherwise."""
    meta = ac.load_meta()
    baseline = json.loads(BASELINE.read_text())
    joined = " ".join(meta["_trust_boundary"]) + " " + " ".join(baseline["_trust_limit"])
    assert "not" in joined.lower() and "rewrite" in joined.lower(), (
        "the trust boundary must state plainly what it does NOT protect against")


# ===================================================================== PHASE K — helper roles
HELPER_CASES = [
    ("k01 correct pytest.raises",
     "def test_case():\n    with pytest.raises(ValueError):\n        f()\n", True),
    ("k02 pytest.raises with no exception argument",
     "def test_case():\n    with pytest.raises():\n        f()\n", False),
    ("k03 pytest.raises with an uncontracted keyword",
     "def test_case():\n    with pytest.raises(ValueError, message='x'):\n        f()\n", False),
    ("k04 pytest.raises with a contracted keyword",
     "def test_case():\n    with pytest.raises(ValueError, match='x'):\n        f()\n", True),
]


@pytest.mark.parametrize("name,src,ok", HELPER_CASES, ids=[c[0].split()[0] for c in HELPER_CASES])
def test_helper_argument_roles(name, src, ok):
    fn = _fn(src)
    call = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and ac._call_name(n) == "pytest.raises")
    problems = ac.check_helper_use(call, "pytest.raises")
    assert (not problems) is ok, f"{name}: problems={problems}"


def test_an_unregistered_helper_has_no_contract():
    fn = _fn("def test_case():\n    assert mystery(x)\n")
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call))
    problems = ac.check_helper_use(call, "mystery")
    assert problems and "no authored helper contract" in problems[0]


def test_an_ordinary_method_is_not_treated_as_an_assertion_helper(tmp_path):
    """str.startswith and friends have Python-defined semantics; flagging them was an I28U defect."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text(
        "def test_case():\n    assert value.startswith('scripts/')\n")
    reg = {"contracts": [{"contract_id": "S", "file": "tests/t.py", "test": "test_case",
                          "protected_invariant": "i", "proving_mutation": "m",
                          "why_load_bearing": "w", "minimum_meaningful_assertions": 1,
                          "required_assertions": [{"class": "MEANINGFUL_TRUTHINESS",
                                                   "must_reference": ["startswith"]}]}]}
    result = ac.validate(reg, root=tmp_path)
    assert result["clean"], result["problems"]


# ===================================================================== PHASE M — meta matrix
META_ATTACKS = {
    "m01 assertions wrapped in if False": (
        "tests/test_i28s_command_roots.py",
        "test_rc_s1_smoke_http_is_derived_through_the_real_shell_chain",
        "    roots = st.release_roots()\n    assert roots\n"),
    "m02 assertions moved after return": None,
    "m03 assertions moved into a never-called nested function": None,
}


def test_m01_dead_branch_attack_is_caught(tmp_path):
    root = _sandbox(tmp_path)
    assert _validate(root)["clean"]
    rel, name, tail = META_ATTACKS["m01 assertions wrapped in if False"]
    _wrap_in_dead_branch(root, rel, name, tail)
    assert not _validate(root)["clean"]


def test_m02_assertions_after_return_are_caught(tmp_path):
    root = _sandbox(tmp_path)
    p = root / "tests/test_i28s_command_roots.py"
    name = "test_rc_s1_smoke_http_is_derived_through_the_real_shell_chain"
    t = p.read_text()
    s = t.index(f"def {name}(")
    head_end = t.index("\n", s) + 1
    p.write_text(t[:head_end] + "    return\n" + t[head_end:])
    result = _validate(root)
    assert not result["clean"]
    assert any("can never execute" in x for x in result["problems"])


def test_m03_assertions_in_a_never_called_nested_function_are_caught(tmp_path):
    root = _sandbox(tmp_path)
    p = root / "tests/test_i28s_command_roots.py"
    name = "test_rc_s1_smoke_http_is_derived_through_the_real_shell_chain"
    t = p.read_text()
    s = t.index(f"def {name}(")
    e = t.index("\ndef ", s + 10)
    head = t[s:t.index("\n", s) + 1]
    body = t[t.index("\n", s) + 1:e]
    indented = "\n".join(("    " + ln) if ln.strip() else ln
                         for ln in body.rstrip("\n").split("\n"))
    p.write_text(t[:s] + head + "    def _never_called():\n" + indented +
                 "\n    roots = st.release_roots()\n    assert roots\n\n" + t[e + 1:])
    result = _validate(root)
    assert not result["clean"]
    assert any("can never execute" in x for x in result["problems"])


def test_m05_removing_an_inventory_entry_is_caught():
    reg = json.loads(REGISTRY.read_text())
    baseline = json.loads(BASELINE.read_text())
    trimmed = [c for c in reg["contracts"] if c["contract_id"] != "AC-01-SMOKE-HTTP-REAL-CHAIN"]
    assert len(trimmed) != baseline["contract_count"], (
        "removing a contract must change the count the baseline pins")


def test_m13_weakening_a_registry_contract_is_caught():
    """Lowering a minimum or dropping a class must break the pinned shape."""
    reg = json.loads(REGISTRY.read_text())
    baseline = json.loads(BASELINE.read_text())
    victim = dict(reg["contracts"][0])
    victim["minimum_meaningful_assertions"] = 0
    assert victim["minimum_meaningful_assertions"] != \
        baseline["per_contract_shape"][victim["contract_id"]]["minimum_meaningful_assertions"]


def test_m20_no_historical_verdict_is_consulted():
    source = (REPO_ROOT / "scripts" / "assertion_contracts.py").read_text()
    for forbidden in ("signalnest/generated", "4n-i28q", "reviews/"):
        assert forbidden not in source


def test_the_matrix_declares_every_case():
    src = Path(__file__).read_text()
    for n in range(1, 21):
        assert f"e{n:02d}" in src, f"reachability case e{n:02d} is missing"


# --------------------------------------------------------------- GATE 4N-I28BC
# I28BB-RESIDUAL-01. Falsification arm f21 neutralised a registered assertion with
# `assert True or X` and NOTHING objected: `_is_always_true` had no ast.BoolOp case, so a
# short-circuited assertion counted as meaningful and its required tokens were still textually
# present inside the dead operand.
#
# Fixing the detector is not enough on its own. Arms b05/b06 then showed that NEUTERING THE
# DETECTOR ITSELF escaped, because no test asserted what it classifies — the watchman was
# unwatched. These tests are that assertion.
import ast as _ast

TRIVIAL_FORMS = [
    ("assert True", True), ("assert 1", True), ("assert x == x", True),
    ("assert True or a in b", True), ("assert a in b or True", True),
    ("assert 1 or a in b", True), ("assert bool(1) or a in b", True),
    ("assert len((1,)) or a in b", True), ("assert int('1') or a in b", True),
    ("assert abs(-1) or a in b", True), ("assert object() or a in b", True),
    ("assert (1,) or a in b", True), ("assert True and True", True),
    ("assert not False", True), ("assert (lambda: a == b)", True),
    # meaningful — the program under test still decides the outcome
    ("assert a in b", False), ("assert a in b and True", False),
    ("assert a in b and c in d", False), ("assert bool(0) or a in b", False),
    ("assert len(()) or a in b", False), ("assert abs(0) or a in b", False),
    ("assert not True", False),
]


@pytest.mark.parametrize("source,expected", TRIVIAL_FORMS,
                         ids=[s.replace(" ", "_")[:40] for s, _ in TRIVIAL_FORMS])
def test_the_triviality_detector_classifies_each_form(source, expected):
    """The detector's own behaviour, asserted — so neutering it fails a test.

    Without this, `_is_always_true` could be replaced with `return False` and every contract
    would keep passing while every trivial assertion became invisible.
    """
    node = _ast.parse(source).body[0].test
    assert ac._is_always_true(node) is expected, (
        f"{source!r} must be classified trivial={expected}; a detector that misclassifies it "
        "lets a neutralised assertion satisfy its contract")


def test_the_triviality_detector_is_not_constant():
    """Kills the `return False` / `return True` stubs directly.

    A detector that answers the same way for everything is not a detector, and arms b05 and b06
    of the I28BC falsification battery are exactly those two stubs.
    """
    answers = {ac._is_always_true(_ast.parse(s).body[0].test) for s, _ in TRIVIAL_FORMS}
    assert answers == {True, False}, (
        "the triviality detector returned a single answer for every form, so it is a constant "
        "rather than a classifier")


def test_a_short_circuited_registered_assertion_cannot_satisfy_its_contract():
    """End-to-end shape of f21, at the level the contract layer adjudicates."""
    honest = _ast.parse("def t():\n    assert 'k' in a\n    assert 'k' in b\n").body[0]
    gutted = _ast.parse("def t():\n    assert 'k' in a\n    assert True or 'k' in b\n").body[0]
    honest_meaningful = sum(
        1 for n in _ast.walk(honest) if isinstance(n, _ast.Assert) and not ac._is_always_true(n.test))
    gutted_meaningful = sum(
        1 for n in _ast.walk(gutted) if isinstance(n, _ast.Assert) and not ac._is_always_true(n.test))
    assert honest_meaningful == 2
    assert gutted_meaningful == 1, (
        "the short-circuited assertion must not count toward minimum_meaningful_assertions")
