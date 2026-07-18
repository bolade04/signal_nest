"""SB-C hardening item 1: the four-active-schedule workspace cap holds under real
concurrency.

The cap is a check-then-act invariant that a naive implementation can violate when two
enable operations interleave. :func:`app.scouting_requests.schedules._lock_workspace_for_cap`
takes a ``SELECT ... FOR UPDATE`` on the stable workspace row so the count + enable is one
atomic critical section. ``FOR UPDATE`` only truly blocks on PostgreSQL, so these tests are
gated on ``TEST_POSTGRES_URL`` and skipped otherwise; the always-run compile proof lives in
``test_scout_schedule_api.py::TestCapLockCompiles``.

Each test drives many threads, each on its own session/transaction, against a single
workspace and asserts the workspace never ends with more than four enabled schedules —
covering concurrent create+create and mixed create+resume contention.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.brands.models import Brand
from app.core.config import get_settings
from app.core.enums import ScheduleInterval
from app.core.errors import ConflictError
from app.db.base import Base
from app.organizations.models import Organization, Workspace
from app.scouting_requests import schedules as sched
from app.scouting_requests.models import ScoutRequest, ScoutSchedule

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL cap-contention test",
)

_ORG = "org-cap"
_WS = "ws-cap"
_BRAND = "brand-cap"
_CAP = sched.MAX_ACTIVE_SCHEDULES_PER_WORKSPACE


def _fresh_engine():  # pragma: no cover - gated on live PG
    url = os.environ["TEST_POSTGRES_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine


def _seed_requests(factory, n: int) -> list[str]:  # pragma: no cover - gated on live PG
    ids: list[str] = []
    with factory() as s:
        s.add(Organization(id=_ORG, name="Cap Org", slug="cap-org"))
        s.add(Workspace(id=_WS, organization_id=_ORG, name="Cap WS", slug="cap-ws"))
        s.flush()
        # PostgreSQL enforces the real scout_requests -> brands foreign key
        # (scout_requests_brand_id_fkey), so the parent brand must exist before any
        # ScoutRequest references it. The local SQLite skip path never exercised this.
        # These models use bare FK columns (no ORM relationships), so the brand is
        # flushed after its org/workspace parents to guarantee insert order.
        s.add(Brand(id=_BRAND, organization_id=_ORG, workspace_id=_WS, name="Cap Brand"))
        s.flush()
        for i in range(n):
            req = ScoutRequest(
                organization_id=_ORG,
                workspace_id=_WS,
                brand_id=_BRAND,
                name=f"req-{i}",
                status="draft",
                source_types=[],
                keywords=[],
                stats={},
            )
            s.add(req)
            s.flush()
            ids.append(req.id)
        s.commit()
    return ids


def _enabled_count(factory) -> int:  # pragma: no cover - gated on live PG
    with factory() as s:
        return int(
            s.scalar(
                select(func.count())
                .select_from(ScoutSchedule)
                .where(ScoutSchedule.workspace_id == _WS, ScoutSchedule.enabled.is_(True))
            )
            or 0
        )


def test_concurrent_creates_never_exceed_cap():  # pragma: no cover - gated on live PG
    get_settings().scout_scheduling_enabled = False  # avoid tick fan-out noise
    engine = _fresh_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        request_ids = _seed_requests(factory, _CAP * 3)  # far more than the cap

        def _create(request_id: str) -> str:
            s = factory()
            try:
                req = s.get(ScoutRequest, request_id)
                sched.create_schedule(s, request=req, interval=ScheduleInterval.DAILY)
                s.commit()
                return "ok"
            except ConflictError:
                s.rollback()
                return "conflict"
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=len(request_ids)) as pool:
            outcomes = list(pool.map(_create, request_ids))

        assert outcomes.count("ok") == _CAP
        assert _enabled_count(factory) == _CAP
    finally:
        engine.dispose()


def test_concurrent_create_and_resume_respect_cap():  # pragma: no cover - gated on live PG
    get_settings().scout_scheduling_enabled = False
    engine = _fresh_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        request_ids = _seed_requests(factory, _CAP * 3)
        # Pre-create the cap's worth of *paused* schedules, then concurrently resume
        # them while also creating fresh enabled ones. Total enabled must still cap.
        paused_ids = request_ids[:_CAP]
        create_ids = request_ids[_CAP:]
        with factory() as s:
            for rid in paused_ids:
                req = s.get(ScoutRequest, rid)
                sched.create_schedule(
                    s, request=req, interval=ScheduleInterval.DAILY, enabled=False
                )
            s.commit()

        def _resume(request_id: str) -> str:
            s = factory()
            try:
                schedule = s.scalar(
                    select(ScoutSchedule).where(ScoutSchedule.scout_request_id == request_id)
                )
                sched.resume_schedule(s, schedule=schedule)
                s.commit()
                return "ok"
            except ConflictError:
                s.rollback()
                return "conflict"
            finally:
                s.close()

        def _create(request_id: str) -> str:
            s = factory()
            try:
                req = s.get(ScoutRequest, request_id)
                sched.create_schedule(s, request=req, interval=ScheduleInterval.DAILY)
                s.commit()
                return "ok"
            except ConflictError:
                s.rollback()
                return "conflict"
            finally:
                s.close()

        tasks = [("resume", r) for r in paused_ids] + [("create", r) for r in create_ids]
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            list(pool.map(lambda t: _resume(t[1]) if t[0] == "resume" else _create(t[1]), tasks))

        assert _enabled_count(factory) == _CAP
    finally:
        engine.dispose()
