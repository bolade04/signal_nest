"""Persistence tests for evidence-backed opportunity intelligence (Batch 4A).

Cover the four disciplines the batch depends on:

* **Bounded serialization** that keeps facts and inference separate on disk.
* **Concurrency-safe idempotency** — the DB unique constraint (not an app-level
  check) is the final guard; a duplicate insert returns the existing row and leaves
  the session usable.
* **Isolation** — records stay bound to their tenant scope and are only ever
  linked to an opportunity within the same workspace (four-market seed).
* **Fail-open ingestion** — a persistence fault can never break the pipeline.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import DecisionAction, OpportunityClassification, RejectionReason
from app.db.models import Base
from app.intelligence.analyze import analyze_signal
from app.intelligence.models import (
    AnalysisInput,
    BusinessRelevance,
    EvidenceSpan,
    ExtractedIntelligence,
    InferredAttribute,
    IntelligenceScore,
    OpportunityCandidate,
    SignalFacts,
)
from app.intelligence.persistence import (
    ANALYSIS_VERSION,
    attach_opportunity,
    persist_intelligence,
    serialize_candidate,
)
from app.intelligence.records import SignalIntelligenceRecord
from app.scoring.types import BusinessContext


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ctx() -> BusinessContext:
    return BusinessContext(
        keywords=["coffee", "delivery"],
        pain_points=["slow delivery"],
        audiences=["urban households"],
        competitors=["rival brew"],
    )


def _real_candidate() -> OpportunityCandidate:
    return analyze_signal(
        AnalysisInput(
            content="coffee delivery is so slow and my espresso is cold",
            source_type="rss_news",
            market="London",
            distinct_source_types=3,
            duplicate_count=5,
            engagement=60,
        ),
        _ctx(),
    )


def _oversized_candidate() -> OpportunityCandidate:
    """A candidate engineered to exceed every payload bound."""
    spans = tuple(EvidenceSpan(0, 6, "coffee" * 200, "lexicon:test") for _ in range(80))
    attr = InferredAttribute("complaint", 0.8, "lexicon:complaint", spans)
    intel = ExtractedIntelligence(
        signal_type=attr,
        pain_point_dna=attr,
        sentiment=attr,
        has_buying_intent=False,
        has_competitor_dissatisfaction=False,
        intent_evidence=spans,
    )
    excerpt = "coffee " * 1000
    facts = SignalFacts("rss_news", "London", None, "en", 1.0, len(excerpt), 1000, excerpt)
    rel = BusinessRelevance(
        score=70,
        below_action_floor=False,
        keyword_hits=tuple(f"k{i}" for i in range(200)),
    )
    score = IntelligenceScore(
        "3b.1", 55, OpportunityClassification.EMERGING,
        {"source_quality": {"weight": 15, "value": 0.5, "points": 7.5}},
    )
    return OpportunityCandidate(
        facts=facts,
        intelligence=intel,
        relevance=rel,
        score=score,
        accepted=True,
        rationale="x" * 5000,
        decision=DecisionAction.MONITOR,
        cluster_key="general",
        evidence_count=len(spans),
    )


@pytest.fixture()
def db(tmp_path) -> Session:
    engine = create_engine(
        f"sqlite:///{tmp_path/'intel.db'}",
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


# --------------------------------------------------------------------------- #
# Serialization (no DB)
# --------------------------------------------------------------------------- #
class TestSerialization:
    def test_facts_and_inference_stay_separate(self):
        payload = serialize_candidate(_real_candidate())
        assert set(payload) == {"facts", "inference", "relevance", "score_components"}
        # Facts carry only observed fields; the inferred signal type is never in facts.
        assert "excerpt" in payload["facts"]
        assert "signal_type" not in payload["facts"]
        assert "signal_type" in payload["inference"]
        assert payload["score_components"]["version"] == "3b.1"

    def test_payloads_are_bounded(self):
        payload = serialize_candidate(_oversized_candidate())
        # Evidence lists capped in count, quotes capped in length.
        assert len(payload["inference"]["intent_evidence"]) <= 32
        for span in payload["inference"]["intent_evidence"]:
            assert len(span["quote"]) <= 400
        assert len(payload["inference"]["signal_type"]["evidence"]) <= 32
        # Excerpt and hit lists are clipped.
        assert len(payload["facts"]["excerpt"]) <= 2000
        assert len(payload["relevance"]["keyword_hits"]) <= 64


# --------------------------------------------------------------------------- #
# Repository idempotency + savepoint isolation
# --------------------------------------------------------------------------- #
class TestRepository:
    def _persist(self, db, cand, **over):
        scope = dict(
            organization_id="org-1",
            workspace_id="ws-1",
            scout_request_id="sr-1",
            normalized_signal_id="ns-1",
        )
        scope.update(over)
        return persist_intelligence(db, candidate=cand, **scope)

    def test_insert_persists_versioned_row(self, db):
        rec = self._persist(db, _real_candidate())
        db.commit()
        assert rec.scoring_version == "3b.1"
        assert rec.analysis_version == ANALYSIS_VERSION
        assert rec.enricher == "deterministic"
        assert db.scalar(select(func.count()).select_from(SignalIntelligenceRecord)) == 1

    def test_persist_is_idempotent(self, db):
        cand = _real_candidate()
        first = self._persist(db, cand)
        db.commit()
        second = self._persist(db, cand)
        db.commit()
        # Same identity → one row; the second call returns the existing row.
        assert first.id == second.id
        assert db.scalar(select(func.count()).select_from(SignalIntelligenceRecord)) == 1

    def test_session_usable_after_duplicate(self, db):
        cand = _real_candidate()
        self._persist(db, cand)
        db.commit()
        # A duplicate insert rolls back only the savepoint; the outer session must
        # remain usable for a subsequent distinct insert + commit.
        self._persist(db, cand)
        other = self._persist(db, cand, normalized_signal_id="ns-2")
        db.commit()
        assert other.normalized_signal_id == "ns-2"
        assert db.scalar(select(func.count()).select_from(SignalIntelligenceRecord)) == 2

    def test_attach_opportunity_is_workspace_scoped(self, db):
        cand = _real_candidate()
        self._persist(db, cand, workspace_id="ws-A", normalized_signal_id="ns-A")
        self._persist(db, cand, workspace_id="ws-B", normalized_signal_id="ns-B")
        db.commit()
        # Linking for ws-A must not touch ws-B's row even for the same signal id set.
        linked = attach_opportunity(
            db,
            workspace_id="ws-A",
            opportunity_id="opp-A",
            normalized_signal_ids=["ns-A", "ns-B"],
        )
        db.commit()
        assert linked == 1
        a = db.scalar(
            select(SignalIntelligenceRecord).where(
                SignalIntelligenceRecord.workspace_id == "ws-A"
            )
        )
        b = db.scalar(
            select(SignalIntelligenceRecord).where(
                SignalIntelligenceRecord.workspace_id == "ws-B"
            )
        )
        assert a.opportunity_id == "opp-A"
        assert b.opportunity_id is None


# --------------------------------------------------------------------------- #
# Fail-open ingestion
# --------------------------------------------------------------------------- #
def test_pipeline_persist_wrapper_is_fail_open(monkeypatch):
    """A persistence fault is swallowed so ingestion is never broken."""
    from app.jobs import pipeline

    def _boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(pipeline, "persist_intelligence", _boom)

    class _Req:
        id = "sr-1"
        organization_id = "org-1"
        workspace_id = "ws-1"
        location_id = None

    class _Norm:
        id = "ns-1"

    # Must not raise.
    pipeline._persist_intelligence_record(object(), _Req(), _Norm(), _real_candidate())


# --------------------------------------------------------------------------- #
# Rejected candidates are persisted (not exposed, but stored)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Pipeline integration + four-market isolation (real seed, four markets)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """Run the deterministic four-market demo seed against a throwaway DB."""
    from app.db import seed as seed_mod

    engine = create_engine(
        f"sqlite:///{tmp_path/'seeded.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(seed_mod, "SessionLocal", factory)
    seed_mod.seed(reset=True)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


class TestPipelineIntegration:
    def test_records_persisted_per_signal_and_versioned(self, seeded):
        from app.signals.models import NormalizedSignal

        n_signals = seeded.scalar(select(func.count()).select_from(NormalizedSignal))
        n_records = seeded.scalar(select(func.count()).select_from(SignalIntelligenceRecord))
        # One intelligence row per normalized signal (accepted and rejected alike).
        assert n_records == n_signals > 0
        for rec in seeded.scalars(select(SignalIntelligenceRecord)):
            assert rec.scoring_version == "3b.1"
            assert rec.analysis_version == ANALYSIS_VERSION
            assert rec.is_simulated is True
            # Exactly one of decision / rejection is set.
            assert (rec.decision is None) != (rec.rejection_reason is None)

    def test_accepted_records_link_to_an_opportunity(self, seeded):
        linked = seeded.scalars(
            select(SignalIntelligenceRecord).where(
                SignalIntelligenceRecord.opportunity_id.is_not(None)
            )
        ).all()
        assert linked, "expected some intelligence rows linked to opportunities"

    def test_four_market_isolation_no_cross_links(self, seeded):
        from app.opportunities.models import Opportunity
        from app.signals.models import NormalizedSignal

        for rec in seeded.scalars(select(SignalIntelligenceRecord)):
            signal = seeded.get(NormalizedSignal, rec.normalized_signal_id)
            # The record's scope always matches its own normalized signal's scope.
            assert signal is not None
            assert rec.workspace_id == signal.workspace_id
            assert rec.scout_request_id == signal.scout_request_id
            assert rec.organization_id == signal.organization_id
            if rec.opportunity_id is not None:
                opp = seeded.get(Opportunity, rec.opportunity_id)
                # A link never crosses workspace, scout request or market.
                assert opp.workspace_id == rec.workspace_id
                assert opp.scout_request_id == rec.scout_request_id

    def test_advisory_annotation_preserved_and_consistent(self, seeded):
        from app.signals.models import NormalizedSignal

        for signal in seeded.scalars(select(NormalizedSignal)):
            annotation = signal.ingest_metadata.get("intelligence")
            assert annotation is not None
            assert "error" not in annotation  # analysis succeeded for fixtures
            rec = seeded.scalar(
                select(SignalIntelligenceRecord).where(
                    SignalIntelligenceRecord.normalized_signal_id == signal.id
                )
            )
            # The persisted record derives from the *same* candidate as the annotation.
            assert rec.scoring_version == annotation["score"]["version"]
            assert rec.score_total == annotation["score"]["total"]
            assert rec.cluster_key == annotation["cluster_key"]


def test_rejected_candidate_is_persisted_with_reason(db):
    rejected = analyze_signal(
        AnalysisInput(content="click here for free money now!!!", source_type="rss_news",
                      market="London"),
        _ctx(),
    )
    assert rejected.rejection == RejectionReason.NOISE
    rec = persist_intelligence(
        db,
        organization_id="org-1",
        workspace_id="ws-1",
        scout_request_id="sr-1",
        normalized_signal_id="ns-noise",
        candidate=rejected,
    )
    db.commit()
    assert rec.accepted is False
    assert rec.rejection_reason == RejectionReason.NOISE.value
    assert rec.decision is None
    assert rec.opportunity_id is None
