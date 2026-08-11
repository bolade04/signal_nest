"""Gate 4N-I28U — assertion self-protection for load-bearing tests.

WHAT THIS CLOSES. Gate 4N-I28T proved, on a git-bearing clone whose clean baseline was green on
every control, that a load-bearing test body could be replaced with a single ``assert True`` and
nothing noticed. Seven real assertions became one trivial one; the gutted test passed, the pins
passed, the universe pin passed, and package coherence stayed green.

THE SHAPE OF THE FIX. The requirement lives in tests/fixtures/assertion-contract-registry.json — an
AUTHORED file, independent of the tests it governs. scripts/assertion_contracts.py reads that
contract and checks each test's AST against it. It never asks a test what it asserts and then
requires that, which would ratify ``assert True`` along with everything else.

MANDATORY PATH. test_the_contracted_assertions_are_all_intact below runs inside the qualified suite,
which a graded CI step executes, so a trivialised load-bearing assertion fails the release job.
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import assertion_contracts as ac  # noqa: E402

REGISTRY_PATH = REPO_ROOT / "tests" / "fixtures" / "assertion-contract-registry.json"


# ===================================================================== the mandatory control
def test_the_contracted_assertions_are_all_intact():
    """THE control. Every load-bearing test must still assert what its contract requires."""
    result = ac.validate()
    assert result["clean"], (
        "a load-bearing test no longer asserts what its contract requires:\n  "
        + "\n  ".join(result["problems"]))
    assert result["contracts"] >= 10, result["contracts"]
    assert result["duplicate_contract_ids"] == 0


def test_every_contract_names_an_invariant_and_a_proving_mutation():
    """Membership in the inventory is earned, not asserted.

    A contract without a proving mutation is a claim that a test is load-bearing with no evidence,
    which is exactly the "it appears in a manifest" reasoning the registry forbids.
    """
    reg = ac.load_registry()
    for c in reg["contracts"]:
        assert c["protected_invariant"].strip(), c["contract_id"]
        assert c["proving_mutation"].strip(), c["contract_id"]
        assert c["why_load_bearing"].strip(), c["contract_id"]
        assert c["required_assertions"], c["contract_id"]
        for req in c["required_assertions"]:
            assert req["class"] in ac.ASSERTION_CLASSES, (c["contract_id"], req["class"])


def test_the_inventory_cannot_lose_an_entry_unnoticed():
    """Removing a contract must be as visible as removing a test."""
    reg = ac.load_registry()
    ids = {c["contract_id"] for c in reg["contracts"]}
    for required in ("AC-01-SMOKE-HTTP-REAL-CHAIN", "AC-02-COMMENT-CANNOT-MOVE-UNIVERSE",
                     "AC-03-MUTATION-HARNESS-CAN-REPORT-MOVEMENT", "AC-04-NO-TEXTUAL-ROOT",
                     "AC-05-SITE-ROLE-EQUALS-ROOT-ROLE",
                     "AC-06-UNGRADED-SMOKE-DOES-NOT-BLOCK-RELEASE",
                     "AC-07-SITE-BEHAVIOR-DISPOSITION", "AC-08-COMMENT-PINS-REMAIN",
                     "AC-09-SITE-UNIVERSE-PIN", "AC-10-UNRESOLVED-MENTION-FAILS-CLOSED"):
        assert required in ids, f"contract {required} was removed from the inventory"


# ===================================================================== PHASE I — 20-case matrix
def _sandbox(tmp_path: Path, body: str, contract: dict) -> dict:
    """Validate one synthetic test body against one synthetic contract."""
    (tmp_path / "tests").mkdir(exist_ok=True)
    src = "import pytest\n\n\n" + textwrap.dedent(body)
    (tmp_path / "tests" / "t.py").write_text(src)
    reg = {"contracts": [{**contract, "file": "tests/t.py", "test": "test_case",
                          "protected_invariant": "synthetic", "proving_mutation": "synthetic",
                          "why_load_bearing": "synthetic"}]}
    return ac.validate(reg, root=tmp_path)


EQ = {"contract_id": "S", "minimum_meaningful_assertions": 1,
      "required_assertions": [{"class": "EXACT_IDENTITY_EQUALITY",
                               "must_reference": ["result", "expected"]}]}

CASES = [
    ("i01 meaningful equality", "def test_case():\n    result = f()\n    expected = 3\n"
                                "    assert result == expected\n", EQ, True),
    ("i02 assert True", "def test_case():\n    assert True\n", EQ, False),
    ("i03 assert 1", "def test_case():\n    assert 1\n", EQ, False),
    ("i04 self comparison", "def test_case():\n    result = f()\n    assert result == result\n",
     EQ, False),
    ("i05 deleted assertion", "def test_case():\n    result = f()\n", EQ, False),
    ("i06 equality weakened to existence", "def test_case():\n    result = f()\n"
                                           "    assert result\n", EQ, False),
    ("i07 valid pytest.raises",
     "def test_case():\n    with pytest.raises(ValueError):\n        f()\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 1,
      "required_assertions": [{"class": "EXPECTED_EXCEPTION"}]}, True),
    ("i08 operation moved outside pytest.raises",
     "def test_case():\n    f()\n    with pytest.raises(ValueError):\n        pass\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 1,
      "required_assertions": [{"class": "EXPECTED_EXCEPTION"}]}, False),
    ("i09 explicit failure helper",
     "def test_case():\n    if f() != 3:\n        pytest.fail('wrong')\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 1, "required_assertions": []}, True),
    ("i10 unknown assertion helper",
     "def test_case():\n    assert mystery_check(f())\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 1,
      "required_assertions": [{"class": "EXACT_IDENTITY_EQUALITY",
                               "must_reference": ["result"]}]}, False),
    ("i11 registered assertion helper",
     "def test_case():\n    result = f()\n    expected = 3\n"
     "    assert result == pytest.approx(expected)\n", EQ, True),
    ("i12 line movement",
     "def test_case():\n\n\n    result = f()\n\n    expected = 3\n\n"
     "    assert result == expected\n", EQ, True),
    ("i13 formatting change",
     "def test_case():\n    result = f()\n    expected = 3\n"
     "    assert (\n        result\n        == expected\n    )\n", EQ, True),
    ("i14 variable rename with equivalent dataflow",
     "def test_case():\n    outcome = f()\n    result = outcome\n    expected = 3\n"
     "    assert result == expected\n", EQ, True),
    ("i15 mutation-consumption assertion",
     "def test_case():\n    before = h(a)\n    after = h(b)\n    assert after != before\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 1,
      "required_assertions": [{"class": "MUTATION_CONSUMPTION_PROOF",
                               "must_reference": ["before", "after"]}]}, False),
    ("i16 green-baseline assertion",
     "def test_case():\n    baseline = run()\n    assert baseline['clean'] == True\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 1,
      "required_assertions": [{"class": "BASELINE_GREEN_BEFORE_MUTATION",
                               "must_reference": ["baseline", "clean"]}]}, True),
    ("i17 inert-mutation stability assertion",
     "def test_case():\n    before = run()\n    after = run()\n"
     "    assert after['hash'] == before['hash']\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 1,
      "required_assertions": [{"class": "INERT_MUTATION_PRESERVES_RESULT",
                               "must_reference": ["before", "after", "hash"]}]}, True),
    ("i18 first-rejecting-control assertion",
     "def test_case():\n    control = run()\n    assert control['first_rejecting'] == 'pins'\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 1,
      "required_assertions": [{"class": "FIRST_REJECTING_CONTROL_PROOF",
                               "must_reference": ["control", "first_rejecting"]}]}, True),
    ("i19 missing-to-missing comparison",
     "def test_case():\n    before = run()\n    after = run()\n"
     "    assert before.get('x') == after.get('x')\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 2,
      "required_assertions": [
          {"class": "EXACT_IDENTITY_EQUALITY", "must_reference": ["before", "after"]},
          {"class": "MEANINGFUL_TRUTHINESS", "must_reference": ["present"]}]}, False),
    ("i20 multiple required, one removed",
     "def test_case():\n    result = f()\n    expected = 3\n    assert result == expected\n",
     {"contract_id": "S", "minimum_meaningful_assertions": 1,
      "required_assertions": [
          {"class": "EXACT_IDENTITY_EQUALITY", "must_reference": ["result", "expected"]},
          {"class": "MEMBERSHIP", "must_reference": ["members"]}]}, False),
]


@pytest.mark.parametrize("name,body,contract,should_pass",
                         [(c[0], c[1], c[2], c[3]) for c in CASES],
                         ids=[c[0].split()[0] for c in CASES])
def test_assertion_class_matrix(tmp_path, name, body, contract, should_pass):
    result = _sandbox(tmp_path, body, contract)
    assert result["clean"] is should_pass, (
        f"{name}: expected {'accept' if should_pass else 'reject'}; problems="
        f"{result['problems']}")


# ===================================================================== PHASE J — self-protection
def _copy_tree(tmp_path: Path) -> Path:
    """Materialise every file the registry references.

    GATE 4N-I28W: this used to copy two hard-coded test files, so expanding the inventory silently
    turned the baseline red — a harness limitation, not a real failure. Deriving the copy list from
    the registry keeps the harness correct as the inventory grows.
    """
    root = tmp_path / "repo"
    (root / "tests" / "fixtures").mkdir(parents=True)
    for rel in sorted({c["file"] for c in json.loads(REGISTRY_PATH.read_text())["contracts"]}):
        src = REPO_ROOT / rel
        if src.is_file():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, root / rel)
    shutil.copy(REGISTRY_PATH, root / "tests/fixtures")
    return root


def _validate_tree(root: Path, registry: dict | None = None) -> dict:
    reg = registry if registry is not None else json.loads(
        (root / "tests/fixtures/assertion-contract-registry.json").read_text())
    return ac.validate(reg, root=root)


def _gut(root: Path, rel: str, name: str, replacement: str = "    assert True\n"):
    p = root / rel
    t = p.read_text()
    s = t.index(f"def {name}(")
    e = t.index("\ndef ", s + 10)
    head = t[:s] + t[s:t.index("\n", s) + 1] + replacement + "\n"
    p.write_text(head + t[e + 1:])


def test_j_green_when_clean_baseline(tmp_path):
    """Without this every rejection below is meaningless."""
    root = _copy_tree(tmp_path)
    assert _validate_tree(root)["clean"], "the copied clean tree must validate"


J_MUTATIONS = {
    "j01 required assertion replaced with assert True":
        lambda r: _gut(r, "tests/test_i28s_command_roots.py",
                       "test_rc_s1_smoke_http_is_derived_through_the_real_shell_chain"),
    "j02 required assertion deleted":
        lambda r: _gut(r, "tests/test_i28s_command_roots.py",
                       "test_h03_the_ungraded_smoke_step_does_not_claim_to_block_release",
                       "    role = _roles()['smoke_http.py']\n"),
    "j03 equality weakened to truthiness":
        lambda r: _gut(r, "tests/test_i28s_command_roots.py",
                       "test_h04b_every_site_role_equals_the_derived_role_of_its_own_root",
                       "    st.reset_caches()\n    assert st.release_roots()\n"),
    "j04 self-comparison substituted":
        lambda r: _gut(r, "tests/test_i28s_command_roots.py",
                       "test_rc_s3_no_root_is_created_by_textual_matching",
                       "    st.reset_caches()\n    x = st.release_roots()\n    assert x == x\n"),
}


@pytest.mark.parametrize("name", sorted(J_MUTATIONS), ids=lambda n: n.split()[0])
def test_j_self_protection_catches_weakening(tmp_path, name):
    root = _copy_tree(tmp_path)
    assert _validate_tree(root)["clean"], "baseline must be green before mutating"
    J_MUTATIONS[name](root)
    result = _validate_tree(root)
    assert not result["clean"], f"{name} was NOT rejected"
    assert any("AC-" in p for p in result["problems"]), result["problems"]


def test_j05_removing_an_inventory_entry_is_caught(tmp_path):
    root = _copy_tree(tmp_path)
    reg = json.loads(REGISTRY_PATH.read_text())
    reg["contracts"] = [c for c in reg["contracts"]
                        if c["contract_id"] != "AC-01-SMOKE-HTTP-REAL-CHAIN"]
    # the validator itself cannot know an entry vanished; the inventory pin above is what does.
    ids = {c["contract_id"] for c in reg["contracts"]}
    assert "AC-01-SMOKE-HTTP-REAL-CHAIN" not in ids
    with pytest.raises(AssertionError):
        for required in ("AC-01-SMOKE-HTTP-REAL-CHAIN",):
            assert required in ids, f"contract {required} was removed from the inventory"


def test_j06_deriving_expectations_from_the_audited_ast_would_defeat_the_control(tmp_path):
    """Demonstrates WHY the requirement must come from an independent source.

    A registry built by observing whichever assertions a test currently has ratifies whatever it
    finds. The weakening used here is deliberately NOT ``assert True``: a generic all-trivial guard
    would catch that even under a derived oracle, which would hide the real loss. Replacing an
    equality with a bare truthiness check keeps the assertion "meaningful" in the generic sense
    while destroying exactly the semantic content the contract names — and the derived oracle
    accepts it.
    """
    root = _copy_tree(tmp_path)
    target = "test_rc_s1_smoke_http_is_derived_through_the_real_shell_chain"
    _gut(root, "tests/test_i28s_command_roots.py", target,
         "    st.reset_caches()\n    roots = st.release_roots()\n    chain = roots\n"
         "    assert chain\n")
    tree = ast.parse((root / "tests/test_i28s_command_roots.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == target)
    observed = ac.analyse_function(fn)
    derived = {"contracts": [{
        "contract_id": "SELF-AUTHORED", "file": "tests/test_i28s_command_roots.py",
        "test": target, "protected_invariant": "derived", "proving_mutation": "derived",
        "why_load_bearing": "derived",
        "minimum_meaningful_assertions": observed["meaningful_assert_count"],
        "required_assertions": []}]}
    assert ac.validate(derived, root=root)["clean"], (
        "the self-authored oracle should ratify the gutted test — that is the point")
    authored = _validate_tree(root)
    assert not authored["clean"], "the AUTHORED registry must reject what the derived one accepts"


def test_j07_replacing_the_validators_rejection_with_success_is_visible():
    """The validator must fail closed on problems, not report clean regardless."""
    fake = {"contracts": [{"contract_id": "X", "file": "tests/does-not-exist.py",
                           "test": "nope", "protected_invariant": "i", "proving_mutation": "m",
                           "why_load_bearing": "w", "required_assertions": []}]}
    r = ac.validate(fake, root=REPO_ROOT)
    assert not r["clean"] and r["problems"], "a missing file must be a problem"


def test_j08_an_unparseable_file_is_a_problem_not_a_skip(tmp_path):
    root = tmp_path / "r"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "broken.py").write_text("def test_case(:\n    pass\n")
    reg = {"contracts": [{"contract_id": "B", "file": "tests/broken.py", "test": "test_case",
                          "protected_invariant": "i", "proving_mutation": "m",
                          "why_load_bearing": "w", "required_assertions": []}]}
    r = ac.validate(reg, root=root)
    assert not r["clean"]
    assert any("does not parse" in p and "refusing to skip" in p for p in r["problems"])


def test_j09_unresolved_helper_semantics_fail_closed(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text(
        "def test_case():\n    assert some_unregistered_helper(x)\n")
    reg = {"contracts": [{"contract_id": "H", "file": "tests/t.py", "test": "test_case",
                          "protected_invariant": "i", "proving_mutation": "m",
                          "why_load_bearing": "w", "minimum_meaningful_assertions": 1,
                          "required_assertions": [{"class": "EXACT_IDENTITY_EQUALITY",
                                                   "must_reference": ["x"]}]}]}
    r = ac.validate(reg, root=tmp_path)
    assert not r["clean"]
    assert any("unregistered helper" in p for p in r["problems"])


def test_j10_a_missing_registry_is_refused_not_defaulted(tmp_path):
    with pytest.raises(ac.ContractError, match="registry is missing"):
        ac.load_registry(tmp_path / "absent.json")


def test_j11_an_unknown_assertion_class_is_refused(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("def test_case():\n    assert a == b\n")
    reg = {"contracts": [{"contract_id": "U", "file": "tests/t.py", "test": "test_case",
                          "protected_invariant": "i", "proving_mutation": "m",
                          "why_load_bearing": "w",
                          "required_assertions": [{"class": "NOT_A_REAL_CLASS"}]}]}
    with pytest.raises(ac.ContractError, match="unknown assertion class"):
        ac.validate(reg, root=tmp_path)


def test_j12_a_renamed_test_is_caught_rather_than_skipped(tmp_path):
    root = _copy_tree(tmp_path)
    p = root / "tests/test_i28s_command_roots.py"
    p.write_text(p.read_text().replace(
        "def test_rc_s3_no_root_is_created_by_textual_matching(",
        "def test_rc_s3_renamed_away("))
    r = _validate_tree(root)
    assert not r["clean"]
    assert any("no longer exists" in x for x in r["problems"])


def test_j13_the_validator_hard_codes_no_test_name_or_expression():
    """The control must be general: its logic may not name a specific test or assertion."""
    source = (REPO_ROOT / "scripts" / "assertion_contracts.py").read_text()
    body = source.split('"""', 2)[-1]          # exclude the module docstring
    for forbidden in ("test_rc_s1", "test_rc_s5", "smoke_http", "ci-smoke.sh",
                      "shell_source_line", "UNGRADED_JOB_STEP"):
        assert forbidden not in body, (
            f"scripts/assertion_contracts.py hard-codes {forbidden!r}; the control must be driven "
            "entirely by the registry")


def test_j14_duplicate_contract_ids_are_reported(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("def test_case():\n    assert a == b\n")
    one = {"contract_id": "D", "file": "tests/t.py", "test": "test_case",
           "protected_invariant": "i", "proving_mutation": "m", "why_load_bearing": "w",
           "required_assertions": []}
    r = ac.validate({"contracts": [one, dict(one)]}, root=tmp_path)
    assert r["duplicate_contract_ids"] == 1
    assert any("duplicate contract id" in p for p in r["problems"])


def test_j15_no_historical_verdict_is_consulted():
    """I28U reuses no reviewer verdict; the control reads only the repository."""
    source = (REPO_ROOT / "scripts" / "assertion_contracts.py").read_text()
    for forbidden in ("signalnest/generated", "4n-i28q", "reviews/", "verdict"):
        assert forbidden not in source, f"the control references {forbidden!r}"
