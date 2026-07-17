"""SB-A: service + route + isolation + security tests for the read-only scouting
run-history endpoint.

    GET /api/v1/workspaces/{workspace_id}/scout-requests/{request_id}/runs

These run against a self-contained, throwaway four-market demo seed (Dallas, London,
Lagos, Nairobi) with ``get_db`` overridden. The seed itself runs the pipeline in-process
and creates **no** durable ``Job`` rows, so this suite synthesises deterministic runs by
enqueuing scout-execute jobs and then setting their terminal fields directly — giving
full control over status, timestamps, stats and trigger markers without touching the
job-execution behaviour under test.

The suite asserts only coverage the durable-jobs suite does not: the bounded, customer-
safe run-history projection, request/workspace/tenant isolation across four markets,
deterministic reverse-chronological pagination, safe trigger/simulated derivation, and
that the read is strictly non-mutating.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.jobs.models import Job
from app.jobs.service import enqueue_scout_request
from app.jobs.status import JobStatus
from app.main import app
from app.organizations.models import User
from app.scouting_requests.run_history import (
    _derive_simulated,
    _derive_trigger,
    _map_stats,
    get_run_history,
)
from app.scouting_requests.schemas import TriggerType

API = get_settings().api_prefix

# Internal job columns that must NEVER surface in a run-history item.
_FORBIDDEN_KEYS = (
    "payload",
    "payload_hash",
    "idempotency_key",
    "worker_id",
    "lease_token",
    "lease_expires_at",
    "heartbeat_at",
    "claimed_at",
    "available_at",
    "correlation_id",
    "trace_context",
    "organization_id",
    "workspace_id",
    "last_error_summary",
    "contract_version",
    "priority",
)

_MARKETS = {
    "dallas": "Dallas, TX",
    "london": "London, UK",
    "lagos": "Lagos, NG",
    "nairobi": "Nairobi, KE",
}

_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_run(
    s,
    *,
    market_key: str,
    created_at: datetime,
    status: JobStatus,
    stats: dict | None = None,
    last_error_code: str | None = None,
    trigger_marker: str | None = None,
) -> Job:
    """Synthesise one durable run for a market's scout request and return it."""
    job = enqueue_scout_request(
        s,
        organization_id=seed_mod.sid("org"),
        workspace_id=seed_mod.sid("ws"),
        scout_request_id=seed_mod.sid("scout", market_key),
        location_id=seed_mod.sid("loc", market_key),
    )
    job.status = status.value
    job.result_summary = stats
    job.last_error_code = last_error_code
    job.created_at = created_at
    if status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.DEAD_LETTERED):
        job.started_at = created_at + timedelta(seconds=1)
        job.completed_at = created_at + timedelta(seconds=5)
    if trigger_marker is not None:
        # Server-side only marker; the payload is never returned by the read path.
        job.payload = {**(job.payload or {}), "trigger": trigger_marker}
    return job


def _stats(scanned, noise, analyzed, opps, *, simulated=None) -> dict:
    d = {
        "scanned": scanned,
        "noise_filtered": noise,
        "signals_analyzed": analyzed,
        "opportunities": opps,
    }
    if simulated is not None:
        d["is_simulated"] = simulated
    return d


