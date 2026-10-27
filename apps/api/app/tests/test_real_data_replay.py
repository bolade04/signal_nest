"""P6-DATA-1 / P6-DATA-2 / P6-CI-11 / P6-PRIV-4 end to end, through the HTTP API.

A throwaway demo database (the real demo seed) is extended by the real replay seed
(``app.connectors.replay.seed_replay``): the capture corpus is served by a real HTTP
server on 127.0.0.1 and read by the live RSS connector through the egress guard, then
through the unmodified scout pipeline. Nothing leaves the machine.

* coverage (P6-DATA-2): every run records a closed reason code, exposed by the scout
  request API — ``fixture_library`` for the four demo markets, ``live_sources`` for the
  replayed market, and an explicit ``not_covered`` for a market nothing serves;
* provenance (P6-DATA-1): replayed opportunities stay in their market and name their
  source on every evidence entry; synthetic captures stay marked simulated;
* takedown (P6-PRIV-4): an operator takedown purges a source's signals in every tenant,
  deletes opportunities built only from it, redacts it from mixed ones, reports a
  recounted zero remainder, and the source is never fetched again. The record it leaves
  is the suppression only (D-9d): key, time, counts — attribution and the public
  reference are in the affected workspaces' audit entries, and the request carries no
  free text (D-9e interim).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.brands.models import Brand
from app.connectors import registry as registry_mod
from app.connectors import replay as replay_mod
from app.connectors import takedown as takedown_mod
from app.connectors.coverage import CoverageReason
from app.connectors.feedcache import feed_cache
from app.connectors.live import live_connector_for
from app.connectors.models import SourceTakedown
from app.connectors.replay import (
    UNCOVERED_MARKET,
    ReplayServer,
    _seed_problems,
    load_corpus,
    seed_replay,
)
from app.connectors.routes import TakedownIn
from app.connectors.sources import LegalStatus, SourceRecord
from app.core.config import Settings, get_settings
from app.core.enums import Role, ScoutRequestStatus
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.intelligence.records import SignalIntelligenceRecord
from app.jobs import pipeline as pipeline_mod
from app.jobs.models import Job
from app.jobs.pipeline import _run
from app.locations.models import BusinessLocation
from app.main import app
from app.opportunities.models import Opportunity
from app.organizations.models import Organization, OrganizationMember, User, Workspace
from app.scouting_requests.connectors import fetch_signals
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.run_history import _map_stats
from app.signals.models import NormalizedSignal, RawSignal
from app.tests._auth2_support import live_bearer

API = get_settings().api_prefix
CORPUS = load_corpus()
CORPUS_KEYS = {c.source_key for c in CORPUS}
CORPUS_MARKETS = {m for c in CORPUS for m in c.markets}
REPLAY_MARKET = "Toronto, ON"
FIXTURE_MARKETS = {"Dallas, TX", "London, UK", "Lagos, NG", "Nairobi, KE"}
TAKEN = "replay-toronto-business"
KEPT = "replay-toronto-food"


@dataclass
class World:
    client: TestClient
    factory: sessionmaker
    server: ReplayServer
    summary: dict[str, dict]
    ws: str
    auth: dict[str, str]

    def get(self, path: str, **params):
        r = self.client.get(f"{API}{path}", headers=self.auth, params=params or None)
        assert r.status_code == 200, r.text
        return r.json()

    def location(self, market: str) -> dict:
        city = market.split(",")[0]
        [loc] = [x for x in self.get(f"/workspaces/{self.ws}/locations") if x["city"] == city]
        return loc

    def requests_by_market(self) -> dict[str, dict]:
        return {r["resolved_market"]: r for r in self.get(f"/workspaces/{self.ws}/scout-requests")}


def _fk_on(engine) -> None:
    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")


@contextmanager
def _world(tmp_path: Path) -> Iterator[World]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'replay.db'}", connect_args={"check_same_thread": False}
    )
    _fk_on(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

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

    feed_cache.clear()
    with pytest.MonkeyPatch.context() as mp, ReplayServer() as server:
        mp.setattr(seed_mod, "SessionLocal", factory)
        seed_mod.seed(reset=True)
        mp.setenv("CONNECTOR_RSS_REPLAY_ORIGIN", server.origin)
        mp.setenv("CONNECTOR_RSS_LIVE_ENABLED", "true")
        # Live feeds are RSS: the workspace's CAP-1 decision needs the global flag on.
        mp.setenv("CONNECTOR_RSS_ENABLED", "true")
        get_settings.cache_clear()
        app.dependency_overrides[get_db] = _override_get_db
        try:
            summary = seed_replay(session_factory=factory)
            yield World(
                client=TestClient(app),
                factory=factory,
                server=server,
                summary=summary,
                ws=seed_mod.sid("ws"),
                auth={"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"},
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
            get_settings.cache_clear()
            feed_cache.clear()
            engine.dispose()


@pytest.fixture(scope="module")
def shared_world(tmp_path_factory) -> Iterator[World]:
    """One replayed world for the read-only tests."""
    with _world(tmp_path_factory.mktemp("replay_shared")) as world:
        yield world


@pytest.fixture
def replay_world(tmp_path) -> Iterator[World]:
    """A fresh replayed world for a test that mutates it (takedowns)."""
    with _world(tmp_path) as world:
        yield world


# --------------------------------------------------------------------------- #
# Replay through the live connector, coverage reasons, provenance
# --------------------------------------------------------------------------- #
def test_the_replay_seed_reports_live_coverage_and_an_explicit_not_covered(shared_world):
    summary = shared_world.summary
    assert set(summary) == CORPUS_MARKETS | {UNCOVERED_MARKET}
    assert _seed_problems(summary, CORPUS_MARKETS) == []
    toronto = summary[REPLAY_MARKET]
    assert toronto["coverage"]["reason"] == "live_sources"
    assert toronto["coverage"]["sources"] == len(CORPUS_KEYS)
    assert toronto["signals_analyzed"] >= 1 and toronto["opportunities"] >= 1
    assert summary[UNCOVERED_MARKET]["coverage"] == {
        "status": "not_covered",
        "reason": "no_source_for_market",
        "is_simulated": False,
        "sources": 0,
        "sources_failed": 0,
    }


def test_every_capture_was_fetched_over_loopback_http_by_the_bot(shared_world):
    hits = shared_world.server.hits
    assert sorted(path for path, _status, _ua in hits) == sorted(
        f"/captures/{c.capture_id}" for c in CORPUS
    )
    assert all(status == 200 for _p, status, _ua in hits)
    assert all(ua and ua.startswith("SignalNestBot/") for _p, _s, ua in hits)


def test_the_scout_request_api_exposes_a_coverage_reason_for_every_run(shared_world):
    by_market = shared_world.requests_by_market()
    assert FIXTURE_MARKETS | CORPUS_MARKETS | {UNCOVERED_MARKET} <= set(by_market)
    for market, request in by_market.items():
        coverage = request["coverage"]
        assert coverage == request["stats"]["coverage"], market
        if market in FIXTURE_MARKETS:
            assert coverage["status"] == "covered"
            assert coverage["reason"] == "fixture_library" and coverage["is_simulated"] is True
    toronto = by_market[REPLAY_MARKET]["coverage"]
    assert (toronto["status"], toronto["reason"]) == ("covered", "live_sources")
    assert (toronto["sources"], toronto["sources_failed"]) == (len(CORPUS_KEYS), 0)
    winnipeg = by_market[UNCOVERED_MARKET]
    assert (winnipeg["coverage"]["status"], winnipeg["coverage"]["reason"]) == (
        "not_covered",
        "no_source_for_market",
    )
    assert winnipeg["stats"]["scanned"] == 0 and winnipeg["stats"]["opportunities"] == 0


def test_replayed_opportunities_stay_in_market_and_name_their_source(shared_world):
    w = shared_world
    loc = w.location(REPLAY_MARKET)
    rows = w.get(f"/workspaces/{w.ws}/opportunities", location_id=loc["id"])
    assert rows and all(r["location_id"] == loc["id"] for r in rows)
    synthetic = any(c.synthetic for c in CORPUS)
    assert all(r["is_simulated"] is synthetic for r in rows)
    for row in rows:
        detail = w.get(f"/workspaces/{w.ws}/opportunities/{row['id']}")
        assert detail["observed_evidence"]
        for entry in detail["observed_evidence"]:
            assert entry["source_type"] == "rss_news"
            assert entry["source_key"] in CORPUS_KEYS
            assert entry["delivery"] == "replay" and entry["publisher"]


def test_the_uncovered_market_returns_nothing_and_fixture_markets_are_unchanged(shared_world):
    w = shared_world
    uncovered = w.location(UNCOVERED_MARKET)
    assert w.get(f"/workspaces/{w.ws}/opportunities", location_id=uncovered["id"]) == []
    dallas = w.location("Dallas, TX")
    rows = w.get(f"/workspaces/{w.ws}/opportunities", location_id=dallas["id"])
    assert rows and all(r["is_simulated"] is True for r in rows)
    detail = w.get(f"/workspaces/{w.ws}/opportunities/{rows[0]['id']}")
    assert all("source_key" not in e for e in detail["observed_evidence"])


def test_replayed_signals_persist_source_keys_and_provenance(shared_world):
    with shared_world.factory() as s:
        raws = s.scalars(select(RawSignal).where(RawSignal.source_key.is_not(None))).all()
        assert {r.source_key for r in raws} == CORPUS_KEYS
        for raw in raws:
            p = raw.raw_metadata["provenance"]
            assert p["source_key"] == raw.source_key and p["delivery"] == "replay"
            assert len(p["feed_sha256"]) == 64 and p["capture_id"]
            assert raw.is_simulated is p["synthetic"]
        norms = s.scalars(select(NormalizedSignal).where(NormalizedSignal.source_key.is_not(None)))
        assert {n.source_key for n in norms} == CORPUS_KEYS
        fixture_raws = s.scalar(
            select(func.count()).select_from(RawSignal).where(RawSignal.source_key.is_(None))
        )
        assert fixture_raws > 0  # the demo library's signals carry no source key


def _relabelled_corpus(dst: Path) -> None:
    """TEST-ONLY: the shipped synthetic captures, copied to a temp dir and marked recorded.

    Exercises the non-simulated path end to end (the bundled corpus is all synthetic, so
    nothing else would catch a pipeline that stamps every live signal simulated again).
    Never written into the repository.
    """
    manifest = json.loads((replay_mod.CORPUS_DIR / "manifest.json").read_text())
    for entry in manifest["captures"]:
        shutil.copy(replay_mod.CORPUS_DIR / entry["file"], dst / entry["file"])
        entry["synthetic"] = False
        entry["captured_at"] = "2026-10-20T00:00:00+00:00"
    (dst / "manifest.json").write_text(json.dumps(manifest))


def _approving_records(corpus: Path) -> tuple[SourceRecord, ...]:
    """TEST-ONLY approved registry records for a relabelled corpus's sources."""
    manifest = json.loads((corpus / "manifest.json").read_text())
    return tuple(
        SourceRecord(
            source_key=e["source_key"],
            connector="rss_news",
            display_name=e["display_name"],
            publisher=e["publisher"],
            feed_url=e["feed_url"],
            markets=tuple(e["markets"]),
            tos_url=e["tos_url"],
            approval_reference=f"DEC-TEST-{e['source_key']}",
            approved_on=date(2026, 10, 22),
            legal_status=LegalStatus.APPROVED,
            enabled=True,
            attribution=f"{e['publisher']} (test)",
        )
        for e in manifest["captures"]
    )


