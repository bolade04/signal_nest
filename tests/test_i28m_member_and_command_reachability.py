"""Gate 4N-I28M — a site needs evidence that something reaches IT, not its neighbourhood.

WHAT GATE 4N-I28L FOUND, by executed mutation rather than by reading code:

    iam_eval.py::Evaluation.allowed              a property referenced NOWHERE in the repository,
                                                 counted as a production/control site because
                                                 constructing its class admitted every member
    production_certification.py::canonical_sha256  reachable only through `establish_eligibility`,
                                                 a subcommand `ci.yml` never invokes — the
                                                 workflow runs `production_certification.py state`

Neither could ever be covered by an executed mutation, because nothing executes them, yet both sat
in the site-coverage denominator claiming to be load-bearing.

Neither is fixed by a denylist. Two rules changed:

* a class on the enforcement path contributes the members something REACHES — an explicit
  attribute access, a dispatch or callback reference, a bounded `getattr`, construction reaching
  `__init__` — and inheritance is followed rather than assumed;
* a root is a command, not a script: reachability begins at the subcommand the workflow selects,
  with shared pre-dispatch code intact and sibling handlers excluded.

The hard part is that BOTH corrections can swing into a false exclusion, and the first run of this
one did: dropping the blanket class rule lost `LoadedAnchor.redacted`, `Arn.components` and
`LoadedInventory.dig`, which are genuinely invoked through loader functions, tuple unpacking and a
module-level instance in another module. Every case below that asserts INCLUSION exists because
something real was briefly lost.
"""
from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import site_taxonomy as st  # noqa: E402

WORKFLOW = """
jobs:
  guards:
    steps:
      - name: guard
        id: guard_step
        run: python3 scripts/guard.py
"""


def build(tmp_path: Path, scripts: dict[str, str], workflow: str = WORKFLOW) -> Path:
    root = tmp_path / "world"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "fixtures").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    for name, source in scripts.items():
        (root / "scripts" / name).write_text(textwrap.dedent(source), encoding="utf-8")
    (root / ".github" / "workflows" / "ci.yml").write_text(workflow, encoding="utf-8")
    return root


def sites_of(root: Path, monkeypatch) -> set[str]:
    monkeypatch.setattr(st, "REPO_ROOT", root)
    monkeypatch.setattr(st, "SCRIPTS", root / "scripts")
    monkeypatch.setattr(st, "TESTS", root / "tests")
    monkeypatch.setattr(st, "WORKFLOW", root / ".github" / "workflows" / "ci.yml")
    st.reset_caches()
    try:
        return {s["id"] for s in st.production_control_function_sites()}
    finally:
        st.reset_caches()


def problems_of(root: Path, monkeypatch) -> list[str]:
    monkeypatch.setattr(st, "REPO_ROOT", root)
    monkeypatch.setattr(st, "SCRIPTS", root / "scripts")
    monkeypatch.setattr(st, "TESTS", root / "tests")
    monkeypatch.setattr(st, "WORKFLOW", root / ".github" / "workflows" / "ci.yml")
    st.reset_caches()
    try:
        return st.check()["problems"]
    finally:
        st.reset_caches()


# =====================================================================================
# 1. Class members — Phase E
# =====================================================================================

CLASS_GUARD = '''
class Rule:
    def __init__(self, limit):
        self.limit = limit

    def invoked(self, v):
        if v > self.limit:
            return False
        return True

    def never_used(self, v):
        if v < 0:
            return False
        return True

    @property
    def used_property(self):
        return self.limit > 0

    @property
    def unused_property(self):
        return self.limit < 0


def main():
    r = Rule(3)
    if not r.invoked(2) or not r.used_property:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def test_cm01_an_invoked_method_is_a_site(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": CLASS_GUARD}), monkeypatch)
    assert "guard.py::Rule.invoked" in sites


def test_cm02_an_unused_method_is_not_a_site(tmp_path, monkeypatch):
    """THE I28L-01 DEFECT: construction used to admit this."""
    sites = sites_of(build(tmp_path, {"guard.py": CLASS_GUARD}), monkeypatch)
    assert "guard.py::Rule.never_used" not in sites


def test_cm03_an_invoked_property_is_a_site(tmp_path, monkeypatch):
    """A property is READ, never called; a call-only walk would lose it."""
    sites = sites_of(build(tmp_path, {"guard.py": CLASS_GUARD}), monkeypatch)
    assert "guard.py::Rule.used_property" in sites


def test_cm04_an_unused_property_is_not_a_site(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": CLASS_GUARD}), monkeypatch)
    assert "guard.py::Rule.unused_property" not in sites


def test_cm05_construction_reaches_init_only(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": CLASS_GUARD}), monkeypatch)
    assert "guard.py::Rule.__init__" in sites
    assert sites == {"guard.py::main", "guard.py::Rule.__init__", "guard.py::Rule.invoked",
                     "guard.py::Rule.used_property"}, sorted(sites)


@pytest.mark.parametrize("form,expected", [
    ("Rule.direct(2)", "guard.py::Rule.direct"),
    ("Rule.staticly(2)", "guard.py::Rule.staticly"),
])
def test_cm06_class_and_static_methods_invoked_directly(form, expected, tmp_path, monkeypatch):
    guard = f'''
    class Rule:
        @classmethod
        def direct(cls, v):
            if v:
                return True
            return False

        @staticmethod
        def staticly(v):
            if v:
                return True
            return False


    def main():
        if not {form}:
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch)
    assert expected in sites