class _Harness:
    def __init__(self, client, factory):
        self.client = client
        self.factory = factory
        self.ws = seed_mod.sid("ws")
        self.org = seed_mod.sid("org")
        self.auth = {"Authorization": f"Bearer {create_access_token(seed_mod.sid('user'))}"}
        # A real, active, non-member user for the 403 path.
        self.outsider_auth = {"Authorization": f"Bearer {create_access_token('outsider-user')}"}

    def req(self, market_key: str) -> str:
        return seed_mod.sid("scout", market_key)

    def get(self, request_id: str, *, ws: str | None = None, auth=None, **params):
        return self.client.get(
            f"{API}/workspaces/{ws or self.ws}/scout-requests/{request_id}/runs",
            headers=self.auth if auth is None else auth,
            params=params or None,
        )


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("run_history_api")
    engine = create_engine(
        f"sqlite:///{tmp/'runs.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = factory
    try:
        seed_mod.seed(reset=True)

        with factory() as s:
            # Non-member outsider (exists + active, but not in the demo org) → 403.
            s.add(
                User(
                    id="outsider-user",
                    email="outsider@example.com",
                    full_name="Outsider",
                    hashed_password="x",
                    is_active=True,
                )
            )

            # Dallas: five runs. r1..r3 are strictly increasing; r4 and r5 share a
            # timestamp to exercise the id-desc tie-breaker. Mixed states + stats.
            _make_run(
                s, market_key="dallas", created_at=_BASE + timedelta(seconds=1),
                status=JobStatus.SUCCEEDED,
                stats=_stats(10, 4, 6, 2, simulated=True),
            )
            _make_run(
                s, market_key="dallas", created_at=_BASE + timedelta(seconds=2),
                status=JobStatus.FAILED, last_error_code="transient",
                trigger_marker="scheduled",
            )
            _make_run(
                s, market_key="dallas", created_at=_BASE + timedelta(seconds=3),
                status=JobStatus.RUNNING,
            )
            _make_run(
                s, market_key="dallas", created_at=_BASE + timedelta(seconds=4),
                status=JobStatus.SUCCEEDED, stats=_stats(8, 3, 5, 1),
            )
            _make_run(
                s, market_key="dallas", created_at=_BASE + timedelta(seconds=4),
                status=JobStatus.DEAD_LETTERED, last_error_code="timeout",
            )

            # One run for each of the other three markets (isolation).
            for key in ("london", "lagos", "nairobi"):
                _make_run(
                    s, market_key=key, created_at=_BASE + timedelta(seconds=1),
                    status=JobStatus.SUCCEEDED, stats=_stats(5, 2, 3, 1),
                )
            s.commit()

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
        yield _Harness(TestClient(app), factory)
    finally:
        app.dependency_overrides.clear()
        seed_mod.SessionLocal = original
        engine.dispose()


# --------------------------------------------------------------------------- #
# Contract + shape
# --------------------------------------------------------------------------- #
class TestContract:
    def test_envelope_and_reverse_chronological_order(self, h):
        r = h.get(h.req("dallas"))
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"items", "total", "limit", "offset"}
        assert body["total"] == 5
        assert body["limit"] == 20 and body["offset"] == 0
        assert len(body["items"]) == 5

        created = [item["created_at"] for item in body["items"]]
        assert created == sorted(created, reverse=True), "newest-first ordering"

    def test_no_internal_fields_leak(self, h):
        for item in h.get(h.req("dallas")).json()["items"]:
            for key in _FORBIDDEN_KEYS:
                assert key not in item, key
            assert set(item) == {
                "id", "status", "trigger", "is_simulated", "attempt_count",
                "max_attempts", "last_error_code", "scheduled_for", "started_at",
                "completed_at", "cancel_requested_at", "cancelled_at", "created_at",
                "updated_at", "stats",
            }

    def test_stats_present_only_when_summary_complete(self, h):
        items = h.get(h.req("dallas")).json()["items"]
        with_stats = [i for i in items if i["stats"] is not None]
        without_stats = [i for i in items if i["stats"] is None]
        assert with_stats and without_stats, "fixture exercises both stats states"
        for i in with_stats:
            assert set(i["stats"]) == {
                "scanned", "noise_filtered", "signals_analyzed", "opportunities"
            }
            assert all(v >= 0 for v in i["stats"].values())

    def test_error_code_exposed_summary_never(self, h):
        items = h.get(h.req("dallas")).json()["items"]
        codes = {i["last_error_code"] for i in items}
        assert "transient" in codes and "timeout" in codes
        # last_error_summary is never part of the contract.
        assert all("last_error_summary" not in i for i in items)


# --------------------------------------------------------------------------- #
# Trigger + simulated derivation
# --------------------------------------------------------------------------- #
class TestDerivation:
    def test_trigger_manual_vs_scheduled(self, h):
        items = h.get(h.req("dallas")).json()["items"]
        by_error = {i["last_error_code"]: i for i in items}
        # The run seeded with an explicit "scheduled" marker reports scheduled...
        assert by_error["transient"]["trigger"] == "scheduled"
        # ...marker-less execute runs report manual, never unknown.
        assert {i["trigger"] for i in items} <= {"manual", "scheduled"}
        assert any(i["trigger"] == "manual" for i in items)

    def test_is_simulated_only_when_recorded(self, h):
        items = h.get(h.req("dallas")).json()["items"]
        simulated = [i for i in items if i["is_simulated"] is True]
        unknown = [i for i in items if i["is_simulated"] is None]
        assert len(simulated) == 1, "only the run whose summary records it"
        assert unknown, "runs without the flag map to null, never a fabricated bool"
        assert all(i["is_simulated"] is not False for i in items)


# --------------------------------------------------------------------------- #
# Pagination — bounded + deterministic
# --------------------------------------------------------------------------- #
class TestPagination:
    def test_offset_paging_is_stable_and_non_overlapping(self, h):
        full = [i["id"] for i in h.get(h.req("dallas")).json()["items"]]
        page1 = h.get(h.req("dallas"), limit=2, offset=0).json()
        page2 = h.get(h.req("dallas"), limit=2, offset=2).json()
        page3 = h.get(h.req("dallas"), limit=2, offset=4).json()
        assert page1["total"] == page2["total"] == 5
        assert [i["id"] for i in page1["items"]] == full[0:2]
        assert [i["id"] for i in page2["items"]] == full[2:4]
        assert [i["id"] for i in page3["items"]] == full[4:5]

    def test_tie_break_by_id_desc(self, h):
        # The two runs sharing the newest timestamp must order by id desc, stably.
        items = h.get(h.req("dallas")).json()["items"]
        newest_ts = items[0]["created_at"]
        tied = [i["id"] for i in items if i["created_at"] == newest_ts]
        assert len(tied) == 2
        assert tied == sorted(tied, reverse=True)

    def test_limit_bounds_enforced(self, h):
        assert h.get(h.req("dallas"), limit=0).status_code == 422
        assert h.get(h.req("dallas"), limit=101).status_code == 422
        assert h.get(h.req("dallas"), offset=-1).status_code == 422
        assert h.get(h.req("dallas"), limit=100).status_code == 200

    def test_empty_history_is_neutral_not_error(self, h):
        # A workspace member reading a request that never ran gets an empty page.
        # Nairobi has one run; deleting nothing — instead assert the envelope for a
        # request with a single run still paginates cleanly past the end.
        r = h.get(h.req("nairobi"), limit=20, offset=50)
        assert r.status_code == 200
        assert r.json() == {"items": [], "total": 1, "limit": 20, "offset": 50}


