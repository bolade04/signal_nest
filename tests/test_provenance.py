"""Value-bearing, type-aware provenance verification (Gate 4N-I16, Defect 2, Phases F/G/H).

THE DEFECT THIS SUITE NOW GUARDS. The Gate 4N-I15 checker verified that a named field
EXISTED. It never compared the claimed value to the source value and had no notion of a field
being RELEVANT to a claim, so the row certifying the boundary policy ARN — the most
load-bearing identifier in this chain — named `_captured_utc`, a capture DATE, carried no
`value` key, and returned verified=True with certifies_production=True. The source file
contained no boundary reference at all.

    A source timestamp cannot certify an ARN.

The tests below check that this is now a MECHANICAL rule. The type-safety table is the
load-bearing part: each entry is a way of supporting a claim with the wrong kind of evidence,
and every one must be rejected.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import provenance as pv  # noqa: E402

import protected_inventory  # noqa: E402

# GATE 4N-I18, SEC-1: the inventory is tier-resolved. Under Tier 1 this is the tracked
# synthetic fixture; the live document no longer exists in the tree at any path.
INVENTORY = protected_inventory.load().source_path


# =====================================================================================
# The shipped record set
# =====================================================================================


def test_the_shipped_record_set_is_clean():
    result = pv.run()
    assert result["clean"], {
        "unsupported": [(r["claim_id"], r["reason"]) for r in result["unsupported"]],
        "unsafe": [r["claim_id"] for r in result["weak_labels_authorizing_production"]],
        "uncompared": [r["claim_id"]
                       for r in result["production_rows_without_a_comparison"]],
    }


def test_every_production_certifying_row_actually_ran_a_comparison():
    """The Gate 4N-I15 defect in one assertion: presence is not evidence."""
    for row in pv.run()["rows"]:
        if row["certifies_production"]:
            assert row["comparison_method"], row["claim_id"]
            assert row["comparison_result"] not in (None, "n/a"), row["claim_id"]


def test_the_boundary_arn_row_is_anchored_to_an_independent_expected_value():
    """The shipped instance, twice repaired.

    Gate 4N-I15 supported this ARN with a capture TIMESTAMP. Gate 4N-I16 replaced that with a
    "deterministic construction" built from the SAME three constants the claimed value came from,
    so falsifying BOUNDARY_POLICY_NAME moved both sides together and provenance stayed clean — an
    ARN certifying itself. The expected side is now a digest in a separately TRACKED fixture, so
    the two lineages cannot move together.
    """
    row = next(r for r in pv.run()["rows"] if r["claim_id"] == "boundary_policy_arn")
    assert row["verified"]
    assert row["claimed_type"] == pv.T_ARN
    assert row["comparison_method"] == pv.C_DIGEST_AGAINST_TRACKED_FIXTURE
    assert row["field"] != "_captured_utc"
    assert "tracked" in row["reason"]


def test_the_expected_provenance_fixture_is_tracked_and_holds_no_identifiers():
    """The expected side must be under version control and must not leak what it anchors."""
    import re
    import subprocess
    tracked = subprocess.run(
        ["git", "ls-files", "tests/fixtures/expected-provenance-values.json"],
        cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    assert tracked, "the expected-value fixture is not tracked by git"
    text = pv.EXPECTED_FIXTURE.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{12}\b", text), "the fixture leaks an account identifier"
    assert "arn:aws:" not in text, "the fixture leaks an ARN"


def _two_lineage_ids():
    """Gate 4N-I23: DISCOVERED from the authored inventory, never transcribed here.

    I22 finding F2: this parametrization was a hand-written list of six ids while seven rows
    carried the two-lineage comparison, and the omitted one was `secrets_cmk_key_id` — the row
    the mechanism exists for. Restoring the self-comparing defect left the whole suite green.
    Reading the ids from the inventory means adding a row to the inventory automatically adds
    a test case, and `test_row_coverage_accounts_for_every_certifying_row` below makes the
    inventory itself unable to omit one.
    """
    return sorted(pv.row_coverage_report()["two_lineage_required"])


@pytest.mark.parametrize("claim_id", _two_lineage_ids())
def test_expected_and_observed_come_from_different_files(claim_id):
    """Defect 5 in one assertion: the two sides must not be the same document."""
    record = next(r for r in pv.records() if r["id"] == claim_id)
    assert record["comparison"] == pv.C_DIGEST_AGAINST_TRACKED_FIXTURE
    assert record["expected_digest_key"] in pv.expected_digests()
    assert str(pv.EXPECTED_FIXTURE) != str(record.get("source"))


def test_secrets_cmk_key_id_is_covered_by_the_aggregate_guard():
    """THE I22 F2 REGRESSION TEST. Named explicitly because this exact row was the omission,
    and because a generic completeness assertion would not say so out loud."""
    report = pv.row_coverage_report()
    assert report["secrets_cmk_key_id_guarded"], (
        "secrets_cmk_key_id is absent from the two-lineage inventory — this is Gate 4N-I22 "
        "finding F2 reintroduced: the guard would pass while never checking the row it exists for")
    assert "secrets_cmk_key_id" in _two_lineage_ids()
    assert report["complete"], report["problems"]


def test_row_coverage_accounts_for_every_certifying_row():
    """Complete two-way accounting: no certifying row may be outside the inventory, and no
    inventoried id may be absent from the schema."""
    report = pv.row_coverage_report()
    assert report["missing"] == [], report["missing"]
    assert report["unknown"] == [], report["unknown"]
    assert report["duplicate"] == [], report["duplicate"]
    assert report["guarded_rows"] == report["expected_rows"]


def test_dropping_a_certifying_row_from_the_inventory_fails(monkeypatch):
    """Negative test: the guard must FAIL when coverage shrinks, not pass by shrinking."""
    real = pv.records

    def one_more():
        rows = [dict(r) for r in real()]
        rows.append({"id": "uninventoried_row", "certifies_production": True,
                     "comparison": pv.C_DIGEST_AGAINST_TRACKED_FIXTURE, "label": "x"})
        return rows

    monkeypatch.setattr(pv, "records", one_more)
    report = pv.row_coverage_report()
    assert not report["complete"]
    assert "uninventoried_row" in report["missing"]


def test_reverting_the_cmk_row_to_a_self_comparison_fails(monkeypatch):
    """The exact I22 F2 falsification, as a shipping test."""
    real = pv.records

    def reverted():
        rows = [dict(r) for r in real()]
        for r in rows:
            if r["id"] == "secrets_cmk_key_id":
                r["comparison"] = pv.C_EXACT_STRING
                r.pop("expected_digest_key", None)
        return rows

    monkeypatch.setattr(pv, "records", reverted)
    report = pv.row_coverage_report()
    assert not report["complete"]
    assert any("secrets_cmk_key_id" in p for p in report["problems"])


def test_the_account_input_to_that_construction_is_graded_separately():
    """The ARN's account must not be smuggled in as unexamined."""
    row = next(r for r in pv.run()["rows"] if r["claim_id"] == "approved_account_id")
    assert row["verified"]
    assert row["claimed_type"] == pv.T_ACCOUNT_ID
    assert row["observed_field_type"] == pv.T_ARN


