"""Repository-selection tests for the Batch 4B read query ``get_latest_for_opportunity``.

These prove the deterministic *latest eligible* rule (§17.21.5) directly against the
DB, using bare :class:`SignalIntelligenceRecord` rows (SQLite does not enforce the FKs,
matching the Batch 4A persistence tests). The rule under test:

1. scope by ``workspace_id`` **and** ``opportunity_id`` (both mandatory);
2. only ``accepted == True`` rows are eligible (rejected/suppressed excluded);
3. order ``score_total DESC, created_at DESC, id ASC`` and take the first row;
4. no eligible row → ``None``; the query never mutates.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.intelligence.persistence import get_latest_for_opportunity
from app.intelligence.records import SignalIntelligenceRecord

_BASE_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path) -> Session:
    engine = create_engine(
        f"sqlite:///{tmp_path/'read.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


_SEQ = {"n": 0}


def _rec(db: Session, **over) -> SignalIntelligenceRecord:
    """Insert one record with sane, unique defaults; ``over`` customizes any field."""
    _SEQ["n"] += 1
    n = _SEQ["n"]
    defaults = dict(
        organization_id="org-1",
        workspace_id="ws-1",
        scout_request_id="sr-1",
        normalized_signal_id=f"ns-{n}",
        opportunity_id="opp-1",
        analysis_version="3b",
        scoring_version="3b.1",
        fingerprint=f"fp-{n}",
        enricher="deterministic",
        accepted=True,
        classification="emerging",
        cluster_key="general",
        score_total=50,
        evidence_count=1,
        is_simulated=True,
        facts={},
        inference={},
        relevance={},
        score_components={},
        provenance={},
    )
    defaults.update(over)
    rec = SignalIntelligenceRecord(**defaults)
    db.add(rec)
    db.commit()
    return rec


def test_returns_highest_score_total(db):
    _rec(db, score_total=40)
    top = _rec(db, score_total=90)
    _rec(db, score_total=60)
    got = get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1")
    assert got is not None and got.id == top.id and got.score_total == 90


def test_both_scope_args_required(db):
    _rec(db, workspace_id="ws-1", opportunity_id="opp-1")
    # Wrong workspace or wrong opportunity → no match.
    assert get_latest_for_opportunity(db, workspace_id="ws-2", opportunity_id="opp-1") is None
    assert get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-2") is None
    assert get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1") is not None


def test_foreign_workspace_row_excluded(db):
    _rec(db, workspace_id="ws-A", opportunity_id="opp-1", score_total=99)
    mine = _rec(db, workspace_id="ws-B", opportunity_id="opp-1", score_total=10)
    got = get_latest_for_opportunity(db, workspace_id="ws-B", opportunity_id="opp-1")
    # The higher-scoring ws-A row must never bleed into ws-B's result.
    assert got is not None and got.id == mine.id


def test_no_record_returns_none(db):
    assert get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="nope") is None


def test_rejected_records_excluded(db):
    _rec(db, accepted=False, score_total=99, rejection_reason="noise")
    accepted = _rec(db, accepted=True, score_total=10)
    got = get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1")
    # A higher-scored rejected row is never eligible; the accepted row wins.
    assert got is not None and got.id == accepted.id and got.accepted is True


def test_only_rejected_returns_none(db):
    _rec(db, accepted=False, score_total=80, rejection_reason="weak_signal")
    assert get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1") is None


def test_score_tie_broken_by_newest_created_at(db):
    _rec(db, score_total=70, created_at=_BASE_TS)
    newer = _rec(db, score_total=70, created_at=_BASE_TS.replace(day=2))
    got = get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1")
    assert got is not None and got.id == newer.id


def test_timestamp_tie_broken_by_id_asc(db):
    _rec(db, id="rec-b", score_total=70, created_at=_BASE_TS)
    _rec(db, id="rec-a", score_total=70, created_at=_BASE_TS)
    got = get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1")
    # Total order: identical score + timestamp resolved by id ASC (byte-stable).
    assert got is not None and got.id == "rec-a"


def test_other_opportunity_record_excluded(db):
    _rec(db, opportunity_id="opp-1", score_total=10)
    other = _rec(db, opportunity_id="opp-2", score_total=99)
    got = get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1")
    assert got is not None and got.id != other.id and got.opportunity_id == "opp-1"


def test_unlinked_record_excluded(db):
    # An accepted row not yet attached to an opportunity is never returned.
    _rec(db, opportunity_id=None, score_total=99)
    assert get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1") is None


def test_same_normalized_signal_across_markets_isolated(db):
    # One physical signal id reused across two tenants/markets must never cross over.
    a = _rec(db, workspace_id="ws-dallas", opportunity_id="opp-dallas",
             normalized_signal_id="shared-ns", fingerprint="fp-a")
    b = _rec(db, workspace_id="ws-lagos", opportunity_id="opp-lagos",
             normalized_signal_id="shared-ns", fingerprint="fp-b")
    got_a = get_latest_for_opportunity(db, workspace_id="ws-dallas", opportunity_id="opp-dallas")
    got_b = get_latest_for_opportunity(db, workspace_id="ws-lagos", opportunity_id="opp-lagos")
    assert got_a is not None and got_a.id == a.id
    assert got_b is not None and got_b.id == b.id


def test_query_is_read_only_and_deterministic(db):
    _rec(db, score_total=40)
    _rec(db, score_total=90)
    before = db.scalar(select(func.count()).select_from(SignalIntelligenceRecord))
    first = get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1")
    second = get_latest_for_opportunity(db, workspace_id="ws-1", opportunity_id="opp-1")
    after = db.scalar(select(func.count()).select_from(SignalIntelligenceRecord))
    # Repeated reads are identical and the row count is untouched.
    assert first is not None and second is not None and first.id == second.id
    assert before == after