def test_cm07_a_callback_stored_then_invoked_is_a_site(tmp_path, monkeypatch):
    guard = '''
    class Rule:
        def handler(self, v):
            if v:
                return True
            return False

        def unused(self, v):
            if v:
                return True
            return False


    def main():
        r = Rule()
        cb = r.handler
        if not cb(1):
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch)
    assert "guard.py::Rule.handler" in sites
    assert "guard.py::Rule.unused" not in sites


def test_cm08_a_dispatch_table_member_reference_is_a_site(tmp_path, monkeypatch):
    guard = '''
    class Rule:
        @staticmethod
        def selected(v):
            if v:
                return True
            return False

        @staticmethod
        def not_selected(v):
            if v:
                return True
            return False


    TABLE = {"a": Rule.selected}


    def main():
        if not TABLE["a"](1):
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch)
    assert "guard.py::Rule.selected" in sites
    assert "guard.py::Rule.not_selected" not in sites


INHERIT_GUARD = '''
class Base:
    def inherited_used(self, v):
        if v:
            return True
        return False

    def inherited_unused(self, v):
        if v:
            return True
        return False


class Child(Base):
    def own(self, v):
        if v:
            return True
        return False


def main():
    c = Child()
    if not c.own(1) or not c.inherited_used(1):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def test_cm09_an_invoked_inherited_method_is_a_site(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": INHERIT_GUARD}), monkeypatch)
    assert "guard.py::Base.inherited_used" in sites


def test_cm10_an_unused_inherited_method_is_not_a_site(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": INHERIT_GUARD}), monkeypatch)
    assert "guard.py::Base.inherited_unused" not in sites


def test_cm11_getattr_with_a_literal_name_resolves(tmp_path, monkeypatch):
    guard = '''
    class Rule:
        def picked(self, v):
            if v:
                return True
            return False

        def other(self, v):
            if v:
                return True
            return False


    def main():
        r = Rule()
        if not getattr(r, "picked")(1):
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch)
    assert "guard.py::Rule.picked" in sites
    assert "guard.py::Rule.other" not in sites


def test_cm12_getattr_with_an_unknown_name_fails_closed(tmp_path, monkeypatch):
    """Unbounded dynamic selection must be UNRESOLVED, never 'all members' and never 'none'."""
    guard = '''
    import os


    class Rule:
        def a(self, v):
            if v:
                return True
            return False

        def b(self, v):
            if v:
                return True
            return False


    def main():
        r = Rule()
        if not getattr(r, os.environ["PICK"])(1):
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    problems = problems_of(build(tmp_path, {"guard.py": guard}), monkeypatch)
    assert any("dynamically" in p for p in problems), problems


def test_cm13_a_method_reached_through_a_loader_and_tuple_unpacking_is_a_site(tmp_path,
                                                                              monkeypatch):
    """The false exclusion the first run of this correction produced."""
    guard = '''
    class Value:
        def differs_from(self, other):
            if self is other:
                return []
            return ["x"]


    def parse(text):
        return Value()


    def main():
        left, right = parse("a"), parse("b")
        if left.differs_from(right):
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch)
    assert "guard.py::Value.differs_from" in sites


def test_cm14_a_member_reached_through_a_module_level_instance_in_another_module(tmp_path,
                                                                                 monkeypatch):
    """`signalnest_identity._INV = _inventory()` reaches `LoadedInventory.dig` exactly this way."""
    scripts = {
        "guard.py": '''
        import holder


        def main():
            if not holder.probe("k"):
                return 1
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
        ''',
        "holder.py": '''
        import store

        _INV = store.load()


        def probe(key):
            if _INV.dig(key):
                return True
            return False
        ''',
        "store.py": '''
        class Loaded:
            def dig(self, key):
                if key:
                    return True
                return False

            def unused(self, key):
                if key:
                    return True
                return False


        def load():
            return Loaded()
        ''',
    }
    sites = sites_of(build(tmp_path, scripts), monkeypatch)
    assert "store.py::Loaded.dig" in sites
    assert "store.py::Loaded.unused" not in sites