def test_the_rds_rows_are_typed_as_repository_expressions_not_live_reads():
    inventory = protected_inventory.load().data
    blob = json.dumps(inventory)
    assert "parameter_group" not in blob and "subnet_group" not in blob
    for claim_id in ("rds_parameter_group", "rds_subnet_group"):
        row = next(r for r in pv.run()["rows"] if r["claim_id"] == claim_id)
        assert row["label"] == pv.REPOSITORY_EXPRESSION
        assert row["claimed_type"] == pv.T_HCL_EXPRESSION


def test_the_provider_pin_reads_the_lockfile_not_the_range():
    """versions.tf carries a RANGE; claiming an exact version from it is presence-not-support."""
    row = next(r for r in pv.run()["rows"] if r["claim_id"] == "provider_pin")
    assert row["source_path"].endswith(".terraform.lock.hcl")
    assert row["observed_field_value"] == "6.55.0"


# =====================================================================================
# PHASE F — the schema
# =====================================================================================


REQUIRED_ROW_FIELDS = ("claim_id", "claimed_type", "claimed_value", "source_label",
                       "source_path", "source_sha256", "field", "observed_field_type",
                       "observed_field_value", "comparison_method", "comparison_result",
                       "confidence", "certifies_production", "verified")


def test_every_row_carries_the_declared_schema():
    for row in pv.run()["rows"]:
        for field in REQUIRED_ROW_FIELDS:
            assert field in row, (row["claim_id"], field)


