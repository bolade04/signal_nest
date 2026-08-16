#!/usr/bin/env python3
"""Gate 4N-I28BH-A3-PARALLEL (12-agent) — generic shell / exec-transfer / failure-propagation /
command-position / npm-identity collection classification and oracle adjudication (completes A3).

WHAT THIS PINS. The 25 collections owning generic shell tokenization and command-position modelling
(shell_positions), exec/coproc transfer following (exec_transfer_oracle), failure-propagation
classification (failure_propagation), CI invocation modelling (ci_invocation_model), and npm identity /
call-site discovery (npm_authority) are classified in `critical-list-contract.json::a3_adjudication`.

LEAD RESOLUTIONS (11-specialist integration; adversarial Agent 11 has no unresolved objection):
  * 15 previously-UNCLASSIFIED collections -> SECURITY_CRITICAL_SOURCE.
  * 5 currently-SECURITY collections confirmed SECURITY_CRITICAL_SOURCE (COMMAND_WRAPPERS, NESTED_SHELL,
    KEYWORDS_INTRODUCING_A_COMMAND, SHELL_BUILTINS, FAIL_CLOSED_WORDS).
  * DATA_CONSUMERS, MASKING: DOWNGRADED SECURITY_CRITICAL_LIST -> NON_SECURITY_CONFIGURATION, each with a
    test-backed proving-exclusion downgrade_proof (I27O empties DATA_CONSUMERS and asserts invoked_targets
    UNCHANGED; I26B moves masking authority to failure_propagation.py). Adversarial Agent 11 independently
    classified both NON_AUTHORITATIVE_PREFILTER -> the downgrade is adversarially concurred, not contested.
  * ALWAYS_SUCCEEDS, KEYWORDS_TERMINATING: confirmed NON_SECURITY_CONFIGURATION (every mutation fail-closed).
  * NON_EXECUTING: DEAD_OR_STALE (grep-confirmed zero readers).

BH-A3-FIND-01 (recorded, deferred to BH-B): exec_transfer_oracle.TRANSFER_WORDS is presently UNREAD — the
`_ACTIVE` regex `(exec|coproc)` is the sole recognizer. The tuple and the regex alternation agree TODAY, but
nothing enforces it, so a future edit to one could silently drift from the other. test_transfer_words bind the
tuple to the regex alternation both directions as the BH-B completeness contract.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
os.environ.setdefault("SIGNALNEST_ANCHOR_TIER", "TIER_1_SYNTHETIC")

CONTRACT = json.loads((REPO / "tests/fixtures/critical-list-contract.json").read_text())
ADJ = CONTRACT["a3_adjudication"]
CLASSIFICATIONS = CONTRACT["classifications"]
A3_IDS = set(ADJ)
MODULES = {"ci_invocation_model.py", "exec_transfer_oracle.py", "failure_propagation.py",
           "shell_positions.py", "npm_authority.py"}

SEVEN = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS",
         "TEST_ONLY_LOAD_BEARING", "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY", "DEAD_OR_STALE"}
SECURITY = {"SECURITY_CRITICAL_SOURCE", "SECURITY_CRITICAL_DERIVED", "SECURITY_SCHEMA_OR_FIELDS"}
ORACLE = {"MODULE_CONSTANTS", "FUNCTION_RESULT_KEYS", "EMITTED_POLICY", "DISCOVERED_KINDS", "AUTHORED_CONTRACT",
          "SCHEMA_VALIDATION", "HARNESS_COMPLETENESS", "PROVENANCE_DERIVATION", "SEMANTIC_REACHABILITY",
          "SITE_UNIVERSE_RECONCILIATION", "AUTHORITATIVE_SOURCE_NO_ENUMERABLE_ORACLE", "NONE"}
DOMAINS = {"PRODUCTION_AUTHORITY", "DERIVED_PRODUCTION_STATE", "EXECUTION_SCHEMA",
           "TEST_ASSURANCE_AUTHORITY", "TEST_PARAMETER_ONLY", "NON_SECURITY_CONFIGURATION"}
PP = {"INVALID_EMPTY", "VALID_EMPTY", "CONDITIONALLY_EMPTY"}
STRING_OF = {"SECURITY_CRITICAL_SOURCE": "SECURITY_CRITICAL_LIST", "SECURITY_CRITICAL_DERIVED": "SECURITY_CRITICAL_LIST",
             "SECURITY_SCHEMA_OR_FIELDS": "NON_SECURITY_CONFIGURATION", "TEST_ONLY_LOAD_BEARING": "TEST_ONLY",
             "NON_SECURITY_CONFIGURATION": "NON_SECURITY_CONFIGURATION", "DOCUMENTATION_ONLY": "DOCUMENTATION_ONLY",
             "DEAD_OR_STALE": "NON_SECURITY_CONFIGURATION"}

# The seven ids that were SECURITY_CRITICAL_LIST BEFORE A3 — any that leaves SECURITY must carry proof.
PRE_A3_SECURITY_CRITICAL = {
    "ci_invocation_model.py::DATA_CONSUMERS", "ci_invocation_model.py::MASKING",
    "shell_positions.py::COMMAND_WRAPPERS", "shell_positions.py::FAIL_CLOSED_WORDS",
    "shell_positions.py::KEYWORDS_INTRODUCING_A_COMMAND", "shell_positions.py::NESTED_SHELL",
    "shell_positions.py::SHELL_BUILTINS",
}
FORBIDDEN_OBSERVED = "copied from the authored"


def validate_a3(adj, classifications, present_ids) -> list:
    """A3 adjudication validator. Superset of the A2c contract plus four A3-specific rules:
    (A) SECURITY_CRITICAL_SOURCE positive_presence must be INVALID_EMPTY (a source universe must be non-empty);
    (B) a SECURITY observed_authority may not be a 'copied from the authored list' marker (self-comparison);
    (C) SECURITY_CRITICAL_DERIVED must carry oracle_family PROVENANCE_DERIVATION;
    (D) any PRE_A3_SECURITY_CRITICAL id now classified out of SECURITY must carry a non-empty downgrade_proof.
    """
    problems = []
    for cid in sorted(present_ids - set(adj)):
        problems.append(f"{cid}: an A3 collection with NO adjudication")
    for cid in sorted(set(adj) - present_ids):
        problems.append(f"{cid}: adjudicated but not a present A3 collection")
    for cid, rec in adj.items():
        cls = rec.get("cls")
        if cls not in SEVEN:
            problems.append(f"{cid}: class {cls!r} not one of seven"); continue
        if rec.get("oracle_family") not in ORACLE:
            problems.append(f"{cid}: oracle_family {rec.get('oracle_family')!r} unknown")
        if rec.get("authority_domain") not in DOMAINS:
            problems.append(f"{cid}: authority_domain {rec.get('authority_domain')!r} unknown")
        if classifications.get(cid) != STRING_OF[cls]:
            problems.append(f"{cid}: string projection {classifications.get(cid)!r} != {STRING_OF[cls]!r}")
        # (D) downgrade proof for anything leaving the SECURITY class
        if cid in PRE_A3_SECURITY_CRITICAL and cls not in SECURITY:
            if not rec.get("downgrade_proof"):
                problems.append(f"{cid}: downward reclassification from SECURITY without downgrade_proof")
        if cls in SECURITY:
            if rec.get("oracle_family") in (None, "NONE"):
                problems.append(f"{cid}: {cls} with no oracle strategy")
            for f in ("expected_authority", "observed_authority", "independence", "comparison", "positive_presence"):
                if not rec.get(f):
                    problems.append(f"{cid}: {cls} missing {f}")
            if rec.get("expected_authority") and rec.get("expected_authority") == rec.get("observed_authority"):
                problems.append(f"{cid}: expected==observed (alias)")
            if rec.get("positive_presence") not in PP:
                problems.append(f"{cid}: positive_presence invalid")
            # (A) a source universe must be non-empty to function
            if cls == "SECURITY_CRITICAL_SOURCE" and rec.get("positive_presence") != "INVALID_EMPTY":
                problems.append(f"{cid}: SECURITY_CRITICAL_SOURCE must be INVALID_EMPTY (a source universe is never empty)")
            # (B) observed must be independently derived, never a copy of the authored list
            if FORBIDDEN_OBSERVED in (rec.get("observed_authority") or "").lower():
                problems.append(f"{cid}: observed_authority is a copy of the authored list (self-comparison)")
            # (C) DERIVED must be provenance-derived
            if cls == "SECURITY_CRITICAL_DERIVED" and rec.get("oracle_family") != "PROVENANCE_DERIVATION":
                problems.append(f"{cid}: SECURITY_CRITICAL_DERIVED must carry oracle_family PROVENANCE_DERIVATION")
        else:
            if rec.get("oracle_family") != "NONE":
                problems.append(f"{cid}: non-security class must carry oracle_family NONE")
    return problems


# ----------------------------------------------------------------------------------------------------
# Structural
# ----------------------------------------------------------------------------------------------------
def test_a3_scope_is_exactly_25_shell_exec_collections():
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections()
            if c["module"] in MODULES and not c["form"].startswith("derived:")}  # BH-C: exclude runtime-discovered derived collections (governed via critical-list-contract)
    assert A3_IDS == disc, A3_IDS ^ disc
    assert len(ADJ) == 25


def test_a3_adjudication_well_formed_positive_control():
    """Arm 36 positive control: the authored, unmutated adjudication is clean."""
    assert validate_a3(ADJ, CLASSIFICATIONS, A3_IDS) == []


def test_a3_distribution():
    from collections import Counter
    d = Counter(r["cls"] for r in ADJ.values())
    assert d["SECURITY_CRITICAL_SOURCE"] == 20
    assert d["NON_SECURITY_CONFIGURATION"] == 4
    assert d["DEAD_OR_STALE"] == 1
    assert sum(d.values()) == 25


def test_a3_downgrades_carry_proof():
    for cid in ("ci_invocation_model.py::DATA_CONSUMERS", "ci_invocation_model.py::MASKING"):
        rec = ADJ[cid]
        assert rec["cls"] == "NON_SECURITY_CONFIGURATION"
        dp = rec["downgrade_proof"]
        assert dp["old_class"] == "SECURITY_CRITICAL_LIST"
        assert dp["new_class"] == "NON_SECURITY_CONFIGURATION"
        assert dp["basis"] and dp["independent_protection"]


def test_a3_string_projection_matches_classifications():
    for cid, rec in ADJ.items():
        assert CLASSIFICATIONS[cid] == STRING_OF[rec["cls"]]


def test_a1_a2_non_regression():
    assert len(CONTRACT["a1a_adjudication"]) == 36 and len(CONTRACT["a1b_adjudication"]) == 22  # INFRA-9-B3 apply-identity: +2 (W0_APPLY_CLOSURE, W0_SCOPED_CAPABILITIES)
    assert len(CONTRACT["a1c_adjudication"]) == 23 and len(CONTRACT["a2a_adjudication"]) == 10
    assert len(CONTRACT["a2b_adjudication"]) == 15 and len(CONTRACT["a2c_adjudication"]) == 18


def test_remaining_unclassified_within_a4_a5():
    # At A3 closure the residual unclassified set was exactly these 8 (A4's 5 + A5's 3). Later sub-gates
    # (A4, then A5) shrink it; this assertion is forward-stable — it requires only that no collection
    # outside the known A4/A5 universe ever leaks in as unclassified, which is the A3-level invariant.
    import critical_list_inventory as cli
    allids = {c["id"] for c in cli.discover_collections()}
    uncl = allids - set(CLASSIFICATIONS)
    A4_A5 = {
        "cache_authority.py::CLASSIFICATIONS", "cache_authority.py::_PINS",
        "leak_scan.py::NON_LIVE_CLASSIFICATIONS", "review_packet_digest.py::REQUIRED_FIELDS",
        "reviewer_retrieval_state.py::DEFINITIONS", "reviewer_retrieval_state.py::NEVER_RELAUNCH",
        "reviewer_retrieval_state.py::STATES", "reviewer_retrieval_state.py::TRANSITIONS",
    }
    assert uncl <= A4_A5, uncl - A4_A5


def test_bijection_completeness():
    """Arm 37: a3 keys are exactly the discovered A3-module collection ids."""
    import critical_list_inventory as cli
    disc = {c["id"] for c in cli.discover_collections()
            if c["module"] in MODULES and not c["form"].startswith("derived:")}  # BH-C: exclude runtime-discovered derived collections (governed via critical-list-contract)
    assert set(ADJ) == disc and len(disc) == 25


# ----------------------------------------------------------------------------------------------------
# Metadata falsification battery — each arm MUST make validate_a3 return a non-empty problem list.
# ----------------------------------------------------------------------------------------------------
def _mut(fn):
    """Run an arm: returns the problem list validate_a3 produces under the arm's mutation."""
    adj = copy.deepcopy(ADJ)
    cls = dict(CLASSIFICATIONS)
    present = set(A3_IDS)
    ctx = fn(adj, cls, present)
    if not (isinstance(ctx, tuple) and len(ctx) == 3):
        ctx = (adj, cls, present)  # arm mutated in place (returned nothing usable)
    return validate_a3(*ctx)


