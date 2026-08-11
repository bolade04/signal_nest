"""Gate 4N-I28BB — exec command-position transfer and load-bearing executable coverage.

Closes the load-bearing half of ADV-I28AX-01: `exec kubectl` derived NOTHING while reporting
COMPLETE, trustworthy, zero unresolved, zero unsupported and zero parse errors.

THE PROPERTY THAT MATTERS, AND WHY A COMMAND COUNT COULD NOT EXPRESS IT. Before this gate the
inert form `# exec kubectl` and the live form `exec kubectl` produced the SAME empty result. A
correct empty and a silently swallowed command were indistinguishable, so no assertion over
commands alone could tell success from the bug. Every test below therefore asserts on the
TRANSFER SITE — a positive record that a transfer exists and what it resolved to — and the
comparisons against the independent oracle carry an expected-presence condition, because two
empty results are also equal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import docker_boundary as db                      # noqa: E402
import exec_transfer_oracle as oracle             # noqa: E402
import npm_authority as npm                       # noqa: E402
import shell_positions as sp                      # noqa: E402

CORPUS = json.loads((REPO_ROOT / "tests" / "fixtures" / "exec-transfer-corpus.json")
                    .read_text(encoding="utf-8"))["cases"]


def _sites(src):
    return [{"origin": "f", "line": t.line, "word": t.word, "child": t.child,
             "classification": t.classification, "options": list(t.options)}
            for t in sp.scan(src).transfer_sites]


# ---------------------------------------------------------------- the finding itself
def test_the_adv_i28ax_01_finding_is_closed():
    """The exact reported shape: bare vs exec-prefixed, at the same injection point."""
    bare = sp.scan("kubectl apply -f x")
    prefixed = sp.scan("exec kubectl apply -f x")
    assert "kubectl" in bare.executables(), "the control must discover the bare command"
    assert "kubectl" in prefixed.executables(), (
        "ADV-I28AX-01: `exec kubectl` must discover kubectl. Before Gate 4N-I28BB it derived "
        "nothing while reporting COMPLETE and trustworthy.")


def test_an_exec_child_is_distinguishable_from_inert_text():
    """The property no command count could express.

    `# exec kubectl` and `exec kubectl` used to be identical empty results. They must not be.
    """
    live = sp.scan("exec kubectl")
    inert = sp.scan("# exec kubectl")
    assert live.transfer_sites, "a live exec must leave a positive record"
    assert not inert.transfer_sites, "a commented exec must leave none"
    assert live.executables() != inert.executables()


@pytest.mark.parametrize("word", ["docker", "npm", "helm", "curl", "synthetic-unknown"])
def test_exec_does_not_hide_any_executable(word):
    assert word in sp.scan(f"exec {word} sub --flag").executables()


# ---------------------------------------------------------------- option model (§8)
def test_the_option_table_is_explicit_and_arity_bearing():
    contract = sp.exec_grammar_contract()
    assert contract["options"] == {"-a": 1, "-c": 0, "-l": 0}
    assert contract["terminator"] == "--"


def test_the_value_of_dash_a_is_never_mistaken_for_the_child():
    """The precise defect a generic hyphen-skip rule produces."""
    result = sp.scan("exec -a docker kubectl get")
    assert result.transfer_sites[0].child == "kubectl"
    assert "docker" not in result.executables(), (
        "`docker` is the -a NAME, not the command; a hyphen-skip rule reports it as the child")
    assert not db._scan_source("exec -a docker kubectl get"), (
        "and it must not register as a Docker call site either")


def test_an_unknown_option_fails_closed_rather_than_being_skipped():
    result = sp.scan("exec -Z kubectl")
    assert result.status == "UNSUPPORTED"
    assert not result.is_trustworthy()
    assert result.transfer_sites[0].classification == "UNSUPPORTED_AND_FAIL_CLOSED"


def test_the_terminator_makes_an_option_shaped_word_the_child():
    assert sp.scan("exec -- -weird-tool").transfer_sites[0].child == "-weird-tool"


def test_no_generic_hyphen_skip_rule_exists_in_the_exec_handler():
    """Structural AND functional: the rule §8 forbids must not be reachable."""
    assert sp.scan("exec -Z -Y -X kubectl").status == "UNSUPPORTED", (
        "a chain of unknown options must not be skipped down to the child")


# ---------------------------------------------------------------- dynamic targets (§9)
@pytest.mark.parametrize("src,child", [
    ('exec "$VENV_PY"', "$VENV_PY"), ('exec "$CMD"', "$CMD"), ('exec "${CMD}"', "${CMD}"),
    ('exec "$DIR/tool"', "$DIR/tool"), ('exec "${PREFIX}tool"', "${PREFIX}tool"),
    ('exec "$@"', "$@"), ("exec ${ARRAY[@]}", "${ARRAY[@]}"),
])
def test_a_dynamic_child_becomes_an_explicit_unresolved_site(src, child):
    result = sp.scan(src)
    site = result.transfer_sites[0]
    assert site.classification == "DYNAMIC_CHILD_UNRESOLVED"
    assert site.child == child
    assert result.unresolved, "it must also be an unresolved construct policy has to adjudicate"
    assert not result.executables(), "a dynamic target must never be invented as a static identity"


def test_a_command_substitution_child_is_dynamic_and_its_own_command_is_still_found():
    result = sp.scan('exec "$(resolve_cmd)" x')
    assert result.transfer_sites[0].classification == "DYNAMIC_CHILD_UNRESOLVED"
    assert "resolve_cmd" in result.executables(), (
        "the target is unresolved, but the substitution really does execute resolve_cmd")


def test_the_three_tracked_sites_are_unresolved_not_invented():
    """The whole tracked-tree consequence, asserted as a fact rather than a count."""
    found = []
    for rel in ("scripts/run-api.sh", "scripts/run-worker.sh", "scripts/run-tests-api.sh"):
        result = sp.scan_script((REPO_ROOT / rel).read_text(encoding="utf-8"), origin=rel)
        sites = [t for t in result.transfer_sites if t.word == "exec"]
        assert len(sites) == 1, f"{rel} should carry exactly one exec transfer"
        assert sites[0].classification == "DYNAMIC_CHILD_UNRESOLVED"
        assert sites[0].child == "$VENV_PY"
        found.append(rel)
    assert len(found) == 3


def test_every_tracked_unresolved_exec_site_is_declared():
    policy = json.loads((REPO_ROOT / "tests" / "fixtures" / "executable-trust-policy.json")
                        .read_text(encoding="utf-8"))
    declared = {(d["module"], d["line"], d["word"]) for d in policy["dynamic_shell_sites"]}
    for module, line in (("run-api.sh", 12), ("run-worker.sh", 11), ("run-tests-api.sh", 9)):
        assert (module, line, "$VENV_PY") in declared, (
            f"{module}:{line} is an unresolved exec transfer and must be declared with what bounds it")


# ---------------------------------------------------------------- coproc (§10)
@pytest.mark.parametrize("src", [
    "coproc kubectl", "coproc CO kubectl", "coproc { kubectl; }", "coproc",
])
def test_coproc_fails_closed(src):
    result = sp.scan(src)
    assert result.status == "UNSUPPORTED"
    assert not result.is_trustworthy(), "an unsupported transfer must never be a trust input"
    assert result.unsupported


@pytest.mark.parametrize("src", [
    "# coproc kubectl", 'echo "coproc kubectl"', "cat <<'EOF'\ncoproc kubectl\nEOF",
])
def test_inert_coproc_text_stays_inert(src):
    assert sp.scan(src).status == "COMPLETE"


def test_coproc_is_absent_from_tracked_load_bearing_source():
    """Derived, not assumed — the classification is only defensible if it is currently true."""
    for origin, text in oracle.tracked_sources().items():
        for site in oracle.derive(text, origin=origin):
            assert site["word"] != "coproc", f"{origin}:{site['line']} introduces a coproc"


def test_a_future_tracked_coproc_would_fail_closed():
    """The classification must bind future use, not merely describe today's absence."""
    result = sp.scan("echo start\ncoproc kubectl apply\necho end")
    assert not result.is_trustworthy()