def test_the_type_and_comparison_vocabularies_are_closed():
    for row in pv.run()["rows"]:
        if row["claimed_type"] is not None:
            assert row["claimed_type"] in pv.TYPES, row["claim_id"]
        if row["comparison_method"] is not None:
            assert row["comparison_method"] in pv.COMPARISONS, row["claim_id"]


def test_the_label_partition_is_total_and_disjoint():
    assert pv.AUTHORITATIVE & pv.NON_CERTIFYING == set()
    assert pv.AUTHORITATIVE | pv.NON_CERTIFYING == set(pv.LABELS)


# =====================================================================================
# PHASE G — semantic type detection
# =====================================================================================


@pytest.mark.parametrize("value,expected", [
    ("arn:aws:iam::111122223333:policy/x", pv.T_ARN),
    ("arn:aws:cloudtrail:us-east-1:111122223333:trail/x", pv.T_ARN),
    ("111122223333", pv.T_ACCOUNT_ID),
    ("z" * 64, pv.T_NAME),                       # not hex -> not a digest
    ("a" * 64, pv.T_SHA256),                     # 64 hex chars IS a digest
    ("0" * 64, pv.T_SHA256),
    ("sha256:" + "b" * 64, pv.T_SHA256),
    ("2026-07-31", pv.T_TIMESTAMP),
    ("2026-07-31T12:00:00Z", pv.T_TIMESTAMP),
    ("6.55.0", pv.T_VERSION),
    ("${var.name_prefix}-pg-params", pv.T_HCL_EXPRESSION),
    ("coalesce(var.x, \"y\")", pv.T_HCL_EXPRESSION),
    ("signalnest-staging-tfstate", pv.T_NAME),
    (True, pv.T_BOOLEAN),
    (None, pv.T_ABSENT),
])
def test_semantic_types_are_detected_from_the_bytes(value, expected):
    assert pv.detect_type(value) == expected


def test_an_arn_is_detected_before_an_account_id():
    """Order matters: an ARN contains a 12-digit account and must not be typed as one."""
    assert pv.detect_type("arn:aws:iam::111122223333:role/x") == pv.T_ARN


def test_arn_comparison_is_componentwise_not_string_equality():
    # GATE 4N-I18: the two ARNs must differ in exactly ONE component — the account — or the
    # test proves nothing about componentwise comparison. Two DIFFERENT placeholder accounts
    # keep that property without writing a live identifier into the tree.
    a = "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary"
    b = "arn:aws:iam::444444444444:policy/signalnest-staging-role-boundary"
    assert a != b and a.split(":")[4] != b.split(":")[4]
    ok, why = pv.compare(pv.C_ARN_COMPONENTWISE, a, b, {})
    assert not ok and "account" in why


def test_a_non_arn_on_either_side_fails_arn_comparison():
    ok, why = pv.compare(pv.C_ARN_COMPONENTWISE, "not-an-arn", "also-not", {})
    assert not ok and "6-part ARN" in why


def test_an_unknown_comparison_method_fails_closed():
    ok, why = pv.compare("NO_SUCH_METHOD", "a", "a", {})
    assert not ok and "unknown comparison method" in why


def test_structural_only_never_certifies_a_value_claim():
    ok, why = pv.compare(pv.C_STRUCTURAL_ONLY, "a", "a", {})
    assert not ok and "never certifies" in why


# =====================================================================================
# PHASE H — type safety. Each entry is a WRONG kind of evidence and must be rejected.
# =====================================================================================


def _row(**kwargs):
    base = {"id": "probe", "source": INVENTORY, "certifies_production": True}
    return {**base, **kwargs}


