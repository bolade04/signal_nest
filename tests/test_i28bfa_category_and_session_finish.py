"""Gate 4N-I28BF-A — exact Docker category resolution and session-finish rederivation.

Closes two defects measured during the stopped I28BF gate.

I28BE-CAT-01: `_resolve_steering_category` lowercased its input and used substring containment, so
FIFTEEN inputs appearing in no authored policy entry resolved to real mechanisms — "contextual
analysis" to DOCKER_CONTEXT, "misconfiguration" to DOCKER_CONFIG and XDG_CONFIG_HOME, "flagrant
nonsense" to all nine steering flags.

I28BE-SESSION-01: `reverify()` had no docker_per_site layer, so per-site enforcement ran once at
establishment and a later mutation was never re-derived or compared.
"""

from __future__ import annotations

import copy
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import docker_boundary as db                                    # noqa: E402
import signalnest_bootstrap as boot                             # noqa: E402

TABLE = db.load_policy()["steering"]

WIDENING_INPUTS = [
    "contextual analysis", "misconfiguration", "flagrant nonsense", "composer packages",
    "context", "configuration", "compose", "tls", "TLS variables.", "TLS VARIABLES",
    "contexts", "subcontext", "TLS variables and more", "tls-variables", "tls_variables",
]


@pytest.mark.parametrize("entry", WIDENING_INPUTS)
def test_every_measured_widening_input_now_fails_closed(entry):
    """The fifteen inputs that previously resolved to real mechanisms."""
    classification, mechanisms = db.resolve_steering_entry(entry, TABLE)
    assert classification == db.CATEGORY_INVALID
    assert mechanisms == ()


