"""Service + route + isolation + security tests for Batch 4B intelligence read API.

The endpoint under test:

    GET /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/intelligence

These run against a self-contained, throwaway four-market demo seed (Dallas, London,
Lagos, Nairobi) with the ``get_db`` dependency overridden, so they are deterministic
and independent of any external demo setup. The ``TestClient`` is intentionally used
without its lifespan context manager: the startup schema gate targets the real engine,
which is irrelevant here — every request is served from the overridden seeded session.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.intelligence.persistence import get_latest_for_opportunity
from app.intelligence.read_service import _map_record, get_opportunity_intelligence
from app.intelligence.records import SignalIntelligenceRecord
from app.main import app
from app.opportunities.models import Opportunity
from app.organizations.models import Workspace

API = get_settings().api_prefix

# Public-facing internal fields that must NEVER appear in a serialized response.
_FORBIDDEN_KEYS = (
    "fingerprint",
    "cluster_key",
    "normalized_signal_id",
    "organization_id",
    "exclusion_hits",
    "rejection_reason",
    "updated_at",
    "author",
)


# --------------------------------------------------------------------------- #
# Harness: throwaway seeded DB + overridden get_db + authenticated client
# --------------------------------------------------------------------------- #
class _Harness:
    def __init__(self, client, factory):
        self.client = client
        self.factory = factory
        self.ws = seed_mod.sid("ws")
        self.org = seed_mod.sid("org")
        self.user = seed_mod.sid("user")
        self.auth = {"Authorization": f"Bearer {create_access_token(self.user)}"}
        self.with_intel: list[str] = []
        self.without_intel: list[str] = []
        self._categorize()

    def _categorize(self) -> None:
        with self.factory() as s:
            opps = s.scalars(select(Opportunity).where(Opportunity.workspace_id == self.ws)).all()
            for o in opps:
                rec = get_latest_for_opportunity(s, workspace_id=self.ws, opportunity_id=o.id)
                (self.with_intel if rec is not None else self.without_intel).append(o.id)

    def get(self, workspace_id: str, opportunity_id: str):
        return self.client.get(
            f"{API}/workspaces/{workspace_id}/opportunities/{opportunity_id}/intelligence",
            headers=self.auth,
        )


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("intel_api")
    engine = create_engine(
        f"sqlite:///{tmp/'api.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = factory
    try:
        seed_mod.seed(reset=True)

        def _override_get_db():
            # Mirror production ``get_db`` semantics: commit on success, rollback on
            # error. Endpoints (e.g. ``auth/register``) flush within the request and
            # rely on this teardown commit for durability across sessions.
            s = factory()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        harness = _Harness(client, factory)
        # Seed sanity: the four-market fixture yields both states we test.
        assert harness.with_intel, "expected some opportunities WITH intelligence"
        assert harness.without_intel, "expected some opportunities WITHOUT intelligence"
        yield harness
    finally:
        app.dependency_overrides.clear()
        seed_mod.SessionLocal = original
        engine.dispose()


# --------------------------------------------------------------------------- #
# Service: mapping + absence + safe failure (no HTTP)
# --------------------------------------------------------------------------- #
class TestService:
    def test_authorized_opportunity_with_intelligence_maps_payload(self, h):
        opp_id = h.with_intel[0]
        with h.factory() as s:
            payload = get_opportunity_intelligence(s, workspace_id=h.ws, opportunity_id=opp_id)
        assert payload is not None
        # Facts vs inference stay structurally separate.
        assert payload.facts.source_type
        assert hasattr(payload.inference, "signal_type")
        assert payload.version.analysis_version == "3b"
        assert payload.version.scoring_version == "3b.1"
        assert payload.provenance.enricher == "deterministic"

    def test_opportunity_without_record_returns_none(self, h):
        opp_id = h.without_intel[0]
        with h.factory() as s:
            payload = get_opportunity_intelligence(s, workspace_id=h.ws, opportunity_id=opp_id)
        assert payload is None

    def test_legacy_ingest_metadata_is_not_fabricated(self, h):
        # An opportunity with no first-class record must yield None even though the
        # advisory ``ingest_metadata["intelligence"]`` annotation still exists on signals.
        opp_id = h.without_intel[0]
        with h.factory() as s:
            assert get_opportunity_intelligence(s, workspace_id=h.ws, opportunity_id=opp_id) is None

    def test_malformed_record_fails_safe_to_none(self, h):
        # Insert an accepted, linked record whose facts payload is structurally broken.
        opp_id = h.with_intel[0]
        with h.factory() as s:
            s.add(
                SignalIntelligenceRecord(
                    organization_id=h.org, workspace_id=h.ws, scout_request_id="sr-x",
                    normalized_signal_id="ns-malformed", opportunity_id=opp_id,
                    analysis_version="3b", scoring_version="3b.1", fingerprint="fp-malformed",
                    enricher="deterministic", accepted=True, classification="emerging",
                    cluster_key="general", score_total=100, evidence_count=0, is_simulated=True,
                    facts={"unexpected": "no source_type key"},  # missing required fields
                    inference={}, relevance={}, score_components={}, provenance={},
                )
            )
            # Flush (not commit): the malformed row is visible to the read within this
            # session — score_total=100 makes it the "latest eligible" row, so mapping
            # is attempted — but it is rolled back on session close so it never pollutes
            # the module-scoped DB for subsequent tests.
            s.flush()
            payload = get_opportunity_intelligence(s, workspace_id=h.ws, opportunity_id=opp_id)
            s.rollback()
        # Fails safe: null, not a 500 or a partial object.
        assert payload is None

    def test_no_persistence_mutation_on_read(self, h):
        opp_id = h.with_intel[0]
        with h.factory() as s:
            before = s.scalar(select(func.count()).select_from(SignalIntelligenceRecord))
        with h.factory() as s:
            get_opportunity_intelligence(s, workspace_id=h.ws, opportunity_id=opp_id)
        with h.factory() as s:
            after = s.scalar(select(func.count()).select_from(SignalIntelligenceRecord))
        assert before == after


# --------------------------------------------------------------------------- #
# Mapper bounds (unit, no DB) — an oversized/hostile row is bounded/clamped
# --------------------------------------------------------------------------- #
class TestMapperBounds:
    def _oversized_record(self) -> SignalIntelligenceRecord:
        span = {"start": 0, "end": 6, "quote": "coffee" * 200, "method": "lexicon:test"}
        attr = {"value": "complaint", "confidence": 5.0, "method": "lexicon:complaint",
                "evidence": [span] * 80}
        return SignalIntelligenceRecord(
            id="rec-big", organization_id="org", workspace_id="ws", scout_request_id="sr",
            normalized_signal_id="ns", opportunity_id="opp", analysis_version="3b",
            scoring_version="3b.1", fingerprint="fp", enricher="deterministic", accepted=True,
            classification="emerging", cluster_key="general", score_total=999, evidence_count=80,
            is_simulated=True,
            facts={"source_type": "rss_news", "market": "London", "language": "en",
                   "published_days_ago": 1.0, "char_count": 10, "word_count": 2,
                   "excerpt": "coffee " * 1000, "distinct_source_types": 1,
                   "duplicate_count": 1, "engagement": 0,
                   "author": "should-never-surface"},
            inference={"signal_type": attr, "pain_point_dna": attr, "sentiment": attr,
                       "has_buying_intent": False, "has_competitor_dissatisfaction": False,
                       "intent_evidence": [span] * 80},
            relevance={"score": -5, "below_action_floor": False,
                       "keyword_hits": [f"k{i}" for i in range(200)],
                       "exclusion_hits": ["should-never-surface"]},
            score_components={"total": 999, "classification": "emerging", "version": "3b.1",
                              "factors": {f"f{i}": {"weight": 1.0, "value": 1.0, "points": 1.0}
                                          for i in range(100)}},
            provenance={},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_all_bounds_and_clamps_enforced(self):
        payload = _map_record(self._oversized_record())
        assert len(payload.facts.excerpt) <= 2000
        assert len(payload.evidence) <= 32
        for e in payload.evidence:
            assert len(e.quote) <= 400
        assert len(payload.relevance.keyword_hits) <= 64
        assert 0.0 <= payload.inference.signal_type.confidence <= 1.0  # 5.0 clamped
        assert payload.score.total == 100  # 999 clamped
        assert payload.relevance.score == 0  # -5 clamped
        assert len(payload.score.factors) <= 32

    def test_excluded_fields_absent_from_serialized_payload(self):
        blob = _map_record(self._oversized_record()).model_dump_json()
        assert "should-never-surface" not in blob  # author + exclusion_hits dropped
        for key in ("author", "exclusion_hits", "fingerprint", "normalized_signal_id"):
            assert key not in blob


# --------------------------------------------------------------------------- #
# Route: auth, absence, contract, mutation verbs, internal-field exclusion
# --------------------------------------------------------------------------- #
class TestRoute:
    def test_authenticated_present_returns_200_with_object(self, h):
        opp_id = h.with_intel[0]
        r = h.get(h.ws, opp_id)
        assert r.status_code == 200
        body = r.json()
        assert body["opportunity_id"] == opp_id
        assert body["intelligence"] is not None
        assert "facts" in body["intelligence"] and "inference" in body["intelligence"]

    def test_authenticated_absent_returns_200_with_null(self, h):
        opp_id = h.without_intel[0]
        r = h.get(h.ws, opp_id)
        assert r.status_code == 200
        assert r.json() == {"opportunity_id": opp_id, "intelligence": None}

    def test_unauthenticated_is_rejected(self, h):
        opp_id = h.with_intel[0]
        r = h.client.get(
            f"{API}/workspaces/{h.ws}/opportunities/{opp_id}/intelligence"
        )
        assert r.status_code == 401

    def test_missing_opportunity_is_404(self, h):
        r = h.get(h.ws, "does-not-exist-0000")
        assert r.status_code == 404

    def test_missing_workspace_is_404(self, h):
        opp_id = h.with_intel[0]
        r = h.get("no-such-workspace", opp_id)
        assert r.status_code == 404

    def test_response_has_no_internal_fields(self, h):
        opp_id = h.with_intel[0]
        # Cross-check against the real record's secret values, not just field names.
        with h.factory() as s:
            rec = get_latest_for_opportunity(s, workspace_id=h.ws, opportunity_id=opp_id)
        blob = h.get(h.ws, opp_id).text
        assert rec.fingerprint not in blob
        assert rec.normalized_signal_id not in blob
        for key in _FORBIDDEN_KEYS:
            assert key not in blob

    def test_facts_and_inference_are_separate(self, h):
        opp_id = h.with_intel[0]
        intel = h.get(h.ws, opp_id).json()["intelligence"]
        assert "signal_type" not in intel["facts"]
        assert "signal_type" in intel["inference"]
        assert "author" not in intel["facts"]

    @pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
    def test_mutation_verbs_not_allowed(self, h, verb):
        opp_id = h.with_intel[0]
        url = f"{API}/workspaces/{h.ws}/opportunities/{opp_id}/intelligence"
        r = getattr(h.client, verb)(url, headers=h.auth)
        assert r.status_code == 405

    def test_opportunity_detail_endpoint_unchanged(self, h):
        # The existing detail contract must remain backward compatible (no intelligence key).
        opp_id = h.with_intel[0]
        r = h.client.get(
            f"{API}/workspaces/{h.ws}/opportunities/{opp_id}", headers=h.auth
        )
        assert r.status_code == 200
        assert "intelligence" not in r.json()

    def test_version_metadata_serialized(self, h):
        opp_id = h.with_intel[0]
        intel = h.get(h.ws, opp_id).json()["intelligence"]
        assert intel["version"] == {"analysis_version": "3b", "scoring_version": "3b.1"}
        assert intel["provenance"]["enricher"] == "deterministic"
        assert "fingerprint" not in intel["provenance"]


# --------------------------------------------------------------------------- #
# Four-market isolation + BOLA/IDOR
# --------------------------------------------------------------------------- #
class TestIsolation:
    def _by_market(self, h) -> dict[str, str]:
        """One opportunity-with-intelligence id per market."""
        out: dict[str, str] = {}
        with h.factory() as s:
            for opp_id in h.with_intel:
                opp = s.get(Opportunity, opp_id)
                out.setdefault(opp.resolved_market, opp_id)
        return out

    def test_each_market_returns_its_own_intelligence_only(self, h):
        markets = self._by_market(h)
        assert len(markets) == 4  # Dallas, London, Lagos, Nairobi
        seen_records: dict[str, str] = {}
        for market, opp_id in markets.items():
            body = h.get(h.ws, opp_id).json()
            assert body["opportunity_id"] == opp_id
            # Every returned record is scoped to exactly this opportunity.
            with h.factory() as s:
                rec = get_latest_for_opportunity(s, workspace_id=h.ws, opportunity_id=opp_id)
                assert rec.opportunity_id == opp_id
            seen_records[market] = rec.id
        # No two markets resolve to the same underlying record (no cross-market bleed).
        # (Excerpt text is intentionally *not* used as a discriminator: the demo seed
        # reuses identical signal text across markets, so record identity is the real
        # isolation guarantee.)
        assert len(set(seen_records.values())) == len(seen_records)

    def test_cross_market_opportunity_ids_do_not_share_records(self, h):
        markets = list(self._by_market(h).values())
        dallas, lagos = markets[0], markets[2]
        with h.factory() as s:
            r_dallas = get_latest_for_opportunity(s, workspace_id=h.ws, opportunity_id=dallas)
            r_lagos = get_latest_for_opportunity(s, workspace_id=h.ws, opportunity_id=lagos)
        assert r_dallas.id != r_lagos.id
        assert r_dallas.scout_request_id != r_lagos.scout_request_id

    def test_foreign_workspace_opportunity_is_404(self, h):
        # A second workspace (same org, so membership passes) with its own opportunity.
        with h.factory() as s:
            s.add(Workspace(id="ws-foreign", organization_id=h.org, name="Foreign", slug="foreign"))
            s.add(
                Opportunity(
                    id="opp-foreign", organization_id=h.org, workspace_id="ws-foreign",
                    brand_id=seed_mod.sid("brand"),
                    scout_request_id=seed_mod.sid("scout", "dallas"),
                    title="Foreign", classification="emerging", decision="monitor",
                )
            )
            s.commit()
        # Demo workspace path + foreign opportunity id → indistinguishable 404.
        assert h.get(h.ws, "opp-foreign").status_code == 404
        # Foreign workspace path + demo opportunity id → 404 (opp not in that workspace).
        assert h.get("ws-foreign", h.with_intel[0]).status_code == 404

    def test_non_member_is_forbidden(self, h):
        # A fresh customer in a different org is not a member of the demo org.
        import uuid

        email = f"outsider-{uuid.uuid4().hex[:8]}@example.com"
        reg = h.client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Outsider", "password": "outsider1234",
                  "organization_name": "Outsider Co"},
        )
        assert reg.status_code == 201, reg.text
        outsider = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        r = h.client.get(
            f"{API}/workspaces/{h.ws}/opportunities/{h.with_intel[0]}/intelligence",
            headers=outsider,
        )
        assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Security: guessed/malformed/injection-shaped ids, no leakage
# --------------------------------------------------------------------------- #
class TestSecurity:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "1 OR 1=1",
            "'; DROP TABLE signal_intelligence_records; --",
            "../../etc/passwd",
            "%00",
            "guessed-uuid-deadbeef",
            "<script>alert(1)</script>",
        ],
    )
    def test_injection_shaped_ids_are_safe_404(self, h, bad_id):
        with h.factory() as s:
            before = s.scalar(select(func.count()).select_from(SignalIntelligenceRecord))
        r = h.get(h.ws, bad_id)
        assert r.status_code == 404
        # The table is intact (parameterized query treated the value as a literal).
        with h.factory() as s:
            after = s.scalar(select(func.count()).select_from(SignalIntelligenceRecord))
        assert before == after

    def test_error_body_leaks_no_internal_detail(self, h):
        blob = h.get(h.ws, "nonexistent-0000").text.lower()
        for forbidden in ("traceback", "sqlalchemy", "select ", "sqlite", "password"):
            assert forbidden not in blob

    def test_no_url_fields_returned(self, h):
        opp_id = h.with_intel[0]
        blob = h.get(h.ws, opp_id).text
        # No source URLs are stored in 4A, so none may appear in the public payload.
        for token in ("http://", "https://", "source_url"):
            assert token not in blob