TYPE_SAFETY_NEGATIVES = {
    "arn_claim_supported_by_a_timestamp": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_ARN, field="_captured_utc",
        value="arn:aws:iam::111122223333:policy/x", comparison=pv.C_ARN_COMPONENTWISE),
    "arn_claim_supported_by_a_bucket_name": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_ARN, field="buckets_by_role.tfstate",
        value="arn:aws:iam::111122223333:policy/x", comparison=pv.C_ARN_COMPONENTWISE),
    "account_claim_supported_by_a_name": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_ACCOUNT_ID,
        field="buckets_by_role.tfstate", value="111122223333",
        comparison=pv.C_EXACT_STRING),
    "timestamp_claim_supported_by_an_arn": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_TIMESTAMP, field="trails.0.1",
        value="2026-07-31", comparison=pv.C_EXACT_STRING),
    "hash_claim_supported_by_a_timestamp": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_SHA256, field="_captured_utc",
        value="0" * 64, comparison=pv.C_EXACT_STRING),
    "value_claim_with_no_comparison_method": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_ARN, field="trails.0.1",
        value="whatever"),
    "value_bearing_row_with_no_claimed_value": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_ARN, field="trails.0.1",
        comparison=pv.C_ARN_COMPONENTWISE),
    "value_bearing_row_with_no_claimed_type": _row(
        label=pv.LIVE_READ_EXACT, field="trails.0.1", value="x",
        comparison=pv.C_EXACT_STRING),
    "exact_claim_over_a_field_that_does_not_exist": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_ARN, field="no.such.field",
        value="arn:aws:iam::111122223333:policy/x", comparison=pv.C_ARN_COMPONENTWISE),
    "right_type_but_wrong_value": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_ARN, field="trails.0.1",
        value="arn:aws:cloudtrail:us-east-1:111122223333:trail/wrong",
        comparison=pv.C_ARN_COMPONENTWISE),
    "live_claim_supported_by_a_synthetic_fixture": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_NAME,
        source="tests/fixtures/synthetic-anchor.json", field="approved_account_id",
        value="111122223333", comparison=pv.C_EXACT_STRING),
    "synthetic_fixture_claiming_production": {
        "id": "probe", "label": pv.SYNTHETIC_TEST_FIXTURE,
        "source": "tests/fixtures/synthetic-anchor.json", "certifies_production": True},
    "ci_equivalent_claiming_production": {
        "id": "probe", "label": pv.CI_EQUIVALENT_LOCAL_REPRODUCTION,
        "source": "scripts/empty_home_ci.sh", "certifies_production": True},
    "inferred_claiming_production": {
        "id": "probe", "label": pv.INFERRED, "explanation": "a guess",
        "certifies_production": True},
    "unknown_claiming_production": {
        "id": "probe", "label": pv.UNKNOWN, "explanation": "unrecoverable",
        "certifies_production": True},
    "uncertainty_with_no_explanation": {
        "id": "probe", "label": pv.INFERRED, "certifies_production": False},
    "a_source_that_does_not_exist": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_ARN, source="no/such/file.json",
        field="x", value="y", comparison=pv.C_EXACT_STRING),
    "a_source_whose_hash_changed": _row(
        label=pv.LIVE_READ_EXACT, claimed_type=pv.T_ARN, field="trails.0.1",
        value="x", comparison=pv.C_EXACT_STRING, source_sha256="0" * 64),
    "an_unknown_label": _row(label="MADE_UP_LABEL", claimed_type=pv.T_ARN,
                             field="trails.0.1", value="x",
                             comparison=pv.C_EXACT_STRING),
    "a_deterministic_construction_that_does_not_reproduce": _row(
        label=pv.DETERMINISTIC_NOT_YET_CREATED, claimed_type=pv.T_ARN,
        source="scripts/signalnest_identity.py",
        value="arn:aws:iam::111122223333:policy/WRONG",
        comparison=pv.C_DETERMINISTIC_CONSTRUCTION,
        construction_rule="test", construct=lambda: "arn:aws:iam::111122223333:policy/right"),
}


@pytest.mark.parametrize("name", sorted(TYPE_SAFETY_NEGATIVES))
def test_every_wrong_kind_of_evidence_is_rejected(name):
    result = pv.verify(TYPE_SAFETY_NEGATIVES[name])
    assert not result["verified"] or result["normalized_label"] in pv.NON_CERTIFYING, (
        f"{name} was ACCEPTED: {result['reason']}")


def test_the_negative_table_is_broad():
    assert len(TYPE_SAFETY_NEGATIVES) >= 18


def test_a_downgraded_row_survives_rather_than_being_deleted():
    result = pv.verify(TYPE_SAFETY_NEGATIVES["exact_claim_over_a_field_that_does_not_exist"])
    assert result["claim_id"] == "probe", "the record must survive its own downgrade"
    assert result["normalized_label"] in (pv.INFERRED, pv.UNKNOWN)
    assert result["normalized_label"] != result["label"]