def test_a_recorded_capture_flows_through_as_real_data(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _relabelled_corpus(corpus)
    approved = _approving_records(corpus)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(replay_mod, "CORPUS_DIR", corpus)
        # A recorded capture is real only under an approved record for its source.
        mp.setattr(replay_mod, "APPROVED_SOURCES", approved)
        with _world(tmp_path) as w:
            by_market = w.requests_by_market()
            coverage = by_market[REPLAY_MARKET]["coverage"]
            assert coverage["reason"] == "live_sources" and coverage["is_simulated"] is False
            loc = w.location(REPLAY_MARKET)
            rows = w.get(f"/workspaces/{w.ws}/opportunities", location_id=loc["id"])
            assert rows and all(r["is_simulated"] is False for r in rows)
            with w.factory() as s:
                raws = s.scalars(select(RawSignal).where(RawSignal.source_key.is_not(None))).all()
                assert {r.is_simulated for r in raws} == {False}
                by_key = {a.source_key: a for a in approved}
                for raw in raws:  # stamped with the approving record, not a test value
                    p = raw.raw_metadata["provenance"]
                    assert p["approval_reference"] == by_key[raw.source_key].approval_reference
                    assert p["tos_url"] == by_key[raw.source_key].tos_url
                    assert "unverified_recorded_capture" not in p
                norms = s.scalars(
                    select(NormalizedSignal).where(NormalizedSignal.source_key.is_not(None))
                ).all()
                assert {n.is_simulated for n in norms} == {False}
                # The intelligence read of a live signal is not labelled simulated either
                # (the SPA renders that flag as a badge).
                assert {n.ingest_metadata["intelligence"]["is_simulated"] for n in norms} == {False}
                records = s.scalars(
                    select(SignalIntelligenceRecord).where(
                        SignalIntelligenceRecord.normalized_signal_id.in_([n.id for n in norms])
                    )
                ).all()
                assert records and {r.is_simulated for r in records} == {False}
            # The demo markets stay simulated alongside.
            assert by_market["Dallas, TX"]["coverage"]["is_simulated"] is True


def test_a_recorded_capture_without_an_approved_source_replays_as_simulated(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _relabelled_corpus(corpus)  # marked recorded, but no APPROVED_SOURCES record vouches
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(replay_mod, "CORPUS_DIR", corpus)
        with _world(tmp_path) as w:
            coverage = w.requests_by_market()[REPLAY_MARKET]["coverage"]
            assert coverage["reason"] == "live_sources" and coverage["is_simulated"] is True
            loc = w.location(REPLAY_MARKET)
            rows = w.get(f"/workspaces/{w.ws}/opportunities", location_id=loc["id"])
            assert rows and all(r["is_simulated"] is True for r in rows)
            with w.factory() as s:
                raws = s.scalars(select(RawSignal).where(RawSignal.source_key.is_not(None))).all()
                assert raws and {r.is_simulated for r in raws} == {True}
                for raw in raws:
                    p = raw.raw_metadata["provenance"]
                    assert p["unverified_recorded_capture"] is True
                    assert p["approval_reference"] == replay_mod.TEST_APPROVAL_REFERENCE


def test_the_replay_cli_reads_one_corpus_on_both_sides(tmp_path):
    """``python -m app.connectors.replay seed --corpus DIR``: the server and the live
    connector must read the SAME corpus (the module runs as ``__main__`` under ``-m``)."""
    import os
    import subprocess
    import sys

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _relabelled_corpus(corpus)
    api_dir = Path(__file__).resolve().parents[2]
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'cli.db'}"}
    for key in (
        "CONNECTOR_RSS_REPLAY_ORIGIN",
        "CONNECTOR_RSS_LIVE_ENABLED",
        "CONNECTOR_RSS_ENABLED",
    ):
        env.pop(key, None)

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", *args], cwd=api_dir, env=env, capture_output=True, text=True
        )

    for step in (("alembic", "upgrade", "head"), ("app.db.seed",)):
        done = run(*step)
        assert done.returncode == 0, done.stdout + done.stderr
    done = run("app.connectors.replay", "seed", "--corpus", str(corpus))
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert out["synthetic_captures"] == [] and len(out["recorded_captures"]) == len(CORPUS)
    # No approved source vouches for them, so they replay as simulated (MINOR-5) ...
    assert sorted(out["unverified_recorded_captures"]) == sorted(out["recorded_captures"])
    coverage = out["summary"][REPLAY_MARKET]["coverage"]
    assert coverage["reason"] == "live_sources" and coverage["is_simulated"] is True
    # ... and the connector read THIS corpus, not the bundled synthetic one: only a
    # relabelled capture carries the "unverified recorded capture" marker.
    import sqlite3

    with sqlite3.connect(tmp_path / "cli.db") as conn:
        stored = [
            json.loads(meta)["provenance"]
            for (meta,) in conn.execute(
                "SELECT raw_metadata FROM raw_signals WHERE source_key IS NOT NULL"
            )
        ]
    assert stored and all(p["unverified_recorded_capture"] is True for p in stored)
    assert {p["capture_id"] for p in stored} == {c.capture_id for c in CORPUS}


