"""Gate 4N-I28K — the production/control site universe is derived, not spelled.

WHAT GATE 4N-I28J FOUND. The universe was built by matching function names against eleven word
endings inside files ci.yml mentions literally. Four executed mutations landed against that rule:

    rename a real, still-invoked control              127 -> 126   a live control DISAPPEARS
    append a never-called `never_called_check`        127 -> 128   dead code ENTERS
    add a live enforcing helper with a neutral name   127 -> 127   a real control is OMITTED
    delete the word "check" from the suffix tuple     127 -> 116   eleven controls LEAVE

Only the first of those involved any change to enforcement, and even there the enforcement was
unchanged — only the spelling moved.

HOW THIS FILE IS BUILT, AND WHY IT MATTERS. The expectations below are NOT produced by calling
`mutation_discovery.discover_sites()` and writing down what came back, and no constant is imported
from the production derivation to serve as its own oracle. That is the failure this gate chain has
hit repeatedly — most recently at I27P, where a five-form masking test was written from the
implementation it was meant to falsify.

Three independent sources are used instead:

1.  SYNTHETIC WORLDS. Each taxonomy mutation builds a complete miniature repository — a workflow,
    one or more guard scripts — where the correct answer is known by construction because the
    world was authored to make it known. A rule that reads names cannot pass these; a rule that
    reads invocation and consequence cannot fail them.

2.  AN EXECUTION TRACE. A real guard is run in a subprocess under a profiler, and every function
    it ACTUALLY EXECUTED must be in the derived universe. Execution knows nothing about the AST
    walk, so it can only agree with it by being right.

3.  A REVIEWED CHAIN. The leak-scan enforcement chain was adjudicated function by function in
    Phase H of the gate, and those decisions are stated here as literals.
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

import site_taxonomy as st  # noqa: E402


# =====================================================================================
# 1. Synthetic worlds — the correct answer is known by construction.
# =====================================================================================

WORKFLOW = """
jobs:
  guards:
    steps:
      - name: guard
        id: guard_step
        run: python3 scripts/guard.py
"""

BASE_GUARD = '''
"""A miniature guard."""
import sys

THRESHOLD = 3


def decides(value):
    """A decision: a branch whose outcome its caller acts on."""
    if value > THRESHOLD:
        return False
    return True


def computes(values):
    """Straight-line, but its result feeds a decision."""
    return sum(values)


def presents(message):
    """Presentation only: it can print and nothing else."""
    print(message)


def main():
    total = computes([1, 2])
    presents("total")
    if not decides(total):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def build_world(tmp_path: Path, scripts: dict[str, str], workflow: str = WORKFLOW) -> Path:
    """A complete miniature repository, so the expected answer is a property of the fixture."""
    root = tmp_path / "world"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "fixtures").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    for name, source in scripts.items():
        (root / "scripts" / name).write_text(textwrap.dedent(source), encoding="utf-8")
    (root / ".github" / "workflows" / "ci.yml").write_text(workflow, encoding="utf-8")
    return root


def sites_of(root: Path, monkeypatch) -> set[str]:
    """The derived production/control function-site identities of one world."""
    monkeypatch.setattr(st, "REPO_ROOT", root)
    monkeypatch.setattr(st, "SCRIPTS", root / "scripts")
    monkeypatch.setattr(st, "TESTS", root / "tests")
    monkeypatch.setattr(st, "WORKFLOW", root / ".github" / "workflows" / "ci.yml")
    st.reset_caches()
    try:
        return {s["id"] for s in st.production_control_function_sites()}
    finally:
        st.reset_caches()


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    return sites_of(build_world(tmp_path, {"guard.py": BASE_GUARD}), monkeypatch)


def test_the_baseline_world_is_what_it_is_supposed_to_be(baseline):
    """Every later case is a delta against this, so it is asserted rather than assumed."""
    assert baseline == {"guard.py::main", "guard.py::decides", "guard.py::computes"}, baseline
    assert "guard.py::presents" not in baseline, (
        "a helper that can only print was counted as a control; the collapse rule is not working "
        "and every mutation below would then pass for the wrong reason")


# ---- the four I28J failures, each as a mutation that must now behave differently -------------

def test_m01_a_real_enforcing_helper_survives_being_renamed(tmp_path, monkeypatch, baseline):
    """I28J's first failure: 127 -> 126 because a live control was spelled differently."""
    renamed = BASE_GUARD.replace("def decides(", "def quietly(").replace(
        "decides(total)", "quietly(total)")
    sites = sites_of(build_world(tmp_path, {"guard.py": renamed}), monkeypatch)
    assert len(sites) == len(baseline)
    assert "guard.py::quietly" in sites
    assert "guard.py::decides" not in sites