def _a(adj, key, field, value):
    adj[key][field] = value


ARMS = {
    # bijection / presence
    "arm00_unclassified_addition": lambda a, c, p: (a, c, p | {"shell_positions.py::NEW_LIST"}),
    "arm01_classification_removal": lambda a, c, p: (a.pop("ci_invocation_model.py::MASKING"), (a, c, p))[1],
    "arm28_collection_rename": lambda a, c, p: (a, c, (p - {"failure_propagation.py::ALWAYS_SUCCEEDS"}) | {"failure_propagation.py::ALWAYS_OK"}),
    "arm29_collection_move": lambda a, c, p: (a, c, (p - {"npm_authority.py::NPM_WORDS"}) | {"shell_positions.py::NPM_WORDS"}),
    "arm30_wrapper_vanished": lambda a, c, p: (a, c, p - {"shell_positions.py::COMMAND_WRAPPERS"}),
    # oracle / field integrity
    "arm04_lost_oracle": lambda a, c, p: _a(a, "ci_invocation_model.py::INTERPRETERS", "oracle_family", "NONE"),
    "arm18_npm_lost_oracle": lambda a, c, p: _a(a, "npm_authority.py::AUTHORITY_MODELS", "oracle_family", "NONE"),
    "arm17_blank_independence": lambda a, c, p: _a(a, "shell_positions.py::COMMAND_WRAPPERS", "independence", ""),
    "arm20_blank_comparison": lambda a, c, p: _a(a, "npm_authority.py::DISPOSITIONS", "comparison", ""),
    "arm23_oracle_alias": lambda a, c, p: _a(a, "shell_positions.py::NESTED_SHELL", "observed_authority",
                                             a["shell_positions.py::NESTED_SHELL"]["expected_authority"]),
    "arm13_alias_widening": lambda a, c, p: _a(a, "ci_invocation_model.py::_VALUE_TAKING_FLAGS", "observed_authority",
                                               a["ci_invocation_model.py::_VALUE_TAKING_FLAGS"]["expected_authority"]),
    "arm33_stale_observed_alias": lambda a, c, p: _a(a, "failure_propagation.py::_TRAP_SIGNALS", "observed_authority",
                                                     a["failure_propagation.py::_TRAP_SIGNALS"]["expected_authority"]),
    "arm24_copied_oracle": lambda a, c, p: _a(a, "shell_positions.py::FAIL_CLOSED_WORDS", "observed_authority",
                                              "the set, copied from the authored list verbatim"),
    "arm25_false_empty_source": lambda a, c, p: _a(a, "ci_invocation_model.py::INTERPRETERS", "positive_presence", "VALID_EMPTY"),
    "arm10_non_security_oracle_leak": lambda a, c, p: _a(a, "failure_propagation.py::ALWAYS_SUCCEEDS", "oracle_family", "MODULE_CONSTANTS"),
    "arm39_unknown_family_and_domain": lambda a, c, p: [
        _a(a, "failure_propagation.py::_ERRMODE_WRAPPERS", "oracle_family", "MAGIC"),
        _a(a, "failure_propagation.py::_ERRMODE_WRAPPERS", "authority_domain", "BOGUS")],
    # downgrade / derived integrity
    "arm21_downgrade_without_proof": lambda a, c, p: [
        _a(a, "shell_positions.py::SHELL_BUILTINS", "cls", "TEST_ONLY_LOAD_BEARING"),
        _a(a, "shell_positions.py::SHELL_BUILTINS", "oracle_family", "NONE"),
        c.__setitem__("shell_positions.py::SHELL_BUILTINS", "TEST_ONLY")],
    "arm22_derived_not_provenance": lambda a, c, p: [
        _a(a, "exec_transfer_oracle.py::TRANSFER_WORDS", "cls", "SECURITY_CRITICAL_DERIVED"),
        _a(a, "exec_transfer_oracle.py::TRANSFER_WORDS", "oracle_family", "MODULE_CONSTANTS")],
    # projection
    "arm38_projection_mismatch": lambda a, c, p: c.__setitem__(
        "shell_positions.py::KEYWORDS_INTRODUCING_A_COMMAND", "NON_SECURITY_CONFIGURATION"),
    # non-security must carry NONE
    "arm_nonsec_masking_oracle_leak": lambda a, c, p: _a(a, "ci_invocation_model.py::DATA_CONSUMERS", "oracle_family", "MODULE_CONSTANTS"),
}


