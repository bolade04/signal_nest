"""Gate 4N-I28BE — Docker per-site steering enforcement consumer.

Closes part A of ADV-I28AX-ARCH-01: every call site carried four steering fields that NO decision
path read. Measured through final-decision behaviour before the fix, 8 of 10 mutations to those
records produced no failure at all — removing every field, blanking them, corrupting them, moving
a source position, duplicating a site identity, inventing an unadjudicated site.

Part B — workflow assurance coverage, workflow-source binding and workflow TOCTOU — is NOT closed
here and is NOT claimed. `enforce_per_site` reports it as explicitly deferred.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import docker_boundary as db                                   # noqa: E402


@pytest.fixture(scope="module")
def policy():
    return db.load_policy()


@pytest.fixture(scope="module")
def state():
    return db.steering_state()


def test_the_universe_is_non_empty_and_every_site_has_exactly_one_decision(policy, state):
    result = db.enforce_per_site(policy, state)
    assert result["sites"] > 0, "an empty universe would make per-site enforcement vacuous"
    ids = [d["id"] for d in result["decisions"]]
    assert len(ids) == len(set(ids)) == result["sites"]


def test_the_baseline_universe_passes(policy, state):
    result = db.enforce_per_site(policy, state)
    assert result["clean"], result["problems"][:3]
    assert result["decision_counts"]["PASS"] == result["sites"]


def test_every_site_is_classified(policy, state):
    for decision in db.enforce_per_site(policy, state)["decisions"]:
        assert decision["classification"] in db.SITE_CLASSIFICATIONS
        assert decision["evidence"], "a classification without evidence is an assertion, not a rule"


def test_classification_is_not_filename_alone():
    """A workflow site and a script site with the SAME subcommand classify differently."""
    graded = {"id": "w#j#1#0", "workflow": "ci.yml", "job": "j", "source": ".github/workflows/ci.yml",
              "subcommand": "push", "trust_boundary": db.EXTERNAL_CI_DAEMON_ASSUMPTION}
    script = dict(graded, id="s#1", workflow=None, job=None, source="scripts/x.sh")
    assert db.classify_site(graded)[0] == db.GRADED_RELEASE_BLOCKING
    assert db.classify_site(script)[0] == db.CI_INFRASTRUCTURE_ONLY


def test_every_required_field_is_consumed_by_the_decision(policy, state):
    """The heart of the finding: a field that is only serialised is not enforcement."""
    decision = db.enforce_per_site(policy, state)["decisions"][0]
    assert set(decision["consumed_fields"]) == db.PER_SITE_REQUIRED_FIELDS, (
        "every authored per-site field must be read by the decision, and nothing else; anything "
        "unread is the evidence-only defect ADV-I28AX-ARCH-01 identified. "
        f"unread={sorted(db.PER_SITE_REQUIRED_FIELDS - set(decision['consumed_fields']))}")


@pytest.mark.parametrize("label,mutate", [
    ("missing permitted_steering", lambda d: d["call_sites"][0].pop("permitted_steering")),
    ("missing prohibited_steering", lambda d: d["call_sites"][0].pop("prohibited_steering")),
    ("blank required_verification", lambda d: d["call_sites"][0].update({"required_verification": ""})),
    ("empty authoritative_inputs", lambda d: d["call_sites"][0].update({"authoritative_inputs": []})),
    ("source position removed", lambda d: d["call_sites"][0].update({"line_in_block": None})),
    ("unknown field added", lambda d: d["call_sites"][0].update({"nobody_reviewed_this": 1})),
    ("duplicate site identity", lambda d: d["call_sites"].append(copy.deepcopy(d["call_sites"][0]))),
    ("unadjudicated new site", lambda d: d["call_sites"].append({"id": "ci.yml#fake#99#0"})),
    ("unclassified trust_boundary", lambda d: d["call_sites"][0].update({"trust_boundary": None})),
    ("continue_on_error true", lambda d: d["call_sites"][0].update({"continue_on_error": True})),
    ("prohibited_steering emptied", lambda d: d["call_sites"][0].update({"prohibited_steering": []})),
    ("undeclared prohibited category",
     lambda d: d["call_sites"][0].update({"prohibited_steering": ["an undeclared category"]})),
    ("permitted names a non-steering variable",
     lambda d: d["call_sites"][0].update({"permitted_steering": ["NOT_A_STEERING_VARIABLE"]})),
    ("site record deleted", lambda d: d["call_sites"].pop(0)),
    ("empty universe", lambda d: d.update({"call_sites": []})),
])
def test_each_per_site_mutation_is_refused(policy, state, label, mutate):
    mutated = copy.deepcopy(policy)
    mutate(mutated)
    assert not db.enforce_per_site(mutated, state)["clean"], f"{label} must not pass"


def test_only_pass_satisfies_a_load_bearing_site(policy, state):
    result = db.enforce_per_site(policy, state)
    for decision in result["decisions"]:
        if decision["classification"] in db.LOAD_BEARING_CLASSIFICATIONS:
            assert decision["decision"] == db.SITE_PASS


def test_unsupported_and_unresolved_always_fail_whatever_the_class(policy, state):
    """A site that cannot be classified cannot be shown to be non-load-bearing.

    The first version of the aggregate only failed LOAD_BEARING classes, so an unclassifiable
    record fell out of that set and escaped entirely.
    """
    mutated = copy.deepcopy(policy)
    mutated["call_sites"].append({"id": "unclassifiable#1"})
    result = db.enforce_per_site(mutated, state)
    assert not result["clean"]
    assert any("UNSUPPORTED" in p or "UNRESOLVED" in p for p in result["problems"])


def test_env_keys_is_consumed_from_the_derived_records(policy, state, monkeypatch):
    """`env_keys` was the field the finding named as produced and never read."""
    real = db.derive_call_sites

    def stripped():
        derived = copy.deepcopy(real())
        for site in derived["sites"]:
            site.pop("env_keys", None)
        return derived

    monkeypatch.setattr(db, "derive_call_sites", stripped)
    assert not db.enforce_per_site(policy, state)["clean"]


def test_a_step_declaring_fatal_steering_in_its_own_env_is_refused(policy, state, monkeypatch):
    """The precise ADV-I28AX-ARCH-01 mechanism: `env:` on a graded Docker step."""
    real = db.derive_call_sites

    def injected():
        derived = copy.deepcopy(real())
        derived["sites"][0]["env_keys"] = ["DOCKER_HOST"]
        return derived

    monkeypatch.setattr(db, "derive_call_sites", injected)
    result = db.enforce_per_site(policy, state)
    assert not result["clean"]
    assert any("DOCKER_HOST" in p for p in result["problems"])


def test_an_empty_derived_universe_fails_rather_than_agreeing_vacuously(policy, state, monkeypatch):
    monkeypatch.setattr(db, "derive_call_sites", lambda: {"sites": [], "problems": [], "count": 0})
    assert not db.enforce_per_site(policy, state)["clean"]


def test_the_docker_aggregate_depends_on_per_site_enforcement():
    """Enforcement must reach the final decision, not merely exist."""
    verdict = db.verify()
    assert "per_site" in verdict, "verify() must carry the per-site result"
    assert verdict["per_site"]["sites"] > 0


def test_workflow_assurance_coverage_is_not_claimed_closed(policy, state):
    """§18: this gate must not silently declare part B done."""
    coverage = db.enforce_per_site(policy, state)["workflow_assurance_coverage"]
    assert "NOT_ADJUDICATED" in coverage and "I28BG" in coverage


def test_the_consumer_works_with_no_arguments():
    """The default-load path, which every other test bypassed by passing the policy explicitly.

    `enforce_per_site()` crashed with AttributeError on `policy.get(...)` when `policy` was None —
    the parameter, not the loaded document. The session baseline and evidence generation both call
    it bare, so the one call shape nothing covered was the one production uses.
    """
    result = db.enforce_per_site()
    assert result["sites"] > 0
    assert result["clean"], result["problems"][:3]
