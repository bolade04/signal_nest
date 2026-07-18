"""Migration-lifecycle test for ``opportunity_feedback`` (Phase 3C, 3C-B).

Exercises the single additive migration against a throwaway SQLite database via the
real Alembic CLI (matching how CI validates migrations, and mirroring
``test_signal_intelligence_migration.py``):

* **upgrade** to head creates the table and its indexes,
* **check** reports no drift between the ORM models and the migrations,
* **downgrade** one step surgically drops only the new table while preserving all
  pre-existing business data,
* **re-upgrade** restores the table.

Separate, ``TEST_POSTGRES_URL``-gated tests assert the two database-enforced
invariants on real PostgreSQL: the reason-polarity check constraint and the
workspace-deletion cascade (retention lifecycle).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# apps/api (holds alembic.ini) — three levels up from this test file.
API_DIR = Path(__file__).resolve().parents[2]
# Revision immediately before the opportunity_feedback migration.
PREV = "b2c3d4e5f6a7"
TABLE = "opportunity_feedback"


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def _tables(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def _indexes(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "migration.db"


def test_upgrade_creates_table_and_indexes(db_path) -> None:
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert TABLE in _tables(db_path)
    indexes = _indexes(db_path)
    assert {
        "ix_opportunity_feedback_organization_id",
        "ix_opportunity_feedback_workspace_id",
        "ix_opportunity_feedback_opportunity_id",
        "ix_opportunity_feedback_intelligence_record_id",
        "ix_opportunity_feedback_submitted_by_user_id",
        "ix_opportunity_feedback_fingerprint",
    }.issubset(indexes)


def test_upgrade_leaves_no_model_drift(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    result = _alembic(db_path, "check")
    # ``alembic check`` exits 0 only when the models match the migrated schema.
    assert result.returncode == 0, result.stdout + result.stderr


def test_single_head(db_path) -> None:
    result = _alembic(db_path, "heads")
    assert result.returncode == 0, result.stderr
    # Exactly one head line — no divergent branches.
    heads = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(heads) == 1, result.stdout
    assert "4945b98229e6" in heads[0]


def test_downgrade_is_surgical_and_preserves_business_data(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (?, ?, ?)",
            ("org-keep-0001", "Keep Co", "keep-co"),
        )
        con.commit()
    finally:
        con.close()

    # Downgrade to the revision immediately before this table was created.
    result = _alembic(db_path, "downgrade", PREV)
    assert result.returncode == 0, result.stderr

    tables = _tables(db_path)
    assert TABLE not in tables  # only the new table is dropped
    assert "organizations" in tables and "signal_intelligence_records" in tables

    con = sqlite3.connect(db_path)
    try:
        kept = con.execute(
            "SELECT name FROM organizations WHERE id = 'org-keep-0001'"
        ).fetchone()
        assert kept is not None and kept[0] == "Keep Co"  # business data preserved
    finally:
        con.close()


def test_reupgrade_restores_table(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    assert _alembic(db_path, "downgrade", PREV).returncode == 0
    assert TABLE not in _tables(db_path)
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    assert TABLE in _tables(db_path)


# --------------------------------------------------------------------------- #
# PostgreSQL-gated: the DB is the real backstop for polarity and retention
# --------------------------------------------------------------------------- #
def _pg_seed_opportunity(factory):  # pragma: no cover - gated helper
    """Seed one full tenant graph on PostgreSQL and return (opp_id, record_id, ws_id)."""
    from app.brands.models import Brand
    from app.intelligence.records import SignalIntelligenceRecord
    from app.opportunities.models import Opportunity
    from app.organizations.models import Organization, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    with factory() as s:
        s.add(Organization(id="org-1", name="Org", slug="org-1"))
        s.add(Workspace(id="ws-1", organization_id="org-1", name="WS", slug="ws-1"))
        s.commit()
    with factory() as s:
        s.add(Brand(id="br-1", organization_id="org-1", workspace_id="ws-1", name="B"))
        s.commit()
    with factory() as s:
        s.add(ScoutRequest(id="sr-1", organization_id="org-1", workspace_id="ws-1",
                           brand_id="br-1", name="scout"))
        s.commit()
    with factory() as s:
        s.add(RawSignal(id="raw-1", organization_id="org-1", workspace_id="ws-1",
                        scout_request_id="sr-1", source_type="rss_news", content="c"))
        s.commit()
    with factory() as s:
        s.add(NormalizedSignal(id="ns-1", organization_id="org-1", workspace_id="ws-1",
                               scout_request_id="sr-1", raw_signal_id="raw-1",
                               source_type="rss_news", excerpt="e", content_hash="h"))
        s.commit()
    with factory() as s:
        s.add(Opportunity(id="opp-1", organization_id="org-1", workspace_id="ws-1",
                          brand_id="br-1", scout_request_id="sr-1", title="T",
                          classification="emerging", decision="monitor"))
        s.commit()
    with factory() as s:
        s.add(SignalIntelligenceRecord(id="sir-1", organization_id="org-1",
                                       workspace_id="ws-1", scout_request_id="sr-1",
                                       normalized_signal_id="ns-1", opportunity_id="opp-1",
                                       analysis_version="3b", scoring_version="3b.1",
                                       fingerprint="fp-1", enricher="deterministic",
                                       accepted=True, classification="emerging"))
        s.commit()
    return "opp-1", "sir-1", "ws-1"


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL invariants test",
)
def test_postgres_reason_polarity_constraint_enforced() -> None:  # pragma: no cover - gated
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.feedback.models import OpportunityFeedback

    url = os.environ["TEST_POSTGRES_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        opp_id, record_id, ws_id = _pg_seed_opportunity(factory)
        # A positive reason with not-useful feedback must be rejected by the DB.
        with factory() as s:  # noqa: SIM117
            s.add(OpportunityFeedback(
                organization_id="org-1", workspace_id=ws_id, opportunity_id=opp_id,
                intelligence_record_id=record_id, is_useful=False,
                reason_code="strong_evidence", analysis_version="3b",
                scoring_version="3b.1", fingerprint="fp-1"))
            with pytest.raises(IntegrityError):
                s.commit()
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL cascade test",
)
def test_postgres_workspace_deletion_cascades_feedback() -> None:  # pragma: no cover - gated
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.feedback.models import OpportunityFeedback
    from app.organizations.models import Workspace

    url = os.environ["TEST_POSTGRES_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        opp_id, record_id, ws_id = _pg_seed_opportunity(factory)
        with factory() as s:
            s.add(OpportunityFeedback(
                organization_id="org-1", workspace_id=ws_id, opportunity_id=opp_id,
                intelligence_record_id=record_id, is_useful=True,
                analysis_version="3b", scoring_version="3b.1", fingerprint="fp-1"))
            s.commit()
        with factory() as s:
            assert s.scalar(select(func.count()).select_from(OpportunityFeedback)) == 1
            s.delete(s.get(Workspace, ws_id))
            s.commit()
        with factory() as s:
            assert s.scalar(select(func.count()).select_from(OpportunityFeedback)) == 0
    finally:
        engine.dispose()