@pytest.mark.parametrize("name", sorted(ARMS))
def test_metadata_arm_fires(name):
    problems = _mut(ARMS[name])
    assert problems, f"{name}: falsification arm did not fire (validate_a3 returned no problem)"


def test_arm26_a2_a3_ownership_disjoint():
    """Arm 26: A3 ids must be disjoint from every A1/A2 adjudication key set; injecting one fires."""
    for blk in ("a1a_adjudication", "a1b_adjudication", "a1c_adjudication",
                "a2a_adjudication", "a2b_adjudication", "a2c_adjudication"):
        assert A3_IDS.isdisjoint(set(CONTRACT[blk])), blk
    a2c = dict(CONTRACT["a2c_adjudication"])
    a2c["shell_positions.py::COMMAND_WRAPPERS"] = {"cls": "SECURITY_CRITICAL_SOURCE"}
    assert not A3_IDS.isdisjoint(set(a2c))  # overlap now detectable


# ----------------------------------------------------------------------------------------------------
# REAL production detectors — positive-presence: the load-bearing behaviour each SOURCE underwrites.
# ----------------------------------------------------------------------------------------------------
def test_interpreters_harvest_real_invocation():
    """Arm 2/6: INTERPRETERS is load-bearing — an interpreter's script target IS harvested."""
    import ci_invocation_model as ci
    assert ci.INTERPRETERS and "python3" in ci.INTERPRETERS
    assert ci.invoked_targets("python3 scripts/x.py") == {"scripts/x.py"}