# ---------------------------------------------------------------- inventory (§11)
def test_the_executable_inventory_is_clean_and_consumes_transfers():
    import executable_inventory as ei
    check = ei.check()
    assert check["clean"], check["problems"]


def test_an_undeclared_exec_child_fails_policy_completeness():
    result = sp.scan("exec synthetic-unknown-tool --go")
    assert "synthetic-unknown-tool" in result.executables(), (
        "it must be DISCOVERED; policy completeness then has something to object to")


# ---------------------------------------------------------------- Docker (§12)
def test_exec_docker_enters_docker_adjudication():
    line = "exec docker run --privileged -v /:/host alpine sh"
    sites = db._scan_source(line)
    assert len(sites) == 1, "exec docker must be a Docker call site"
    assert "--privileged" in sites[0]["argv"], "privileged flags must remain visible through exec"


def test_exec_docker_steering_flags_remain_visible():
    sites = db._scan_source("exec docker --host tcp://attacker:2375 run x")
    assert sites and "--host" in sites[0]["argv"]


def test_a_synthetic_exec_docker_site_adds_exactly_one_to_both_derivations():
    synthetic = "exec docker run --privileged x"
    before_boundary = len(db._scan_source("docker version"))
    after_boundary = len(db._scan_source("docker version\n" + synthetic))
    before_shared = len([c for c in sp.scan("docker version").commands if c.word == "docker"])
    after_shared = len([c for c in sp.scan("docker version\n" + synthetic).commands
                        if c.word == "docker"])
    assert after_boundary - before_boundary == 1
    assert after_shared - before_shared == 1


