"""Gate 4N-I28AR — trust-cache authority, closing Gate 4N-I28AP finding ADV-I28AP-03.

THE DEFECT. `site_taxonomy._DERIVED` memoised the release-root, production-site and CI-release-site
derivations in a plain mutable dict. Poisoning it took release roots 41 -> 0 and production sites
492 -> 0 while executed-code provenance, executed-state provenance, startup policy, executable
trust, the executable inventory AND session-finish reverification all reported clean; the poisoned
root set was then consumed by executable reachability adjudication, so an
UNREACHABLE_FROM_GRADED_ROOTS precondition stopped refusing a genuinely reachable executable.

The sharpest shape never rebound `_DERIVED` at all — it mutated a list nested inside it — so the
`VOLATILE_CACHE:<type>` token that was supposed to cover the cache was satisfied by construction:
the poisoned dict is still a dict.

WHAT THESE CONTROLS PROTECT. Four independent properties, each closing a different escape:

  1. Cached values are FROZEN, so in-place mutation is impossible rather than merely detectable.
  2. Every write to the cache goes through ONE function, so coverage is checkable by reading.
  3. The authoritative answer is RECOMPUTED from staged source without consulting any cache, at
     bootstrap and again at session finish, and a disagreement fails closed naming the cache.
  4. Every mutable cache carries an explicit AUTHORITY CLASSIFICATION, and an unclassified cache is
     refused rather than excused.

Controls that poison a live cache restore it in a `finally`. A control that leaves the session's own
taxonomy poisoned would corrupt every test that runs after it — and this suite is one of the things
the cache decides about.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cache_authority as ca                                     # noqa: E402
import shell_command_model as scm                                # noqa: E402
import site_taxonomy as st                                       # noqa: E402

POLICY = REPO_ROOT / "tests" / "fixtures" / "cache-authority-policy.json"
PROTECTED_SET = REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json"
CONTRACT = REPO_ROOT / "tests" / "fixtures" / "executed-state-contract.json"


def _warm():
    """A session whose caches are populated, which is the state the attack presumes."""
    st.release_roots()
    st.production_control_function_sites()
    st.ci_release_control_sites()


@pytest.fixture
def warm_caches():
    """Populate the caches and guarantee restoration, however the control ends."""
    _warm()
    saved = (dict(st._DERIVED), dict(st._INDEX), dict(scm._cache))
    try:
        yield
    finally:
        for container, contents in zip((st._DERIVED, st._INDEX, scm._cache), saved):
            container.clear()
            container.update(contents)
        assert ca.verify()["clean"], "a control left the session's caches poisoned"


# --------------------------------------------------------------------- 1. immutability
def test_c01_every_cached_value_is_frozen(warm_caches):
    """The property that makes nested mutation impossible rather than merely detectable."""
    assert st._DERIVED, "the cache is empty, so this control would assert nothing"
    for key, value in st._DERIVED.items():
        assert ca.is_frozen(value), (
            f"_DERIVED[{key!r}] holds a mutable value of type {type(value).__name__}. "
            "ADV-I28AP-03's sharpest shape mutates a list nested inside the cache without ever "
            "rebinding it, which no type-binding token can see.")


@pytest.mark.parametrize("label,mutate", [
    ("rewrite a field of a cached root",
     lambda: st._DERIVED["resolved_roots"][0].__setitem__("module", "attacker.py")),
    ("append a root",
     lambda: st._DERIVED["resolved_roots"].append({})),
    ("truncate the root list in place",
     lambda: st._DERIVED["resolved_roots"].__delitem__(slice(1, None))),
    ("clear the production site list",
     lambda: st._DERIVED["production"].clear()),
    ("clear a list nested two levels down",
     lambda: st._DERIVED["resolved_roots"][0]["chains"].clear()),
    ("pop a key from a cached record",
     lambda: st._DERIVED["resolved_roots"][0].pop("module")),
])
def test_c02_the_adv_i28ap_03_mutation_shapes_are_impossible(label, mutate, warm_caches):
    """Not 'detected'. IMPOSSIBLE — a frozen structure has no mutating operation to reach for."""
    with pytest.raises((AttributeError, TypeError)):
        mutate()


def test_c03_the_exact_reported_escape_no_longer_reproduces(warm_caches):
    """The finding verbatim: roots 41 -> 0 with every layer clean. Now it cannot even be staged."""
    before = len(st.release_roots())
    assert before == 43, f"expected the reported 43 release roots, derived {before}"  # Gate 4N-I28BH-B: +1 (security_collection_assurance graded step)  # INFRA-9-B3: +1 (root_wiring)
    with pytest.raises((AttributeError, TypeError)):
        st._DERIVED["resolved_roots"].clear()
    assert len(st.release_roots()) == before, "the root set moved despite the mutation failing"


# --------------------------------------------------------------------- 2. one population point
def test_c04_every_write_to_the_cache_goes_through_store():
    """Derived from the AST, because the Gate 4N-I28AR reconnaissance ASSUMED this and was WRONG.

    That reconnaissance recorded `_cached` as the single population point. It was not: three keys
    were assigned directly, and one of them was `resolved_roots` — precisely the key the finding
    empties. A fix applied only at `_cached` would have left the most load-bearing value in the
    cache mutable, and every control above would still have passed.
    """
    tree = ast.parse((REPO_ROOT / "scripts" / "site_taxonomy.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            continue
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
            if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and target.value.id == "_DERIVED"):
                offenders.append(node.lineno)
    assert len(offenders) == 1, (
        f"_DERIVED is assigned at line(s) {offenders}; exactly one write site is expected, inside "
        "_store(). A second write site is a value entering the cache unfrozen.")


def test_c05_store_freezes_what_it_is_given():
    """The choke point must actually freeze, or being a choke point buys nothing."""
    frozen = ca.deep_freeze({"a": [1, {"b": ["c"]}]})
    assert ca.is_frozen(frozen)
    assert isinstance(frozen, Mapping) and frozen["a"][1]["b"] == ("c",)
    with pytest.raises((AttributeError, TypeError)):
        frozen["a"][1]["b"].append("d")


def test_c06_freezing_preserves_the_consumer_api():
    """A control that breaks its consumers gets reverted, so this is load-bearing too.

    Cached derivations are read as `record["module"]` and `site["canonical_site_id"]` throughout the
    taxonomy. Canonical comparison forms (tuples of pairs) would have broken every one of those
    readers; a mapping proxy keeps the access and blocks the assignment.
    """
    roots = st.release_roots()
    assert roots and isinstance(roots[0], Mapping)
    assert roots[0]["module"] and isinstance(roots[0]["release_role"], Mapping)
    sites = st.production_control_function_sites()
    assert sites and sites[0]["canonical_site_id"]


# --------------------------------------------------------------------- 3. fresh recomputation
def test_c07_the_fresh_derivation_does_not_consult_the_cache(warm_caches):
    """If it did, the comparison in verify() would be a cache verified against itself."""
    poisoned = ca.deep_freeze([])
    st._DERIVED["resolved_roots"] = poisoned
    fresh = ca.fresh_taxonomy()
    assert fresh["release_root_count"] == 43, (  # Gate 4N-I28BH-B: +1 root (security_collection_assurance)  # INFRA-9-B3: +1 (root_wiring)
        "fresh_taxonomy() returned the poisoned answer, so it consulted the cache it exists to "
        "check. This is the tautology the layer is built to avoid.")


@pytest.mark.parametrize("label,poison", [
    ("every key emptied", lambda: [st._DERIVED.__setitem__(k, ca.deep_freeze([]))
                                   for k in list(st._DERIVED)]),
    ("resolved_roots emptied", lambda: st._DERIVED.__setitem__("resolved_roots", ca.deep_freeze([]))),
    ("production emptied", lambda: st._DERIVED.__setitem__("production", ca.deep_freeze([]))),
    ("ci_release emptied", lambda: st._DERIVED.__setitem__("ci_release", ca.deep_freeze([]))),
    ("roots truncated to one", lambda: st._DERIVED.__setitem__(
        "resolved_roots", ca.deep_freeze(list(st._DERIVED["resolved_roots"])[:1]))),
    ("a single root dropped", lambda: st._DERIVED.__setitem__(
        "resolved_roots", ca.deep_freeze(list(st._DERIVED["resolved_roots"])[:-1]))),
    ("an extra fabricated root added", lambda: st._DERIVED.__setitem__(
        "resolved_roots", ca.deep_freeze(list(st._DERIVED["resolved_roots"])
                                         + [dict(st._DERIVED["resolved_roots"][0])]))),
])
def test_c08_a_poisoned_derived_cache_is_refused_and_named(label, poison, warm_caches):
    """Replacement survives freezing — so replacement is what recomputation must catch."""
    poison()
    result = ca.verify()
    assert not result["clean"], f"a poisoned cache ({label}) was not refused"
    assert any("_DERIVED" in p for p in result["problems"]), (
        f"the refusal does not name the cache: {result['problems'][:2]}")


def test_c09_a_poisoned_index_is_refused_in_the_ordering_that_can_reach_a_decision(warm_caches):
    """`_INDEX` is second-order: it decides nothing unless `_DERIVED` must rebuild.

    Measured, not assumed. With `_DERIVED` warm, poisoning `_INDEX` changes no answer, because the
    memoised values short-circuit it — so the honest classification is derivative rather than
    authoritative. The ordering where it CAN reach a decision is a rebuild, and there it is caught.
    """
    st._DERIVED.clear()
    for key in list(st._INDEX):
        st._INDEX[key] = {"functions": {}, "imports": {}, "aliases": {}, "entry_points": []}
    result = ca.verify()
    assert not result["clean"], "a poisoned module index survived a cache rebuild"
    assert any("does NOT match a fresh derivation" in p for p in result["problems"]), \
        result["problems"][:2]


def test_c10_a_poisoned_shell_model_cache_cannot_suppress_a_root(warm_caches):
    """A well-formed poison — a real record belonging to a DIFFERENT script.

    Deliberately well-formed. A malformed poison (None values) raises inside the derivation, and an
    unrelated exception is not evidence that the intended refusal works. This one produces a
    genuinely smaller universe and must be caught on CONTENT.
    """
    assert len(scm._cache) >= 2, "too few shell-model entries for this control to mean anything"
    st._DERIVED.clear()
    keys = sorted(scm._cache)
    scm._cache[keys[0]] = scm._cache[keys[-1]]
    result = ca.verify()
    assert not result["clean"], "a poisoned shell-model cache suppressed sites undetected"
    assert any("does NOT match a fresh derivation" in p for p in result["problems"]), \
        result["problems"][:2]


def test_c11_a_cache_that_breaks_the_derivation_is_refused_not_crashed(warm_caches):
    """A crash is not a verdict. The refusal must describe the cause a reader needs."""
    st._DERIVED.clear()
    for key in list(scm._cache):
        scm._cache[key] = None
    result = ca.verify()
    assert not result["clean"]
    assert any("raised" in p and "crash is not a verdict" in p or "cannot serve the answer" in p
               for p in result["problems"]), result["problems"][:2]


def test_c12_an_empty_universe_is_refused_even_when_both_sides_agree(warm_caches):
    """Agreement is not correctness: a derivation that failed everywhere agrees with itself."""
    result = ca.verify()
    assert result["records"]["fresh"]["release_root_count"] > 0
    assert result["records"]["fresh"]["production_site_count"] > 0
    source = (REPO_ROOT / "scripts" / "cache_authority.py").read_text(encoding="utf-8")
    assert "produced ZERO" in source, (
        "verify() no longer refuses an empty authoritative universe; an empty derivation is "
        "indistinguishable from one that failed")


# --------------------------------------------------------------------- 4. identity pinning
def test_c13_replacing_the_cache_object_is_detected():
    probe = {"k": ca.deep_freeze([1])}
    ca.pin("probe-c13", probe)
    with pytest.raises(ca.CacheAuthorityError, match="OBJECT has been replaced"):
        ca.assert_pinned("probe-c13", dict(probe))


def test_c14_replacing_a_cached_value_is_detected():
    probe = {"k": ca.deep_freeze([1])}
    ca.pin("probe-c14", probe)
    probe["k"] = ca.deep_freeze([1])                # equal content, different object
    with pytest.raises(ca.CacheAuthorityError, match="REPLACED since verification"):
        ca.assert_pinned("probe-c14", probe)


def test_c15_removing_a_cached_value_is_detected():
    probe = {"k": ca.deep_freeze([1])}
    ca.pin("probe-c15", probe)
    del probe["k"]
    with pytest.raises(ca.CacheAuthorityError, match="has been REMOVED"):
        ca.assert_pinned("probe-c15", probe)


def test_c16_an_untouched_cache_passes_its_pin():
    """The negative control. A check that fires on everything distinguishes nothing."""
    probe = {"k": ca.deep_freeze([1])}
    ca.pin("probe-c16", probe)
    ca.assert_pinned("probe-c16", probe)


# --------------------------------------------------------------------- 5. classification
def test_c17_every_discovered_cache_is_classified():
    result = ca.verify()
    discovered = set(result["records"]["discovered"])
    declared = set(json.loads(POLICY.read_text(encoding="utf-8"))["caches"])
    assert discovered <= declared, (
        f"unclassified mutable cache(s): {sorted(discovered - declared)}. A cache nothing "
        "classifies is a cache nothing stops from deciding.")
    assert discovered, "discovery found no caches at all, so this control asserted nothing"


def test_c18_an_unclassified_cache_fails_closed(monkeypatch):
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["caches"].pop("site_taxonomy._DERIVED")
    result = ca.verify(policy=doc)
    assert not result["clean"]
    assert any("NO authority classification" in p for p in result["problems"]), \
        result["problems"][:3]


@pytest.mark.parametrize("mutation,expected", [
    ({"classification": "TRUSTED"}, "is not one of"),
    ({"classification": None}, "is not one of"),
    ({"why": ""}, "must state why it holds"),
])
def test_c19_a_malformed_classification_is_refused(mutation, expected, tmp_path):
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["caches"]["site_taxonomy._DERIVED"].update(mutation)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ca.CacheAuthorityError, match=expected):
        ca.load_policy(path)


def test_c20_an_empty_policy_is_refused(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"caches": {}}))
    with pytest.raises(ca.CacheAuthorityError, match="classifies no cache"):
        ca.load_policy(path)


def test_c21_a_missing_policy_is_refused(tmp_path):
    with pytest.raises(ca.CacheAuthorityError, match="policy is missing"):
        ca.load_policy(tmp_path / "absent.json")


def test_c22_a_classification_whose_subject_no_longer_exists_is_refused():
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["caches"]["site_taxonomy._VANISHED"] = dict(
        doc["caches"]["site_taxonomy._DERIVED"], name="_VANISHED")
    result = ca.verify(policy=doc)
    assert not result["clean"]
    assert any("not present in the running modules" in p for p in result["problems"]), \
        result["problems"][:3]


# --------------------------------------------------------------------- 6. VOLATILE_CACHE semantics
def test_c23_a_volatile_cache_exclusion_must_be_earned_by_a_classification(monkeypatch):
    """Gate 4N-I28AR redefines the token. 'Mutable' is not by itself a reason to trust something."""
    import executed_state_provenance as esp

    real = ca.load_policy

    def stripped(path=None):
        doc = json.loads(json.dumps(real()))
        doc["caches"].pop("site_taxonomy._DERIVED")
        return doc

    monkeypatch.setattr(ca, "load_policy", stripped)
    identity, problems = esp.state_identity(st, ["release_roots"])
    assert identity["_DERIVED"] == "VOLATILE_CACHE:UNCLASSIFIED"
    assert any("does not classify" in p for p in problems), problems[:3]


def test_c24_the_volatile_token_binds_the_classification_not_the_type():
    """Binding the TYPE is what let ADV-I28AP-03 through: a poisoned dict is still a dict."""
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    tokens = {name: token for name, token in contract["modules"]["site_taxonomy"]["names"].items()
              if str(token).startswith("VOLATILE_CACHE")}
    assert tokens, "no VOLATILE_CACHE token is pinned, so this control asserts nothing"
    for name, token in tokens.items():
        assert token.split(":", 1)[1] in ca.CLASSIFICATIONS, (
            f"{name} is pinned as {token}, which binds a TYPE rather than an authority "
            "classification. That is the exact token ADV-I28AP-03 satisfied by construction.")


def test_c25_every_volatile_cache_declaration_is_classified():
    """Both directions. A declared exclusion with no classification is the hole itself."""
    import executed_state_provenance as esp

    declared = {f"{module}.{name}" for module, names in esp.VOLATILE_CACHES.items()
                for name in names}
    classified = set(json.loads(POLICY.read_text(encoding="utf-8"))["caches"])
    assert declared <= classified, sorted(declared - classified)


# --------------------------------------------------------------------- 7. wiring
def test_c26_cache_authority_is_a_protected_module_with_critical_callables():
    entries = {e["module"]: e for e in
               json.loads(PROTECTED_SET.read_text(encoding="utf-8"))["protected_modules"]}
    entry = entries.get("cache_authority")
    assert entry is not None, "the cache-authority layer is not protected, so it can be replaced"
    for callable_name in ("verify", "fresh_taxonomy", "deep_freeze", "load_policy"):
        assert callable_name in entry["critical_callables"], callable_name
    assert entry["relative_path"] == "scripts/cache_authority.py"
    assert entry["proving_substitution"].strip()


def test_c27_store_and_cached_are_critical_callables_of_site_taxonomy():
    """The choke point is as load-bearing as the derivations it feeds."""
    entries = {e["module"]: e for e in
               json.loads(PROTECTED_SET.read_text(encoding="utf-8"))["protected_modules"]}
    criticals = entries["site_taxonomy"]["critical_callables"]
    assert "_store" in criticals and "_cached" in criticals, criticals


def test_c28_the_bootstrap_runs_cache_authority_as_a_layer():
    source = (REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text(encoding="utf-8")
    assert "cache authority" in source, "the layer is not in the refusal loop"
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in ("establish", "reverify"):
        body = ast.dump(functions[name])
        assert "cache_authority" in body, (
            f"{name}() does not consult the cache-authority layer. Verifying at configure and "
            "never again leaves exactly the window session-finish reverification exists to close.")


def test_c29_the_attestation_carries_the_layers_result():
    """Evidence that it EXECUTED, not that it exists — the Gate 4N-I28AE lesson."""
    import signalnest_bootstrap as sb

    attestation = sb.establish(strict=False)
    record = attestation.get("cache_authority")
    assert record is not None, "establish() produced no cache-authority record"
    assert record["clean"] is True, record["problems"][:3]
    assert record["fresh"]["release_root_count"] == 43  # Gate 4N-I28BH-B: +1 root (security_collection_assurance)  # INFRA-9-B3: +1 (root_wiring)
    assert record["policy_sha256"]


def test_c30_the_session_that_is_running_now_had_the_layer_active():
    """This session, not a synthetic one. The layer must be in force where it matters."""
    import signalnest_bootstrap as sb

    outcome = sb.reverify()
    assert outcome["layers"].get("cache_authority") is True, outcome["problems"][:3]


# --------------------------------------------------------------------- 8. anti-tautology
def test_c31_a_verifier_that_compares_the_cache_with_itself_is_visibly_different(warm_caches):
    """The proving substitution from the protected-module set, executed rather than asserted.

    Replacing fresh_taxonomy() with cached_taxonomy() makes the comparison succeed by construction.

    The poison here drops exactly ONE root rather than emptying the set, and that detail is the
    whole control. My first version emptied it — and the substituted verifier STILL refused,
    because the independent empty-universe check fired. That would have been a probe measuring a
    different control than the one it named. A single dropped root is invisible to every check
    except the comparison, so it isolates the tautology exactly.
    """
    real_fresh = ca.fresh_taxonomy
    try:
        st._DERIVED["resolved_roots"] = ca.deep_freeze(list(st._DERIVED["resolved_roots"])[:-1])
        honest = ca.verify()
        assert not honest["clean"], "the honest verifier failed to refuse a dropped root"
        assert any("does NOT match a fresh derivation" in p for p in honest["problems"])

        ca.fresh_taxonomy = ca.cached_taxonomy
        substituted = ca.verify()
        assert substituted["clean"], (
            "substituting the fresh derivation did NOT make the poisoned cache pass, so this "
            "control is not measuring the tautology it claims to measure")
    finally:
        ca.fresh_taxonomy = real_fresh


def test_c31b_the_empty_universe_control_is_independent_of_the_comparison(warm_caches):
    """Defence in depth, measured: emptying the cache is refused even by a tautological verifier.

    Discovered while repairing c31. It is worth its own control because it establishes that the two
    checks fail independently — the comparison catches a subtle poison, the universe check catches
    a total one, and neither relies on the other being intact.
    """
    real_fresh = ca.fresh_taxonomy
    try:
        st._DERIVED["resolved_roots"] = ca.deep_freeze([])
        ca.fresh_taxonomy = ca.cached_taxonomy
        result = ca.verify()
        assert not result["clean"], "an emptied root set passed a tautological verifier"
        assert any("produced ZERO" in p for p in result["problems"]), result["problems"][:3]
    finally:
        ca.fresh_taxonomy = real_fresh


def test_c32_freezing_is_what_blocks_mutation_not_a_coincidence():
    """deep_freeze() replaced by identity restores the finding; prove the dependency is real."""
    unfrozen = {"a": [1, 2]}
    assert not ca.is_frozen(unfrozen)
    unfrozen["a"].clear()                            # succeeds precisely because it is not frozen
    assert unfrozen["a"] == []
    assert ca.is_frozen(ca.deep_freeze({"a": [1, 2]}))


def test_c33_the_policy_and_the_implementation_agree_about_fresh_recomputation():
    """A claim in a policy that no code enforces is the shape of defect this gate closes.

    The policy asserts each cache is cleared and re-derived by fresh_taxonomy(). That is enforced
    by driving suspension FROM the policy, so a cache classified tomorrow is suspended without
    anyone remembering to add it. This control checks the claim against behaviour.
    """
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    for name, entry in doc["caches"].items():
        assert entry.get("fresh_recomputation"), f"{name}: no fresh-recomputation claim"
    observed = {}
    with ca._suspend_caches():
        for name, entry in doc["caches"].items():
            module = sys.modules.get(entry["module"])
            observed[name] = len(getattr(module, entry["name"], {}))
    assert all(size == 0 for size in observed.values()), (
        f"the policy claims every cache is cleared for a fresh derivation, but {observed} were "
        "still populated. A claim no code enforces is not a control.")


def test_c34_suspension_restores_every_cache_afterwards():
    """A verification pass that leaves the session's caches empty would change what follows."""
    _warm()
    before = {name: len(container) for name, container in
              (("_DERIVED", st._DERIVED), ("_INDEX", st._INDEX), ("shell", scm._cache))}
    ca.verify()
    after = {name: len(container) for name, container in
             (("_DERIVED", st._DERIVED), ("_INDEX", st._INDEX), ("shell", scm._cache))}
    assert before == after, f"verify() disturbed the caches: {before} -> {after}"