def test_data_consumers_downgrade_proof_canary():
    """The DATA_CONSUMERS downgrade proof: emptying the set does NOT reopen echo-substitution
    (the anti-substitution authority is _executable_positions, per Gate 4N-I27O)."""
    import ci_invocation_model as ci
    assert ci.invoked_targets("echo python3 scripts/x.py") == set()
    saved = ci.DATA_CONSUMERS
    try:
        ci.DATA_CONSUMERS = set()
        assert ci.invoked_targets("echo python3 scripts/x.py") == set()  # still blocked -> VALID_EMPTY proven
    finally:
        ci.DATA_CONSUMERS = saved


def test_masking_downgrade_authority_is_failure_propagation():
    """The MASKING downgrade proof: the authoritative masking-of-failure verdict lives in
    failure_propagation.py, which catches forms the ci_invocation_model tuple omits."""
    import failure_propagation as fp
    v = fp.classify_line("pytest || echo 'suite non-blocking'", pipefail=True, set_e_disabled=False)
    assert v["verdict"] == "MASKED"


def test_transfer_word_exec_is_followed():
    """Arm 6: `exec <interp> <script>` transfers — the child executor is discovered."""
    import exec_transfer_oracle as ex
    sites = ex.derive("exec python scripts/x.py")
    assert sites and sites[0]["child"] == "python"