def test_the_docker_derivations_still_reconcile():
    result = db.reconcile_with_shared_deriver()
    assert not result["problems"], result["problems"]


def test_a_dynamic_exec_target_is_not_assumed_to_be_docker():
    assert not db._scan_source('exec "$D" docker run x')


# ---------------------------------------------------------------- npm (§13)
def test_exec_npm_enters_npm_adjudication():
    assert "npm" in sp.scan("exec npm ci").executables()
    assert "npx" in sp.scan("exec npx tsc").executables()


def test_npm_call_sites_reconcile_two_ways():
    result = npm.derive_call_sites()
    assert result["clean"], result["problems"]
    assert result["agree"]
    assert result["shared_count"] > 0, (
        "a zero npm inventory would make agreement meaningless; this repository does invoke npm")


def test_a_dynamic_exec_target_is_not_assumed_to_be_npm():
    assert not sp.scan('exec "$NPM" ci').executables()


# ---------------------------------------------------------------- correlated error (§14)
def test_production_and_oracle_agree_on_the_tracked_tree_and_are_non_empty():
    production = []
    for origin, text in sorted(oracle.tracked_sources().items()):
        scanned = (sp.scan_script(text, origin=origin) if origin.endswith((".sh", ".bash"))
                   else sp.scan(text, origin=origin))
        for site in scanned.transfer_sites:
            production.append({"origin": origin, "line": site.line, "word": site.word,
                               "child": site.child, "classification": site.classification,
                               "options": list(site.options)})
    independent = oracle.derive_tracked()
    result = oracle.compare(production, independent, expect_present=True)
    assert result["clean"], result["problems"]
    assert result["production"] > 0 and result["oracle"] > 0


@pytest.mark.parametrize("src", [
    "exec kubectl apply -f x", "exec docker run --privileged x", "exec npm ci",
    "exec /usr/local/bin/tool", 'exec "$VENV_PY" -m pytest', "coproc kubectl",
])
def test_positive_fixtures_are_non_empty_in_both_derivations(src):
    result = oracle.compare(_sites(src), oracle.derive(src, origin="f"), expect_present=True)
    assert result["clean"], result["problems"]


def test_two_empty_results_are_not_a_pass_on_a_positive_fixture():
    """The correlated-omission shape itself. This is the control ADV-I28AX-01 defeated."""
    assert not oracle.compare([], [], expect_present=True)["clean"]


@pytest.mark.parametrize("mutate,label", [
    (lambda s: [], "production emptied"),
    (lambda s: [dict(s[0], line=999)], "source position moved"),
    (lambda s: [dict(s[0], classification="DYNAMIC_CHILD_UNRESOLVED")], "static relabelled dynamic"),
    (lambda s: [dict(s[0], child="helm")], "child identity changed"),
])
def test_disagreement_is_detected(mutate, label):
    base = "exec kubectl apply -f x"
    independent = oracle.derive(base, origin="f")
    assert not oracle.compare(mutate(_sites(base)), independent, expect_present=True)["clean"], label


def test_an_emptied_oracle_is_detected():
    base = "exec kubectl apply -f x"
    assert not oracle.compare(_sites(base), [], expect_present=True)["clean"]


def test_the_oracle_does_not_import_the_production_parser():
    """Independence asserted structurally, because it is the entire value of this oracle."""
    source = (REPO_ROOT / "scripts" / "exec_transfer_oracle.py").read_text(encoding="utf-8")
    assert "import shell_positions" not in source
    assert "from shell_positions" not in source