def test_run_history_stats_carry_the_coverage_reason() -> None:
    coverage = {
        "status": "not_covered",
        "reason": "no_source_for_market",
        "is_simulated": False,
        "sources": 0,
        "sources_failed": 0,
    }
    base = {"scanned": 0, "noise_filtered": 0, "signals_analyzed": 0, "opportunities": 0}
    stats = _map_stats(Job(result_summary={**base, "coverage": coverage}))
    assert stats is not None and stats.coverage is not None
    assert stats.coverage.reason is CoverageReason.NO_SOURCE_FOR_MARKET
    # A run that predates coverage recording (or a malformed record) maps to None.
    assert _map_stats(Job(result_summary=base)).coverage is None
    assert _map_stats(Job(result_summary={**base, "coverage": {"reason": "x"}})).coverage is None


# --------------------------------------------------------------------------- #
# Source selection (no database)
# --------------------------------------------------------------------------- #
def _settings(**overrides) -> Settings:
    # Explicit values: the shared world exports replay settings for this module's lifetime.
    dark = {"connector_rss_live_enabled": False, "connector_rss_replay_origin": None}
    return Settings(_env_file=None, **{**dark, **overrides})


@pytest.mark.parametrize(
    ("market", "overrides", "reason", "has_signals"),
    [
        ("Dallas, TX", {}, CoverageReason.FIXTURE_LIBRARY, True),
        ("Berlin, DE", {}, CoverageReason.NO_SOURCE_FOR_MARKET, False),
        (REPLAY_MARKET, {}, CoverageReason.NO_SOURCE_FOR_MARKET, False),
        ("Dallas, TX", {"connector_fixture_fallback_enabled": False},
         CoverageReason.NO_SOURCE_FOR_MARKET, False),
        ("Dallas, TX", {"connector_rss_enabled": True}, CoverageReason.SANDBOX_FEED, True),
        ("Berlin, DE", {"connector_rss_enabled": True}, CoverageReason.NO_SOURCE_FOR_MARKET, False),
    ],
)
def test_source_selection_always_states_a_reason(market, overrides, reason, has_signals):
    batch = fetch_signals(
        market=market,
        keywords=[],
        source_types=[],
        rss_enabled=overrides.get("connector_rss_enabled", False),
        settings=_settings(**overrides),
    )
    assert batch.coverage.reason is reason
    assert bool(batch.signals) is has_signals
    assert batch.coverage.is_simulated is (reason in {
        CoverageReason.FIXTURE_LIBRARY, CoverageReason.SANDBOX_FEED
    })