def test_m02_scan_decision_survives_being_renamed(tmp_path, monkeypatch, baseline):
    """The same property stated at the symbol Gate 4N-I28J named first."""
    guard = BASE_GUARD.replace("def decides(", "def scan_decision(").replace(
        "decides(total)", "scan_decision(total)")
    before = sites_of(build_world(tmp_path, {"guard.py": guard}), monkeypatch)
    after = sites_of(build_world(tmp_path, {"guard.py": guard.replace(
        "scan_decision", "path_disposition")}), monkeypatch)
    assert len(before) == len(after) == len(baseline)
    assert "guard.py::path_disposition" in after


def test_m03_scan_text_survives_being_renamed(tmp_path, monkeypatch, baseline):
    guard = BASE_GUARD.replace("def decides(", "def scan_text(").replace(
        "decides(total)", "scan_text(total)")
    after = sites_of(build_world(tmp_path, {"guard.py": guard.replace(
        "scan_text", "examine")}), monkeypatch)
    assert "guard.py::examine" in after
    assert len(after) == len(baseline)


def test_m04_a_decisive_name_renamed_to_neutral_text_keeps_its_site(tmp_path, monkeypatch,
                                                                    baseline):
    guard = BASE_GUARD.replace("def decides(", "def verify(").replace(
        "decides(total)", "verify(total)")
    neutral = guard.replace("def verify(", "def thing(").replace("verify(total)", "thing(total)")
    assert len(sites_of(build_world(tmp_path, {"guard.py": neutral}), monkeypatch)) == len(baseline)


def test_m05_a_dead_helper_with_a_decisive_suffix_is_not_a_site(tmp_path, monkeypatch, baseline):
    """I28J's second failure: 127 -> 128 for a function nothing calls."""
    guard = BASE_GUARD + "\n\ndef never_called_check(x):\n    if x:\n        return 1\n    return 0\n"
    sites = sites_of(build_world(tmp_path, {"guard.py": guard}), monkeypatch)
    assert sites == baseline
    assert "guard.py::never_called_check" not in sites


@pytest.mark.parametrize("name", ["audit_check", "final_verify", "run", "main_report",
                                  "authorize", "load", "requirements", "contract"])
def test_m06_dead_helpers_in_a_guard_script_stay_out_whatever_they_are_called(
        name, tmp_path, monkeypatch, baseline):
    """Every word the removed suffix tuple contained, one at a time, on dead code."""
    guard = BASE_GUARD + f"\n\ndef {name}(x):\n    if x:\n        raise SystemExit(2)\n"
    assert sites_of(build_world(tmp_path, {"guard.py": guard}), monkeypatch) == baseline


def test_m07_a_live_neutral_name_enforcing_helper_is_a_site(tmp_path, monkeypatch, baseline):
    """I28J's third failure: a genuinely enforcing helper omitted for having a plain name."""
    guard = BASE_GUARD.replace(
        "    if not decides(total):",
        "    if not decides(total) or not plainly_named(total):")
    guard += "\n\ndef plainly_named(value):\n    if value < 0:\n        return False\n    return True\n"
    sites = sites_of(build_world(tmp_path, {"guard.py": guard}), monkeypatch)
    assert "guard.py::plainly_named" in sites
    assert len(sites) == len(baseline) + 1


def test_m08_a_transitively_invoked_helper_is_a_site(tmp_path, monkeypatch, baseline):
    """Two hops from the entry point, reached only through another helper."""
    guard = BASE_GUARD.replace(
        "    if value > THRESHOLD:", "    if value > THRESHOLD and deeper(value):")
    guard += "\n\ndef deeper(value):\n    if value % 2:\n        return True\n    return False\n"
    sites = sites_of(build_world(tmp_path, {"guard.py": guard}), monkeypatch)
    assert "guard.py::deeper" in sites
    assert len(sites) == len(baseline) + 1