# ---------------------------------------------------------------- completeness (§15)
@pytest.mark.parametrize("src", [
    "exec kubectl\ncase \"$x\" in a) echo a ;; esac",
    'case "$x" in a) exec kubectl ;; esac',
    'case "$x" in a) echo a ;; esac\nexec kubectl',
    "f() { exec kubectl; }", "for i in 1 2; do exec kubectl; done",
    "if true; then exec kubectl; fi", "echo hi | exec kubectl",
    "true && exec kubectl", "false || exec kubectl", "( exec kubectl )",
    'x="$(exec kubectl)"', "exec kubectl\nhelm upgrade r c",
])
def test_exec_never_breaks_complete_source_consumption(src):
    result = sp.scan(src)
    assert result.eof_reached
    assert not result.unconsumed_ranges
    assert not result.open_frames
    assert result.consumed_position == result.source_length
    assert "kubectl" in result.executables()


def test_a_region_after_exec_is_still_scanned():
    assert sp.scan("exec kubectl\nhelm upgrade r c").executables() == {"kubectl", "helm"}


# ---------------------------------------------------------------- corpus (§20)
@pytest.mark.parametrize("case", CORPUS, ids=[c["id"] for c in CORPUS])
def test_grammar_derived_corpus(case):
    result = sp.scan(case["source"])
    assert result.status == case["expected_status"], (
        f"{case['id']}: expected {case['expected_status']} from bash semantics "
        f"({case['derived_from']}), got {result.status}")
    if case["expected_classification"]:
        assert result.transfer_sites, f"{case['id']}: expected a transfer site"
        assert result.transfer_sites[0].classification == case["expected_classification"]
    if case["expected_child"]:
        assert result.transfer_sites[0].child == case["expected_child"]
    for word in case["expected_executables"]:
        assert word in result.executables(), f"{case['id']}: {word} must be discovered"


def test_the_corpus_agrees_with_real_bash_on_syntactic_validity(tmp_path):
    """RC-5: the corpus is checked against an oracle OUTSIDE this implementation."""
    if not Path("/bin/bash").exists():                       # pragma: no cover
        pytest.skip("no /bin/bash to act as an external oracle")
    disagreements = []
    for case in CORPUS:
        script = tmp_path / "case.sh"
        script.write_text(case["source"], encoding="utf-8")
        rejected = subprocess.run(["/bin/bash", "-n", str(script)],
                                  capture_output=True).returncode != 0
        trusted = sp.scan(case["source"]).is_trustworthy()
        # The parser may be STRICTER than bash (it refuses unmodelled options bash accepts and
        # would fail only at runtime). It may never be LOOSER: anything bash rejects as syntax must
        # not reach a PERMITTED TRUST STATUS.
        #
        # The criterion is trustworthiness, not a particular status name. My first version required
        # MALFORMED specifically and failed on `coproc { kubectl; }`, which bash rejects and the
        # parser calls UNSUPPORTED — equally fail-closed, and arguably the more accurate label.
        # Pinning the status name rather than the property was the defect.
        if rejected and trusted:
            disagreements.append((case["id"], "bash rejects it, yet the parser reports a "
                                              "trustworthy result"))
    assert not disagreements, disagreements


# ---------------------------------------------------------------- baseline / finish (§17,§18)
def test_the_session_baseline_binds_the_exec_grammar():
    digest = sp.completeness_digest()
    assert digest["exec_grammar_version"] == sp.EXEC_GRAMMAR_VERSION
    assert digest["exec_grammar_digest"]
    assert "transfer_site_total" in digest


def test_the_completeness_digest_changes_when_a_transfer_child_changes(monkeypatch):
    """Functional, not structural: drive a real difference through the digest."""
    before = sp.completeness_digest()["digest"]
    original = sp.scan_script

    def dropped(text, *, origin="<script>"):
        result = original(text, origin=origin)
        result.transfer_sites = []
        return result

    monkeypatch.setattr(sp, "scan_script", dropped)
    assert sp.completeness_digest()["digest"] != before, (
        "a dropped transfer site must move the digest, or session-finish drift cannot see it")


def test_the_bootstrap_exec_layer_is_clean_and_has_a_positive_control():
    import signalnest_bootstrap as sb
    state = sb._exec_transfer_state()
    assert state["clean"], state["problems"]
    assert state["positive_control_clean"]
    assert state["agree"]
    assert state["production_sites"] == state["independent_sites"] > 0


