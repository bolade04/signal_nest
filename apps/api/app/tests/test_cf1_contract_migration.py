"""Project Phase 5 CF-1 (W21): the contracts-freeze migration ``f3a9c1e7b2d5``.

Through the real Alembic CLI on a throwaway SQLite database (as CI validates migrations):

* the head is single and follows ``e7b3c5a9d1f4``; ``upgrade head`` then ``alembic check``
  reports no drift between the CF-1 models and the migration (Phase 5 plan §6.2.0);
* **the migrated database's CHECKs equal their Python authorities, in both directions** — the
  capability set (``persisted_values()``), the approval decision / subject-type / decided-role
  vocabularies and the reason-code **polarity partition** (decision → codes, not a flat set,
  so a swapped or dropped limb fails). Alembic never compares CHECKs, so this is the only
  gate on them (a CF-1-scoped form of the plan's ``PP5-CHK-001`` control, §9.2). Discovery
  runs too: every CHECK on the three tables must be one this module knows;
* the CHECKs hold on insert (capability widened; polarity; the brief/subject binding);
* the downgrade refuses, before any DDL, while an override names ``recommendation_briefs``;
  once it is cleared, one step down removes exactly this revision (both tables gone, the
  capability CHECK narrowed, the override table's indexes intact) and ``upgrade`` restores it.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.approvals.models import Approval
from app.approvals.reasons import REASONS_BY_DECISION, ApprovalReason
from app.briefs.models import RecommendationBrief
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import persisted_values
from app.core.enums import ApprovalDecision, ApprovalSubjectType, Role

API_DIR = Path(__file__).resolve().parents[2]
HEAD = "f3a9c1e7b2d5"
PREV = "e7b3c5a9d1f4"
OVERRIDES = "workspace_capability_overrides"

#: Every CHECK the CF-1 tables (and the widened override table) may carry.
EXPECTED_CHECKS = {
    "approvals": {
        "ck_approval_brief_subject",
        "ck_approval_decided_role",
        "ck_approval_decision",
        "ck_approval_decision_seq",
        "ck_approval_reason_polarity",
        "ck_approval_subject_type",
        "ck_approval_subject_version",
    },
    "recommendation_briefs": {"ck_recommendation_brief_version"},
    OVERRIDES: {"ck_workspace_capability_override_capability"},
}


# --------------------------------------------------------------------------- #
# Alembic plumbing (as the sibling migration tests do)
# --------------------------------------------------------------------------- #
def _confirmation(db_path: Path, target: str) -> list[str]:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app.db.downgrade_guard import (
        chain_identity,
        confirmation_token,
        live_identity,
        resolve_downgrade,
    )

    url = f"sqlite:///{db_path}"
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            heads = MigrationContext.configure(conn).get_current_heads()
            identity = live_identity(conn, url)
    finally:
        engine.dispose()
    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    resolved, chain = resolve_downgrade(script, heads[0], target)
    token = confirmation_token(
        database_url=url,
        live_identity=identity,
        source_revision=heads[0],
        requested_target=target,
        resolved_destination=resolved,
        chain_identity=chain_identity(script, chain),
    )
    return ["-x", f"confirm={token}"]


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    confirm = _confirmation(db_path, args[1]) if args[0] == "downgrade" else []
    return subprocess.run(
        [sys.executable, "-m", "alembic", *confirm, *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def _sql(db_path: Path, statement: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(statement, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _checks(db_path: Path, table: str) -> dict[str, str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return {c["name"]: c["sqltext"] for c in inspect(engine).get_check_constraints(table)}
    finally:
        engine.dispose()


def _in_list(sqltext: str, column: str) -> set[str]:
    match = re.fullmatch(rf"\s*{column} IN \(([^)]*)\)\s*", sqltext)
    assert match, sqltext
    return set(re.findall(r"'([^']*)'", match.group(1)))


def _polarity(sqltext: str) -> dict[str, frozenset[str]]:
    """decision → codes, read from the CHECK text. The NULL limb must be present."""
    assert sqltext.strip().startswith("reason_code IS NULL OR "), sqltext
    limbs = re.findall(r"\(decision = '([^']+)' AND reason_code IN \(([^)]*)\)\)", sqltext)
    partition = {d: frozenset(re.findall(r"'([^']*)'", codes)) for d, codes in limbs}
    assert len(partition) == len(limbs), "a decision appears in two limbs"
    return partition


def _authority_polarity() -> dict[str, frozenset[str]]:
    return {d.value: frozenset(r.value for r in codes) for d, codes in REASONS_BY_DECISION.items()}


@pytest.fixture()
def migrated(tmp_path) -> Path:
    db_path = tmp_path / "cf1.db"
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return db_path


# --------------------------------------------------------------------------- #
# Head, drift, and the CHECKs against their Python authorities
# --------------------------------------------------------------------------- #
def test_the_head_is_single_and_follows_the_finding_identity() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    assert [r.revision for r in script.get_revisions("heads")] == [HEAD]
    assert script.get_revision(HEAD).down_revision == PREV


def test_upgrade_leaves_no_model_drift(migrated: Path) -> None:
    result = _alembic(migrated, "check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert {"recommendation_briefs", "approvals"} <= {
        r[0] for r in _sql(migrated, "SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_every_check_is_known(migrated: Path) -> None:
    """Discovery: a CHECK the database carries but this module does not know fails here."""
    for table, names in EXPECTED_CHECKS.items():
        assert set(_checks(migrated, table)) == names, table
    # And the models declare the same names (the model side of the same contract).
    for model in (Approval, RecommendationBrief, WorkspaceCapabilityOverride):
        declared = {
            c.name for c in model.__table__.constraints if c.__class__.__name__ == "CheckConstraint"
        }
        assert declared == EXPECTED_CHECKS[model.__tablename__]


def test_capability_check_equals_the_registry(migrated: Path) -> None:
    sqltext = _checks(migrated, OVERRIDES)["ck_workspace_capability_override_capability"]
    in_db, authority = _in_list(sqltext, "capability"), set(persisted_values())
    assert in_db - authority == set(), "unauthorized_in_db"
    assert authority - in_db == set(), "missing_from_db"
    assert "recommendation_briefs" in in_db


def test_approval_vocabulary_checks_equal_their_enums(migrated: Path) -> None:
    checks = _checks(migrated, "approvals")
    for name, column, enum in (
        ("ck_approval_decision", "decision", ApprovalDecision),
        ("ck_approval_subject_type", "subject_type", ApprovalSubjectType),
        ("ck_approval_decided_role", "decided_role", Role),
    ):
        in_db, authority = _in_list(checks[name], column), {m.value for m in enum}
        assert (in_db - authority, authority - in_db) == (set(), set()), name


def test_reason_polarity_check_equals_the_partition(migrated: Path) -> None:
    in_db = _polarity(_checks(migrated, "approvals")["ck_approval_reason_polarity"])
    assert in_db == _authority_polarity()
    # The authority itself is a partition of the vocabulary: disjoint and complete.
    sets = list(REASONS_BY_DECISION.values())
    assert set(REASONS_BY_DECISION) == set(ApprovalDecision)
    assert sum(len(s) for s in sets) == len(ApprovalReason)
    assert frozenset().union(*sets) == frozenset(ApprovalReason)


def test_the_polarity_comparison_catches_a_swapped_limb() -> None:
    """Negative control: swapping two codes between decisions keeps the flat literal set
    identical, and the partition comparison still fails."""
    authority = _authority_polarity()
    swapped = dict(authority)
    swapped["approved"] = (authority["approved"] - {"approved_other"}) | {"rejected_other"}
    swapped["rejected"] = (authority["rejected"] - {"rejected_other"}) | {"approved_other"}
    flat = frozenset().union(*authority.values())
    assert frozenset().union(*swapped.values()) == flat
    assert swapped != authority


# --------------------------------------------------------------------------- #
# The CHECKs hold on insert
# --------------------------------------------------------------------------- #
_APPROVAL_SQL = (
    "INSERT INTO approvals (id, organization_id, workspace_id, subject_type, subject_id,"
    " subject_version, brief_id, decision, decision_seq, reason_code, decided_by_snapshot,"
    " decided_role, is_simulated) VALUES (?, 'o', 'w', ?, 's1', 1, ?, ?, ?, ?, 'snap', ?, 0)"
)


def test_the_checks_hold_on_insert(migrated: Path) -> None:
    def approval(row_id, *, subject="recommendation_brief", brief=None, decision="approved",
                 seq=1, reason=None, role="reviewer"):
        _sql(migrated, _APPROVAL_SQL, (row_id, subject, brief, decision, seq, reason, role))

    approval("ok-1", reason="evidence_sufficient")
    approval("ok-2", decision="revoked", seq=2, reason="superseded")
    approval("ok-3", decision="rejected", seq=3, reason=None)
    for row_id, kwargs in (
        ("bad-polarity", {"decision": "approved", "reason": "inaccurate", "seq": 4}),
        ("bad-reason", {"reason": "not_a_code", "seq": 5}),
        ("bad-decision", {"decision": "maybe", "seq": 6}),
        ("bad-subject", {"subject": "creative_draft", "seq": 7}),
        ("bad-role", {"role": "superuser", "seq": 8}),
        ("bad-binding", {"brief": "another-brief", "seq": 9}),
        ("bad-seq", {"seq": 0}),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            approval(row_id, **kwargs)
    # One decision_seq per subject version.
    with pytest.raises(sqlite3.IntegrityError):
        approval("dup-seq", seq=1)

    insert_override = (
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES (?, 'o', 'w', ?, 1)"
    )
    _sql(migrated, insert_override, ("ov-1", "recommendation_briefs"))
    with pytest.raises(sqlite3.IntegrityError):
        _sql(migrated, insert_override, ("ov-2", "not_a_capability"))


# --------------------------------------------------------------------------- #
# Downgrade and re-upgrade
# --------------------------------------------------------------------------- #
def _version(db_path: Path) -> list[str]:
    return [r[0] for r in _sql(db_path, "SELECT version_num FROM alembic_version")]


def _tables(db_path: Path) -> set[str]:
    return {r[0] for r in _sql(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(db_path: Path, table: str) -> set[str]:
    return {
        r[0]
        for r in _sql(
            db_path,
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        )
    }


def test_downgrade_refuses_while_an_override_names_the_capability(migrated: Path) -> None:
    _sql(
        migrated,
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES ('ov-briefs', 'o', 'w', 'recommendation_briefs', 1)",
    )
    tables_before = _tables(migrated)
    refused = _alembic(migrated, "downgrade", "-1")
    assert refused.returncode != 0
    assert "clear them before downgrading" in refused.stdout + refused.stderr
    # Nothing changed: same revision, same tables, the override still there.
    assert _version(migrated) == [HEAD]
    assert _tables(migrated) == tables_before
    assert _sql(migrated, f"SELECT capability FROM {OVERRIDES}") == [("recommendation_briefs",)]


def test_one_step_down_removes_exactly_this_revision_and_up_restores_it(
    migrated: Path,
) -> None:
    override_indexes = _indexes(migrated, OVERRIDES)
    assert len(override_indexes) == 3
    _sql(
        migrated,
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES ('ov-feedback', 'o', 'w', 'opportunity_feedback', 1)",
    )

    down = _alembic(migrated, "downgrade", "-1")
    assert down.returncode == 0, down.stdout + down.stderr
    assert _version(migrated) == [PREV]
    assert not {"recommendation_briefs", "approvals"} & _tables(migrated)
    narrowed = _checks(migrated, OVERRIDES)["ck_workspace_capability_override_capability"]
    assert _in_list(narrowed, "capability") == {
        "connector_rss", "opportunity_feedback", "scout_scheduling"
    }
    assert _indexes(migrated, OVERRIDES) == override_indexes  # the rebuild kept them
    assert _sql(migrated, f"SELECT id FROM {OVERRIDES}") == [("ov-feedback",)]  # data kept
    assert "opportunity_observations" in _tables(migrated)  # the revision below: untouched

    up = _alembic(migrated, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert _version(migrated) == [HEAD]
    assert {"recommendation_briefs", "approvals"} <= _tables(migrated)
    assert _alembic(migrated, "check").returncode == 0