def test_m09_removing_the_direct_call_keeps_a_transitively_invoked_site(tmp_path, monkeypatch):
    """A control moved one hop further from the entry point has not stopped being a control."""
    direct = BASE_GUARD
    indirect = BASE_GUARD.replace(
        "    total = computes([1, 2])", "    total = wrapper([1, 2])")
    indirect += "\n\ndef wrapper(values):\n    if values:\n        return computes(values)\n    return 0\n"
    before = sites_of(build_world(tmp_path, {"guard.py": direct}), monkeypatch)
    after = sites_of(build_world(tmp_path, {"guard.py": indirect}), monkeypatch)
    assert "guard.py::computes" in before and "guard.py::computes" in after


def test_m10_there_is_no_suffix_configuration_left_to_alter():
    """I28J's fourth failure: 127 -> 116 by deleting one word from a tuple.

    The mutation cannot be performed any more, and that is the assertion: the constant is gone
    from the discovery module and nothing name-shaped replaced it.
    """
    import mutation_discovery

    assert not hasattr(mutation_discovery, "DECISIVE_SUFFIXES")
    source = (REPO_ROOT / "scripts" / "mutation_discovery.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        assert node.func.attr != "endswith", (
            f"a name-suffix test is back in mutation_discovery: {ast.unparse(node)}")
        if node.func.attr == "startswith":
            # The one surviving prefix test skips PRIVATE fixture keys — `_comment`, `_rule` —
            # and decides nothing about any function. Anything else is the defect returning.
            assert [ast.unparse(a) for a in node.args] == ["'_'"], ast.unparse(node)


def test_m11_and_m12_no_word_list_participates_in_membership():
    """Removing `check` from a list, or adding an irrelevant word to one, must be impossible.

    Stated structurally rather than by trying the edit: every module-level string collection in
    the taxonomy is enumerated, and none of them may be consulted with a prefix/suffix test.
    """
    source = (REPO_ROOT / "scripts" / "site_taxonomy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    collections = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, (ast.Set, ast.List, ast.Tuple)) or (
                    isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id in ("frozenset", "set")):
                for target in targets:
                    if getattr(target, "id", "").isupper():
                        collections.add(target.id)
    # Every one of these names PYTHON's own vocabulary — what ends a process, what mutates a
    # container, what a visitor protocol dispatches — not this repository's symbols. That is the
    # line the removed DECISIVE_SUFFIXES crossed.
    assert collections == {"TERMINAL_CALLS", "MUTATING_METHODS", "PRIMARY_CATEGORIES",
                           "_DUNDER_AND_STDLIB_SAFE", "FRAMEWORK_VISITOR_BASES",
                           "FRAMEWORK_DISPATCH_PREFIXES", "FRAMEWORK_DISPATCH_NAMES"}, collections
    # A name test is allowed ONLY against Python's own visitor protocol. That is not a loophole:
    # `ast.NodeVisitor` dispatches BY NAME, so matching `visit_`/`generic_visit` is reading the
    # language's protocol, and the membership below proves the collection contains nothing else.
    import site_taxonomy as _st

    protocol_only = {"FRAMEWORK_DISPATCH_PREFIXES", "FRAMEWORK_DISPATCH_NAMES"}
    assert set(_st.FRAMEWORK_DISPATCH_PREFIXES) == {"visit_"}
    assert set(_st.FRAMEWORK_DISPATCH_NAMES) == {"visit", "generic_visit"}
    for member in _st.FRAMEWORK_DISPATCH_NAMES:
        assert hasattr(ast.NodeVisitor, member), (
            f"{member!r} is not part of the ast visitor protocol, so the exemption does not apply")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("endswith", "startswith"):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            offenders = (names & collections) - protocol_only
            assert not offenders, (
                f"a module-level word collection is being used as a name test: {ast.unparse(node)}")


