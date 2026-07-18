"""3C-C: the append-only feedback capture holds under real concurrency.

Feedback is deliberately append-only: there is no per-target uniqueness constraint, so
many concurrent judgements on the *same* opportunity + intelligence record must all
persist as distinct rows — no lost write, no spurious conflict, no interference. This is
the dedicated concurrency proof the 3C-B foundation observed was missing.

``FOR UPDATE``-style blocking and true row contention only behave faithfully on
PostgreSQL, so these tests are gated on ``TEST_POSTGRES_URL`` and skipped otherwise.
Each worker runs on its own session/transaction against one shared target.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.enums import FeedbackReason
from app.db.base import Base
from app.feedback.models import OpportunityFeedback
from app.feedback.service import create_feedback
from app.intelligence.records import SignalIntelligenceRecord
from app.opportunities.models import Opportunity

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL feedback-concurrency test",
)

_ORG, _WS, _BRAND, _SR = "org-fbc", "ws-fbc", "brand-fbc", "sr-fbc"
_OPP, _RECORD = "opp-fbc", "sir-fbc"


def _fresh_engine():  # pragma: no cover - gated on live PG
    url = os.environ["TEST_POSTGRES_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine


def _seed(factory) -> None:  # pragma: no cover - gated on live PG
    from app.brands.models import Brand
    from app.organizations.models import Organization, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    with factory() as s:
        s.add(Organization(id=_ORG, name="FBC Org", slug="fbc-org"))
        s.add(Workspace(id=_WS, organization_id=_ORG, name="FBC WS", slug="fbc-ws"))
        s.flush()
        s.add(Brand(id=_BRAND, organization_id=_ORG, workspace_id=_WS, name="FBC Brand"))
        s.flush()
        s.add(ScoutRequest(id=_SR, organization_id=_ORG, workspace_id=_WS,
                           brand_id=_BRAND, name="scout"))
        s.flush()
        s.add(RawSignal(id="raw-fbc", organization_id=_ORG, workspace_id=_WS,
                        scout_request_id=_SR, source_type="rss_news", content="c"))
        s.flush()
        s.add(NormalizedSignal(id="ns-fbc", organization_id=_ORG, workspace_id=_WS,
                               scout_request_id=_SR, raw_signal_id="raw-fbc",
                               source_type="rss_news", excerpt="e", content_hash="h"))
        s.flush()
        s.add(Opportunity(id=_OPP, organization_id=_ORG, workspace_id=_WS, brand_id=_BRAND,
                          scout_request_id=_SR, title="T", classification="emerging",
                          decision="monitor", opportunity_score=50))
        s.flush()
        s.add(SignalIntelligenceRecord(
            id=_RECORD, organization_id=_ORG, workspace_id=_WS, scout_request_id=_SR,
            normalized_signal_id="ns-fbc", opportunity_id=_OPP, analysis_version="3c",
            scoring_version="3c.1", fingerprint="fp-fbc", enricher="deterministic",
            accepted=True, classification="emerging"))
        s.commit()


def _feedback_count(factory) -> int:  # pragma: no cover - gated on live PG
    with factory() as s:
        return int(s.scalar(select(func.count()).select_from(OpportunityFeedback)) or 0)


def test_concurrent_captures_all_persist():  # pragma: no cover - gated on live PG
    engine = _fresh_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        _seed(factory)
        n = 24  # far more than any incidental interleave window

        def _capture(i: int) -> str:
            s = factory()
            try:
                opp = s.get(Opportunity, _OPP)
                record = s.get(SignalIntelligenceRecord, _RECORD)
                # Alternate polarity so we also exercise the reason/polarity path.
                if i % 2 == 0:
                    create_feedback(s, opportunity=opp, intelligence_record=record,
                                    is_useful=True, reason=FeedbackReason.USEFUL_INSIGHT)
                else:
                    create_feedback(s, opportunity=opp, intelligence_record=record,
                                    is_useful=False, reason=FeedbackReason.OUTDATED)
                s.commit()
                return "ok"
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=n) as pool:
            outcomes = list(pool.map(_capture, range(n)))

        # Append-only: every concurrent judgement persists as its own row.
        assert outcomes.count("ok") == n
        assert _feedback_count(factory) == n
    finally:
        engine.dispose()
