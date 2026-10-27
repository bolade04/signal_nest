"""P6-PLAT-1 strategy B: the bounded in-process per-run similarity (operator decision 2026-10-27).

Production runs similarity per scout run, in process, over at most
``vector_inprocess_max_signals_per_run`` signals (N_max = 100, hard ceiling 100). This
module pins the behaviour of that bound where it runs:

* the pipeline **refuses** a run that would index more signals than the bound - before
  the index is built, so nothing is stored, embedded, compared, truncated or chunked;
* the refusal is a recorded, visible outcome: the durable job fails ``non_retryable``
  with the stable reason ``signal_volume_above_bound`` as its summary, and the scout
  request settles to ``failed`` with ``stats`` carrying the reason code and counts;
* the bound is per run, per workspace: one workspace's refusal never touches another
  workspace's run in the same worker loop;
* readiness and the runtime report state the bound that runs and the absence of
  cross-run, cross-workspace or history-wide similarity;
* the migration actor imports no Redis adapter (the basis of its Redis exemption).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.brands.models import Brand
from app.connectors.base import ConnectorSignal
from app.core.config import Settings, get_settings
from app.core.enums import ScoutRequestStatus
from app.core.runtime import build_runtime_report
from app.db import seed as seed_mod
from app.db.models import Base
from app.infra.vector import (
    SIGNAL_VOLUME_ABOVE_BOUND,
    SignalVolumeAboveBoundError,
    check_inprocess_bound,
)
from app.jobs import handlers, pipeline
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.service import enqueue_scout_request
from app.jobs.status import JobErrorCode, JobStatus
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner

# Importing the app registers every ORM model + job handler on the shared Base/registry.
from app.main import app  # noqa: F401
from app.organizations.models import Workspace
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.run_history import get_run_history
from app.signals.models import RawSignal
from app.system.probes import ProbeStatus, _check_vector
from app.tests._platform_support import production_vector_kwargs
from app.tests._seed_support import clear_seeded_runs

_API_DIR = Path(__file__).resolve().parents[2]
#: A small bound for the pipeline tests (the validator caps it at the ceiling, 100).
_BOUND = 5


# --------------------------------------------------------------------------- #
# The bound itself
# --------------------------------------------------------------------------- #
class TestCheckInprocessBound:
    def test_reason_code_is_stable(self):
        assert SIGNAL_VOLUME_ABOVE_BOUND == "signal_volume_above_bound"
        assert SignalVolumeAboveBoundError.reason == SIGNAL_VOLUME_ABOVE_BOUND

    @pytest.mark.parametrize("count", [0, 1, 99, 100])
    def test_at_or_below_the_bound_returns_it(self, count):
        assert check_inprocess_bound(count, Settings(_env_file=None)) == 100

    def test_above_the_bound_refuses_with_the_counts(self):
        with pytest.raises(SignalVolumeAboveBoundError) as exc:
            check_inprocess_bound(101, Settings(_env_file=None))
        assert (exc.value.scanned, exc.value.bound) == (101, 100)
        assert str(exc.value).startswith("signal_volume_above_bound: 101 signals exceed")

    def test_bound_is_read_from_settings(self):
        s = Settings(_env_file=None, vector_inprocess_max_signals_per_run=7)
        assert check_inprocess_bound(7, s) == 7
        with pytest.raises(SignalVolumeAboveBoundError):
            check_inprocess_bound(8, s)


def test_refused_run_stats_shape_is_pinned():
    assert pipeline.refused_run_stats(scanned=101, bound=100) == {
        "scanned": 101,
        "noise_filtered": 0,
        "signals_analyzed": 0,
        "opportunities": 0,
        "refused": True,
        "refusal_reason": "signal_volume_above_bound",
        "bound": 100,
    }


# --------------------------------------------------------------------------- #
# Harness: four-market demo seed on a throwaway file-backed SQLite + a stub connector
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("inprocess_bound")
    engine = create_engine(
        f"sqlite:///{tmp / 'bound.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        seed_mod.seed(reset=True)
        clear_seeded_runs(make)
        yield make
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


def _signals(count: int, *, tag: str) -> list[ConnectorSignal]:
    """``count`` distinct signals (distinct tokens: none is a near-duplicate)."""
    return [
        ConnectorSignal(
            source_type="forum",
            content=f"{tag} signal {i} token{i}a token{i}b token{i}c unique{i}",
            market=None,
            source_url=f"https://example.invalid/{tag}/{i}",
            author=f"user{i}",
            topics=[f"topic{i}"],
            engagement=5,
        )
        for i in range(count)
    ]


class _StubConnector:
    """Returns a fixed number of signals per workspace; records what was asked."""

    is_simulated = True
    name = "stub"

    def __init__(self, per_workspace: dict[str, int], workspace_id: str) -> None:
        self._count = per_workspace[workspace_id]
        self._tag = workspace_id

    def fetch(self, *, market, keywords, source_types):
        return _signals(self._count, tag=self._tag)


@pytest.fixture
def bound(monkeypatch):
    """A small per-run bound on the cached settings the pipeline reads."""
    monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", _BOUND)
    return _BOUND


@pytest.fixture
def connector_counts(monkeypatch):
    """Route the pipeline's connector resolution to the stub, per workspace."""
    counts: dict[str, int] = {}

    def _get_connector(
        *, source_types=None, market=None, db=None, organization_id=None, workspace_id=None
    ):
        return _StubConnector(counts, workspace_id)

    monkeypatch.setattr(pipeline, "get_connector", _get_connector)
    return counts