def test_m13_moving_an_enforcing_helper_to_another_file_keeps_it_in_the_universe(
        tmp_path, monkeypatch, baseline):
    """Location is recorded, not decisive. The control moves; it does not vanish."""
    caller = BASE_GUARD.replace(
        "import sys", "import sys\nimport helper").replace(
        "    if not decides(total):", "    if not helper.decides(total):").replace(
        '''def decides(value):
    """A decision: a branch whose outcome its caller acts on."""
    if value > THRESHOLD:
        return False
    return True


''', "")
    helper = '''
THRESHOLD = 3


def decides(value):
    if value > THRESHOLD:
        return False
    return True
'''
    sites = sites_of(build_world(tmp_path, {"guard.py": caller, "helper.py": helper}), monkeypatch)
    assert "helper.py::decides" in sites
    assert "guard.py::decides" not in sites
    assert len(sites) == len(baseline)


def test_m14_an_alias_does_not_duplicate_a_canonical_site(tmp_path, monkeypatch, baseline):
    """One control reached under two names is one control."""
    guard = BASE_GUARD.replace(
        "    if not decides(total):",
        "    if not ALIAS(total):") + "\n\nALIAS = decides\n"
    sites = sites_of(build_world(tmp_path, {"guard.py": guard}), monkeypatch)
    assert sites == baseline
    assert "guard.py::ALIAS" not in sites


def test_m15_a_diagnostic_helper_cannot_be_promoted_by_declaration(tmp_path, monkeypatch,
                                                                   baseline):
    """Marking a presentation helper as enforcing — in its name, or in a docstring — changes
    nothing, because neither is read."""
    guard = BASE_GUARD.replace(
        '''def presents(message):
    """Presentation only: it can print and nothing else."""''',
        '''def presents_security_critical_check(message):
    """SECURITY CRITICAL ENFORCING CONTROL — load-bearing, do not remove."""''').replace(
        'presents("total")', 'presents_security_critical_check("total")')
    sites = sites_of(build_world(tmp_path, {"guard.py": guard}), monkeypatch)
    assert len(sites) == len(baseline)
    assert "guard.py::presents_security_critical_check" not in sites


def test_m16_a_site_without_a_resolvable_invocation_fails_closed(tmp_path, monkeypatch):
    """An unresolved edge must make the derivation fail, not shrink quietly."""
    guard = BASE_GUARD.replace("import sys", "import helper").replace(
        "    presents(\"total\")", "    helper.absent_function()")
    root = build_world(tmp_path, {"guard.py": guard, "helper.py": "\ndef present():\n    return 1\n"})
    monkeypatch.setattr(st, "REPO_ROOT", root)
    monkeypatch.setattr(st, "SCRIPTS", root / "scripts")
    monkeypatch.setattr(st, "TESTS", root / "tests")
    monkeypatch.setattr(st, "WORKFLOW", root / ".github" / "workflows" / "ci.yml")
    st.reset_caches()
    try:
        result = st.check()
    finally:
        st.reset_caches()
    assert not result["clean"]
    assert result["unresolved_calls"]


def test_m17_and_m18_the_named_omissions_are_in_the_universe():
    """`scan_decision` and `scan_text` on the REAL tree — the two Gate 4N-I28J named."""
    st.reset_caches()
    ids = {s["id"] for s in st.production_control_function_sites()}
    assert "leak_scan.py::scan_decision" in ids
    assert "leak_scan.py::scan_text" in ids


def test_m19_a_top_level_entry_point_does_not_represent_its_chain(baseline):
    """`main` alone is not the universe — the state leak_scan.py was in before this gate."""
    assert baseline != {"guard.py::main"}
    assert len(baseline) > 1


def test_m19_real_tree_the_leak_scanner_is_more_than_its_entry_point():
    """Asserted separately from the synthetic case: a test holding a monkeypatched world cannot
    also speak about the real one, and mixing the two is how a real-tree claim quietly becomes a
    claim about a fixture."""
    st.reset_caches()
    leak = {s["id"] for s in st.production_control_function_sites()
            if s["module"] == "leak_scan.py"}
    assert len(leak) > 1 and "leak_scan.py::main" in leak


