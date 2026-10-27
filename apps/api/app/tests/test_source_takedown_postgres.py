"""P6-PRIV-4 on PostgreSQL: replayed live runs and a source takedown on the migrated schema.

The SQLite suite (``test_real_data_replay``) proves the behaviour through the HTTP API;
this module proves the same purge on PostgreSQL — the production database — where the
foreign keys, the ``CAST(json AS TEXT) LIKE`` evidence prefilter and the multi-table
deletes run on the real engine. The schema comes from Alembic (``upgrade head``) on a
database of this module's own; the runs go through the real pipeline with the live RSS
connector reading the replay corpus from a loopback server.

Gate: :func:`require_postgres` — FAILS in CI when ``TEST_POSTGRES_URL`` is unset and
skips only on a developer machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.brands.models import Brand
from app.connectors.feedcache import feed_cache
from app.connectors.models import SourceTakedown
from app.connectors.replay import ReplayServer
from app.connectors.takedown import lock_sources, source_signal_counts, takedown_source
from app.core.config import get_settings
from app.core.enums import ScoutRequestStatus
from app.jobs.pipeline import _run
from app.locations.models import BusinessLocation
from app.opportunities.models import Opportunity
from app.organizations.models import Organization, Workspace
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, RawSignal
from app.tests._auth2_support import pg_database, require_postgres

API_DIR = Path(__file__).resolve().parents[2]
MARKET = "Toronto, ON"
TAKEN = "replay-toronto-business"
KEPT = "replay-toronto-food"


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


@pytest.fixture
def pg_factory() -> Iterator[sessionmaker]:
    with pg_database("sn_tkd") as url:
        env = {**os.environ, "DATABASE_URL": url}
        env.pop("SN_MIGRATION_MODE", None)
        done = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_DIR, env=env, capture_output=True, text=True,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        engine = create_engine(url)
        try:
            yield sessionmaker(bind=engine, expire_on_commit=False)
        finally:
            engine.dispose()


@pytest.fixture
def replay_settings() -> Iterator[ReplayServer]:
    feed_cache.clear()
    with pytest.MonkeyPatch.context() as mp, ReplayServer() as server:
        mp.setenv("CONNECTOR_RSS_REPLAY_ORIGIN", server.origin)
        mp.setenv("CONNECTOR_RSS_LIVE_ENABLED", "true")
        # P6-CAP-1: live feeds also need the capability resolver's RSS decision, i.e. the
        # global flag (and no workspace disable override), as the SQLite replay world sets.
        mp.setenv("CONNECTOR_RSS_ENABLED", "true")
        get_settings.cache_clear()
        try:
            yield server
        finally:
            get_settings.cache_clear()
            feed_cache.clear()


def _tenant_run(factory, slug: str) -> str:
    with factory() as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        s.flush()
        ws = Workspace(organization_id=org.id, name="Main", slug="main")
        s.add(ws)
        s.flush()
        brand = Brand(organization_id=org.id, workspace_id=ws.id, name=slug, industry="coffee")
        s.add(brand)
        s.flush()
        loc = BusinessLocation(
            organization_id=org.id, workspace_id=ws.id, brand_id=brand.id,
            name=f"{slug} Toronto", city="Toronto", state_province="ON", is_active=True,
        )
        s.add(loc)
        s.flush()
        req = ScoutRequest(
            organization_id=org.id, workspace_id=ws.id, brand_id=brand.id, location_id=loc.id,
            name="Toronto", status=ScoutRequestStatus.QUEUED.value, source_types=[],
            keywords=["coffee", "cafe", "café", "loyalty", "cold brew"], resolved_market=MARKET,
        )
        s.add(req)
        s.flush()
        stats = _run(s, req.id)
        s.commit()
        assert stats["coverage"]["reason"] == "live_sources", stats
        return org.id


def test_replayed_runs_and_a_takedown_purge_on_postgresql(pg_factory, replay_settings):
    orgs = {_tenant_run(pg_factory, "tenant-a"), _tenant_run(pg_factory, "tenant-b")}
    with pg_factory() as s:
        assert set(
            s.scalars(select(RawSignal.organization_id).where(RawSignal.source_key == TAKEN))
        ) == orgs
        kept_before = s.scalar(
            select(func.count()).select_from(RawSignal).where(RawSignal.source_key == KEPT)
        )
        evidence = {
            o.id: {e.get("source_key") for e in o.observed_evidence}
            for o in s.scalars(select(Opportunity))
        }
    only_taken = {o for o, keys in evidence.items() if keys == {TAKEN}}
    mixed = {o for o, keys in evidence.items() if TAKEN in keys and len(keys) > 1}
    assert only_taken and mixed

    with pg_factory() as s:
        result = takedown_source(
            s, source_key=TAKEN, reason="Publisher request", reference="TKD-PG",
            actor_user_id=None,
        )
        s.commit()
    assert result.verified
    zero = {"raw_signals": 0, "normalized_signals": 0, "opportunities_citing": 0}
    assert result.remaining == zero
    assert result.purged["opportunities_deleted"] == len(only_taken)
    assert result.purged["opportunities_redacted"] == len(mixed)

    with pg_factory() as s:  # an independent recount in a new session
        assert source_signal_counts(s, TAKEN) == result.remaining
        assert s.scalar(
            select(func.count()).select_from(NormalizedSignal)
            .where(NormalizedSignal.source_key == TAKEN)
        ) == 0
        assert s.scalar(
            select(func.count()).select_from(RawSignal).where(RawSignal.source_key == KEPT)
        ) == kept_before
        remaining = {o.id: o for o in s.scalars(select(Opportunity))}
        assert not (only_taken & set(remaining))
        for oid in mixed:
            assert all(e.get("source_key") != TAKEN for e in remaining[oid].observed_evidence)
        assert s.scalar(select(SourceTakedown.reference)) == "TKD-PG"

    with pg_factory() as s:  # idempotent repeat
        again = takedown_source(
            s, source_key=TAKEN, reason="again", reference=None, actor_user_id=None
        )
        s.commit()
    assert again.already_taken_down and again.verified
    assert all(v == 0 for v in again.purged.values())


def _lock_timeout(session) -> None:
    session.execute(text("SET LOCAL lock_timeout = '300ms'"))


def test_a_takedown_waits_for_a_run_that_is_writing_the_source(pg_factory):
    """The shared per-source lock a scout run takes before writing a live source's
    signals (``without_taken_down``) holds a takedown of that source until the run
    commits, so the purge always sees the run's rows; other sources and other runs are
    not held up."""
    run, other_run, take = pg_factory(), pg_factory(), pg_factory()
    try:
        lock_sources(run, [TAKEN], exclusive=False)  # a run mid-write, transaction open
        lock_sources(other_run, [TAKEN], exclusive=False)  # runs share the lock
        other_run.rollback()

        _lock_timeout(take)
        with pytest.raises(DBAPIError) as held:
            lock_sources(take, [TAKEN], exclusive=True)
        assert getattr(held.value.orig, "sqlstate", None) == "55P03"  # lock_not_available
        take.rollback()

        _lock_timeout(take)
        lock_sources(take, [KEPT], exclusive=True)  # another source: not held
        take.rollback()

        run.commit()  # the run's write is durable; its lock is released with it
        _lock_timeout(take)
        lock_sources(take, [TAKEN], exclusive=True)
        take.rollback()
    finally:
        for session in (run, other_run, take):
            session.close()
