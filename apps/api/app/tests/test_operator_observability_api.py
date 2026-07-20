"""Phase 4A-B: operator read-only observability — classifier units + route tests.

Covers the additive operator surface added in
``app.system.internal_observability_routes`` plus the centralized stuck-job
classifier in :mod:`app.jobs.stuck`:

    GET /internal/system/overview
    GET /internal/system/jobs/list
    GET /internal/system/jobs/stuck
    GET /internal/system/jobs/dead-letter
    GET /internal/system/jobs/{job_id}
    GET /internal/system/jobs/{job_id}/events
    GET /internal/system/schedules

These run against a self-contained, throwaway SQLite database with ``get_db``
overridden and rows inserted directly, so the suite has full deterministic
control over job lifecycle/lease/heartbeat state, dead-letter and schedule state
across two isolated tenants — without touching job-execution behaviour.

What is asserted here (and nowhere else): operator-only authorization on every
route (401 anonymous / 403 non-operator), the secret-free operator projection
(never a raw payload, lease token, correlation/trace id or worker secret),
bounded/deterministic pagination + filters, live stuck classification against an
injected clock, dead-letter visibility, derived schedule state, cross-tenant
listing paired with per-tenant filter isolation, and that every read is strictly
non-mutating.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.jobs.models import Job, JobEvent
from app.jobs.status import JobStatus, JobType
from app.jobs.stuck import is_job_stuck
from app.main import app
from app.organizations.models import User
from app.scouting_requests.models import ScoutSchedule

API = get_settings().api_prefix

# Far-past / far-future anchors keep the live-clock stuck predicate deterministic:
# a heartbeat/lease in 2020 is unambiguously stale/expired at any real "now",
# and one in 2035 is unambiguously fresh/valid.
_PAST = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
_FUTURE = datetime(2035, 1, 1, 0, 0, 0, tzinfo=UTC)

# Fields that must NEVER appear in any operator observability response body.
_FORBIDDEN = (
    "lease_token",
    "correlation_id",
    "trace_context",
    "password",
    "api_key",
    "secret",
    "redis://",
    "postgresql://",
)

# Two isolated tenants.
_ORG_A, _WS_A = "org-a", "ws-a"
_ORG_B, _WS_B = "org-b", "ws-b"

_OPERATOR = "operator-user"
_CUSTOMER = "customer-user"

# Every route added by this batch (path, whether it is a plain collection read).
_ROUTES = [
    "/internal/system/overview",
    "/internal/system/jobs/list",
    "/internal/system/jobs/stuck",
    "/internal/system/jobs/dead-letter",
    "/internal/system/jobs/job-a-pending",
    "/internal/system/jobs/job-a-pending/events",
    "/internal/system/schedules",
]


def _job(
    s,
    *,
    job_id: str,
    org: str,
    ws: str,
    job_type: JobType = JobType.SCOUT_REQUEST_EXECUTE,
    status: JobStatus = JobStatus.PENDING,
    location_id: str | None = None,
    scout_request_id: str | None = None,
    lease_expires_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
    completed_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Job:
    """Insert one durable job row directly (bypassing enqueue) and return it."""
    job = Job(
        id=job_id,
        organization_id=org,
        workspace_id=ws,
        location_id=location_id,
        scout_request_id=scout_request_id,
        job_type=job_type.value,
        payload={"trigger": "manual", "secret_field": "should-never-surface"},
        payload_hash="deadbeef",
        idempotency_key=None,
        status=status.value,
        lease_expires_at=lease_expires_at,
        heartbeat_at=heartbeat_at,
        completed_at=completed_at,
        # Internal-only fields the operator projection must never echo back.
        lease_token="LEASE-TOKEN-SECRET",
        correlation_id="corr-secret",
        trace_context="trace-secret",
        last_error_summary="raw internal detail should never surface",
    )
    if created_at is not None:
        job.created_at = created_at
    s.add(job)
    return job


def _schedule(
    s, *, sid: str, org: str, ws: str, request_id: str, enabled: bool
) -> ScoutSchedule:
    sch = ScoutSchedule(
        id=sid,
        organization_id=org,
        workspace_id=ws,
        location_id=None,
        scout_request_id=request_id,
        interval="daily",
        enabled=enabled,
        next_run_at=_FUTURE if enabled else None,
    )
    s.add(sch)
    return sch


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("operator_observability")
    engine = create_engine(
        f"sqlite:///{tmp/'obs.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    with factory() as s:
        # Identities: a platform operator and an ordinary (non-operator) customer.
        s.add(
            User(
                id=_OPERATOR,
                email="operator@example.com",
                full_name="Operator",
                hashed_password="x",
                is_active=True,
                is_operator=True,
            )
        )
        s.add(
            User(
                id=_CUSTOMER,
                email="customer@example.com",
                full_name="Customer",
                hashed_password="x",
                is_active=True,
                is_operator=False,
            )
        )

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        # --- Tenant A jobs --------------------------------------------------
        _job(
            s, job_id="job-a-pending", org=_ORG_A, ws=_WS_A,
            status=JobStatus.PENDING, location_id="loc-a",
            scout_request_id="req-a", created_at=base + timedelta(seconds=1),
        )
        # Claimed + fresh lease/heartbeat → healthy, NOT stuck.
        _job(
            s, job_id="job-a-running", org=_ORG_A, ws=_WS_A,
            status=JobStatus.RUNNING, lease_expires_at=_FUTURE,
            heartbeat_at=_FUTURE, created_at=base + timedelta(seconds=2),
        )
        # Claimed + expired lease + stale heartbeat → STUCK.
        _job(
            s, job_id="job-a-stuck", org=_ORG_A, ws=_WS_A,
            status=JobStatus.CLAIMED, lease_expires_at=_PAST,
            heartbeat_at=_PAST, created_at=base + timedelta(seconds=3),
        )
        # Dead-lettered.
        _job(
            s, job_id="job-a-dead", org=_ORG_A, ws=_WS_A,
            status=JobStatus.DEAD_LETTERED,
            completed_at=base + timedelta(seconds=10),
            created_at=base + timedelta(seconds=4),
        )
        # A schedule-tick job that makes schedule S-active look ACTIVE.
        _job(
            s, job_id="job-a-tick", org=_ORG_A, ws=_WS_A,
            job_type=JobType.SCOUT_SCHEDULE_TICK, status=JobStatus.PENDING,
            scout_request_id="req-sa", created_at=base + timedelta(seconds=5),
        )

        # --- Tenant B jobs (isolation) -------------------------------------
        _job(
            s, job_id="job-b-pending", org=_ORG_B, ws=_WS_B,
            status=JobStatus.PENDING, location_id="loc-b",
            scout_request_id="req-b", created_at=base + timedelta(seconds=6),
        )
        _job(
            s, job_id="job-b-dead", org=_ORG_B, ws=_WS_B,
            status=JobStatus.DEAD_LETTERED,
            completed_at=base + timedelta(seconds=11),
            created_at=base + timedelta(seconds=7),
        )

        # --- Events for job-a-pending (sanitized timeline) -----------------
        s.add(
            JobEvent(
                id="evt-1", job_id="job-a-pending", organization_id=_ORG_A,
                workspace_id=_WS_A, event_type="enqueued", previous_status=None,
                new_status="pending", attempt=0,
                worker_id="worker-secret-id",  # must NOT surface in JobEventOut
                event_metadata={"note": "created"},
            )
        )
        s.add(
            JobEvent(
                id="evt-2", job_id="job-a-pending", organization_id=_ORG_A,
                workspace_id=_WS_A, event_type="claimed", previous_status="pending",
                new_status="claimed", attempt=1, worker_id="worker-secret-id",
                event_metadata={},
            )
        )

        # --- Schedules across states + tenants -----------------------------
        # ACTIVE: enabled + a live tick chain (job-a-tick above).
        _schedule(s, sid="sch-active", org=_ORG_A, ws=_WS_A, request_id="req-sa", enabled=True)
        # ACTIVATION_REQUIRED: enabled but no live tick.
        _schedule(s, sid="sch-inert", org=_ORG_A, ws=_WS_A, request_id="req-sb", enabled=True)
        # PAUSED: disabled, in tenant B.
        _schedule(s, sid="sch-paused", org=_ORG_B, ws=_WS_B, request_id="req-sc", enabled=False)
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
    try:
        client = TestClient(app)
        yield _Harness(client, factory)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


class _Harness:
    def __init__(self, client, factory):
        self.client = client
        self.factory = factory
        self.op = {"Authorization": f"Bearer {create_access_token(_OPERATOR)}"}
        self.cust = {"Authorization": f"Bearer {create_access_token(_CUSTOMER)}"}

    def get(self, path: str, *, auth=None, **params):
        return self.client.get(
            f"{API}{path}", headers=self.op if auth is None else auth,
            params=params or None,
        )


# --------------------------------------------------------------------------- #
# Pure classifier units (no HTTP) — is_job_stuck boundaries
# --------------------------------------------------------------------------- #
class TestStuckClassifier:
    NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

    def _job(self, **kw) -> Job:
        return Job(
            organization_id="o", workspace_id="w", job_type="scout_request.execute",
            payload={}, payload_hash="x", **kw,
        )

    def test_expired_lease_is_stuck(self):
        j = self._job(status=JobStatus.CLAIMED.value, lease_expires_at=_PAST)
        assert is_job_stuck(j, now=self.NOW, stale_after_seconds=60) is True

    def test_stale_heartbeat_is_stuck(self):
        j = self._job(status=JobStatus.RUNNING.value, heartbeat_at=_PAST)
        assert is_job_stuck(j, now=self.NOW, stale_after_seconds=60) is True

    def test_fresh_lease_and_heartbeat_is_not_stuck(self):
        j = self._job(
            status=JobStatus.RUNNING.value, lease_expires_at=_FUTURE, heartbeat_at=_FUTURE
        )
        assert is_job_stuck(j, now=self.NOW, stale_after_seconds=60) is False

    def test_no_lease_no_heartbeat_is_not_stuck(self):
        j = self._job(status=JobStatus.CLAIMED.value)
        assert is_job_stuck(j, now=self.NOW, stale_after_seconds=60) is False

    def test_heartbeat_exactly_at_threshold_is_not_stuck(self):
        # cutoff == now - stale; strictly-older is stale, equal is not.
        hb = self.NOW - timedelta(seconds=60)
        j = self._job(status=JobStatus.RUNNING.value, heartbeat_at=hb)
        assert is_job_stuck(j, now=self.NOW, stale_after_seconds=60) is False

    def test_heartbeat_just_past_threshold_is_stuck(self):
        hb = self.NOW - timedelta(seconds=61)
        j = self._job(status=JobStatus.RUNNING.value, heartbeat_at=hb)
        assert is_job_stuck(j, now=self.NOW, stale_after_seconds=60) is True

    @pytest.mark.parametrize(
        "status",
        [
            JobStatus.PENDING,
            JobStatus.SCHEDULED,
            JobStatus.RETRY_WAIT,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.DEAD_LETTERED,
            JobStatus.CANCELLED,
            JobStatus.CANCEL_REQUESTED,
        ],
    )
    def test_non_candidate_status_never_stuck(self, status):
        # Even with an expired lease, only claimed/running can be stuck. Notably a
        # cancel-requested job (a cooperative stop) is excluded by design.
        j = self._job(status=status.value, lease_expires_at=_PAST, heartbeat_at=_PAST)
        assert is_job_stuck(j, now=self.NOW, stale_after_seconds=60) is False


# --------------------------------------------------------------------------- #
# Authorization — every route is operator-only
# --------------------------------------------------------------------------- #
class TestAuthorization:
    @pytest.mark.parametrize("path", _ROUTES)
    def test_anonymous_is_401(self, h, path):
        assert h.client.get(f"{API}{path}").status_code == 401

    @pytest.mark.parametrize("path", _ROUTES)
    def test_non_operator_is_403(self, h, path):
        assert h.get(path, auth=h.cust).status_code == 403


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
class TestOverview:
    def test_shape_and_counts(self, h):
        r = h.get("/internal/system/overview")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {
            "stale_after_seconds", "as_of", "jobs", "workers", "schedules"
        }
        assert set(body["jobs"]) == {
            "total", "status_counts", "stuck_count", "dead_letter_count"
        }
        assert set(body["workers"]) == {"status_counts", "active_count", "stale_count"}
        assert set(body["schedules"]) == {"total", "state_counts"}

        jobs = body["jobs"]
        assert jobs["total"] == 7  # all inserted jobs, cross-tenant
        assert jobs["stuck_count"] == 1  # only job-a-stuck
        assert jobs["dead_letter_count"] == 2  # job-a-dead + job-b-dead
        assert jobs["status_counts"]["dead_lettered"] == 2
        assert body["stale_after_seconds"] == 60.0

        sched = body["schedules"]
        assert sched["total"] == 3
        assert sched["state_counts"] == {
            "active": 1, "activation_required": 1, "paused": 1
        }

    def test_secret_free(self, h):
        blob = h.get("/internal/system/overview").text.lower()
        for token in _FORBIDDEN:
            assert token not in blob


# --------------------------------------------------------------------------- #
# Jobs listing — pagination, filters, isolation, projection
# --------------------------------------------------------------------------- #
class TestJobsList:
    def test_envelope_and_cross_tenant(self, h):
        body = h.get("/internal/system/jobs/list").json()
        assert set(body) == {"items", "total", "limit", "offset"}
        assert body["total"] == 7
        assert body["limit"] == 50 and body["offset"] == 0
        # Newest-first ordering by created_at desc.
        created = [i["created_at"] for i in body["items"]]
        assert created == sorted(created, reverse=True)

    def test_filter_by_workspace_isolates_tenant(self, h):
        a = h.get("/internal/system/jobs/list", workspace_id=_WS_A).json()
        b = h.get("/internal/system/jobs/list", workspace_id=_WS_B).json()
        assert a["total"] == 5 and b["total"] == 2
        assert all(i["workspace_id"] == _WS_A for i in a["items"])
        assert all(i["workspace_id"] == _WS_B for i in b["items"])

    def test_filter_by_status_and_type(self, h):
        dead = h.get("/internal/system/jobs/list", status="dead_lettered").json()
        assert dead["total"] == 2
        assert all(i["status"] == "dead_lettered" for i in dead["items"])

        ticks = h.get(
            "/internal/system/jobs/list", job_type="scout_schedule.tick"
        ).json()
        assert ticks["total"] == 1
        assert ticks["items"][0]["id"] == "job-a-tick"

    def test_filter_by_scout_request_and_location(self, h):
        by_req = h.get("/internal/system/jobs/list", scout_request_id="req-a").json()
        assert {i["id"] for i in by_req["items"]} == {"job-a-pending"}
        by_loc = h.get("/internal/system/jobs/list", location_id="loc-b").json()
        assert {i["id"] for i in by_loc["items"]} == {"job-b-pending"}

    def test_pagination_is_bounded_and_stable(self, h):
        full = [i["id"] for i in h.get("/internal/system/jobs/list").json()["items"]]
        p1 = h.get("/internal/system/jobs/list", limit=3, offset=0).json()
        p2 = h.get("/internal/system/jobs/list", limit=3, offset=3).json()
        assert p1["total"] == p2["total"] == 7
        assert [i["id"] for i in p1["items"]] == full[0:3]
        assert [i["id"] for i in p2["items"]] == full[3:6]

    def test_limit_bounds_enforced(self, h):
        assert h.get("/internal/system/jobs/list", limit=0).status_code == 422
        assert h.get("/internal/system/jobs/list", limit=201).status_code == 422
        assert h.get("/internal/system/jobs/list", offset=-1).status_code == 422
        assert h.get("/internal/system/jobs/list", limit=200).status_code == 200

    def test_invalid_status_enum_is_422(self, h):
        assert h.get("/internal/system/jobs/list", status="nonsense").status_code == 422

    def test_projection_is_operator_safe(self, h):
        body = h.get("/internal/system/jobs/list").json()
        blob = h.get("/internal/system/jobs/list").text
        for token in _FORBIDDEN:
            assert token.lower() not in blob.lower()
        # Operator view exposes safe diagnostics but never the raw payload.
        item = body["items"][0]
        assert "payload" not in item
        assert "payload_hash" in item  # a hash, not the payload itself
        assert "worker_id" in item  # safe operator diagnostic


# --------------------------------------------------------------------------- #
# Stuck listing
# --------------------------------------------------------------------------- #
class TestStuckListing:
    def test_lists_only_the_stuck_job(self, h):
        body = h.get("/internal/system/jobs/stuck").json()
        assert set(body) == {
            "stuck_count", "stale_after_seconds", "as_of", "limit", "offset", "items"
        }
        assert body["stuck_count"] == 1
        assert body["stale_after_seconds"] == 60.0
        assert [i["id"] for i in body["items"]] == ["job-a-stuck"]

    def test_self_describing_clock(self, h):
        body = h.get("/internal/system/jobs/stuck").json()
        assert isinstance(body["as_of"], str) and body["as_of"]

    def test_bounds_enforced(self, h):
        assert h.get("/internal/system/jobs/stuck", limit=0).status_code == 422
        assert h.get("/internal/system/jobs/stuck", limit=201).status_code == 422


# --------------------------------------------------------------------------- #
# Dead-letter listing
# --------------------------------------------------------------------------- #
class TestDeadLetter:
    def test_lists_all_dead_lettered_cross_tenant(self, h):
        body = h.get("/internal/system/jobs/dead-letter").json()
        assert set(body) == {"total", "limit", "offset", "items"}
        assert body["total"] == 2
        assert {i["id"] for i in body["items"]} == {"job-a-dead", "job-b-dead"}
        assert all(i["status"] == "dead_lettered" for i in body["items"])


# --------------------------------------------------------------------------- #
# Job detail + event timeline
# --------------------------------------------------------------------------- #
class TestJobDetail:
    def test_detail_ok_and_safe(self, h):
        body = h.get("/internal/system/jobs/job-a-stuck").json()
        assert body["id"] == "job-a-stuck"
        assert body["status"] == "claimed"
        assert "payload" not in body
        blob = h.get("/internal/system/jobs/job-a-stuck").text.lower()
        for token in _FORBIDDEN:
            assert token not in blob

    def test_detail_unknown_is_404(self, h):
        assert h.get("/internal/system/jobs/does-not-exist").status_code == 404

    def test_events_are_sanitized_and_ordered(self, h):
        events = h.get("/internal/system/jobs/job-a-pending/events").json()
        assert [e["id"] for e in events] == ["evt-1", "evt-2"]  # oldest-first
        # JobEventOut deliberately omits worker_id and any secret.
        for e in events:
            assert "worker_id" not in e
            assert set(e) == {
                "id", "event_type", "previous_status", "new_status", "attempt",
                "error_code", "event_metadata", "created_at",
            }
        blob = h.get("/internal/system/jobs/job-a-pending/events").text
        assert "worker-secret-id" not in blob

    def test_events_unknown_job_is_404(self, h):
        assert h.get("/internal/system/jobs/nope/events").status_code == 404

    def test_events_limit_bounds(self, h):
        assert h.get(
            "/internal/system/jobs/job-a-pending/events", limit=0
        ).status_code == 422
        assert h.get(
            "/internal/system/jobs/job-a-pending/events", limit=501
        ).status_code == 422


# --------------------------------------------------------------------------- #
# Schedule visibility
# --------------------------------------------------------------------------- #
class TestSchedules:
    def test_all_states_derived(self, h):
        body = h.get("/internal/system/schedules").json()
        assert set(body) == {"total", "limit", "offset", "items"}
        assert body["total"] == 3
        by_id = {i["id"]: i for i in body["items"]}
        assert by_id["sch-active"]["state"] == "active"
        assert by_id["sch-inert"]["state"] == "activation_required"
        assert by_id["sch-paused"]["state"] == "paused"

    def test_workspace_filter_isolates(self, h):
        a = h.get("/internal/system/schedules", workspace_id=_WS_A).json()
        b = h.get("/internal/system/schedules", workspace_id=_WS_B).json()
        assert {i["id"] for i in a["items"]} == {"sch-active", "sch-inert"}
        assert {i["id"] for i in b["items"]} == {"sch-paused"}

    def test_schedule_projection_is_safe(self, h):
        blob = h.get("/internal/system/schedules").text.lower()
        for token in _FORBIDDEN:
            assert token not in blob


# --------------------------------------------------------------------------- #
# Every read is strictly non-mutating
# --------------------------------------------------------------------------- #
class TestNonMutating:
    def test_reads_never_change_state(self, h):
        def snapshot():
            with h.factory() as s:
                jobs = s.execute(
                    select(Job.id, Job.status).order_by(Job.id)
                ).all()
                events = s.scalar(select(func.count()).select_from(JobEvent))
                schedules = s.execute(
                    select(ScoutSchedule.id, ScoutSchedule.enabled).order_by(ScoutSchedule.id)
                ).all()
                return jobs, events, schedules

        before = snapshot()
        for path in _ROUTES:
            h.get(path)
        h.get("/internal/system/jobs/list", workspace_id=_WS_A)
        h.get("/internal/system/jobs/stuck")
        h.get("/internal/system/jobs/dead-letter")
        assert snapshot() == before