def test_m20_not_every_reachable_helper_is_a_site(tmp_path, monkeypatch, baseline):
    """The other extreme. A reachable function with no consequence is excluded, and the
    exclusion is recorded rather than silent."""
    root = build_world(tmp_path, {"guard.py": BASE_GUARD})
    monkeypatch.setattr(st, "REPO_ROOT", root)
    monkeypatch.setattr(st, "SCRIPTS", root / "scripts")
    monkeypatch.setattr(st, "TESTS", root / "tests")
    monkeypatch.setattr(st, "WORKFLOW", root / ".github" / "workflows" / "ci.yml")
    st.reset_caches()
    try:
        collapsed = st.collapsed_presentation_helpers()
    finally:
        st.reset_caches()
    assert [c["id"] for c in collapsed] == ["guard.py::presents"]
    assert "guard.py::presents" not in baseline


# =====================================================================================
# 2. The execution trace — an oracle that never reads the AST derivation.
# =====================================================================================

TRACER = r'''
import json, sys, runpy
sys.path.insert(0, "SCRIPTS")
executed = set()
def profile(frame, event, arg):
    if event == "call":
        code = frame.f_code
        executed.add((code.co_filename, code.co_name))
    return None
import MODULE as target
sys.setprofile(profile)
try:
    target.ENTRY()
except SystemExit:
    pass
except Exception:
    pass
finally:
    sys.setprofile(None)
names = sorted({n for f, n in executed if f.endswith("/MODULE.py")})
print(json.dumps(names))
'''


def _traced_functions(module: str, entry: str) -> list[str]:
    source = (TRACER.replace("SCRIPTS", str(REPO_ROOT / "scripts"))
              .replace("MODULE", module).replace("ENTRY", entry))
    proc = subprocess.run([sys.executable, "-c", source], cwd=REPO_ROOT,
                          capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": "/tmp",
                               "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module,entry", [("leak_scan", "main"),
                                          ("expiry_authorization", "main")])
def test_every_function_a_guard_actually_executes_is_in_the_universe(module, entry):
    """THE INDEPENDENT ORACLE.

    A real guard is executed under a profiler in a subprocess and the functions it entered are
    recorded. Execution has no access to the reachability walk, the consequence rule, or any
    constant in `site_taxonomy`; it can agree with the derived universe only by the derivation
    being correct. Under the rule this gate replaced, `leak_scan` executed eleven functions and
    exactly one of them — `main` — was in the universe.
    """
    traced = _traced_functions(module, entry)
    assert traced, "the trace recorded nothing, so it proves nothing"
    st.reset_caches()
    # A traced frame carries `co_name`, which is the BARE name: the profiler reports
    # `_CredentialRule.search` as `search`. Comparison is therefore on the last component, and
    # the first run of this test is what established that the credential matcher was reachable
    # only through a module-level class alias.
    derived = {s["symbol"].split(".")[-1] for s in st.production_control_function_sites()
               if s["module"] == f"{module}.py"}
    collapsed = {c["id"].split("::", 1)[1].split(".")[-1]
                 for c in st.collapsed_presentation_helpers()}
    missing = [name for name in traced
               if name not in derived and name not in collapsed and not name.startswith("<")]
    assert not missing, (
        f"{module}.py executed {missing} and the derived universe does not contain them. A "
        f"function the guard RAN is load-bearing by demonstration.")


def test_the_trace_oracle_can_fail():
    """A green oracle that cannot go red is not an oracle.

    `deny_requirements.py::independence` is defined and called by nothing, so it must never
    appear in a trace; asserting that a traced set is non-empty and that a known-dead symbol is
    absent from the universe keeps the check above from passing vacuously.
    """
    st.reset_caches()
    ids = {s["id"] for s in st.production_control_function_sites()}
    assert "deny_requirements.py::independence" not in ids
    assert "deny_requirements.py::_principal_matches" not in ids


# =====================================================================================
# 3. The reviewed leak-scan chain — Phase H, adjudicated function by function.
# =====================================================================================

REVIEWED_LEAK_SCAN_CHAIN = {
    "candidate_files": "discovery and the outer filter; the ADV-01 defect lived here",
    "scan_decision": "the single function deciding SCANNED versus each categorised skip",
    "is_scannable": "the inner scan filter, pinned by Gates 4N-I28C and I28D",
    "scan_text": "the protected-token detector itself",
    "_CredentialRule.search": "the credential matcher, reached through a module-level class alias",
    "_zone_is_declared_synthetic": "decides whether a hosted-zone candidate is exempt",
    "approved_accounts": "the account registry loader, which refuses on a bad registry",
    "require_registered_allowed_accounts": "refuses an allowed account with no registry entry",
    "require_registry_references_resolve": "refuses a registry citing a file that is not there",
    "scan_repository": "the top-level scan",
    "_ignored": "decides whether a finding is suppressed",
    "main": "the CLI entry point and exit code",
}