@pytest.mark.parametrize("name", ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"])
def test_concrete_names_resolve_only_to_themselves(name):
    classification, mechanisms = db.resolve_steering_entry(name, TABLE)
    assert classification == db.CATEGORY_CONCRETE
    assert mechanisms == (name,)


@pytest.mark.parametrize("category,count", [
    ("TLS variables", 3), ("steering flags", 9), ("a defined Docker context", 1)])
def test_the_three_canonical_categories_resolve_exactly(category, count):
    classification, mechanisms = db.resolve_steering_entry(category, TABLE)
    assert classification == db.CATEGORY_CANONICAL
    assert len(mechanisms) == count


@pytest.mark.parametrize("value", ["", "   ", None, 123, [], {}, "unknown future category"])
def test_invalid_empty_and_non_string_values_fail_closed(value):
    """`None` previously raised AttributeError rather than failing closed."""
    assert db.resolve_steering_entry(value, TABLE) == (db.CATEGORY_INVALID, ())


def test_normalization_is_outer_whitespace_only():
    assert db.resolve_steering_entry("  TLS variables  ", TABLE)[0] == db.CATEGORY_CANONICAL
    for altered in ("TLS VARIABLES", "tls variables", "TLS  variables", "TLS variables."):
        assert db.resolve_steering_entry(altered, TABLE)[0] == db.CATEGORY_INVALID, (
            f"{altered!r} differs by more than outer whitespace and must not be guessed at")


def test_no_substring_matching_remains():
    """Structural AND functional: containment must not resolve anything."""
    import inspect
    source = inspect.getsource(db.resolve_steering_entry)
    assert " in lowered" not in source, (
        "substring containment must not be reachable from the resolver. The check reads the "
        "RESOLVER's source, not the whole module: the module's own comment quotes the removed "
        "`\"context\" in lowered` test, and matching that comment would be a false positive.")
    assert db.resolve_steering_entry("flagrant nonsense", TABLE)[1] == ()


def test_the_category_table_is_structurally_sound():
    assert db.category_table_problems() == []
    assert len(db.DOCKER_STEERING_CATEGORIES) == 3


def test_the_category_table_digest_changes_when_the_table_changes(monkeypatch):
    before = db.category_table_digest()
    widened = dict(db.DOCKER_STEERING_CATEGORIES)
    widened["TLS variables"] = tuple(widened["TLS variables"]) + ("DOCKER_HOST",)
    monkeypatch.setattr(db, "DOCKER_STEERING_CATEGORIES", widened)
    assert db.category_table_digest() != before, "a widened mapping must move the digest"


def test_a_zero_mechanism_category_is_refused(monkeypatch):
    monkeypatch.setattr(db, "DOCKER_STEERING_CATEGORIES", {"empty category": ()})
    assert db.category_table_problems(), "a category enforcing nothing must be refused"


def test_two_categories_with_identical_mechanisms_are_ambiguous(monkeypatch):
    monkeypatch.setattr(db, "DOCKER_STEERING_CATEGORIES",
                        {"a": ("DOCKER_CONTEXT",), "b": ("DOCKER_CONTEXT",)})
    assert any("ambiguous" in p for p in db.category_table_problems())


def test_every_authored_entry_resolves_to_a_valid_classification():
    policy = db.load_policy()
    for site in policy["call_sites"]:
        for entry in (site.get("prohibited_steering") or []) + (site.get("permitted_steering") or []):
            classification, mechanisms = db.resolve_steering_entry(entry, TABLE)
            assert classification in (db.CATEGORY_CONCRETE, db.CATEGORY_CANONICAL), (
                f"authored entry {entry!r} does not resolve")
            assert mechanisms, f"authored entry {entry!r} resolves to no mechanism"


# ---------------------------------------------------------------- session finish
def test_the_per_site_snapshot_binds_the_category_table():
    state = db.per_site_state()
    assert state["category_table_version"] == db.CATEGORY_TABLE_VERSION
    assert state["normalization_version"] == db.NORMALIZATION_VERSION
    assert state["category_table_digest"] == db.category_table_digest()
    assert state["sites"] > 0 and state["clean"]


def test_an_identical_state_shows_no_difference():
    state = db.per_site_state()
    assert db.per_site_differences(state, state) == []


@pytest.mark.parametrize("label,mutate", [
    ("decision forced", lambda s: s["per_site"][0].update({"decision": "FAIL"})),
    ("classification changed", lambda s: s["per_site"][0].update({"classification": "X"})),
    ("consumed set changed", lambda s: s["per_site"][0].update({"consumed": "id"})),
    ("site removed", lambda s: s["per_site"].pop(0)),
    ("category digest forged", lambda s: s.update({"category_table_digest": "forged"})),
    ("table version changed", lambda s: s.update({"category_table_version": "x"})),
    ("normalization changed", lambda s: s.update({"normalization_version": "x"})),
    ("policy digest changed", lambda s: s.update({"policy_digest": "x"})),
    ("site count changed", lambda s: s.update({"sites": 1})),
    ("load-bearing count changed", lambda s: s.update({"load_bearing": 1})),
    ("coverage marker completed", lambda s: s.update({"workflow_assurance_coverage": "COMPLETE"})),
])
def test_every_late_difference_is_detected(label, mutate):
    fresh = db.per_site_state()
    tampered = copy.deepcopy(fresh)
    mutate(tampered)
    assert db.per_site_differences(tampered, fresh), f"{label} must be detected"


def _attested_config():
    attestation = boot.establish(strict=False)
    config = types.SimpleNamespace()
    setattr(config, boot.BOOTSTRAP_ATTESTATION, attestation)
    return config, attestation


def test_reverify_has_a_docker_per_site_layer_and_it_is_clean():
    """I28BE-SESSION-01: this layer did not exist."""
    config, _ = _attested_config()
    result = boot.reverify(config)
    assert "docker_per_site" in result["layers"], (
        "session finish must re-derive Docker per-site enforcement; without this layer a late "
        "mutation after a clean baseline is never compared")
    # reverify flattens every layer to a boolean in its return value, exactly as it does for
    # docker_snapshot; asserting a dict shape here was my assumption, not the contract.
    assert result["layers"]["docker_per_site"] is True


@pytest.mark.parametrize("label,mutate", [
    ("site decision", lambda a: a["docker_per_site"]["per_site"][0].update({"decision": "FAIL"})),
    ("category digest", lambda a: a["docker_per_site"].update({"category_table_digest": "forged"})),
    ("coverage marker", lambda a: a["docker_per_site"].update({"workflow_assurance_coverage": "COMPLETE"})),
    ("site removed", lambda a: a["docker_per_site"]["per_site"].pop(0)),
])
def test_a_late_mutation_after_a_clean_baseline_fails_the_session(label, mutate):
    _, attestation = _attested_config()
    tampered = copy.deepcopy(attestation)
    mutate(tampered)
    config = types.SimpleNamespace()
    setattr(config, boot.BOOTSTRAP_ATTESTATION, tampered)
    result = boot.reverify(config)
    assert any("docker_per_site" in p for p in result["problems"]), f"{label} must fail the session"


def test_session_finish_does_not_reuse_the_establishment_object():
    """A fresh rederivation, not the bound object handed back."""
    _, attestation = _attested_config()
    fresh = db.per_site_state()
    assert fresh is not attestation["docker_per_site"]
    assert db.per_site_differences(attestation["docker_per_site"], fresh) == []


def test_the_deferred_marker_names_i28bg():
    assert "I28BG" in db.per_site_state()["workflow_assurance_coverage"]


# ---------------------------------------------------------------- GATE 4N-I28BF-A3
# A2-FIND-01: per_site_state carried id, decision, classification and the consumed-field set, and
# NO source position. Measured before the fix: moving a load-bearing Docker call from line 1 to
# line 41 — same command, class, steering fields and decision — produced NO difference at session
# finish and left enforcement clean. Late attack 12 was uncovered and had been miscounted complete.
def test_the_snapshot_carries_a_canonical_source_position():
    record = db.per_site_state()["per_site"][0]
    assert "position" in record and record["position"], (
        "a per-site record with no position cannot detect a moved call site")
    assert db.per_site_state()["source_position_version"] == db.SOURCE_POSITION_VERSION


def test_the_position_is_not_a_basename_and_not_the_id_split_on_hash():
    """The I28BD probe defect: splitting the id on '#' discards the owning source."""
    site = db.load_policy()["call_sites"][0]
    position = db.canonical_source_position(site)
    assert site["source"] in position, "the OWNING SOURCE path must be part of the position"
    assert position != site["id"].split("#")[0]
    assert str(site["line_in_block"]) in position


def test_attack_12_a_moved_load_bearing_site_is_detected():
    """The proving mutation for A2-FIND-01."""
    policy = db.load_policy()
    state = db.steering_state()
    before = db.per_site_state(policy, state)
    moved = copy.deepcopy(policy)
    moved["call_sites"][0]["line_in_block"] = moved["call_sites"][0]["line_in_block"] + 40
    differences = db.per_site_differences(before, db.per_site_state(moved, state))
    assert differences, "moving a load-bearing Docker call must be a difference"
    assert any("position changed" in d for d in differences)


@pytest.mark.parametrize("label,value", [
    ("missing", None), ("negative", -5), ("non-integer", "12"),
])
def test_a_missing_or_malformed_position_fails_closed(label, value):
    policy = copy.deepcopy(db.load_policy())
    policy["call_sites"][0]["line_in_block"] = value
    assert not db.per_site_state(policy, db.steering_state())["clean"], f"{label} must fail closed"


def test_a_missing_source_path_fails_closed():
    policy = copy.deepcopy(db.load_policy())
    policy["call_sites"][0]["source"] = ""
    assert not db.per_site_state(policy, db.steering_state())["clean"]


def test_source_position_is_enforced_without_polluting_the_authored_consumed_set():
    """Position is DERIVED, so it is enforced by its own fail-closed check and by the snapshot
    comparison — not by being counted as an authored consumed field, which would blur what
    `consumed_fields` means and break AC-22's exact equality."""
    record = db.enforce_per_site()["decisions"][0]
    assert "source_position" not in record["consumed_fields"]
    assert record["position"], "the decision must still carry the position"
    policy = copy.deepcopy(db.load_policy())
    policy["call_sites"][0]["line_in_block"] = None
    assert not db.enforce_per_site(policy, db.steering_state())["clean"]


def test_the_position_representation_is_deterministic():
    site = db.load_policy()["call_sites"][0]
    assert db.canonical_source_position(site) == db.canonical_source_position(dict(site))


def test_attack_6_widening_a_mapping_to_all_mechanisms_moves_the_digest(monkeypatch):
    before = db.category_table_digest()
    every = tuple(sorted({m for mechs in db.DOCKER_STEERING_CATEGORIES.values() for m in mechs}))
    monkeypatch.setattr(db, "DOCKER_STEERING_CATEGORIES",
                        {name: every for name in db.DOCKER_STEERING_CATEGORIES})
    assert db.category_table_digest() != before


def test_attack_19_an_empty_independent_universe_is_refused(monkeypatch):
    monkeypatch.setattr(db, "derive_call_sites", lambda: {"sites": [], "problems": [], "count": 0})
    assert not db.enforce_per_site()["clean"]


def test_attack_16_the_consumed_set_is_derived_not_authored():
    """A field cannot be marked consumed without the decision path reading it."""
    policy = copy.deepcopy(db.load_policy())
    policy["call_sites"][0].pop("required_verification")
    result = db.enforce_per_site(policy, db.steering_state())
    assert not result["clean"]
    target = next(d for d in result["decisions"] if d["id"] == policy["call_sites"][0]["id"])
    assert "required_verification" not in target["consumed_fields"]