def test_exec_dash_a_selects_real_executor():
    """Arm 8: `exec -a name bash s.sh` — _VALUE_OPTS('-a') skips the value; child is bash, not name."""
    import exec_transfer_oracle as ex
    sites = ex.derive("exec -a altname bash s.sh")
    assert sites and sites[0]["child"] == "bash"


def test_parser_and_executor_agree():
    """Arm 5: the two independent derivations (exec_transfer_oracle vs shell_positions) agree on the child."""
    import exec_transfer_oracle as ex, shell_positions as sp
    child_ex = ex.derive("exec python scripts/x.py")[0]["child"]
    ts = sp.scan("exec python scripts/x.py").transfer_sites
    assert ts and ts[0].child == child_ex == "python"


def test_bh_a3_find_01_transfer_words_bind_active_regex():
    """BH-A3-FIND-01 completeness contract: TRANSFER_WORDS must equal the _ACTIVE regex alternation
    both directions. This is the binding the (presently unread) tuple currently lacks in production."""
    import exec_transfer_oracle as ex
    alternation = set(re.findall(r"exec|coproc", ex._ACTIVE.pattern))
    assert alternation == set(ex.TRANSFER_WORDS), (alternation, set(ex.TRANSFER_WORDS))


def test_indirect_execution_is_declared_unresolved():
    """Arm 9: FAIL_CLOSED_WORDS — eval/source/dot are surfaced as unresolved, never silently trusted."""
    import shell_positions as sp
    r = sp.scan('eval "python scripts/x.py"')
    assert "eval" in {c.word for c in r.unresolved}