def test_the_reviewed_leak_scan_chain_is_present_in_full():
    st.reset_caches()
    ids = {s["id"] for s in st.production_control_function_sites()}
    missing = sorted(f"leak_scan.py::{s}" for s in REVIEWED_LEAK_SCAN_CHAIN
                     if f"leak_scan.py::{s}" not in ids)
    assert not missing, missing


def test_scan_accounting_is_a_ci_release_site_not_a_production_one():
    """It is reached only from the graded suite, and a graded test failure still stops a release.

    Categorising it as production control would overstate the guard pipeline; dropping it would
    lose the RC-2 reconciliation invariant entirely — the I28J disappearance, arrived at from the
    other side.
    """
    st.reset_caches()
    production = {s["id"] for s in st.production_control_function_sites()}
    ci_release = {s["id"] for s in st.ci_release_control_sites()}
    assert "leak_scan.py::scan_accounting" not in production
    assert "leak_scan.py::scan_accounting" in ci_release


def test_enforcement_path_module_is_categorised_by_what_invokes_it():
    """Gate 4N-I28J left this open. It is answered by derivation, in both directions."""
    st.reset_caches()
    production = {s["id"] for s in st.production_control_function_sites()}
    ci_release = {s["id"] for s in st.ci_release_control_sites()}
    assert not [i for i in production if i.startswith("enforcement_path.py::")], (
        "no ci.yml step runs this module, so it is not production control flow")
    assert "enforcement_path.py::enforcement_inventory" in ci_release
    assert "enforcement_path.py::reachable_functions" in ci_release
    # Its own `check()` and `main()` are invoked by nothing at all — not by the workflow and not
    # by the suite. They are diagnostics until something calls them, and saying so is the whole
    # point of deriving the category instead of assuming it from the module's importance.
    assert "enforcement_path.py::check" not in production | ci_release
    assert "enforcement_path.py::main" not in production | ci_release


# =====================================================================================
# 4. Structural invariants of the universe itself.
# =====================================================================================

def test_the_taxonomy_resolves_completely_on_the_real_tree():
    st.reset_caches()
    result = st.check()
    assert list(result["unresolved_calls"]) == []
    assert result["duplicates"] == []
    assert result["problems"] == []
    assert result["clean"]


def test_every_site_carries_the_evidence_its_category_claims():
    st.reset_caches()
    required = ("canonical_site_id", "implementation_path", "symbol", "direct_callers",
                "invocation_chain", "release_entry_point", "protected_invariant",
                "security_or_release_consequence", "terminal_failure_behaviour",
                "release_role", "execution_evidence", "independent_mutation_pin",
                "enforcement_fingerprint", "primary_category")
    for site in st.production_control_function_sites():
        for field in required:
            assert field in site and site[field] not in (None, ""), (site["id"], field)
        assert site["primary_category"] in st.PRIMARY_CATEGORIES
        assert site["invocation_chain"][-1] == site["canonical_site_id"]


def test_a_site_belongs_to_exactly_one_primary_category():
    st.reset_caches()
    production = {s["id"] for s in st.production_control_function_sites()}
    ci_release = {s["id"] for s in st.ci_release_control_sites()}
    collapsed = {c["id"] for c in st.collapsed_presentation_helpers()}
    unreachable = set(st.unreachable_functions())
    groups = [production, ci_release, collapsed, unreachable]
    for i, left in enumerate(groups):
        for right in groups[i + 1:]:
            assert not (left & right), sorted(left & right)[:5]


# =====================================================================================
# 5. Self-protection — this file must not become the thing it exists to catch.
#
# Gate 4N-I28K's own falsification found this missing. Rewriting `REVIEWED_LEAK_SCAN_CHAIN` into
# a comprehension over `mutation_discovery.discover_sites()` made every expectation agree with the
# implementation by construction, and the whole file still passed. That is the self-authored
# oracle this chain has hit at I23, I25, I27P and I28G, arriving inside the remediation built to
# end it. The guards below are the reason it cannot arrive again silently.
# =====================================================================================