def _seeded_request_id() -> str:
    return seed_mod.sid("scout", "dallas")


def _raw_count(s, request_id: str) -> int:
    return s.scalar(
        select(func.count()).select_from(RawSignal).where(RawSignal.scout_request_id == request_id)
    )


# --------------------------------------------------------------------------- #
# The pipeline refuses before any work
# --------------------------------------------------------------------------- #
def test_run_above_the_bound_is_refused_before_any_work(
    factory, bound, connector_counts, monkeypatch
):
    request_id = _seeded_request_id()
    with factory() as s:
        request = s.get(ScoutRequest, request_id)
        connector_counts[request.workspace_id] = bound + 1
        before = _raw_count(s, request_id)  # the demo seed's own run (P6-UI-009)

    def _never(*_a, **_k):  # pragma: no cover - the assertion is that it is not reached
        raise AssertionError("the dedupe stage ran for a refused run")

    monkeypatch.setattr(pipeline, "build_index", _never)
    monkeypatch.setattr(pipeline, "embed_text", _never)

    with factory() as s:
        with pytest.raises(SignalVolumeAboveBoundError) as exc:
            pipeline._run(s, request_id)
        s.rollback()
        assert (exc.value.scanned, exc.value.bound) == (bound + 1, bound)
        assert _raw_count(s, request_id) == before  # nothing stored: no partial run


def test_run_at_the_bound_proceeds(factory, bound, connector_counts):
    request_id = _seeded_request_id()
    with factory() as s:
        request = s.get(ScoutRequest, request_id)
        connector_counts[request.workspace_id] = bound
        before = _raw_count(s, request_id)
        result = pipeline._run(s, request_id)
        s.commit()
        assert result["scanned"] == bound
        assert s.get(ScoutRequest, request_id).status == ScoutRequestStatus.COMPLETED.value
        assert _raw_count(s, request_id) == before + bound


# --------------------------------------------------------------------------- #
# Through the real worker: outcome shape and tenant isolation (two workspaces)
# --------------------------------------------------------------------------- #
def _second_workspace(factory) -> str:
    """A second workspace (same organization) with its own brand and scout request."""
    with factory() as s:
        seeded = s.get(ScoutRequest, _seeded_request_id())
        ws = Workspace(organization_id=seeded.organization_id, name="Second", slug="second-ws")
        s.add(ws)
        s.flush()
        brand = Brand(organization_id=seeded.organization_id, workspace_id=ws.id, name="B2")
        s.add(brand)
        s.flush()
        request = ScoutRequest(
            organization_id=seeded.organization_id,
            workspace_id=ws.id,
            brand_id=brand.id,
            name="second workspace scout",
            status=ScoutRequestStatus.DRAFT.value,
            source_types=["forum"],
            keywords=["coffee"],
        )
        s.add(request)
        s.commit()
        return request.id


def _enqueue(factory, request_id: str) -> str:
    with factory() as s:
        request = s.get(ScoutRequest, request_id)
        request.status = ScoutRequestStatus.QUEUED.value
        job = enqueue_scout_request(
            s,
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            scout_request_id=request.id,
            location_id=request.location_id,
            campaign_id=request.campaign_id,
            request_id=request.id,
        )
        s.commit()
        return job.id