# --------------------------------------------------------------------- 9. reconciliation
def test_c35_a_fresh_interpreter_agrees_with_this_sessions_derivation():
    """The strongest independent check available: a process this one cannot have poisoned."""
    code = ("import sys, json; sys.path.insert(0, 'scripts'); import site_taxonomy as st; "
            "print(json.dumps({'roots': len(st.release_roots()), 'production': "
            "len({s['canonical_site_id'] for s in st.production_control_function_sites()})}))")
    # Inherit the ambient environment and override only what this probe needs. A hand-built
    # minimal env looked tidier and was wrong: it dropped PYTHONUSERBASE, so under an empty HOME
    # the child could not resolve third-party imports and the control failed for a reason that had
    # nothing to do with the derivation it measures.
    import os

    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                         text=True, timeout=900,
                         env=dict(os.environ,
                                  SIGNALNEST_ANCHOR_TIER="TIER_1_SYNTHETIC",
                                  PYTHONPATH=str(REPO_ROOT / "scripts")))
    assert out.returncode == 0, out.stderr[-2000:]
    fresh = json.loads(out.stdout)
    assert fresh["roots"] == len(st.release_roots()) == 43  # INFRA-9-B3: +1 (root_wiring)
    # GATE 4N-I28BH-B0a-SLICE2: 494 -> 746 (+252). The signed completeness verifier landed as
    # scripts/completeness_framework.py, reached from the graded release root
    # scripts/collection_completeness.py; its functions are production/control sites by the same rule
    # as every other release-reachable function. Corroborated by mutation_discovery's independent
    # count (test_i27r) and by site_taxonomy resolving the call graph completely (unresolved_calls==0).
    # GATE 4N-I28BH-B (this gate): 41 -> 42 roots and 746 -> 782 production sites (+1 root, +36 fns; the +1 vs prior is _runtime_scan_decisions_schema).
    # The +1 root is the new mandatory ci.yml step `python3 scripts/security_collection_assurance.py`
    # (the sibling property-specific assurance validator, outcome read by the aggregator so it blocks
    # release). The +32 functions are that module's 21 functions + review_pin_control.py's 10 (the
    # reviewed-integrity control it delegates to) + critical_list_inventory.py::assurance_registry;
    # security_collection_assurance's 23 include the _root_of_trust + _canonical_file_digest pair.
    # Established by IDENTITY against START_TREE 45eb4d72: exactly these 33 sites (1 graded_step + 34
    # functions) were ADDED and ZERO were removed; site_taxonomy resolves them (unresolved_calls==0).
    assert fresh["production"] == len(
        {s["canonical_site_id"] for s in st.production_control_function_sites()}) == 821  # BH-C-E1: +1 (completeness_applicable)  # BH-C: +1 (_collection_value; roots unchanged at 42)  # E2: +9 site_coverage/ci_env_dataflow/ci_harness helpers (roots unchanged at 42)  # INFRA-9-B3: +28 (root_wiring_check.py function sites; new graded root)


