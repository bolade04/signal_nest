"""Project Phase 5 CF-2 (W21): the 5C contracts-freeze migration ``a7d2e9f4c1b8``.

Through the real Alembic CLI on a throwaway SQLite database (as CI validates migrations),
with the plumbing of ``test_cf1_contract_migration``:

* the head is single and follows CF-1's ``f3a9c1e7b2d5``; ``upgrade head`` then ``alembic
  check`` reports no drift (Phase 5 plan §6.2.0);
* **the migrated CHECKs equal their Python authorities, in both directions** — the draft
  channel (``CreativeChannel``), the review risk level (``ClaimRisk``), the approval subject
  type (``ApprovalSubjectType``, now with ``creative_draft``) and the capability set
  (``persisted_values()``, now with ``creative_generation``); the three logical CHECKs
  (review blocked / passed, approval draft binding) equal the model's SQL text. Discovery:
  every CHECK on the two new tables must be one this module knows;
* the CHECKs, the per-brief version uniqueness and the one-review-per-draft rule hold on
  insert;
* the downgrade **refuses before any DDL** while an override names ``creative_generation``,
  while an approval decides on a ``creative_draft`` (a compliance record, O-6) or while a
  claims-library entry is retired (dropping ``retired_at`` would re-activate it); otherwise
  one step down removes exactly this revision (both tables, ``approvals.draft_id`` and
  ``claims_library.retired_at`` gone, both CHECKs narrowed, every other index and row kept)
  and ``upgrade`` restores it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from app.approvals.models import DRAFT_SUBJECT_SQL
from app.capabilities.registry import persisted_values
from app.core.enums import ApprovalSubjectType, ClaimRisk, CreativeChannel
from app.creative.models import (
    DRAFT_USAGE_SQL,
    REVIEW_BLOCKED_SQL,
    REVIEW_PASSED_SQL,
    ContentReview,
    CreativeDraft,
)
from app.tests.test_cf1_contract_migration import (
    _alembic,
    _checks,
    _in_list,
    _indexes,
    _sql,
    _tables,
    _version,
)

API_DIR = Path(__file__).resolve().parents[2]
HEAD = "a7d2e9f4c1b8"
CF1 = "f3a9c1e7b2d5"
OVERRIDES = "workspace_capability_overrides"
NEW_TABLES = {"creative_drafts", "content_reviews"}

#: Every CHECK the two new tables may carry.
EXPECTED_CHECKS = {
    "creative_drafts": {
        "ck_creative_draft_channel",
        "ck_creative_draft_usage",
        "ck_creative_draft_version",
    },
    "content_reviews": {
        "ck_content_review_blocked",
        "ck_content_review_draft_version",
        "ck_content_review_passed",
        "ck_content_review_risk_level",
    },
}


def _columns(db_path: Path, table: str) -> set[str]:
    return {r[1] for r in _sql(db_path, f"PRAGMA table_info({table})")}


def _norm(sqltext: str) -> str:
    return re.sub(r"\s+", " ", sqltext).strip()


@pytest.fixture()
def migrated(tmp_path) -> Path:
    db_path = tmp_path / "cf2.db"
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert _version(db_path) == [HEAD]
    return db_path


# --------------------------------------------------------------------------- #
# Head, drift, and the CHECKs against their Python authorities
# --------------------------------------------------------------------------- #
def test_the_head_is_single_and_follows_cf1() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    assert [r.revision for r in script.get_revisions("heads")] == [HEAD]
    assert script.get_revision(HEAD).down_revision == CF1


def test_upgrade_leaves_no_model_drift(migrated: Path) -> None:
    result = _alembic(migrated, "check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert NEW_TABLES <= _tables(migrated)
    assert "draft_id" in _columns(migrated, "approvals")
    assert "retired_at" in _columns(migrated, "claims_library")
    # No llm_usage_records table (coordinator decision I-4): the draft links the P6 ledger.
    assert "llm_usage_records" not in _tables(migrated)
    assert "llm_usage_event_id" in _columns(migrated, "creative_drafts")


def test_every_check_is_known(migrated: Path) -> None:
    """Discovery: a CHECK the database carries but this module does not know fails here."""
    for table, names in EXPECTED_CHECKS.items():
        assert set(_checks(migrated, table)) == names, table
    for model in (CreativeDraft, ContentReview):
        declared = {
            c.name for c in model.__table__.constraints if c.__class__.__name__ == "CheckConstraint"
        }
        assert declared == EXPECTED_CHECKS[model.__tablename__]


def test_vocabulary_checks_equal_their_authorities(migrated: Path) -> None:
    for table, name, column, authority in (
        ("creative_drafts", "ck_creative_draft_channel", "channel",
         {c.value for c in CreativeChannel}),
        ("content_reviews", "ck_content_review_risk_level", "risk_level",
         {r.value for r in ClaimRisk}),
        ("approvals", "ck_approval_subject_type", "subject_type",
         {s.value for s in ApprovalSubjectType}),
        (OVERRIDES, "ck_workspace_capability_override_capability", "capability",
         set(persisted_values())),
    ):
        in_db = _in_list(_checks(migrated, table)[name], column)
        assert (in_db - authority, authority - in_db) == (set(), set()), name
    assert "creative_draft" in _in_list(
        _checks(migrated, "approvals")["ck_approval_subject_type"], "subject_type"
    )
    assert "creative_generation" in _in_list(
        _checks(migrated, OVERRIDES)["ck_workspace_capability_override_capability"],
        "capability",
    )


def test_logical_checks_equal_the_models_sql(migrated: Path) -> None:
    drafts = _checks(migrated, "creative_drafts")
    assert _norm(drafts["ck_creative_draft_usage"]) == _norm(DRAFT_USAGE_SQL)
    reviews = _checks(migrated, "content_reviews")
    assert _norm(reviews["ck_content_review_blocked"]) == _norm(REVIEW_BLOCKED_SQL)
    assert _norm(reviews["ck_content_review_passed"]) == _norm(REVIEW_PASSED_SQL)
    approvals = _checks(migrated, "approvals")
    assert _norm(approvals["ck_approval_draft_subject"]) == _norm(DRAFT_SUBJECT_SQL)


# --------------------------------------------------------------------------- #
# The CHECKs hold on insert (foreign keys are off on this bare connection)
# --------------------------------------------------------------------------- #
_DRAFT_SQL = (
    "INSERT INTO creative_drafts (id, organization_id, workspace_id, brief_id, opportunity_id,"
    " draft_version, channel, body, prompt_name, prompt_version, model_identifier, input_hash,"
    " is_simulated, input_tokens, output_tokens, estimated_cost_usd, created_by_snapshot)"
    " VALUES (?, 'o', 'w', ?, 'opp', ?, ?, 'text', 'generate_creative', '1.0.0', 'mock', ?,"
    " 1, ?, ?, ?, 'snap')"
)
_REVIEW_SQL = (
    "INSERT INTO content_reviews (id, organization_id, workspace_id, draft_id, draft_version,"
    " risk_level, is_blocked, findings, warnings, blocked_claim_ids, required_disclaimers,"
    " disclaimers_resolved, passed) VALUES (?, 'o', 'w', ?, ?, ?, ?, '[]', '[]', '[]', '[]',"
    " ?, ?)"
)
_APPROVAL_SQL = (
    "INSERT INTO approvals (id, organization_id, workspace_id, subject_type, subject_id,"
    " subject_version, brief_id, draft_id, decision, decision_seq, decided_by_snapshot,"
    " decided_role, is_simulated) VALUES (?, 'o', 'w', ?, ?, 1, NULL, ?, 'approved', ?,"
    " 'snap', 'reviewer', 0)"
)


def test_the_draft_checks_hold_on_insert(migrated: Path) -> None:
    def draft(row_id, *, brief="b1", version=1, channel="email", usage=(12, 34, 0)):
        _sql(migrated, _DRAFT_SQL, (row_id, brief, version, channel, "0" * 64, *usage))

    draft("d1")
    draft("d2", version=2, channel="social_post")
    draft("d3", brief="b2", version=1)  # version numbering is per brief
    for row_id, kwargs in (
        ("bad-channel", {"version": 3, "channel": "video"}),
        ("bad-version", {"version": 0}),
        ("dup-version", {"version": 1}),
        ("negative-input", {"version": 4, "usage": (-1, 0, 0)}),
        ("negative-output", {"version": 4, "usage": (0, -1, 0)}),
        ("negative-cost", {"version": 4, "usage": (0, 0, -0.01)}),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            draft(row_id, **kwargs)


def test_the_review_checks_hold_on_insert(migrated: Path) -> None:
    for n in range(1, 4):
        _sql(migrated, _DRAFT_SQL, (f"d{n}", "b1", n, "email", "0" * 64, 0, 0, 0))

    def review(row_id, draft_id, *, risk="low", blocked=0, resolved=1, passed=1, version=1):
        _sql(migrated, _REVIEW_SQL, (row_id, draft_id, version, risk, blocked, resolved, passed))

    review("r1", "d1")  # clean and passed
    review("r2", "d2", risk="blocked", blocked=1, resolved=1, passed=0)  # blocked, not passed
    for row_id, kwargs in (
        ("blocked-without-level", {"risk": "low", "blocked": 1, "passed": 0}),
        ("level-without-blocked", {"risk": "blocked", "blocked": 0, "passed": 0}),
        ("passed-while-blocked", {"risk": "blocked", "blocked": 1, "passed": 1}),
        ("passed-unresolved", {"risk": "medium", "resolved": 0, "passed": 1}),
        ("bad-risk", {"risk": "severe", "passed": 0}),
        ("bad-version", {"version": 0}),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            review(row_id, "d3", **kwargs)
    review("r3", "d3", risk="medium", resolved=0, passed=0)  # unresolved disclaimer: not passed
    with pytest.raises(sqlite3.IntegrityError):  # one review per draft version
        review("r1-again", "d1")


def test_the_approval_draft_binding_holds_on_insert(migrated: Path) -> None:
    def approval(row_id, *, subject="creative_draft", subject_id="d1", draft="d1", seq=1):
        _sql(migrated, _APPROVAL_SQL, (row_id, subject, subject_id, draft, seq))

    approval("ok-draft")  # a draft decision bound to its draft
    approval("ok-unbound", draft=None, seq=2)  # the link nulled (draft deleted): still valid
    for row_id, kwargs in (
        ("other-draft", {"draft": "d2", "seq": 3}),
        ("brief-subject", {"subject": "recommendation_brief", "seq": 4}),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            approval(row_id, **kwargs)


# --------------------------------------------------------------------------- #
# Downgrade and re-upgrade
# --------------------------------------------------------------------------- #
_CLAIM_SQL = (
    "INSERT INTO claims_library (id, organization_id, workspace_id, brand_id, text, kind,"
    " risk_level, retired_at, created_at, updated_at) VALUES (?, 'o', 'w', 'b', 'claim',"
    " 'blocked', 'low', ?, '2026-11-01 00:00:00', '2026-11-01 00:00:00')"
)


def _refused(db_path: Path, message: str) -> None:
    tables_before = _tables(db_path)
    refused = _alembic(db_path, "downgrade", "-1")
    assert refused.returncode != 0
    assert message in refused.stdout + refused.stderr
    # Nothing changed: same revision, same tables, the widened CHECKs and new columns kept.
    assert _version(db_path) == [HEAD]
    assert _tables(db_path) == tables_before
    assert "draft_id" in _columns(db_path, "approvals")
    assert "retired_at" in _columns(db_path, "claims_library")
    assert "creative_generation" in _in_list(
        _checks(db_path, OVERRIDES)["ck_workspace_capability_override_capability"],
        "capability",
    )


def test_downgrade_refuses_while_an_override_names_the_capability(migrated: Path) -> None:
    _sql(
        migrated,
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES ('ov-creative', 'o', 'w', 'creative_generation', 1)",
    )
    _refused(migrated, "name 'creative_generation'; clear them before downgrading")
    assert _sql(migrated, f"SELECT capability FROM {OVERRIDES}") == [("creative_generation",)]


def test_downgrade_refuses_while_an_approval_decides_on_a_draft(migrated: Path) -> None:
    _sql(migrated, _APPROVAL_SQL, ("kept-draft-decision", "creative_draft", "d1", None, 1))
    _refused(migrated, "approval record(s) decide on a creative_draft")
    assert _sql(migrated, "SELECT id, subject_type FROM approvals") == [
        ("kept-draft-decision", "creative_draft")
    ]


def test_downgrade_refuses_while_a_claim_is_retired(migrated: Path) -> None:
    _sql(migrated, _CLAIM_SQL, ("retired-claim", "2026-11-01 00:00:00"))
    _refused(migrated, "dropping retired_at would re-activate them")
    assert _sql(migrated, "SELECT id, retired_at FROM claims_library") == [
        ("retired-claim", "2026-11-01 00:00:00")
    ]


def test_one_step_down_removes_exactly_this_revision_and_up_restores_it(
    migrated: Path,
) -> None:
    override_indexes = _indexes(migrated, OVERRIDES)
    approval_indexes = _indexes(migrated, "approvals")
    claim_indexes = _indexes(migrated, "claims_library")
    assert "ix_approvals_draft_id" in approval_indexes
    # Rows the downgrade must keep: a brief decision, an active claim, a CF-1 override.
    _sql(
        migrated,
        _APPROVAL_SQL.replace("NULL, ?, 'approved'", "?, ?, 'approved'"),
        ("brief-decision", "recommendation_brief", "brief-1", "brief-1", None, 1),
    )
    _sql(migrated, _CLAIM_SQL, ("active-claim", None))
    _sql(
        migrated,
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES ('ov-briefs', 'o', 'w', 'recommendation_briefs', 1)",
    )

    down = _alembic(migrated, "downgrade", "-1")
    assert down.returncode == 0, down.stdout + down.stderr
    assert _version(migrated) == [CF1]
    assert not NEW_TABLES & _tables(migrated)
    assert "draft_id" not in _columns(migrated, "approvals")
    assert "retired_at" not in _columns(migrated, "claims_library")
    checks = _checks(migrated, "approvals")
    assert "ck_approval_draft_subject" not in checks
    assert _in_list(checks["ck_approval_subject_type"], "subject_type") == {
        "recommendation_brief"
    }
    narrowed = _checks(migrated, OVERRIDES)["ck_workspace_capability_override_capability"]
    assert _in_list(narrowed, "capability") == {
        "connector_rss", "opportunity_feedback", "recommendation_briefs", "scout_scheduling"
    }
    # The batch rebuilds kept every other index and every row.
    assert _indexes(migrated, OVERRIDES) == override_indexes
    assert _indexes(migrated, "approvals") == approval_indexes - {"ix_approvals_draft_id"}
    assert _indexes(migrated, "claims_library") == claim_indexes
    assert _sql(migrated, "SELECT id, brief_id FROM approvals") == [("brief-decision", "brief-1")]
    assert _sql(migrated, "SELECT id FROM claims_library") == [("active-claim",)]
    assert _sql(migrated, f"SELECT id FROM {OVERRIDES}") == [("ov-briefs",)]
    assert {"recommendation_briefs", "approvals"} <= _tables(migrated)  # CF-1: untouched

    up = _alembic(migrated, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert _version(migrated) == [HEAD]
    assert NEW_TABLES <= _tables(migrated)
    assert _indexes(migrated, "approvals") == approval_indexes
    assert _alembic(migrated, "check").returncode == 0
