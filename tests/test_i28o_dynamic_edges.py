"""Gate 4N-I28O — dynamic execution edges and real CI command roots.

WHAT GATE 4N-I28N FOUND, by execution rather than by reading code:

    I28N-01  `site_taxonomy.py::_prune_dispatch.Pruner.visit_If` RUNS under the graded
             `mutation_discovery.py --fail-on-untested-layer`, IS the branch-pruning rule, and
             belonged to no taxonomy category at all. A class defined inside a function had its
             methods flattened onto the enclosing function, and a method dispatched by
             `ast.NodeTransformer.visit` has no edge naming it.

    I28N-02  the graded step `certification_gate` runs a Python heredoc calling
             `subprocess.run([sys.executable, "scripts/production_certification.py", *args])` with
             `verify`, `eligibility` and `certify`. `release_roots()` saw `state` alone, so six
             functions a graded step really executes were pruned out of the universe.

Neither is fixed by naming a symbol. Identities are now lexical, so a class inside a function keeps
its class; framework dispatch links a DRIVEN visitor to the overrides an execution has been
observed to run; and a root is whatever a graded step actually executes, including inside a
bounded heredoc.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import execution_trace as et  # noqa: E402
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


def _point_at(root: Path, monkeypatch, observed=None):
    monkeypatch.setattr(st, "REPO_ROOT", root)
    monkeypatch.setattr(st, "SCRIPTS", root / "scripts")
    monkeypatch.setattr(st, "TESTS", root / "tests")
    monkeypatch.setattr(st, "WORKFLOW", root / ".github" / "workflows" / "ci.yml")
    pin = root / "tests" / "fixtures" / "framework-dispatch-observed.json"
    pin.write_text(json.dumps({"observed": sorted(observed or [])}), encoding="utf-8")
    monkeypatch.setattr(st, "_OBSERVED_DISPATCH", pin)
    st.reset_caches()


def sites_of(root: Path, monkeypatch, observed=None) -> set[str]:
    _point_at(root, monkeypatch, observed)
    try:
        return {s["id"] for s in st.production_control_function_sites()}
    finally:
        st.reset_caches()


def roots_of(root: Path, monkeypatch) -> list[dict]:
    _point_at(root, monkeypatch)
    try:
        return st.release_roots()
    finally:
        st.reset_caches()


def problems_of(root: Path, monkeypatch, observed=None) -> list[str]:
    _point_at(root, monkeypatch, observed)
    try:
        return st.check()["problems"]
    finally:
        st.reset_caches()


# =====================================================================================
# 1. Function-local classes and framework dispatch — Phases D, E, F
# =====================================================================================

VISITOR_GUARD = '''
import ast


def prune(tree):
    class Pruner(ast.NodeTransformer):
        def visit_If(self, node):
            self.generic_visit(node)
            if isinstance(node.test, ast.Constant):
                return None
            return node

        def visit_While(self, node):
            return node

    return Pruner().visit(tree)


def main():
    if prune(ast.parse("if 1:\\n    pass\\n")) is None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

DISPATCHED = "guard.py::prune.Pruner.visit_If"
UNUSED = "guard.py::prune.Pruner.visit_While"


def test_fd01_a_function_local_class_keeps_its_lexical_identity(tmp_path, monkeypatch):
    _point_at(build(tmp_path, {"guard.py": VISITOR_GUARD}), monkeypatch)
    try:
        index = st.module_index("guard.py")
    finally:
        st.reset_caches()
    assert "prune.Pruner" in index["classes"]
    assert "prune.Pruner.visit_If" in index["functions"]
    assert "prune.visit_If" not in index["functions"], "the class was flattened away again"


def test_fd02_a_method_local_class_keeps_its_lexical_identity(tmp_path, monkeypatch):
    guard = '''
    import ast


    class Outer:
        def build(self):
            class Inner(ast.NodeVisitor):
                def visit_If(self, node):
                    return node
            return Inner()


    def main():
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    _point_at(build(tmp_path, {"guard.py": guard}), monkeypatch)
    try:
        index = st.module_index("guard.py")
    finally:
        st.reset_caches()
    assert "Outer.build.Inner" in index["classes"]
    assert "Outer.build.Inner.visit_If" in index["functions"]


def test_fd03_a_dispatched_override_is_a_site(tmp_path, monkeypatch):
    """THE I28N-01 DEFECT."""
    sites = sites_of(build(tmp_path, {"guard.py": VISITOR_GUARD}), monkeypatch,
                     observed=[DISPATCHED])
    assert DISPATCHED in sites


def test_fd04_an_unexercised_override_is_not_a_site(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": VISITOR_GUARD}), monkeypatch,
                     observed=[DISPATCHED])
    assert UNUSED not in sites


def test_fd05_generic_visit_is_a_candidate_and_follows_the_same_rule(tmp_path, monkeypatch):
    # `generic_visit` must NOT be called explicitly here: an explicit `self.generic_visit(...)`
    # is an ordinary edge and would make the member a site regardless of the pin, which is right
    # but would not test protocol dispatch.
    guard = (VISITOR_GUARD.replace("def visit_While(self, node):", "def generic_visit(self, node):")
             .replace("            self.generic_visit(node)\n", ""))
    generic = "guard.py::prune.Pruner.generic_visit"
    with_pin = sites_of(build(tmp_path / "a", {"guard.py": guard}), monkeypatch,
                        observed=[DISPATCHED, generic])
    without = sites_of(build(tmp_path / "b", {"guard.py": guard}), monkeypatch,
                       observed=[DISPATCHED])
    assert generic in with_pin
    assert generic not in without


def test_fd06_an_inherited_visitor_override_is_reached_through_a_local_base(tmp_path, monkeypatch):
    guard = '''
    import ast


    class Base(ast.NodeTransformer):
        def visit_If(self, node):
            return node


    def prune(tree):
        class Child(Base):
            pass
        return Child().visit(tree)


    def main():
        if prune(ast.parse("if 1:\\n    pass\\n")) is None:
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch,
                     observed=["guard.py::Base.visit_If"])
    assert "guard.py::Base.visit_If" in sites


def test_fd07_a_visitor_instance_returned_by_a_helper_is_driven(tmp_path, monkeypatch):
    guard = '''
    import ast


    class Walker(ast.NodeVisitor):
        def visit_If(self, node):
            return node


    def make():
        return Walker()


    def main():
        walker = make()
        if walker.visit(ast.parse("if 1:\\n    pass\\n")) is None:
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch,
                     observed=["guard.py::Walker.visit_If"])
    assert "guard.py::Walker.visit_If" in sites


def test_fd08_two_local_classes_with_the_same_name_stay_distinct(tmp_path, monkeypatch):
    guard = '''
    import ast


    def first(tree):
        class Pruner(ast.NodeTransformer):
            def visit_If(self, node):
                return node
        return Pruner().visit(tree)


    def second(tree):
        class Pruner(ast.NodeTransformer):
            def visit_If(self, node):
                return node
        return Pruner().visit(tree)


    def main():
        if first(ast.parse("if 1:\\n    pass\\n")) is None:
            return 1
        if second(ast.parse("if 1:\\n    pass\\n")) is None:
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    both = ["guard.py::first.Pruner.visit_If", "guard.py::second.Pruner.visit_If"]
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch, observed=both)
    assert set(both) <= sites, "identities collapsed across owners"


def test_fd09_a_dead_visitor_class_contributes_nothing(tmp_path, monkeypatch):
    guard = VISITOR_GUARD.replace("    return Pruner().visit(tree)", "    return tree")
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch, observed=[DISPATCHED])
    assert DISPATCHED not in sites, "an undriven visitor was admitted on the strength of the pin"


def test_fd10_an_instantiated_but_never_driven_visitor_contributes_nothing(tmp_path, monkeypatch):
    guard = VISITOR_GUARD.replace("    return Pruner().visit(tree)", "    Pruner()\n    return tree")
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch, observed=[DISPATCHED])
    assert DISPATCHED not in sites


NON_PROTOCOL_GUARD = '''
import ast


def prune(tree):
    class Pruner(ast.NodeTransformer):
        def reset(self):
            return None

        def visit_If(self, node):
            return node

    p = Pruner()
    p.reset()
    return tree


def main():
    if prune(ast.parse("if 1:\\n    pass\\n")) is None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def test_fd10b_calling_a_non_protocol_method_does_not_drive_the_visitor(tmp_path, monkeypatch):
    """Only the PROTOCOL entry points drive a dispatcher.

    Gate 4N-I28O's own falsification found this case missing: removing the check that the called
    method is `visit` or `generic_visit` was caught by nothing, because no case called an
    unrelated method on a visitor instance. Touching a visitor is not driving it.
    """
    sites = sites_of(build(tmp_path, {"guard.py": NON_PROTOCOL_GUARD}), monkeypatch,
                     observed=["guard.py::prune.Pruner.visit_If"])
    assert "guard.py::prune.Pruner.reset" in sites, "the explicitly called method is a real edge"
    assert "guard.py::prune.Pruner.visit_If" not in sites, (
        "an unrelated method call was treated as driving the visitor")


def test_fd11_a_framework_base_without_an_override_adds_nothing(tmp_path, monkeypatch):
    guard = '''
    import ast


    def prune(tree):
        class Empty(ast.NodeTransformer):
            pass
        return Empty().visit(tree)


    def main():
        if prune(ast.parse("if 1:\\n    pass\\n")) is None:
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
    root = build(tmp_path, {"guard.py": guard})
    _point_at(root, monkeypatch)
    try:
        assert st.dispatch_candidates("guard.py", "prune.Empty") == []
    finally:
        st.reset_caches()


def test_fd12_a_non_framework_class_is_not_treated_as_a_visitor(tmp_path, monkeypatch):
    guard = VISITOR_GUARD.replace("class Pruner(ast.NodeTransformer):", "class Pruner:")
    sites = sites_of(build(tmp_path, {"guard.py": guard}), monkeypatch, observed=[DISPATCHED])
    assert DISPATCHED not in sites


def test_fd13_the_real_override_is_a_site_on_the_real_tree():
    st.reset_caches()
    sites = {s["id"] for s in st.production_control_function_sites()}
    assert "site_taxonomy.py::_prune_dispatch.Pruner.visit_If" in sites


def test_fd14_the_observation_pin_matches_what_actually_dispatches():
    """The pin is re-derived by EXECUTION, so it can be neither padded nor left to rot.

    The full graded command takes minutes under a profiler, so the owning function is executed
    directly on a minimal input under a narrow profiler instead — the same evidence, cheaply.
    """
    observed = st.observed_dispatch()
    assert observed, "the observation pin is empty; nothing would ever be admitted"

    seen: set[str] = set()

    def profile(frame, event, arg):
        if event == "call":
            code = frame.f_code
            try:
                relative = Path(code.co_filename).resolve().relative_to(REPO_ROOT)
            except (ValueError, OSError):
                return None
            if relative.parts[0] == "scripts":
                seen.add(f"{relative.name}::{code.co_qualname.replace('.<locals>', '')}")
        return None

    source = ast.parse("def f(c):\n    if c == 'a':\n        return 1\n    return 0\n")
    function = source.body[0]
    sys.setprofile(profile)
    try:
        st._prune_dispatch(function, "c", "a")
    finally:
        sys.setprofile(None)

    dispatched = {i for i in seen
                  if i.rsplit(".", 1)[-1].startswith("visit_")
                  or i.rsplit(".", 1)[-1] == "generic_visit"}
    assert dispatched == observed, (
        f"the pin says {sorted(observed)} but execution dispatched {sorted(dispatched)}")


# =====================================================================================
# 2. Real graded command roots — Phases H, I, J, K
# =====================================================================================

SUB_GUARD = '''
import argparse


def shared_setup():
    return {"ok": True}


def verify_only(state):
    if state["ok"]:
        return 0
    return 1


def eligibility_only(state):
    if state["ok"]:
        return 0
    return 1


def certify_only(state):
    if state["ok"]:
        return 0
    return 1


def never_invoked(state):
    if state["ok"]:
        return 0
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    for name in ("verify", "eligibility", "certify", "sleeper"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    command = args.command or "verify"
    state = shared_setup()
    if command == "verify":
        return verify_only(state)
    if command == "eligibility":
        return eligibility_only(state)
    if command == "certify":
        return certify_only(state)
    if command == "sleeper":
        return never_invoked(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

HEREDOC_WORKFLOW = """
jobs:
  guards:
    steps:
      - name: certification gate
        id: certification_gate
        if: always()
        run: |
          python3 - <<'PY'
          import subprocess, sys

          def run(*args):
              return subprocess.run([sys.executable, "scripts/guard.py", *args],
                                    capture_output=True, text=True)

          run("verify", "--artifact", "x")
          run("verify", "--artifact", "y")
          run("eligibility", "--out", "z")
          run("certify", "--eligibility", "w")
          PY