# --------------------------------------------------------------------- 10. measured obligations
def test_c36_a_cache_may_not_be_classified_stronger_than_it_behaves():
    """Falsification arm f17, which ESCAPED the first sound run of the battery.

    Reclassifying the mutable memo dict as an immutable validated snapshot was accepted, because
    the classification was authored text that no measurement contradicted. It is now checked
    against the live object, so the label cannot claim more than the cache delivers.
    """
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["caches"]["site_taxonomy._DERIVED"]["classification"] = "IMMUTABLE_VALIDATED_SNAPSHOT"
    result = ca.verify(policy=doc)
    assert not result["clean"], "a mutable dict was accepted as an immutable validated snapshot"
    assert any("may not claim more than the object delivers" in p for p in result["problems"]), \
        result["problems"][:3]


def test_c37_a_prohibited_cache_must_actually_be_empty():
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["caches"]["site_taxonomy._DERIVED"]["classification"] = "PROHIBITED_FOR_TRUST_DECISIONS"
    _warm()
    result = ca.verify(policy=doc)
    assert not result["clean"]
    assert any("must be empty in a graded session" in p for p in result["problems"]), \
        result["problems"][:3]


def test_c38_an_authoritative_cache_must_declare_what_binds_it():
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["caches"]["site_taxonomy._DERIVED"]["classification"] = "AUTHORITATIVE_CONTENT_BOUND_CACHE"
    result = ca.verify(policy=doc)
    assert not result["clean"]
    assert any("without declaring a content_binding" in p for p in result["problems"]), \
        result["problems"][:3]


def test_c39_every_classification_carries_a_measurable_obligation():
    """An unenforceable category would move the f17 hole rather than close it."""
    source = (REPO_ROOT / "scripts" / "cache_authority.py").read_text(encoding="utf-8")
    body = source[source.index("def classification_obligations"):source.index("# ----", source.index("def classification_obligations"))]
    for classification in sorted(ca.CLASSIFICATIONS):
        assert classification in body, (
            f"{classification} has no obligation branch, so a cache carrying it is checked by "
            "nothing. That is the f17 escape with a different label.")