def test_cm15_a_dead_member_with_a_decisive_name_stays_out(tmp_path, monkeypatch):
    guard = CLASS_GUARD.replace("def never_used(", "def never_used_check(")
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch)
    assert "guard.py::Rule.never_used_check" not in sites


# =====================================================================================
# 2. Command roots — Phase G
# =====================================================================================

SUB_GUARD = '''
import argparse


def shared_setup():
    return {"ok": True}


def shared_helper(state):
    if state["ok"]:
        return True
    return False


def only_a(state):
    if not shared_helper(state):
        return 1
    return 0


def only_b(state):
    if shared_helper(state):
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("a")
    sub.add_parser("b")
    args = parser.parse_args(argv)
    command = args.command or "a"
    state = shared_setup()
    if command == "a":
        return only_a(state)
    if command == "b":
        return only_b(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def workflow_for(argv: str) -> str:
    return f"""
jobs:
  guards:
    steps:
      - name: guard
        id: guard_step
        run: python3 scripts/guard.py {argv}
"""


def test_sc01_the_invoked_subcommand_handler_is_a_site(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow_for("a")), monkeypatch)
    assert "guard.py::only_a" in sites


def test_sc02_a_non_invoked_sibling_is_not_a_site(tmp_path, monkeypatch):
    """THE I28L-02 DEFECT."""
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow_for("a")), monkeypatch)
    assert "guard.py::only_b" not in sites


def test_sc03_shared_pre_dispatch_code_stays_reachable(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow_for("a")), monkeypatch)
    assert "guard.py::shared_setup" in sites


def test_sc04_a_helper_shared_by_both_handlers_stays_reachable(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow_for("a")), monkeypatch)
    assert "guard.py::shared_helper" in sites


def test_sc05_selecting_the_other_subcommand_inverts_the_membership(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow_for("b")), monkeypatch)
    assert "guard.py::only_b" in sites
    assert "guard.py::only_a" not in sites


def test_sc06_the_declared_default_is_used_when_no_positional_is_given(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow_for("")), monkeypatch)
    assert "guard.py::only_a" in sites, "the `or \"a\"` default selects handler a"
    assert "guard.py::only_b" not in sites


def test_sc07_two_invocations_of_different_subcommands_keep_both(tmp_path, monkeypatch):
    workflow = """
jobs:
  guards:
    steps:
      - name: guard a
        id: guard_a
        run: python3 scripts/guard.py a
      - name: guard b
        id: guard_b
        run: python3 scripts/guard.py b
"""
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow), monkeypatch)
    assert "guard.py::only_a" in sites and "guard.py::only_b" in sites, (
        "a script the workflow runs twice with different subcommands reaches both handlers")


def test_sc08_options_do_not_confuse_the_selection(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow_for("--json a")),
                     monkeypatch)
    assert "guard.py::only_a" in sites
    assert "guard.py::only_b" not in sites


def test_sc09_an_unknown_positional_fails_closed(tmp_path, monkeypatch):
    problems = problems_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow_for("zzz")),
                           monkeypatch)
    assert any("UNRESOLVED" in p for p in problems), problems


def test_sc10_a_script_without_subcommands_is_unaffected(tmp_path, monkeypatch):
    guard = '''
    def helper(v):
        if v:
            return True
        return False


    def main():
        if not helper(1):
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch)
    assert sites == {"guard.py::main", "guard.py::helper"}


def test_sc11_the_real_workflow_selects_every_subcommand_it_invokes():
    """CORRECTED AT GATE 4N-I28O. This test used to assert that `ci.yml` runs
    `production_certification.py state` AND NOTHING ELSE. Gate 4N-I28N disproved that by
    execution: the graded `certification_gate` step invokes `verify`, `eligibility` and `certify`
    through `subprocess.run` inside a heredoc. The premise was wrong, so the assertion is now the
    corrected one — every subcommand the workflow really invokes is selected."""
    st.reset_caches()
    st.production_control_function_sites()
    selections = st._DERIVED.get("command_selections", [])
    rows = [s for s in selections if s["module"] == "production_certification.py"]
    assert rows, "no command selection was derived for the one script that dispatches"
    assert {r["selected"] for r in rows} == {"state", "verify", "eligibility", "certify"}, rows