"""


def test_cr01_heredoc_subprocess_invocations_become_roots(tmp_path, monkeypatch):
    """THE I28N-02 DEFECT."""
    roots = roots_of(build(tmp_path, {"guard.py": SUB_GUARD}, HEREDOC_WORKFLOW), monkeypatch)
    assert roots, "no root at all was derived from the heredoc"
    selected = {i[0] for i in roots[0]["invocations"] if i}
    assert {"verify", "eligibility", "certify"} <= selected, selected


def test_cr02_the_step_id_and_condition_are_recorded(tmp_path, monkeypatch):
    roots = roots_of(build(tmp_path, {"guard.py": SUB_GUARD}, HEREDOC_WORKFLOW), monkeypatch)
    # Cached derivations are frozen at Gate 4N-I28AR, so these arrive as tuples. Compare content.
    assert list(roots[0]["release_entry_points"]) == ["certification_gate"]
    assert list(roots[0]["conditions"]) == ["always()"]


@pytest.mark.parametrize("symbol", ["verify_only", "eligibility_only", "certify_only"])
def test_cr03_every_invoked_handler_is_a_site(symbol, tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, HEREDOC_WORKFLOW), monkeypatch)
    assert f"guard.py::{symbol}" in sites


def test_cr04_a_subcommand_nothing_invokes_is_not_a_site(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, HEREDOC_WORKFLOW), monkeypatch)
    assert "guard.py::never_invoked" not in sites


def test_cr05_shared_setup_stays_reachable(tmp_path, monkeypatch):
    sites = sites_of(build(tmp_path, {"guard.py": SUB_GUARD}, HEREDOC_WORKFLOW), monkeypatch)
    assert "guard.py::shared_setup" in sites


def test_cr06_a_repeated_subcommand_is_one_root_not_two(tmp_path, monkeypatch):
    roots = roots_of(build(tmp_path, {"guard.py": SUB_GUARD}, HEREDOC_WORKFLOW), monkeypatch)
    verify_invocations = [i for i in roots[0]["invocations"] if i and i[0] == "verify"]
    assert len(verify_invocations) == 2, "two verify calls with different arguments"
    assert len({tuple(i) for i in verify_invocations}) == 2


def test_cr07_a_direct_subprocess_call_without_a_wrapper_resolves(tmp_path, monkeypatch):
    workflow = """