def _drain(factory, limit: int = 10) -> int:
    runner = JobRunner(
        settings=Settings(_env_file=None), session_factory=factory, store=DurableJobStore()
    )
    ran = 0
    for _ in range(limit):
        if not runner.poll_once(worker_id="w-bound"):
            break
        ran += 1
    else:  # pragma: no cover - a runaway loop would trip this guard
        raise AssertionError("worker did not reach quiescence")
    return ran


def test_refusal_is_recorded_and_isolated_per_workspace(factory, bound, connector_counts):
    refused_id = _seeded_request_id()
    other_id = _second_workspace(factory)
    with factory() as s:
        refused_ws = s.get(ScoutRequest, refused_id).workspace_id
        other_ws = s.get(ScoutRequest, other_id).workspace_id
    assert refused_ws != other_ws
    connector_counts[refused_ws] = bound + 1  # above the bound: refused
    connector_counts[other_ws] = 3  # within the bound: runs
    with factory() as s:
        refused_before = _raw_count(s, refused_id)

    refused_job_id = _enqueue(factory, refused_id)
    other_job_id = _enqueue(factory, other_id)

    assert _drain(factory) == 2

    with factory() as s:
        refused_job = s.get(Job, refused_job_id)
        other_job = s.get(Job, other_job_id)
        refused = s.get(ScoutRequest, refused_id)
        other = s.get(ScoutRequest, other_id)

        # --- The refused run: a non-retryable failure with the stable reason code.
        assert refused_job.status == JobStatus.FAILED.value
        assert refused_job.attempt_count == 1  # fail fast, never retried
        assert refused_job.last_error_code == JobErrorCode.NON_RETRYABLE.value
        assert refused_job.last_error_summary == SIGNAL_VOLUME_ABOVE_BOUND
        assert refused_job.result_summary is None
        assert refused.status == ScoutRequestStatus.FAILED.value  # settled; runnable again
        # The stub declares no fetch limit, so the refusal carries that fact (#268).
        assert refused.stats == pipeline.refused_run_stats(
            scanned=bound + 1,
            bound=bound,
            fetch_limits=[
                {
                    "source_key": "stub",
                    "max_items": None,
                    "clipped": False,
                    "fetched": bound + 1,
                    "eligible_seen": bound + 1,
                    "failed": False,
                    "failure_reason": None,
                }
            ],
        )
        assert _raw_count(s, refused_id) == refused_before  # no partial run written

        # --- The other workspace's run in the same loop was not affected.
        assert other_job.status == JobStatus.SUCCEEDED.value
        assert other_job.result_summary["scanned"] == 3
        assert other.status == ScoutRequestStatus.COMPLETED.value
        assert other.stats["scanned"] == 3 and "refused" not in other.stats
        assert _raw_count(s, other_id) == 3

        # --- Customer-safe run history: the refusal is visible (status + coarse code);
        # the free-text summary never crosses that boundary, and a refused run carries
        # no stats (it produced no summary).
        history = get_run_history(
            s,
            organization_id=refused.organization_id,
            workspace_id=refused.workspace_id,
            scout_request_id=refused.id,
        )
        assert history.total == 1
        item = history.items[0]
        assert item.status == JobStatus.FAILED.value
        assert item.last_error_code == JobErrorCode.NON_RETRYABLE.value
        assert item.stats is None
        assert not hasattr(item, "last_error_summary")
        other_history = get_run_history(
            s,
            organization_id=other.organization_id,
            workspace_id=other.workspace_id,
            scout_request_id=other.id,
        )
        assert other_history.items[0].status == JobStatus.SUCCEEDED.value
        assert other_history.items[0].stats.scanned == 3


# --------------------------------------------------------------------------- #
# The after-rollback writer's own tenant guard (defence in depth: ``_run``'s
# ``scope_matches`` guard raises before the bound check, so only a direct call
# reaches it - review r1 MINOR-1, mutant M4)
# --------------------------------------------------------------------------- #
def _context_for(request: ScoutRequest, **overrides: str) -> ExecutionContext:
    return ExecutionContext.for_scout_request(
        organization_id=overrides.get("organization_id", request.organization_id),
        workspace_id=overrides.get("workspace_id", request.workspace_id),
        request_id=request.id,
    )