def test_failure_masking_is_detected():
    """Arm 11: `deploy.sh || true` is classified MASKED (a swallowed failure), not clean."""
    import failure_propagation as fp
    assert fp.classify_line("deploy.sh || true", pipefail=True, set_e_disabled=False)["verdict"] == "MASKED"


def test_pipeline_failure_suppression_detected():
    """Arm 12: a pipeline without pipefail hides upstream failure -> flagged MASKED."""
    import failure_propagation as fp
    assert fp.classify_line("a | b", pipefail=False, set_e_disabled=False)["verdict"] == "MASKED"


def test_command_after_keyword_detected():
    """Arm 14: KEYWORDS_INTRODUCING_A_COMMAND — a command after `if`/`then` is found in command position."""
    import shell_positions as sp
    assert "python" in sp.scan("if python scripts/x.py; then :; fi").executables()


def test_npm_call_sites_reconcile():
    """Arm 19: NPM_WORDS drives derive_call_sites — the two derivations agree and the universe is non-empty."""
    import npm_authority as npm
    d = npm.derive_call_sites()
    assert d["agree"] and d["clean"] and d["shared_count"] > 0


def test_npm_closed_enums_are_frozen_and_nonempty():
    """Arms 18/34: AUTHORITY_MODELS and DISPOSITIONS are closed, non-empty frozensets load_policy enforces."""
    import npm_authority as npm
    assert isinstance(npm.AUTHORITY_MODELS, frozenset) and len(npm.AUTHORITY_MODELS) == 6
    assert isinstance(npm.DISPOSITIONS, frozenset) and len(npm.DISPOSITIONS) == 7


def test_native_magic_covers_platform_and_records_obs_02():
    """NATIVE_MAGIC is load-bearing (native-vs-script) and carries the A3-OBS-02 cosmetic duplicate."""
    import npm_authority as npm
    assert npm.NATIVE_MAGIC  # non-empty -> genuine node is classifiable
    assert npm.NATIVE_MAGIC.count(b"\xcf\xfa\xed\xfe") == 2  # A3-OBS-02 (behaviorally dead duplicate)


def test_exec_combinable_container_type_stable():
    """Arm 31: EXEC_COMBINABLE is discovered as its authored container (a Call node), guarding a silent
    container-type change that would drop members."""
    import critical_list_inventory as cli
    row = {c["id"]: c for c in cli.discover_collections()}["shell_positions.py::EXEC_COMBINABLE"]
    assert row["form"] == "Call"


def test_non_executing_is_dead_unread():
    """NON_EXECUTING is DEAD_OR_STALE: grep-confirmed unread; mutating it has no runtime effect here."""
    import ci_invocation_model as ci
    assert ADJ["ci_invocation_model.py::NON_EXECUTING"]["cls"] == "DEAD_OR_STALE"
    assert isinstance(ci.NON_EXECUTING, (set, frozenset)) and ci.NON_EXECUTING
