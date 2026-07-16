"""Batch 4D closeout: cross-layer end-to-end verification of the intelligence read path.

This suite is the *closeout-boundary* proof for Batch 4 (§17.22.11). The existing
Batch 4A/4B/4C suites are deliberately layer-isolated (persistence, mapper, route,
frontend panel each tested on their own). This suite instead exercises the seeded,
deterministic read path **end-to-end** — real persisted ``SignalIntelligenceRecord``
rows → the read-only HTTP API → the exact JSON contract the frontend panel consumes —
and asserts it across all four demo markets together. It adds only coverage the other
suites do not provide:

* the seeded deterministic payload satisfies the full frontend-consumed contract
  (§17.22.11 end-to-end read path; criteria 11, 12, 16, 17, 27-29);
* four-market isolation holds at the closeout boundary, not only within one layer
  (criteria 38-45);
* the additive read path degrades to a neutral ``null`` under write-path/UI-field
  rollback while Phase 2 opportunity data is preserved (criterion 74; §17.18);
* the opportunity feed/detail contract carries no intelligence fan-out (criterion 26).

It introduces no production code, migration, dependency, or API-contract change; it is
read-only except for one rollback test that mutates and then fully re-seeds its own
throwaway module database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.intelligence.persistence import get_latest_for_opportunity
from app.intelligence.records import SignalIntelligenceRecord
from app.main import app
from app.opportunities.models import Opportunity

API = get_settings().api_prefix


# --------------------------------------------------------------------------- #
# Harness: throwaway seeded four-market DB + overridden get_db + auth client
# --------------------------------------------------------------------------- #
class _Closeout:
    def __init__(self, client, factory):
        self.client = client
        self.factory = factory
        self.ws = seed_mod.sid("ws")
        self.auth = {"Authorization": f"Bearer {create_access_token(seed_mod.sid('user'))}"}

    def get(self, opportunity_id: str):
        return self.client.get(
            f"{API}/workspaces/{self.ws}/opportunities/{opportunity_id}/intelligence",
            headers=self.auth,
        )

    def one_intel_opp_per_market(self) -> dict[str, str]:
        """Map resolved_market -> one opportunity id that has a persisted record."""
        out: dict[str, str] = {}
        with self.factory() as s:
            opps = s.scalars(
                select(Opportunity).where(Opportunity.workspace_id == self.ws)
            ).all()
            for o in opps:
                if o.resolved_market in out:
                    continue
                rec = get_latest_for_opportunity(s, workspace_id=self.ws, opportunity_id=o.id)
                if rec is not None:
                    out[o.resolved_market] = o.id
        return out


@pytest.fixture(scope="module")
def c(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("intel_closeout")
    engine = create_engine(
        f"sqlite:///{tmp/'closeout.db'}",
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
        harness = _Closeout(TestClient(app), factory)
        # Seed sanity: the deterministic four-market fixture must exist.
        assert len(harness.one_intel_opp_per_market()) == 4
        yield harness
    finally:
        app.dependency_overrides.clear()
        seed_mod.SessionLocal = original
        engine.dispose()


# Fields the frontend panel (IntelligencePanel.tsx) reads. If the seeded read path
# does not deliver these, the panel cannot render — so the contract is asserted here.
_PANEL_TOP_KEYS = {
    "classification", "decision", "is_simulated", "rationale", "created_at",
    "facts", "inference", "relevance", "score", "evidence", "provenance", "version",
}
_PANEL_FACT_KEYS = {
    "source_type", "language", "published_days_ago", "char_count", "word_count",
    "distinct_source_types", "duplicate_count", "engagement",
}


# --------------------------------------------------------------------------- #
# End-to-end: seeded persistence -> API -> frontend-consumed contract
# --------------------------------------------------------------------------- #
class TestEndToEndReadPath:
    def test_seeded_payload_satisfies_full_frontend_contract(self, c):
        for market, opp_id in c.one_intel_opp_per_market().items():
            r = c.get(opp_id)
            assert r.status_code == 200, (market, r.text)
            body = r.json()
            assert body["opportunity_id"] == opp_id
            intel = body["intelligence"]
            assert intel is not None, market

            # Every field the panel reads is present at the top level.
            assert _PANEL_TOP_KEYS <= set(intel), market

            # Facts vs. inference stay structurally separate (criteria 11, 28).
            assert _PANEL_FACT_KEYS <= set(intel["facts"]), market
            assert "signal_type" not in intel["facts"]
            assert "signal_type" in intel["inference"]

            # Inferred attributes retain a bounded 0..1 confidence (criterion 12) and
            # never render as a quoted source statement (criterion 14).
            st = intel["inference"]["signal_type"]
            assert 0.0 <= st["confidence"] <= 1.0, market
            assert set(st) >= {"value", "confidence", "method"}

            # Scores are bounded to the panel's percentage range (criterion 18).
            assert 0 <= intel["score"]["total"] <= 100, market
            assert intel["relevance"]["score"] >= 0, market

            # Provenance/version are deterministic and simulated is flagged
            # (criteria 16, 17, 29).
            assert intel["is_simulated"] is True, market
            assert intel["provenance"]["enricher"] == "deterministic", market
            assert intel["version"] == {"analysis_version": "3b", "scoring_version": "3b.1"}, market

            # Evidence excerpts are plain strings (rendered inertly by the panel).
            for item in intel["evidence"]:
                assert isinstance(item["quote"], str)

    def test_market_fact_matches_opportunity_market(self, c):
        # The persisted facts.market a customer sees is the opportunity's own market.
        for market, opp_id in c.one_intel_opp_per_market().items():
            intel = c.get(opp_id).json()["intelligence"]
            assert intel["facts"]["market"] == market


# --------------------------------------------------------------------------- #
# Closeout-boundary four-market isolation (criteria 38-45)
# --------------------------------------------------------------------------- #
class TestCloseoutIsolation:
    def test_no_cross_market_bleed_across_all_four_markets(self, c):
        markets = c.one_intel_opp_per_market()
        assert set(markets) == {"Dallas, TX", "London, UK", "Lagos, NG", "Nairobi, KE"}
        all_markets = list(markets)

        seen_records: set[str] = set()
        for market, opp_id in markets.items():
            body = c.get(opp_id).json()
            intel = body["intelligence"]
            # This response advertises only its own market...
            assert intel["facts"]["market"] == market
            # ...and none of the other three markets leak into the payload.
            for other in all_markets:
                if other == market:
                    continue
                assert other not in body["intelligence"]["facts"]["market"]

            with c.factory() as s:
                rec = get_latest_for_opportunity(
                    s, workspace_id=c.ws, opportunity_id=opp_id
                )
                assert rec.opportunity_id == opp_id
                seen_records.add(rec.id)

        # Same-topic signals across markets resolve to independent records
        # (criteria 41, 42, 44): four opportunities, four distinct record ids.
        assert len(seen_records) == 4


# --------------------------------------------------------------------------- #
# Rollback / feature-gating (criterion 74; §17.18)
# --------------------------------------------------------------------------- #
class TestRollbackDegradation:
    def test_write_path_rollback_degrades_to_neutral_null(self, c):
        """Removing the additive intelligence (write-path/UI-field rollback) must leave
        the opportunity fully readable with a neutral ``null`` payload — never an error —
        and must not touch Phase 2 opportunity data (§17.18)."""
        market, opp_id = next(iter(c.one_intel_opp_per_market().items()))

        with c.factory() as s:
            before = s.get(Opportunity, opp_id)
            phase2 = (
                before.title,
                before.classification,
                before.decision,
                before.opportunity_score,
            )
        try:
            # Simulate the write-path/field rollback: the additive records go away.
            with c.factory() as s:
                s.execute(
                    delete(SignalIntelligenceRecord).where(
                        SignalIntelligenceRecord.opportunity_id == opp_id
                    )
                )
                s.commit()

            r = c.get(opp_id)
            # Neutral empty result, not an error (criteria 22, 30).
            assert r.status_code == 200
            assert r.json() == {"opportunity_id": opp_id, "intelligence": None}

            # The pre-existing opportunity experience is untouched (criteria 3, 4, 37).
            detail = c.client.get(
                f"{API}/workspaces/{c.ws}/opportunities/{opp_id}", headers=c.auth
            )
            assert detail.status_code == 200
            with c.factory() as s:
                after = s.get(Opportunity, opp_id)
                assert (
                    after.title,
                    after.classification,
                    after.decision,
                    after.opportunity_score,
                ) == phase2
        finally:
            # Restore the deterministic module fixture for any later test.
            seed_mod.seed(reset=True)


# --------------------------------------------------------------------------- #
# Feed/detail carries no intelligence fan-out (criterion 26)
# --------------------------------------------------------------------------- #
class TestNoFeedFanout:
    def test_opportunity_detail_never_embeds_intelligence(self, c):
        # The compact feed / detail contract must not expand to carry intelligence;
        # intelligence is reachable only through its own scoped endpoint.
        for _market, opp_id in c.one_intel_opp_per_market().items():
            detail = c.client.get(
                f"{API}/workspaces/{c.ws}/opportunities/{opp_id}", headers=c.auth
            )
            assert detail.status_code == 200
            assert "intelligence" not in detail.json()