def _this_file_ast() -> ast.Module:
    return ast.parse(Path(__file__).read_text(encoding="utf-8"))


def _module_assignment(name: str) -> ast.AST:
    for node in _this_file_ast().body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name
                                                for t in node.targets):
            return node.value
    raise AssertionError(f"{name} is no longer a module-level assignment in this file")


def test_self_protection_the_reviewed_chain_is_a_literal():
    """It records a HUMAN adjudication. A comprehension here would be the implementation
    describing itself, and the file would pass no matter what the derivation did."""
    value = _module_assignment("REVIEWED_LEAK_SCAN_CHAIN")
    assert isinstance(value, ast.Dict), type(value).__name__
    assert value.keys and all(isinstance(k, ast.Constant) for k in value.keys)
    assert all(isinstance(v, ast.Constant) for v in value.values)


def test_self_protection_the_synthetic_worlds_are_literals():
    for name in ("BASE_GUARD", "WORKFLOW", "TRACER"):
        value = _module_assignment(name)
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), name


def test_self_protection_the_production_discoverer_is_never_an_expectation_source():
    """`site_taxonomy` is the subject under test and may be called. `mutation_discovery` is the
    consumer whose output an expectation must never be built from — and the one falsification
    F14 used."""
    tree = _this_file_ast()
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 and node.module}
    for node in tree.body:
        assert not (isinstance(node, (ast.Import, ast.ImportFrom))
                    and "mutation_discovery" in ast.unparse(node)), (
            "mutation_discovery is imported at module level, where its output can reach an "
            "expectation constant. The one legitimate use is inside a single test asserting the "
            "suffix constant is gone.")
    uses = [n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and ast.unparse(n).startswith("mutation_discovery.")]
    assert not uses, [ast.unparse(u) for u in uses]


def test_self_protection_the_mutation_domain_cannot_be_emptied():
    """A shrinking matrix reads exactly like a passing one."""
    import re as _re

    names = [n.name for n in _this_file_ast().body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name.startswith("test_m")]
    # Cases are counted by MUTATION NUMBER, not by function, because two of them are asserted in
    # one body (m11/m12, m17/m18) and one is split across two (m19). Counting functions would
    # let a case be dropped by merging it into a neighbour.
    covered = {int(m) for name in names for m in _re.findall(r"_m(\d\d)", name)}
    assert covered == set(range(1, 21)), f"missing mutation cases: {sorted(set(range(1, 21)) - covered)}"


def test_self_protection_no_production_word_collection_is_read_as_an_expectation():
    """Importing the derivation's own vocabulary would make the oracle agree by definition."""
    tree = _this_file_ast()
    reads = {n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
             and n.value.id == "st" and n.attr.isupper()}
    assert reads <= {"PRIMARY_CATEGORIES"}, (
        f"this file reads production constants as expectations: {sorted(reads)}")


def test_renaming_does_not_change_a_controls_enforcement_fingerprint(tmp_path, monkeypatch):
    """Identity of the CONTROL is independent of what it is called and where it lives."""
    def fingerprints(scripts):
        root = build_world(tmp_path / scripts["marker"], {k: v for k, v in scripts.items()
                                                          if k != "marker"})
        monkeypatch.setattr(st, "REPO_ROOT", root)
        monkeypatch.setattr(st, "SCRIPTS", root / "scripts")
        monkeypatch.setattr(st, "TESTS", root / "tests")
        monkeypatch.setattr(st, "WORKFLOW", root / ".github" / "workflows" / "ci.yml")
        st.reset_caches()
        try:
            return {s["symbol"]: s["enforcement_fingerprint"]
                    for s in st.production_control_function_sites()}
        finally:
            st.reset_caches()

    before = fingerprints({"marker": "a", "guard.py": BASE_GUARD})
    renamed = BASE_GUARD.replace("def decides(", "def quietly(").replace(
        "decides(total)", "quietly(total)")
    after = fingerprints({"marker": "b", "guard.py": renamed})
    assert before["decides"] == after["quietly"]