@pytest.mark.parametrize(
    "foreign",
    [
        {"organization_id": "org-other"},
        {"workspace_id": "ws-other"},
        {"organization_id": "org-other", "workspace_id": "ws-other"},
    ],
    ids=["other-organization", "other-workspace", "both"],
)
def test_record_refused_run_writes_nothing_for_a_foreign_tenant_context(factory, foreign):
    request_id = _seeded_request_id()
    exc = SignalVolumeAboveBoundError(scanned=101, bound=100)
    with factory() as s:
        request = s.get(ScoutRequest, request_id)
        before = (request.status, dict(request.stats or {}))
        writer = handlers._record_refused_run(request_id, _context_for(request, **foreign), exc)
        writer(s)
        s.flush()
        s.expire_all()
        request = s.get(ScoutRequest, request_id)
        assert (request.status, dict(request.stats or {})) == before
        s.rollback()


def test_record_refused_run_writes_the_outcome_for_the_matching_tenant_context(factory):
    request_id = _seeded_request_id()
    exc = SignalVolumeAboveBoundError(scanned=101, bound=100)
    with factory() as s:
        request = s.get(ScoutRequest, request_id)
        writer = handlers._record_refused_run(request_id, _context_for(request), exc)
        writer(s)
        s.expire_all()
        request = s.get(ScoutRequest, request_id)
        assert request.status == ScoutRequestStatus.FAILED.value
        assert request.stats == pipeline.refused_run_stats(scanned=101, bound=100)
        s.rollback()


def test_record_refused_run_ignores_an_unknown_request(factory):
    exc = SignalVolumeAboveBoundError(scanned=101, bound=100)
    context = ExecutionContext.for_scout_request(organization_id="org-x", workspace_id="ws-x")
    with factory() as s:
        handlers._record_refused_run("no-such-request", context, exc)(s)  # no raise, no write
        s.rollback()


# --------------------------------------------------------------------------- #
# Readiness + runtime report: the bound that runs, from settings
# --------------------------------------------------------------------------- #
_DETAIL = (
    "in-process per-run similarity; bound N_max={n} signals per run "
    "(engineering capacity bound); per-source fetch limit 100 items (independent of the run "
    "bound); no cross-run, cross-workspace or history-wide similarity"
)


@pytest.mark.parametrize("n", [1, 25, 100])
def test_readiness_and_runtime_report_state_the_bound(n):
    s = Settings(_env_file=None, vector_inprocess_max_signals_per_run=n)
    status, summary, detail, retryable = _check_vector(s)
    assert status is ProbeStatus.HEALTHY
    assert f"N_max={n} signals per run" in summary
    assert detail == _DETAIL.format(n=n)
    assert retryable is False
    cap = {c.name: c for c in build_runtime_report(s).capabilities}["vector"]
    assert cap.configured is True and cap.backend == "bruteforce"
    assert cap.detail == _DETAIL.format(n=n)


def _production_profile(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        _env_file=None,
        app_mode="full",
        environment="production",
        database_url="postgresql://u:p@db.invalid/db",
        secret_key="a-real-secret",
        queue_backend="redis",
        cache_backend="redis",
        redis_url="redis://cache.invalid:6379/0",
        rate_limit_backend="redis",
        storage_backend="s3",
        s3_bucket="prod-bucket",
        llm_provider="openai",
        llm_api_key="placeholder-never-called",
        mail_backend="ses",
        public_web_origin="https://app.example.com",
        mail_from_address="no-reply@example.com",
        mail_ses_region="us-east-1",
        **production_vector_kwargs(),
    )
    base.update(overrides)
    return Settings(**base)


def test_tier1_production_runtime_report_is_all_configured():
    # P6-PLAT-1 Tier 1: a production Settings with no patched build fact validates, and
    # the runtime report shows every capability configured, the vector one truthfully.
    report = build_runtime_report(_production_profile())
    assert report.all_configured is True
    assert report.environment == "production" and report.is_local_mode is False
    cap = {c.name: c for c in report.capabilities}["vector"]
    assert cap.backend == "bruteforce" and cap.detail == _DETAIL.format(n=100)


def test_production_readiness_vector_probe_reports_the_configured_bound():
    status, summary, detail, _ = _check_vector(
        _production_profile(vector_inprocess_max_signals_per_run=50)
    )
    assert status is ProbeStatus.HEALTHY
    assert "N_max=50" in summary and detail == _DETAIL.format(n=50)


# --------------------------------------------------------------------------- #
# Migration actor: no Redis adapter is imported (basis of the Redis exemption)
# --------------------------------------------------------------------------- #
def test_migration_actor_imports_no_redis_adapter():
    tree = ast.parse((_API_DIR / "app" / "db" / "migrate.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("redis", "app.infra.cache", "app.jobs", "app.core.middleware")
    assert not [m for m in imported if m == "redis" or m.startswith(forbidden)], imported
