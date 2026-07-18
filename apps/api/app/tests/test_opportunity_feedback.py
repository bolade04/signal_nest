"""3C-B: opportunity-feedback persistence foundation — model, service, isolation.

Exercises the dark-deployed feedback capture seam directly (no HTTP): the
:class:`~app.feedback.models.OpportunityFeedback` model and
:func:`~app.feedback.service.create_feedback`. Everything runs against a throwaway
SQLite database with ``PRAGMA foreign_keys=ON`` so the workspace-deletion cascade is
really exercised, and a full four-market graph (Dallas / London / Lagos / Nairobi)
proves cross-market isolation.

Sessions roll back per test, so nothing leaks between cases. The whole subsystem is
capture-only: these tests assert that persisting feedback never rescoring anything —
no scoring record is written and the opportunity score never moves.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.enums import FeedbackReason
from app.core.errors import ValidationDomainError
from app.db.models import Base
from app.feedback.models import OpportunityFeedback
from app.feedback.service import create_feedback
from app.intelligence.records import SignalIntelligenceRecord
from app.opportunities.models import Opportunity

_MARKETS = ("dallas", "london", "lagos", "nairobi")


@dataclass
class Market:
    """The handful of rows a feedback test needs for one isolated market."""

    organization_id: str
    workspace_id: str
    opportunity: Opportunity
    record: SignalIntelligenceRecord
    user_id: str


# --------------------------------------------------------------------------- #
# Harness — SQLite with real FK enforcement so CASCADE actually fires
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("opportunity_feedback")
    engine = create_engine(
        f"sqlite:///{tmp/'feedback.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def s(factory):
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _seed_market(
    s,
    key: str,
    *,
    analysis_version: str = "3b",
    scoring_version: str = "3b.1",
    fingerprint: str | None = None,
    opportunity_score: int = 50,
) -> Market:
    """Create a full, independent tenant graph for one market and return its handles.

    Builds organization → workspace → brand → scout request → raw/normalized signal →
    opportunity → intelligence record → user, all namespaced by ``key`` so four
    markets never collide.
    """
    from app.brands.models import Brand
    from app.organizations.models import Organization, OrganizationMember, User, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    org_id, ws_id = f"org-{key}", f"ws-{key}"
    # Flush in dependency order so SQLite's enforced FKs always see their parents.
    s.add(Organization(id=org_id, name=f"Org {key}", slug=f"org-{key}"))
    s.flush()
    s.add(Workspace(id=ws_id, organization_id=org_id, name=f"WS {key}", slug=f"ws-{key}"))
    user = User(
        id=f"user-{key}",
        email=f"{key}@example.com",
        full_name=f"User {key}",
        hashed_password="x",
    )
    s.add(user)
    s.flush()
    s.add(
        OrganizationMember(
            id=f"mem-{key}", organization_id=org_id, user_id=user.id, role="marketer"
        )
    )
    s.add(
        Brand(id=f"br-{key}", organization_id=org_id, workspace_id=ws_id, name=f"Brand {key}")
    )
    s.flush()
    s.add(
        ScoutRequest(
            id=f"sr-{key}",
            organization_id=org_id,
            workspace_id=ws_id,
            brand_id=f"br-{key}",
            name=f"scout {key}",
        )
    )
    s.flush()
    s.add(
        RawSignal(
            id=f"raw-{key}",
            organization_id=org_id,
            workspace_id=ws_id,
            scout_request_id=f"sr-{key}",
            source_type="rss_news",
            content="coffee",
        )
    )
    s.flush()
    s.add(
        NormalizedSignal(
            id=f"ns-{key}",
            organization_id=org_id,
            workspace_id=ws_id,
            scout_request_id=f"sr-{key}",
            raw_signal_id=f"raw-{key}",
            source_type="rss_news",
            excerpt="coffee delivery is slow",
            content_hash=f"h-{key}",
        )
    )
    s.flush()
    opp = Opportunity(
        id=f"opp-{key}",
        organization_id=org_id,
        workspace_id=ws_id,
        brand_id=f"br-{key}",
        scout_request_id=f"sr-{key}",
        title=f"Opportunity {key}",
        classification="emerging",
        decision="monitor",
        opportunity_score=opportunity_score,
    )
    s.add(opp)
    s.flush()
    record = SignalIntelligenceRecord(
        id=f"sir-{key}",
        organization_id=org_id,
        workspace_id=ws_id,
        scout_request_id=f"sr-{key}",
        normalized_signal_id=f"ns-{key}",
        opportunity_id=opp.id,
        analysis_version=analysis_version,
        scoring_version=scoring_version,
        fingerprint=fingerprint or f"fp-{key}",
        enricher="deterministic",
        accepted=True,
        classification="emerging",
    )
    s.add(record)
    s.flush()
    return Market(
        organization_id=org_id,
        workspace_id=ws_id,
        opportunity=opp,
        record=record,
        user_id=user.id,
    )


def _count(s) -> int:
    return int(s.scalar(select(func.count()).select_from(OpportunityFeedback)) or 0)


# --------------------------------------------------------------------------- #
# Happy path, provenance, attribution
# --------------------------------------------------------------------------- #
class TestCapture:
    def test_useful_with_positive_reason_persists(self, s):
        m = _seed_market(s, "dallas")
        fb = create_feedback(
            s,
            opportunity=m.opportunity,
            intelligence_record=m.record,
            is_useful=True,
            reason=FeedbackReason.STRONG_EVIDENCE,
            submitted_by_user_id=m.user_id,
        )
        assert fb.id is not None
        assert fb.is_useful is True
        assert fb.reason_code == "strong_evidence"
        assert fb.submitted_by_user_id == m.user_id
        assert fb.opportunity_id == m.opportunity.id
        assert fb.intelligence_record_id == m.record.id

    def test_not_useful_with_negative_reason_persists(self, s):
        m = _seed_market(s, "london")
        fb = create_feedback(
            s,
            opportunity=m.opportunity,
            intelligence_record=m.record,
            is_useful=False,
            reason=FeedbackReason.WRONG_MARKET,
        )
        assert fb.is_useful is False
        assert fb.reason_code == "wrong_market"
        assert fb.submitted_by_user_id is None  # attribution is optional

    def test_reasonless_feedback_is_valid_either_polarity(self, s):
        m = _seed_market(s, "lagos")
        useful = create_feedback(
            s, opportunity=m.opportunity, intelligence_record=m.record, is_useful=True
        )
        not_useful = create_feedback(
            s, opportunity=m.opportunity, intelligence_record=m.record, is_useful=False
        )
        assert useful.reason_code is None and not_useful.reason_code is None

    def test_provenance_is_copied_from_the_record_not_the_caller(self, s):
        m = _seed_market(
            s, "nairobi", analysis_version="4a", scoring_version="4a.2", fingerprint="deadbeef"
        )
        fb = create_feedback(
            s, opportunity=m.opportunity, intelligence_record=m.record, is_useful=True
        )
        assert fb.analysis_version == "4a"
        assert fb.scoring_version == "4a.2"
        assert fb.fingerprint == "deadbeef"

    def test_string_reason_is_coerced(self, s):
        m = _seed_market(s, "dallas")
        fb = create_feedback(
            s,
            opportunity=m.opportunity,
            intelligence_record=m.record,
            is_useful=True,
            reason="useful_insight",
        )
        assert fb.reason_code == "useful_insight"

    def test_unknown_reason_is_rejected(self, s):
        m = _seed_market(s, "london")
        with pytest.raises(ValidationDomainError):
            create_feedback(
                s,
                opportunity=m.opportunity,
                intelligence_record=m.record,
                is_useful=True,
                reason="totally_made_up",
            )


# --------------------------------------------------------------------------- #
# Append-only
# --------------------------------------------------------------------------- #
class TestAppendOnly:
    def test_second_judgement_appends_a_new_row(self, s):
        m = _seed_market(s, "dallas")
        first = create_feedback(
            s, opportunity=m.opportunity, intelligence_record=m.record, is_useful=True
        )
        second = create_feedback(
            s,
            opportunity=m.opportunity,
            intelligence_record=m.record,
            is_useful=False,
            reason=FeedbackReason.OUTDATED,
        )
        assert first.id != second.id
        assert _count(s) == 2  # nothing overwritten; history retained


# --------------------------------------------------------------------------- #
# Polarity — domain guard and DB backstop
# --------------------------------------------------------------------------- #
class TestPolarity:
    def test_positive_reason_rejected_when_not_useful(self, s):
        m = _seed_market(s, "dallas")
        with pytest.raises(ValidationDomainError):
            create_feedback(
                s,
                opportunity=m.opportunity,
                intelligence_record=m.record,
                is_useful=False,
                reason=FeedbackReason.STRONG_EVIDENCE,
            )
        assert _count(s) == 0

    def test_negative_reason_rejected_when_useful(self, s):
        m = _seed_market(s, "london")
        with pytest.raises(ValidationDomainError):
            create_feedback(
                s,
                opportunity=m.opportunity,
                intelligence_record=m.record,
                is_useful=True,
                reason=FeedbackReason.IRRELEVANT,
            )
        assert _count(s) == 0

    def test_db_check_constraint_is_the_backstop(self, s):
        """Bypassing the service, a contradictory row is rejected by the DB itself."""
        m = _seed_market(s, "lagos")
        s.add(
            OpportunityFeedback(
                organization_id=m.organization_id,
                workspace_id=m.workspace_id,
                opportunity_id=m.opportunity.id,
                intelligence_record_id=m.record.id,
                is_useful=True,
                reason_code=FeedbackReason.IRRELEVANT.value,  # negative code, useful=True
                analysis_version=m.record.analysis_version,
                scoring_version=m.record.scoring_version,
                fingerprint=m.record.fingerprint,
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()


# --------------------------------------------------------------------------- #
# Scope integrity / IDOR defense / four-market isolation
# --------------------------------------------------------------------------- #
class TestIsolation:
    def test_record_from_another_market_is_rejected(self, s):
        dallas = _seed_market(s, "dallas")
        london = _seed_market(s, "london")
        with pytest.raises(ValidationDomainError):
            create_feedback(
                s,
                opportunity=dallas.opportunity,
                intelligence_record=london.record,  # wrong tenant scope
                is_useful=True,
            )
        assert _count(s) == 0

    def test_record_from_sibling_opportunity_same_workspace_is_rejected(self, s):
        m = _seed_market(s, "dallas")
        # A second opportunity + record in the *same* workspace.
        from app.signals.models import NormalizedSignal, RawSignal

        s.add(
            RawSignal(
                id="raw-dallas-2",
                organization_id=m.organization_id,
                workspace_id=m.workspace_id,
                scout_request_id="sr-dallas",
                source_type="rss_news",
                content="x",
            )
        )
        s.flush()
        s.add(
            NormalizedSignal(
                id="ns-dallas-2",
                organization_id=m.organization_id,
                workspace_id=m.workspace_id,
                scout_request_id="sr-dallas",
                raw_signal_id="raw-dallas-2",
                source_type="rss_news",
                excerpt="y",
                content_hash="h2",
            )
        )
        s.flush()
        other_opp = Opportunity(
            id="opp-dallas-2",
            organization_id=m.organization_id,
            workspace_id=m.workspace_id,
            brand_id="br-dallas",
            scout_request_id="sr-dallas",
            title="Second",
            classification="emerging",
            decision="monitor",
        )
        s.add(other_opp)
        s.flush()
        other_record = SignalIntelligenceRecord(
            id="sir-dallas-2",
            organization_id=m.organization_id,
            workspace_id=m.workspace_id,
            scout_request_id="sr-dallas",
            normalized_signal_id="ns-dallas-2",
            opportunity_id=other_opp.id,
            analysis_version="3b",
            scoring_version="3b.1",
            fingerprint="fp-dallas-2",
            enricher="deterministic",
            accepted=True,
            classification="emerging",
        )
        s.add(other_record)
        s.flush()
        with pytest.raises(ValidationDomainError):
            create_feedback(
                s,
                opportunity=m.opportunity,
                intelligence_record=other_record,  # belongs to a sibling opportunity
                is_useful=True,
            )
        assert _count(s) == 0

    def test_four_markets_each_bind_to_their_own_scope(self, s):
        markets = {key: _seed_market(s, key) for key in _MARKETS}
        for key, m in markets.items():
            fb = create_feedback(
                s, opportunity=m.opportunity, intelligence_record=m.record, is_useful=True
            )
            assert fb.organization_id == f"org-{key}"
            assert fb.workspace_id == f"ws-{key}"
            assert fb.opportunity_id == f"opp-{key}"
        assert _count(s) == 4

    def test_cross_market_pairs_are_all_rejected(self, s):
        markets = {key: _seed_market(s, key) for key in _MARKETS}
        for a in _MARKETS:
            for b in _MARKETS:
                if a == b:
                    continue
                with pytest.raises(ValidationDomainError):
                    create_feedback(
                        s,
                        opportunity=markets[a].opportunity,
                        intelligence_record=markets[b].record,
                        is_useful=True,
                    )
        assert _count(s) == 0


# --------------------------------------------------------------------------- #
# Lifecycle — workspace-deletion cascade (FK enforced on this engine)
# --------------------------------------------------------------------------- #
class TestLifecycle:
    def test_deleting_workspace_cascades_feedback(self, s):
        from app.organizations.models import Workspace

        m = _seed_market(s, "dallas")
        create_feedback(
            s, opportunity=m.opportunity, intelligence_record=m.record, is_useful=True
        )
        assert _count(s) == 1
        s.delete(s.get(Workspace, m.workspace_id))
        s.flush()
        assert _count(s) == 0  # retention follows the workspace lifecycle

    def test_deleting_user_nulls_attribution_but_keeps_feedback(self, s):
        from app.organizations.models import OrganizationMember, User

        m = _seed_market(s, "london")
        fb = create_feedback(
            s,
            opportunity=m.opportunity,
            intelligence_record=m.record,
            is_useful=True,
            submitted_by_user_id=m.user_id,
        )
        # Remove the membership first (FK), then the user.
        s.delete(s.get(OrganizationMember, "mem-london"))
        s.flush()
        s.delete(s.get(User, m.user_id))
        s.flush()
        s.refresh(fb)
        assert fb.submitted_by_user_id is None  # forgets the author, keeps the record
        assert _count(s) == 1


# --------------------------------------------------------------------------- #
# Capture-only regression — no scoring side effects
# --------------------------------------------------------------------------- #
class TestCaptureOnly:
    def test_feedback_never_rescoring_or_writes_intelligence(self, s):
        m = _seed_market(s, "dallas", opportunity_score=42)
        records_before = s.scalar(
            select(func.count()).select_from(SignalIntelligenceRecord)
        )
        create_feedback(
            s,
            opportunity=m.opportunity,
            intelligence_record=m.record,
            is_useful=True,
            reason=FeedbackReason.COMMERCIALLY_RELEVANT,
        )
        s.refresh(m.opportunity)
        assert m.opportunity.opportunity_score == 42  # score untouched
        records_after = s.scalar(
            select(func.count()).select_from(SignalIntelligenceRecord)
        )
        assert records_after == records_before  # no new/altered intelligence record