def test_sc12_the_dead_class_member_stays_out_of_the_production_universe():
    """CORRECTED AT GATE 4N-I28O. `Evaluation.allowed` is referenced nowhere and stays out.
    `canonical_sha256` was removed here on the premise that no graded command invokes the
    subcommand reaching it; Gate 4N-I28N disproved that premise, so it is a production site again
    and its absence is no longer the property to assert."""
    st.reset_caches()
    production = {s["id"] for s in st.production_control_function_sites()}
    assert "iam_eval.py::Evaluation.allowed" not in production
    assert "production_certification.py::canonical_sha256" in production


def test_sc13_the_invoked_state_path_kept_every_function_it_executes():
    """The other direction: narrowing must not drop what the workflow actually runs."""
    st.reset_caches()
    production = {s["id"] for s in st.production_control_function_sites()}
    for symbol in ("main", "derive_current_state"):
        assert f"production_certification.py::{symbol}" in production


# =====================================================================================
# 3. Self-protection — this file must not be weakenable in silence.
#
# Gate 4N-I28M's own falsification found this missing: replacing the decisive assertion in the
# class-member matrix, the subcommand matrix or the real-tree finding check with `assert True`
# was caught by NOTHING. Gate 4N-I28K had already learned this lesson for its own matrix and I
# did not carry it forward, which is precisely how a corpus decays into decoration.
# =====================================================================================

def _this_file_ast():
    import ast

    return ast.parse(Path(__file__).read_text(encoding="utf-8"))


def _test_functions() -> dict:
    import ast

    return {n.name: n for n in _this_file_ast().body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_")}


def test_selfprotect_both_matrices_keep_their_cases():
    names = _test_functions()
    members = [n for n in names if n.startswith("test_cm")]
    commands = [n for n in names if n.startswith("test_sc")]
    assert len(members) >= 15, f"the class-member matrix shrank to {len(members)}: {members}"
    assert len(commands) >= 14, f"the subcommand matrix shrank to {len(commands)}: {commands}"


def test_selfprotect_no_case_asserts_a_tautology():
    """`assert True` passes forever and proves nothing; a case reduced to it is a case removed."""
    import ast

    offenders = []
    for name, node in _test_functions().items():
        if name.startswith("test_selfprotect"):
            continue
        real = [a for a in ast.walk(node)
                if isinstance(a, ast.Assert)
                and not (isinstance(a.test, ast.Constant) and a.test.value is True)]
        if not real:
            offenders.append(name)
    assert not offenders, f"these cases assert nothing that can fail: {offenders}"


def test_selfprotect_the_exclusion_cases_still_assert_exclusion():
    """Every case whose point is that something must be ABSENT must still test absence."""
    import ast

    required = {"test_cm02_an_unused_method_is_not_a_site": "Rule.never_used",
                "test_cm04_an_unused_property_is_not_a_site": "Rule.unused_property",
                "test_cm10_an_unused_inherited_method_is_not_a_site": "Base.inherited_unused",
                "test_cm15_a_dead_member_with_a_decisive_name_stays_out": "Rule.never_used_check",
                "test_sc02_a_non_invoked_sibling_is_not_a_site": "only_b",
                "test_sc12_the_dead_class_member_stays_out_of_the_production_universe":
                    "allowed"}
    functions = _test_functions()
    for name, symbol in required.items():
        assert name in functions, f"{name} was removed"
        body = ast.unparse(functions[name])
        assert symbol in body and "not in" in body, (
            f"{name} no longer asserts that {symbol} is absent")


def test_selfprotect_the_synthetic_worlds_are_literals():
    import ast

    for constant in ("CLASS_GUARD", "SUB_GUARD", "INHERIT_GUARD", "WORKFLOW"):
        assigned = [n.value for n in _this_file_ast().body
                    if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == constant for t in n.targets)]
        assert assigned, f"{constant} is no longer a module-level assignment"
        assert isinstance(assigned[0], ast.Constant) and isinstance(assigned[0].value, str), (
            f"{constant} is computed rather than authored")


def test_sc14_no_special_case_exists_for_either_finding():
    """Neither fix may be a denylist entry.

    Asserted structurally rather than by scanning text: the two symbols are DISCUSSED at length in
    the module's docstrings, which is the point of them, so a substring search over the source
    would fail for the right reason and the wrong cause. What must hold is that no identifier and
    no non-docstring string constant names them — that is what a special case would look like.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "scripts" / "site_taxonomy.py").read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    executable_strings = [n.value for n in ast.walk(tree)
                          if isinstance(n, ast.Constant) and isinstance(n.value, str)
                          and n.value not in docstrings]
    identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    identifiers |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    identifiers |= {n.name for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    for token in ("Evaluation", "canonical_sha256", "iam_eval", "production_certification",
                  "allowed"):
        assert not [s for s in executable_strings if token in s], (
            f"{token} appears in a string the module actually evaluates")
        assert not [i for i in identifiers if token in i], (
            f"{token} appears as an identifier in the module")