jobs:
  guards:
    steps:
      - name: direct
        id: direct_step
        run: |
          python3 - <<'PY'
          import subprocess, sys
          subprocess.run([sys.executable, "scripts/guard.py", "eligibility"])
          PY
"""
    roots = roots_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow), monkeypatch)
    assert ["eligibility"] in [list(i) for i in roots[0]["invocations"]]


def test_cr08_a_literal_shell_command_still_resolves(tmp_path, monkeypatch):
    workflow = """
jobs:
  guards:
    steps:
      - name: literal
        id: literal_step
        run: python3 scripts/guard.py certify
"""
    roots = roots_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow), monkeypatch)
    assert ["certify"] in [list(i) for i in roots[0]["invocations"]]


def test_cr09_an_unknown_positional_fails_closed(tmp_path, monkeypatch):
    workflow = """
jobs:
  guards:
    steps:
      - name: literal
        id: literal_step
        run: python3 scripts/guard.py zzz
"""
    problems = problems_of(build(tmp_path, {"guard.py": SUB_GUARD}, workflow), monkeypatch)
    assert any("UNRESOLVED" in p for p in problems), problems


def test_cr10_the_real_certification_gate_contributes_all_four_subcommands():
    st.reset_caches()
    roots = [r for r in st.release_roots() if r["module"] == "production_certification.py"]
    assert roots, "the certification script is not a root at all"
    selected = {i[0] for i in roots[0]["invocations"] if i}
    assert {"state", "verify", "eligibility", "certify"} == selected, selected
    assert "certification_gate" in roots[0]["release_entry_points"]


@pytest.mark.parametrize("symbol", ["establish_eligibility", "load_json", "required_checks",
                                    "transition", "validate_checks", "generate_certification",
                                    "production_gate", "validate_artifact"])
def test_cr11_the_functions_the_gate_executes_are_production_sites(symbol):
    st.reset_caches()
    sites = {s["id"] for s in st.production_control_function_sites()}
    assert f"production_certification.py::{symbol}" in sites


def test_cr12_no_command_root_is_unresolved_on_the_real_tree():
    st.reset_caches()
    result = st.check()
    assert not [p for p in result["problems"] if "UNRESOLVED" in p], result["problems"]


# =====================================================================================
# 3. Trace quality — Phases N, O
# =====================================================================================

TINY = '''
class Holder:
    def method(self):
        return 1


def duplicate():
    return 1


def outer():
    def duplicate():
        return 2
    return duplicate()


def main():
    return Holder().method() + duplicate() + outer() - 4


if __name__ == "__main__":
    raise SystemExit(main())
'''


@pytest.fixture
def tiny_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "tiny.py").write_text(textwrap.dedent(TINY), encoding="utf-8")
    return root


def test_tq01_a_relative_invocation_is_not_discarded(tiny_repo):
    """THE I28L PROBE DEFECT, pinned: runpy reports the relative name it was given."""
    result = et.trace_command("tiny.py", cwd=tiny_repo, repo=tiny_repo)
    assert result["complete"]
    assert "tiny.py::main" in result["executed"], result["executed"]


def test_tq02_qualified_identities_distinguish_duplicate_names(tiny_repo):
    executed = set(et.trace_command("tiny.py", cwd=tiny_repo, repo=tiny_repo)["executed"])
    assert "tiny.py::duplicate" in executed
    assert "tiny.py::outer.duplicate" in executed, "two same-named functions collapsed"


def test_tq03_a_class_method_keeps_its_class(tiny_repo):
    executed = set(et.trace_command("tiny.py", cwd=tiny_repo, repo=tiny_repo)["executed"])
    assert "tiny.py::Holder.method" in executed


def test_tq04_stdlib_and_dependency_frames_are_excluded(tiny_repo):
    executed = et.trace_command("tiny.py", cwd=tiny_repo, repo=tiny_repo)["executed"]
    assert all(i.startswith("tiny.py::") for i in executed), executed


def test_tq05_a_script_outside_the_repository_contributes_nothing(tiny_repo, tmp_path):
    outside = tmp_path / "outside"
    (outside / "scripts").mkdir(parents=True)
    (outside / "scripts" / "tiny.py").write_text(textwrap.dedent(TINY), encoding="utf-8")
    result = et.trace_command("tiny.py", cwd=outside, repo=tiny_repo)
    assert result["executed"] == [], result["executed"]


def test_tq06_a_clone_path_produces_the_same_identities(tiny_repo, tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "scripts").mkdir()
    (clone / "scripts" / "tiny.py").write_text(
        (tiny_repo / "scripts" / "tiny.py").read_text(encoding="utf-8"), encoding="utf-8")
    original = et.trace_command("tiny.py", cwd=tiny_repo, repo=tiny_repo)["executed"]
    copied = et.trace_command("tiny.py", cwd=clone, repo=clone)["executed"]
    assert original == copied


def test_tq07_an_absolute_invocation_matches_a_relative_one(tiny_repo):
    relative = et.trace_command("tiny.py", cwd=tiny_repo, repo=tiny_repo)["executed"]
    absolute = et.trace_command("tiny.py", cwd=tiny_repo, repo=tiny_repo.resolve())["executed"]
    assert relative == absolute


def test_tq08_the_old_filter_would_have_discarded_everything(tiny_repo):
    """The defect itself, asserted rather than described."""
    result = et.trace_command("tiny.py", cwd=tiny_repo, repo=tiny_repo)
    assert result["executed"], "nothing executed, so the comparison would be vacuous"
    old_filter_survivors = [i for i in result["executed"] if "/scripts/" in i]
    assert not old_filter_survivors
    assert et.RELATIVE_FILENAME_DEFECT["old_result"] == "every script frame discarded"


def test_tq09_arguments_and_command_are_recorded(tiny_repo):
    result = et.trace_command("tiny.py", ["--flag"], cwd=tiny_repo, repo=tiny_repo)
    assert result["command"] == "tiny.py --flag"
    assert result["argv"] == ["--flag"]


def test_tq10_dispatched_overrides_are_extracted_from_a_trace():
    fake = [{"executed": ["m.py::A.visit_If", "m.py::A.generic_visit", "m.py::A.other"]}]
    assert et.dispatched_overrides(fake) == ["m.py::A.generic_visit", "m.py::A.visit_If"]