def test_live_sources_that_all_fail_report_unavailable_not_empty():
    feed_cache.clear()
    with ReplayServer() as server:
        origin = server.origin
    # The server is gone: every live source now fails to connect.
    settings = _settings(
        connector_rss_live_enabled=True,
        connector_rss_replay_origin=origin,
        connector_rss_max_attempts=1,
    )
    batch = fetch_signals(
        market=REPLAY_MARKET, keywords=[], source_types=[], rss_enabled=True, settings=settings
    )
    assert batch.signals == []
    assert batch.coverage.reason is CoverageReason.SOURCES_UNAVAILABLE
    assert batch.coverage.status.value == "unavailable"
    assert (batch.coverage.sources, batch.coverage.sources_failed) == (2, 2)


def test_an_undecodable_capture_fails_its_source_only_end_to_end(tmp_path):
    """The reviewer's shape: one capture re-declared Shift_JIS, served over loopback."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    manifest = json.loads((replay_mod.CORPUS_DIR / "manifest.json").read_text())
    bad, good = manifest["captures"][0], manifest["captures"][1]
    for entry in manifest["captures"]:
        data = (replay_mod.CORPUS_DIR / entry["file"]).read_bytes()
        if entry is bad:
            data = data.replace(b'encoding="UTF-8"', b'encoding="shift_jis"', 1)
            assert b"shift_jis" in data
            entry["sha256"] = hashlib.sha256(data).hexdigest()
        (corpus / entry["file"]).write_bytes(data)
    (corpus / "manifest.json").write_text(json.dumps(manifest))
    feed_cache.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(replay_mod, "CORPUS_DIR", corpus)
        with ReplayServer(corpus) as server:
            settings = _settings(
                connector_rss_live_enabled=True, connector_rss_replay_origin=server.origin
            )
            batch = fetch_signals(
                market=REPLAY_MARKET, keywords=list(good["keywords"]), source_types=[],
                rss_enabled=True, settings=settings,
            )
            requested = sorted(p for p, _s, _u in server.hits)
    assert batch.coverage.reason is CoverageReason.LIVE_SOURCES
    assert (batch.coverage.sources, batch.coverage.sources_failed) == (2, 1)
    assert batch.signals
    assert {s.attribution["source_key"] for s in batch.signals} == {good["source_key"]}
    assert requested == sorted(f"/captures/{c['capture_id']}" for c in (bad, good))
    assert feed_cache.cached(bad["source_key"]) is None  # never cached
    feed_cache.clear()


def test_an_opportunity_is_simulated_if_any_member_is():
    from types import SimpleNamespace

    live, simulated = SimpleNamespace(is_simulated=False), SimpleNamespace(is_simulated=True)
    assert pipeline_mod._card_is_simulated([live, simulated]) is True
    assert pipeline_mod._card_is_simulated([simulated, live]) is True
    assert pipeline_mod._card_is_simulated([live, live]) is False
    assert pipeline_mod._signal_is_simulated(SimpleNamespace()) is True  # fixtures: no flag
    assert pipeline_mod._signal_is_simulated(live) is False


def test_live_feeds_need_the_workspace_rss_decision():
    """P6-CAP-1: a workspace whose RSS is off gets no live egress, whatever the live gate says."""
    feed_cache.clear()
    with ReplayServer() as server:
        settings = _settings(
            connector_rss_live_enabled=True, connector_rss_replay_origin=server.origin
        )
        off = fetch_signals(
            market=REPLAY_MARKET, keywords=[], source_types=[], rss_enabled=False,
            settings=settings,
        )
        assert server.hits == []
        on = fetch_signals(
            market=REPLAY_MARKET, keywords=[], source_types=[], rss_enabled=True,
            settings=settings,
        )
        assert server.hits
    assert off.coverage.reason is CoverageReason.NO_SOURCE_FOR_MARKET and off.signals == []
    assert on.coverage.reason is CoverageReason.LIVE_SOURCES
    feed_cache.clear()


@pytest.mark.parametrize(
    "fn",
    [live_connector_for, fetch_signals, registry_mod.resolve_connector],
    ids=["live_connector_for", "fetch_signals", "resolve_connector"],
)
def test_the_rss_decision_cannot_be_omitted_by_any_caller(fn):
    """P6-CAP-1 (delta review r3, C-r3-2): ``rss_enabled`` is keyword-only with no default
    on every connector entry point, so no caller can bypass the per-workspace decision by
    omission. A default of ``True`` would silently reopen live egress."""
    parameter = inspect.signature(fn, eval_str=True).parameters["rss_enabled"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is bool


def test_the_kill_switch_returns_a_live_market_to_not_covered():
    feed_cache.clear()
    with ReplayServer() as server:
        settings = _settings(
            connector_rss_live_enabled=True,
            connector_rss_kill_switch=True,
            connector_rss_replay_origin=server.origin,
        )
        batch = fetch_signals(
            market=REPLAY_MARKET, keywords=[], source_types=[], rss_enabled=True,
            settings=settings,
        )
        assert server.hits == []
    assert batch.coverage.reason is CoverageReason.NO_SOURCE_FOR_MARKET


# --------------------------------------------------------------------------- #
# Takedown (P6-PRIV-4)
# --------------------------------------------------------------------------- #
def _second_tenant_toronto_run(factory) -> str:
    """A different organization runs its own Toronto scout over the same sources."""
    with factory() as s:
        org = Organization(name="Other Tenant", slug="other-tenant")
        s.add(org)
        s.flush()
        ws = Workspace(organization_id=org.id, name="Main", slug="main")
        s.add(ws)
        s.flush()
        brand = Brand(organization_id=org.id, workspace_id=ws.id, name="Other", industry="coffee")
        s.add(brand)
        s.flush()
        loc = BusinessLocation(
            organization_id=org.id, workspace_id=ws.id, brand_id=brand.id,
            name="Other — Toronto", city="Toronto", state_province="ON", is_active=True,
        )
        s.add(loc)
        s.flush()
        req = ScoutRequest(
            organization_id=org.id, workspace_id=ws.id, brand_id=brand.id, location_id=loc.id,
            name="Other Toronto", status=ScoutRequestStatus.QUEUED.value, source_types=[],
            keywords=["coffee", "cafe", "café", "loyalty", "cold brew"],
            resolved_market=REPLAY_MARKET,
        )
        s.add(req)
        s.flush()
        _run(s, req.id)
        s.commit()
        return org.id


def _toronto_evidence(factory) -> dict[str, set[str]]:
    with factory() as s:
        rows = s.scalars(select(Opportunity).where(Opportunity.resolved_market == REPLAY_MARKET))
        return {
            o.id: {e.get("source_key") for e in o.observed_evidence if isinstance(e, dict)}
            for o in rows
        }


def _count(factory, model, key) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model).where(model.source_key == key))


def test_takedown_purges_every_tenant_and_reports_a_recounted_zero(replay_world):
    w = replay_world
    other_org = _second_tenant_toronto_run(w.factory)
    with w.factory() as s:
        tenants = set(
            s.scalars(select(RawSignal.organization_id).where(RawSignal.source_key == TAKEN))
        )
        fixture_before = s.scalar(
            select(func.count()).select_from(RawSignal).where(RawSignal.source_key.is_(None))
        )
    assert tenants == {seed_mod.sid("org"), other_org}
    raw_before = _count(w.factory, RawSignal, TAKEN)
    kept_before = _count(w.factory, RawSignal, KEPT)
    evidence = _toronto_evidence(w.factory)
    only_taken = {o for o, keys in evidence.items() if keys == {TAKEN}}
    mixed = {o for o, keys in evidence.items() if TAKEN in keys and len(keys) > 1}
    assert only_taken and mixed  # both purge paths are exercised by the corpus

    r = w.client.post(
        f"{API}/internal/system/data-sources/{TAKEN}/takedown",
        headers=w.auth,
        json={"reference": "TKD-1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_taken_down"] is False and body["verified"] is True
    zero = {"raw_signals": 0, "normalized_signals": 0, "opportunities_citing": 0}
    assert body["remaining"] == zero
    assert body["purged"]["raw_signals"] == raw_before
    assert body["purged"]["opportunities_deleted"] == len(only_taken)
    assert body["purged"]["opportunities_redacted"] == len(mixed)

    # Independent recount, not the service's own numbers.
    assert _count(w.factory, RawSignal, TAKEN) == 0
    assert _count(w.factory, NormalizedSignal, TAKEN) == 0
    assert _count(w.factory, RawSignal, KEPT) == kept_before
    after = _toronto_evidence(w.factory)
    assert not (only_taken & set(after))
    assert mixed <= set(after) and all(TAKEN not in after[o] for o in mixed)
    with w.factory() as s:
        assert s.scalar(
            select(func.count()).select_from(RawSignal).where(RawSignal.source_key.is_(None))
        ) == fixture_before
        row = s.scalar(select(SourceTakedown).where(SourceTakedown.source_key == TAKEN))
        # D-9d: the row is the suppression record only (key, time, counts). Who took the
        # source down and which request it answers are in the affected workspaces' audit
        # entries, not here.
        assert row.purge_summary == body["purged"] and row.repeat_purges == []
        entries = s.scalars(
            select(AuditLog).where(AuditLog.action == "data_source.taken_down")
        ).all()
        assert entries and all(
            e.actor_user_id == seed_mod.sid("user") and e.context["reference"] == "TKD-1"
            for e in entries
        )

    listing = w.get("/internal/system/data-sources")
    entry = {e["source_key"]: e for e in listing["sources"]}[TAKEN]
    assert set(entry["takedown"]) == {"taken_down_at", "purge_summary", "repeat_purges"}
    assert entry["stored"] == {"raw_signals": 0, "normalized_signals": 0, "opportunities_citing": 0}
    assert listing["replay_mode"] is True


def test_a_taken_down_source_is_never_fetched_again(replay_world):
    w = replay_world
    r = w.client.post(
        f"{API}/internal/system/data-sources/{TAKEN}/takedown",
        headers=w.auth,
        json={"reference": "TKD-2"},
    )
    assert r.status_code == 200
    feed_cache.clear()
    hits_before = len(w.server.hits)
    with w.factory() as s:
        req = s.scalar(select(ScoutRequest).where(ScoutRequest.resolved_market == REPLAY_MARKET))
        stats = _run(s, req.id)
        s.commit()
    new_paths = [p for p, _s, _u in w.server.hits[hits_before:]]
    assert new_paths and all(TAKEN not in p and "business" not in p for p in new_paths)
    assert stats["coverage"]["sources"] == len(CORPUS_KEYS) - 1
    assert _count(w.factory, RawSignal, TAKEN) == 0


def test_takedown_is_idempotent_and_permanent(replay_world):
    w = replay_world
    url = f"{API}/internal/system/data-sources/{TAKEN}/takedown"
    first = w.client.post(url, headers=w.auth, json={"reference": "TKD-3"}).json()
    second = w.client.post(url, headers=w.auth, json={"reference": "TKD-3-again"}).json()
    assert second["already_taken_down"] is True and second["verified"] is True
    assert second["taken_down_at"] == first["taken_down_at"]
    assert all(v == 0 for v in second["purged"].values())
    assert first["purged"]["raw_signals"] > 0
    with w.factory() as s:
        [row] = s.scalars(select(SourceTakedown)).all()
        # The first purge's evidence is kept; the repeat is appended, not written over it,
        # and it holds a time and counts only (D-9d).
        assert row.purge_summary == first["purged"]
        [repeat] = row.repeat_purges
        assert set(repeat) == {"at", "purged"} and repeat["purged"] == second["purged"]
    entry = {e["source_key"]: e for e in w.get("/internal/system/data-sources")["sources"]}[TAKEN]
    assert entry["takedown"]["purge_summary"] == first["purged"]
    assert [set(r) for r in entry["takedown"]["repeat_purges"]] == [{"at", "purged"}]
    assert entry["takedown"]["repeat_purges"][0]["purged"] == second["purged"]


def test_a_run_in_flight_never_reinserts_a_source_taken_down_meanwhile(replay_world, monkeypatch):
    """A run that read the takedowns before a takedown committed (emulated: its
    start-of-run read returns nothing) must not write the source back after the purge
    answered verified."""
    w = replay_world
    url = f"{API}/internal/system/data-sources/{TAKEN}/takedown"
    done = w.client.post(url, headers=w.auth, json={"reference": "TKD-4"}).json()
    assert done["verified"] is True and _count(w.factory, RawSignal, TAKEN) == 0
    kept_before = _count(w.factory, RawSignal, KEPT)
    monkeypatch.setattr(pipeline_mod, "active_takedowns", lambda db: frozenset())
    feed_cache.clear()
    hits_before = len(w.server.hits)
    with w.factory() as s:
        req = s.scalar(select(ScoutRequest).where(ScoutRequest.resolved_market == REPLAY_MARKET))
        _run(s, req.id)
        s.commit()
    fetched = [p for p, _s, _u in w.server.hits[hits_before:]]
    assert any("business" in p for p in fetched)  # the stale read did fetch the source
    assert _count(w.factory, RawSignal, TAKEN) == 0
    assert _count(w.factory, NormalizedSignal, TAKEN) == 0
    assert _count(w.factory, RawSignal, KEPT) > kept_before  # the other source still lands
    entry = {e["source_key"]: e for e in w.get("/internal/system/data-sources")["sources"]}[TAKEN]
    assert entry["stored"] == {"raw_signals": 0, "normalized_signals": 0, "opportunities_citing": 0}


def test_takedown_writes_one_audit_entry_in_each_affected_workspace(replay_world):
    w = replay_world
    other_org = _second_tenant_toronto_run(w.factory)
    url = f"{API}/internal/system/data-sources/{TAKEN}/takedown"
    # An operator's free-text reason may be legal or confidential and must not reach any
    # tenant's audit log (adversarial r2-MINOR-2, mutant N10: ``reason=takedown.reason``).
    # Since D-9e (interim) the request has no free-text field at all: text sent anyway is
    # refused, nothing is taken down, and the only operator-supplied value that reaches
    # a tenant row is the public reference.
    operator_reason = "Publisher request, counsel memo 2026-10-27 (confidential)"
    assert set(TakedownIn.model_fields) == {"reference"}
    refused = w.client.post(
        url, headers=w.auth, json={"reason": operator_reason, "reference": "TKD-9"}
    )
    assert refused.status_code == 422, refused.text
    assert _count(w.factory, RawSignal, TAKEN) > 0
    with w.factory() as s:
        assert s.scalars(select(SourceTakedown)).all() == []
    body = w.client.post(url, headers=w.auth, json={"reference": "TKD-9"}).json()
    with w.factory() as s:
        takedown = s.scalar(select(SourceTakedown).where(SourceTakedown.source_key == TAKEN))
        entries = s.scalars(
            select(AuditLog).where(AuditLog.action == "data_source.taken_down")
        ).all()
        assert {e.organization_id for e in entries} == {seed_mod.sid("org"), other_org}
        assert len({(e.organization_id, e.workspace_id) for e in entries}) == len(entries)
        for e in entries:
            assert e.entity_type == "source_takedown" and e.entity_id == takedown.id
            assert e.actor_user_id == seed_mod.sid("user")
            assert e.context["source_key"] == TAKEN and e.context["reference"] == "TKD-9"
            assert e.context["repeat"] is False
            assert len(e.context["opportunities_deleted"]) == e.new_state["opportunities_deleted"]
            assert len(e.context["opportunities_redacted"]) == e.new_state["opportunities_redacted"]
            assert e.reason == takedown_mod._AUDIT_REASON
            assert operator_reason not in json.dumps([e.reason, e.context, e.new_state])
            assert set(e.context) == {
                "source_key", "reference", "repeat", "opportunities_deleted",
                "opportunities_redacted",
            }
        assert operator_reason not in json.dumps(
            [takedown.purge_summary, takedown.repeat_purges]
        )
        # The per-workspace entries add up to exactly what the takedown removed.
        for kind in (
            "raw_signals", "normalized_signals", "opportunities_deleted",
            "opportunities_redacted", "feedback_removed",
        ):
            assert sum(e.new_state[kind] for e in entries) == body["purged"][kind], kind
    # The customer sees it in their own audit log.
    logs = w.get(f"/workspaces/{w.ws}/audit-logs")
    assert any(r["action"] == "data_source.taken_down" for r in logs)
    # A repeat that removes nothing writes no further tenant entry.
    w.client.post(url, headers=w.auth, json={"reference": "TKD-9-again"})
    with w.factory() as s:
        count = s.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "data_source.taken_down"
            )
        )
    assert count == len(entries)


def test_takedown_requires_an_operator_and_changes_nothing_otherwise(replay_world):
    w = replay_world
    url = f"{API}/internal/system/data-sources/{TAKEN}/takedown"
    before = _count(w.factory, RawSignal, TAKEN)
    assert w.client.post(url, json={"reference": "TKD-X"}).status_code == 401
    with w.factory() as s:
        s.add(User(id="plain-user", email="plain@example.com", full_name="Plain",
                   hashed_password="x", is_active=True, is_operator=False))
        s.commit()
    plain = {"Authorization": f"Bearer {live_bearer('plain-user')}"}
    assert w.client.post(url, headers=plain, json={"reference": "TKD-X"}).status_code == 403
    assert w.client.get(f"{API}/internal/system/data-sources", headers=plain).status_code == 403
    assert _count(w.factory, RawSignal, TAKEN) == before > 0
    with w.factory() as s:
        assert s.scalars(select(SourceTakedown)).all() == []


def test_an_organization_owner_gets_the_operator_refusal_and_learns_nothing(replay_world):
    """A customer OWNER is not an operator: both data-source routes answer the same 403
    for a real key and an unknown one, so a customer cannot enumerate sources."""
    w = replay_world
    owner_id = seed_mod.sid("user")
    with w.factory() as s:
        member = s.scalars(
            select(OrganizationMember).where(OrganizationMember.user_id == owner_id)
        ).one()
        assert member.role == Role.OWNER.value
        s.get(User, owner_id).is_operator = False  # the demo owner minus the dev/test grant
        s.commit()
    before = _count(w.factory, RawSignal, TAKEN)
    base = f"{API}/internal/system/data-sources"
    answers = [
        w.client.get(base, headers=w.auth),
        w.client.post(f"{base}/{TAKEN}/takedown", headers=w.auth, json={"reference": "TKD-X"}),
        w.client.post(
            f"{base}/no-such-source/takedown", headers=w.auth, json={"reference": "TKD-X"}
        ),
    ]
    assert [r.status_code for r in answers] == [403, 403, 403]
    # A real key and an unknown one look alike (only the per-request id differs).
    bodies = [{k: v for k, v in r.json()["error"].items() if k != "request_id"} for r in answers]
    assert bodies[1] == bodies[2] == {
        "code": "permission_denied",
        "message": "Operator privileges are required for this resource.",
    }
    assert _count(w.factory, RawSignal, TAKEN) == before > 0
    with w.factory() as s:
        assert s.scalars(select(SourceTakedown)).all() == []


@pytest.mark.parametrize(
    ("key", "body", "status"),
    [
        ("no-such-source", {"reference": "TKD-1"}, 404),
        ("Bad_Key", {"reference": "TKD-1"}, 422),
        (TAKEN, {"reference": ""}, 422),
        (TAKEN, {"reference": "   "}, 422),
        (TAKEN, {}, 422),
        (TAKEN, {"reference": "x" * 201}, 422),
        # A public ticket id only: never an address, never a URL with credentials, never
        # more than one line, and no free-text field at all (D-9d / D-9e interim).
        (TAKEN, {"reference": "counsel@example.com"}, 422),
        (TAKEN, {"reference": "https://user:secret@tracker.example/T-1"}, 422),
        (TAKEN, {"reference": "TKD-1\nsee attached memo"}, 422),
        (TAKEN, {"reference": "TKD-1", "reason": "Publisher request"}, 422),
    ],
)
def test_takedown_input_is_validated(replay_world, key, body, status):
    w = replay_world
    url = f"{API}/internal/system/data-sources/{key}/takedown"
    r = w.client.post(url, headers=w.auth, json=body)
    assert r.status_code == status, r.text
    assert _count(w.factory, RawSignal, TAKEN) > 0
