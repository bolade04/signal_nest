"""Migration-lifecycle test for ``signal_intelligence_records`` (Phase 3B Batch 4A).

Exercises the single additive migration against a throwaway SQLite database via the
real Alembic CLI (matching how CI validates migrations, and mirroring
``test_worker_migration.py``):

* **upgrade** to head creates the table and its indexes,
* **check** reports no drift between the ORM models and the migrations,
* **downgrade** one step surgically drops only the new table while preserving all
  pre-existing business data,
* **re-upgrade** restores the table.

A separate, ``TEST_POSTGRES_URL``-gated test asserts the unique identity constraint
is the real, database-enforced concurrency guard for idempotent persistence.
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
# Revision immediately before the signal_intelligence_records migration.
PREV = "a1b2c3d4e5f6"
TABLE = "signal_intelligence_records"


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
        "ix_signal_intelligence_records_workspace_id",
        "ix_signal_intelligence_records_normalized_signal_id",
        "ix_signal_intelligence_records_opportunity_id",
        "ix_signal_intelligence_records_fingerprint",
    }.issubset(indexes)


def test_upgrade_leaves_no_model_drift(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    result = _alembic(db_path, "check")
    # ``alembic check`` exits 0 only when the models match the migrated schema.
    assert result.returncode == 0, result.stdout + result.stderr


def test_downgrade_is_surgical_and_preserves_business_data(db_path) -> None:
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (?, ?, ?)",
            ("org-keep-0001", "Keep Co", "keep-co"),
        )
        con.execute(
            f"INSERT INTO {TABLE} "
            "(id, organization_id, workspace_id, scout_request_id, normalized_signal_id, "
            "analysis_version, scoring_version, fingerprint, enricher, accepted, "
            "classification, cluster_key, score_total, evidence_count, is_simulated, "
            "facts, inference, relevance, score_components, provenance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sir-0001", "org-keep-0001", "ws-1", "sr-1", "ns-1",
                "3b", "3b.1", "abc123", "deterministic", 1,
                "emerging", "general", 55, 3, 1,
                "{}", "{}", "{}", "{}", "{}",
            ),
        )
        con.commit()
    finally:
        con.close()

    # Downgrade to the revision immediately before this table was created.
    result = _alembic(db_path, "downgrade", PREV)
    assert result.returncode == 0, result.stderr

    tables = _tables(db_path)
    assert TABLE not in tables  # only the new table is dropped
    assert "organizations" in tables and "normalized_signals" in tables

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
# PostgreSQL-gated: the unique constraint is the real concurrency guard
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL constraint test",
)
def test_postgres_unique_constraint_enforces_idempotency() -> None:  # pragma: no cover - gated
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.intelligence.analyze import analyze_signal
    from app.intelligence.models import AnalysisInput
    from app.intelligence.persistence import persist_intelligence
    from app.intelligence.records import SignalIntelligenceRecord
    from app.scoring.types import BusinessContext

    url = os.environ["TEST_POSTGRES_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    # PostgreSQL enforces the record's foreign keys, so build the full parent chain.
    from app.brands.models import Brand
    from app.organizations.models import Organization, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    with factory() as s:
        s.add(Organization(id="org-1", name="Org One", slug="org-one"))
        s.add(Workspace(id="ws-1", organization_id="org-1", name="WS", slug="ws"))
        s.add(Brand(id="br-1", organization_id="org-1", workspace_id="ws-1",
                    name="B", industry="coffee", business_type="cafe"))
        s.add(ScoutRequest(id="sr-1", organization_id="org-1", workspace_id="ws-1",
                           brand_id="br-1", name="scout"))
        s.add(RawSignal(id="raw-1", organization_id="org-1", workspace_id="ws-1",
                        scout_request_id="sr-1", source_type="rss_news", content="coffee"))
        s.add(NormalizedSignal(id="ns-1", organization_id="org-1", workspace_id="ws-1",
                               scout_request_id="sr-1", raw_signal_id="raw-1",
                               source_type="rss_news", excerpt="coffee delivery is slow",
                               content_hash="h"))
        s.commit()

    cand = analyze_signal(
        AnalysisInput(content="coffee delivery is so slow and cold", source_type="rss_news",
                      market="London", distinct_source_types=3, duplicate_count=5),
        BusinessContext(keywords=["coffee", "delivery"], pain_points=["slow delivery"],
                        audiences=["urban"], competitors=["rival"]),
    )

    scope = dict(organization_id="org-1", workspace_id="ws-1",
                 scout_request_id="sr-1", normalized_signal_id="ns-1")
    try:
        # Two independent sessions persist the same identity; the DB unique
        # constraint must converge them onto exactly one row.
        with factory() as a:
            first = persist_intelligence(a, candidate=cand, **scope)
            a.commit()
            first_id = first.id
        with factory() as b:
            second = persist_intelligence(b, candidate=cand, **scope)
            b.commit()
            second_id = second.id
        assert first_id == second_id
        with factory() as c:
            assert c.scalar(select(func.count()).select_from(SignalIntelligenceRecord)) == 1
    finally:
        engine.dispose()