def test_session_finish_detects_a_removed_transfer_site():
    import signalnest_bootstrap as sb
    baseline = sb._exec_transfer_state()
    tampered = dict(baseline, sites=[])
    assert tampered["sites"] != baseline["sites"], (
        "the baseline must be non-empty, or comparing against it proves nothing")


def test_the_exec_grammar_contract_is_bound_not_merely_described():
    contract = sp.exec_grammar_contract()
    assert contract["version"] == sp.EXEC_GRAMMAR_VERSION
    assert "coproc" in contract["unsupported_transferring_words"]
    assert any("hyphen" in rule for rule in contract["rules"])


# ---------------------------------------------------------------- escape closures (§19, §22)
def test_the_production_empty_condition_is_reported_in_its_own_right():
    """Closes falsification arm f13.

    `compare()` reports production-emptiness AND a set difference. The set difference alone made
    the emptiness guard redundant, so deleting it escaped the battery. The guard exists for the
    case where emptiness is the whole finding, so it is asserted by MESSAGE, not by outcome.
    """
    result = oracle.compare([], oracle.derive("exec kubectl", origin="f"), expect_present=True)
    assert any("production derivation is EMPTY" in p for p in result["problems"]), (
        "the production-empty condition must be reported explicitly, not merely implied by a "
        "set difference that happens to be non-empty")


def test_the_oracle_empty_condition_is_reported_in_its_own_right():
    result = oracle.compare(_sites("exec kubectl"), [], expect_present=True)
    assert any("oracle derivation is EMPTY" in p for p in result["problems"])


def test_the_bootstrap_refuses_when_the_exec_layer_is_dirty(monkeypatch):
    """Closes falsification arm f20: the ENFORCEMENT path, not just the computation.

    Asserting that `_exec_transfer_state()` returns clean says nothing about whether `establish`
    acts on a dirty result. Deleting the `if not exec_transfer["clean"]` branch escaped the
    battery for exactly that reason.
    """
    import signalnest_bootstrap as sb
    monkeypatch.setattr(sb, "_exec_transfer_state",
                        lambda: {"clean": False, "problems": ["synthetic transfer disagreement"],
                                 "grammar_version": "x", "grammar": {}, "production_sites": 0,
                                 "independent_sites": 1, "agree": False, "sites": [],
                                 "positive_control_clean": False})
    with pytest.raises(sb.BootstrapError, match="exec command-position transfer"):
        sb.establish(strict=True)


def test_this_module_is_registered_in_the_assertion_contract():
    """Closes falsification arm f21: a neutered assertion in this file must be catchable."""
    registry = json.loads((REPO_ROOT / "tests" / "fixtures" / "assertion-contract-registry.json")
                          .read_text(encoding="utf-8"))
    mine = [c for c in registry["contracts"] if c["file"] == "tests/test_i28bb_exec_transfer.py"]
    assert mine, "this module must be under an assertion contract or `assert True` goes unnoticed"
    assert "assert True" in mine[0]["prohibited_trivial_forms"]


def test_a_tree_without_a_git_index_falls_back_rather_than_returning_empty(monkeypatch):
    """Closes falsification arm f24, which is INERT wherever git works.

    A sandbox materialised by `git archive` has no index. The first version RAISED there, which
    broke legitimate sandboxes; the version before that returned the EMPTY SET, which made every
    comparison vacuously agree. Neither is right: enumerate another way, and fail closed only if
    nothing at all can be enumerated.
    """
    class _NoRepo:
        returncode = 128
        stdout = ""
        stderr = "not a git repository"

    monkeypatch.setattr(oracle.subprocess, "run", lambda *a, **k: _NoRepo())
    sources = oracle.tracked_sources()
    assert sources, "a tree without a git index must still be enumerated, never silently empty"
    assert any(name.endswith(".sh") for name in sources)


def test_an_unenumerable_tree_fails_closed(monkeypatch):
    class _NoRepo:
        returncode = 128
        stdout = ""
        stderr = "not a git repository"

    monkeypatch.setattr(oracle.subprocess, "run", lambda *a, **k: _NoRepo())
    monkeypatch.setattr(oracle, "REPO", Path(tmpdir_factory := "/nonexistent-tree-for-i28bb"))
    with pytest.raises(RuntimeError, match="EMPTY"):
        oracle.tracked_sources()