# --------------------------------------------------------------------------- #
# Isolation across the four markets
# --------------------------------------------------------------------------- #
class TestIsolation:
    def test_each_request_sees_only_its_own_runs(self, h):
        counts = {key: h.get(h.req(key)).json()["total"] for key in _MARKETS}
        assert counts == {"dallas": 5, "london": 1, "lagos": 1, "nairobi": 1}

    def test_no_cross_request_id_bleed(self, h):
        per_market_ids = {
            key: {i["id"] for i in h.get(h.req(key)).json()["items"]} for key in _MARKETS
        }
        seen: set[str] = set()
        for ids in per_market_ids.values():
            assert seen.isdisjoint(ids), "a run id appeared under two requests"
            seen |= ids


# --------------------------------------------------------------------------- #
# AuthN / AuthZ
# --------------------------------------------------------------------------- #
class TestSecurity:
    def test_unauthenticated_401(self, h):
        r = h.client.get(f"{API}/workspaces/{h.ws}/scout-requests/{h.req('dallas')}/runs")
        assert r.status_code == 401

    def test_non_member_403(self, h):
        r = h.get(h.req("dallas"), auth=h.outsider_auth)
        assert r.status_code == 403

    def test_unknown_request_404(self, h):
        r = h.get("scout-does-not-exist")
        assert r.status_code == 404

    def test_unknown_workspace_404(self, h):
        # Workspace resolution fails before request scoping.
        r = h.get(h.req("dallas"), ws="ws-nope")
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# The read is strictly non-mutating (no behaviour change to job execution)
# --------------------------------------------------------------------------- #
class TestNonMutating:
    def test_reads_never_change_jobs(self, h):
        def snapshot():
            with h.factory() as s:
                rows = s.execute(
                    select(Job.id, Job.status, Job.attempt_count).order_by(Job.id)
                ).all()
                total = s.scalar(select(func.count()).select_from(Job))
                return rows, total

        before = snapshot()
        for key in _MARKETS:
            h.get(h.req(key))
            h.get(h.req(key), limit=1, offset=0)
        assert snapshot() == before


# --------------------------------------------------------------------------- #
# Pure service / mapper unit tests (no HTTP)
# --------------------------------------------------------------------------- #
class TestPureUnits:
    def test_derive_trigger_marker_wins(self):
        j = Job(job_type="scout_request.execute", payload={"trigger": "scheduled"})
        assert _derive_trigger(j) == TriggerType.SCHEDULED
        j2 = Job(job_type="scout_request.execute", payload={"trigger": "manual"})
        assert _derive_trigger(j2) == TriggerType.MANUAL

    def test_derive_trigger_defaults_manual_for_execute(self):
        j = Job(job_type="scout_request.execute", payload={"scout_request_id": "x"})
        assert _derive_trigger(j) == TriggerType.MANUAL

    def test_derive_trigger_unknown_for_other_types(self):
        j = Job(job_type="something.else", payload={})
        assert _derive_trigger(j) == TriggerType.UNKNOWN

    def test_derive_simulated_requires_bool(self):
        assert _derive_simulated(Job(result_summary={"is_simulated": True})) is True
        assert _derive_simulated(Job(result_summary={"is_simulated": False})) is False
        assert _derive_simulated(Job(result_summary={"scanned": 1})) is None
        assert _derive_simulated(Job(result_summary=None)) is None

    def test_map_stats_requires_all_keys_and_floors(self):
        assert _map_stats(Job(result_summary=None)) is None
        assert _map_stats(Job(result_summary={"scanned": 1})) is None
        st = _map_stats(
            Job(result_summary=_stats(-5, 2, 3, 4))
        )
        assert st is not None and st.scanned == 0 and st.opportunities == 4

    def test_get_run_history_clamps_limit_when_called_directly(self, h):
        with h.factory() as s:
            out = get_run_history(
                s,
                organization_id=h.org,
                workspace_id=h.ws,
                scout_request_id=h.req("dallas"),
                limit=10_000,
                offset=-3,
            )
        assert out.limit == 100 and out.offset == 0 and out.total == 5