# =====================================================================================
# The GitHub-Actions claim control — now STRUCTURAL, after two prose heuristics failed
# =====================================================================================
#
# v1 matched "not" inside the key name "note". v2 was broken six ways by the Gate 4N-I15
# adversarial lane. A v3 was drafted for this gate and abandoned: no window or word-distance
# rule separates "GitHub Actions has never run" from "executed on GitHub Actions and the
# result is not in dispute", because the difference is grammatical, not positional.
#
# The control is now a declared boolean. These tests assert the STRUCTURAL property and,
# just as importantly, assert that prose is NOT treated as evidence in either direction.


def test_a_ci_equivalent_record_must_declare_the_boolean():
    result = pv.verify({"id": "probe", "label": pv.CI_EQUIVALENT_LOCAL_REPRODUCTION,
                        "source": "scripts/empty_home_ci.sh",
                        "certifies_production": False})
    assert not result["verified"]
    assert "explicit boolean" in result["reason"]


def test_declaring_a_github_actions_run_under_a_local_label_is_rejected():
    result = pv.verify({"id": "probe", "label": pv.CI_EQUIVALENT_LOCAL_REPRODUCTION,
                        "source": "scripts/empty_home_ci.sh",
                        "certifies_production": False, "github_actions_run": True})
    assert not result["verified"]
    assert result["normalized_label"] == pv.UNKNOWN


@pytest.mark.parametrize("bad", ["true", 1, None, "yes"])
def test_a_non_boolean_declaration_is_rejected(bad):
    result = pv.verify({"id": "probe", "label": pv.CI_EQUIVALENT_LOCAL_REPRODUCTION,
                        "source": "scripts/empty_home_ci.sh",
                        "certifies_production": False, "github_actions_run": bad})
    assert not result["verified"]


PROSE_THAT_MUST_NOT_CHANGE_THE_VERDICT = {
    "affirmative_plain": "this ran on GitHub Actions",
    "affirmative_with_an_unrelated_not": "executed on GitHub Actions and the result is not "
                                         "in dispute",
    "affirmative_with_an_unrelated_never": "GitHub Actions ran green and we have never seen "
                                           "it red",
    "sentence_split": "Verified on GitHub. Actions workflow run 991 was green",
    "gh_abbreviation": "workflow run 17 on GH Actions passed",
    "truthful_negation": "GitHub Actions has never run for this candidate",
    "runs_none": "GitHub Actions runs for this branch: none",
    "zero_runs": "There have been zero GitHub Actions runs",
}


@pytest.mark.parametrize("name", sorted(PROSE_THAT_MUST_NOT_CHANGE_THE_VERDICT))
def test_prose_does_not_change_the_verdict_in_either_direction(name):
    """The boolean decides. Prose — affirmative, negated, or ambiguous — is documentation.

    This is the property that makes the control robust: there is no sentence a record can
    contain that flips the outcome, so there is nothing for a future reviewer to evade.
    """
    prose = PROSE_THAT_MUST_NOT_CHANGE_THE_VERDICT[name]
    base = {"id": "probe", "label": pv.CI_EQUIVALENT_LOCAL_REPRODUCTION,
            "source": "scripts/empty_home_ci.sh", "certifies_production": False,
            "explanation": prose, "detail": {"nested": prose}, "notes": [prose]}
    assert pv.verify({**base, "github_actions_run": False})["verified"], prose
    assert not pv.verify({**base, "github_actions_run": True})["verified"], prose


def test_the_shipped_empty_home_record_declares_no_ci_run():
    record = next(r for r in pv.records() if r["id"] == "empty_home_run")
    assert record["github_actions_run"] is False


# =====================================================================================
# The empty-HOME harness must EXIST as a runnable file, not as a summary line
# =====================================================================================


def test_the_empty_home_harness_exists_and_sets_home_rather_than_unsetting_it():
    """Gate 4N-I15 recorded an empty-HOME result with no command anywhere in the repo, so
    no reviewer could tell whether HOME had been SET to an empty dir or merely UNSET — and
    with HOME unset, Path.home() falls back to the real home directory."""
    harness = REPO_ROOT / "scripts" / "empty_home_ci.sh"
    assert harness.exists()
    text = harness.read_text(encoding="utf-8")
    assert "HOME=\"$SANDBOX_HOME\"" in text
    assert "mktemp -d" in text
    assert "unset HOME" not in text
    assert "refusing to run" in text, "the harness must verify its own isolation"
