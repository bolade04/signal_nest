"""3C-C.1: the intelligence-read ``intelligence_record_id`` unblocks feedback capture.

This is the contract-unblock proof for the precise 3C-D blocker. Through the *real*
customer endpoints and a self-contained single-tenant graph, it shows that a client can:

    1. GET the opportunity-intelligence read response,
    2. read the newly exposed ``intelligence_record_id``,
    3. POST feedback using only that id + the binary verdict (no caller-supplied
       provenance / scope / version fields),
    4. and have the feedback bound to the *same* immutable record.

The feedback feature flag is dark by default and enabled only for this proof; the
intelligence read exposes the id regardless of the feedback flag because it is part of
the already-authorized intelligence response.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.enums import Role
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.feedback.models import OpportunityFeedback
from app.main import app

API = get_settings().api_prefix

_ORG, _WS, _USER = "org-boot", "ws-boot", "owner-boot"
_BRAND, _SR, _OPP, _RECORD = "brand-boot", "sr-boot", "opp-boot", "rec-boot-0001"

_FACTS = {
    "source_type": "rss_news", "market": "Dallas", "language": "en",
    "published_days_ago": 1.0, "char_count": 12, "word_count": 2, "excerpt": "hello",
    "distinct_source_types": 1, "duplicate_count": 1, "engagement": 0,
}
_INFERENCE = {"has_buying_intent": False, "has_competitor_dissatisfaction": False}
_RELEVANCE = {"score": 50, "below_action_floor": False, "keyword_hits": []}
_SCORE = {"total": 50, "classification": "emerging", "factors": {}}


def _seed(s) -> None:
    from app.brands.models import Brand
    from app.intelligence.records import SignalIntelligenceRecord
    from app.opportunities.models import Opportunity
    from app.organizations.models import Organization, OrganizationMember, User, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    s.add(Organization(id=_ORG, name="Boot Org", slug="boot-org"))
    s.flush()
    s.add(Workspace(id=_WS, organization_id=_ORG, name="Boot WS", slug="boot-ws"))
    s.add(User(id=_USER, email="owner-boot@example.com", full_name="Owner",
               hashed_password="x", is_active=True))
    s.flush()
    s.add(OrganizationMember(id="m-boot", organization_id=_ORG, user_id=_USER,
                             role=Role.OWNER.value))
    s.add(Brand(id=_BRAND, organization_id=_ORG, workspace_id=_WS, name="Boot Brand"))
    s.flush()
    s.add(ScoutRequest(id=_SR, organization_id=_ORG, workspace_id=_WS,
                       brand_id=_BRAND, name="scout"))
    s.flush()
    s.add(RawSignal(id="raw-boot", organization_id=_ORG, workspace_id=_WS,
                    scout_request_id=_SR, source_type="rss_news", content="c"))
    s.flush()
    s.add(NormalizedSignal(id="ns-boot", organization_id=_ORG, workspace_id=_WS,
                           scout_request_id=_SR, raw_signal_id="raw-boot",
                           source_type="rss_news", excerpt="e", content_hash="h"))
    s.flush()
    s.add(Opportunity(id=_OPP, organization_id=_ORG, workspace_id=_WS, brand_id=_BRAND,
                      scout_request_id=_SR, title="T", classification="emerging",
                      decision="monitor", opportunity_score=50, resolved_market="Dallas"))
    s.flush()
    s.add(SignalIntelligenceRecord(
        id=_RECORD, organization_id=_ORG, workspace_id=_WS, scout_request_id=_SR,
        normalized_signal_id="ns-boot", opportunity_id=_OPP, analysis_version="3b",
        scoring_version="3b.1", fingerprint="fp-boot", enricher="deterministic",
        accepted=True, classification="emerging", cluster_key="general",
        score_total=50, evidence_count=0, is_simulated=True,
        facts=_FACTS, inference=_INFERENCE, relevance=_RELEVANCE,
        score_components=_SCORE, provenance={}, created_at=datetime(2026, 1, 1, tzinfo=UTC),
    ))
    s.commit()


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'bootstrap.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        _seed(s)

    def _override_get_db():
        s = make()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    auth = {"Authorization": f"Bearer {create_access_token(_USER)}"}
    original_flag = get_settings().opportunity_feedback_enabled
    try:
        yield TestClient(app), make, auth
    finally:
        app.dependency_overrides.clear()
        get_settings().opportunity_feedback_enabled = original_flag
        engine.dispose()


def test_get_intelligence_id_then_post_feedback_binds_same_record(client):
    tc, make, auth = client

    # 1. GET the intelligence read response and read the exposed record id.
    r = tc.get(f"{API}/workspaces/{_WS}/opportunities/{_OPP}/intelligence", headers=auth)
    assert r.status_code == 200
    record_id = r.json()["intelligence"]["intelligence_record_id"]
    assert record_id == _RECORD  # exactly the persisted record

    # 2. Enable the (otherwise dark) feedback feature only for this proof.
    get_settings().opportunity_feedback_enabled = True

    # 3. POST feedback using ONLY the read id + verdict — no caller-supplied
    #    provenance, scope, actor or version fields.
    r = tc.post(
        f"{API}/workspaces/{_WS}/opportunities/{_OPP}/feedback",
        headers=auth,
        json={"intelligence_record_id": record_id, "is_useful": True,
              "reason_code": "useful_insight"},
    )
    assert r.status_code == 201, r.text
    body = r.json()

    # 4. The created feedback is bound to the same immutable record, and the
    #    server derived the version provenance from that record (not the caller).
    assert body["intelligence_record_id"] == record_id
    assert body["analysis_version"] == "3b"
    assert body["scoring_version"] == "3b.1"

    # 5. Exactly one persisted feedback row, pointing at the same record id.
    with make() as s:
        rows = list(s.scalars(select(OpportunityFeedback)))
    assert len(rows) == 1
    assert rows[0].intelligence_record_id == _RECORD


def test_intelligence_id_is_exposed_even_while_feedback_dark(client):
    # The read id is part of the already-authorized intelligence response, so it is
    # present regardless of the (default-dark) feedback flag.
    tc, _make, auth = client
    assert get_settings().opportunity_feedback_enabled is False
    r = tc.get(f"{API}/workspaces/{_WS}/opportunities/{_OPP}/intelligence", headers=auth)
    assert r.status_code == 200
    assert r.json()["intelligence"]["intelligence_record_id"] == _RECORD
    # And the feedback endpoint is still dark (503) — 3C-C.1 does not enable it.
    r = tc.post(
        f"{API}/workspaces/{_WS}/opportunities/{_OPP}/feedback",
        headers=auth,
        json={"intelligence_record_id": _RECORD, "is_useful": True},
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "capability_unavailable"
