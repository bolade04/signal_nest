"""Bounded, active readiness probes.

Readiness answers "can this instance serve traffic?" with a *real*, time-bounded
check of each selected backend rather than a config-only guess. Design rules:

* **Bounded.** Every probe runs under a strict per-probe timeout and the whole
  sweep runs under a total timeout, so a hung backend can never stall readiness.
* **Two disclosure levels.** ``ProbeResult.summary`` is safe for anonymous
  public readiness (capability name + coarse status only). ``detail`` is an
  operator-only diagnostic and may name the concrete failure. Neither ever
  contains hosts, ports, URLs, bucket names, filesystem paths, secret variable
  names, or raw exception text destined for the public surface.
* **No paid calls.** Provider probes verify configuration only; they never make
  a billable request.
* **Placeholders are never healthy.** A backend that cannot be actively verified
  reports ``not_configured``/``degraded``/``unavailable`` — never ``healthy``.

Probes are synchronous and independent, so the runner fans them out across a
thread pool and enforces the deadline with ``concurrent.futures``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import inspect, text

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.session import SessionLocal, engine

logger = get_logger("signalnest.probes")


class ProbeStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"
    TIMEOUT = "timeout"


#: Statuses that keep a *required* capability from being considered ready.
_BLOCKING = {ProbeStatus.UNAVAILABLE, ProbeStatus.NOT_CONFIGURED, ProbeStatus.TIMEOUT}


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single readiness probe."""

    name: str
    status: ProbeStatus
    required: bool
    #: Safe, coarse, public-surface message (no infra topology).
    summary: str
    #: Operator-only diagnostic (may name the concrete failure). Never public.
    detail: str | None = None
    duration_ms: float = 0.0
    retryable: bool = False
    timestamp: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.required and self.status in _BLOCKING

    def to_public_dict(self) -> dict[str, object]:
        return {"name": self.name, "status": self.status.value, "required": self.required}

    def to_operator_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status.value,
            "required": self.required,
            "summary": self.summary,
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 2),
            "retryable": self.retryable,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class ReadinessProbe:
    """A named, active readiness check bound to one capability."""

    name: str
    required: bool
    check: Callable[[Settings], tuple[ProbeStatus, str, str | None, bool]]

    def run(self, settings: Settings, *, timeout: float) -> ProbeResult:
        started = time.perf_counter()
        ts = datetime.now(UTC).isoformat()
        # A dedicated single-thread executor bounds this probe. We shut it down
        # without waiting so a hung backend thread can never block the caller
        # (the abandoned thread finishes on its own; it holds no lock we need).
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            status, summary, detail, retryable = pool.submit(
                self.check, settings
            ).result(timeout=timeout)
        except FuturesTimeout:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.TIMEOUT,
                required=self.required,
                summary="check did not complete within the timeout",
                detail=f"probe exceeded {timeout:.2f}s budget",
                duration_ms=(time.perf_counter() - started) * 1000,
                retryable=True,
                timestamp=ts,
            )
        except Exception as exc:  # noqa: BLE001 - any backend error => unavailable
            logger.warning("probe.error", extra={"extra_fields": {"probe": self.name}})
            # Report only the exception *class*, never its message: a raw adapter
            # error (e.g. a driver connection error) can embed a host, port or URL,
            # and this detail is surfaced on the operator diagnostics endpoint.
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNAVAILABLE,
                required=self.required,
                summary="check failed",
                detail=type(exc).__name__,
                duration_ms=(time.perf_counter() - started) * 1000,
                retryable=True,
                timestamp=ts,
            )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return ProbeResult(
            name=self.name,
            status=status,
            required=self.required,
            summary=summary,
            detail=detail,
            duration_ms=(time.perf_counter() - started) * 1000,
            retryable=status in (ProbeStatus.TIMEOUT, ProbeStatus.UNAVAILABLE),
            timestamp=ts,
        )


# --- Individual capability checks -------------------------------------------
# Each returns (status, safe_summary, operator_detail, retryable).


def _check_database(_settings: Settings) -> tuple[ProbeStatus, str, str | None, bool]:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        tables = set(inspect(conn).get_table_names())
    if "users" not in tables or "alembic_version" not in tables:
        return (
            ProbeStatus.UNAVAILABLE,
            "connected but schema is not migrated",
            "missing users/alembic_version tables",
            False,
        )
    return (ProbeStatus.HEALTHY, "connected and schema migrated", None, False)


def _check_queue(settings: Settings) -> tuple[ProbeStatus, str, str | None, bool]:
    if settings.queue_backend == "redis":
        return _ping_redis(settings, "queue")
    # In-process queue: the synchronous executor is available as soon as its
    # adapter is constructed (it needs no broker or worker).
    from app.infra.queue import queue

    if queue is not None:
        return (ProbeStatus.HEALTHY, "in-process queue initialized", None, False)
    return (
        ProbeStatus.UNAVAILABLE,
        "in-process queue is not initialized",
        "queue adapter is not constructed",
        True,
    )


def _check_durable_queue(_settings: Settings) -> tuple[ProbeStatus, str, str | None, bool]:
    # Verify the durable job store is queryable (its schema is migrated). This is
    # a schema/connectivity check only — it never makes a billable call and does
    # not depend on a worker being up.
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        if "jobs" not in tables or "job_events" not in tables:
            return (
                ProbeStatus.UNAVAILABLE,
                "durable job schema is not migrated",
                "missing jobs/job_events tables",
                False,
            )
        conn.execute(text("SELECT 1 FROM jobs LIMIT 1"))
    return (ProbeStatus.HEALTHY, "durable job store is queryable", None, False)


def _check_cache(settings: Settings) -> tuple[ProbeStatus, str, str | None, bool]:
    if settings.cache_backend == "redis":
        return _ping_redis(settings, "cache")
    return (ProbeStatus.HEALTHY, "in-memory cache initialized", None, False)


def _check_storage(settings: Settings) -> tuple[ProbeStatus, str, str | None, bool]:
    if settings.storage_backend == "s3":
        # Config-only: no billable S3 call. A placeholder without a bucket is
        # explicitly not-configured rather than falsely healthy.
        if not settings.s3_bucket:
            return (
                ProbeStatus.NOT_CONFIGURED,
                "object storage is not configured",
                "s3_bucket is not set",
                False,
            )
        return (
            ProbeStatus.DEGRADED,
            "object storage configured (not actively verified)",
            "s3 connectivity not probed (config-only)",
            False,
        )
    # Local filesystem: verify the storage root is writable without a destructive
    # write. The adapter creates the directory lazily, so a not-yet-created root
    # is healthy as long as its nearest existing ancestor is writable. Reject any
    # parent-directory traversal in the configured path.
    raw = settings.local_storage_dir
    if os.path.pardir in os.path.normpath(raw).split(os.sep):
        return (
            ProbeStatus.UNAVAILABLE,
            "storage path is not permitted",
            "local_storage_dir contains a parent-directory traversal",
            False,
        )
    probe_dir = os.path.abspath(raw)
    while not os.path.isdir(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:  # reached the filesystem root
            break
        probe_dir = parent
    if not os.access(probe_dir, os.W_OK):
        return (
            ProbeStatus.UNAVAILABLE,
            "storage root is not writable",
            "local storage directory is not writable",
            True,
        )
    return (ProbeStatus.HEALTHY, "local storage root is writable", None, False)


def _check_vector(settings: Settings) -> tuple[ProbeStatus, str, str | None, bool]:
    if settings.vector_backend == "pgvector":
        if settings.is_sqlite:
            return (
                ProbeStatus.NOT_CONFIGURED,
                "vector backend requires a PostgreSQL database",
                "pgvector selected on a sqlite database",
                False,
            )
        return (
            ProbeStatus.DEGRADED,
            "pgvector configured (not actively verified)",
            "pgvector connectivity not probed (config-only)",
            False,
        )
    return (ProbeStatus.HEALTHY, "brute-force vector index initialized", None, False)


def _check_llm(settings: Settings) -> tuple[ProbeStatus, str, str | None, bool]:
    # Config-only: never make a billable model call from a readiness probe.
    if settings.llm_provider == "mock":
        return (ProbeStatus.HEALTHY, "deterministic mock provider ready", None, False)
    if not settings.llm_api_key:
        return (
            ProbeStatus.NOT_CONFIGURED,
            "LLM provider is not configured",
            "llm_api_key is not set",
            False,
        )
    # Real provider configured but not actively verified (no paid call).
    return (
        ProbeStatus.DEGRADED,
        "LLM provider configured (not actively verified)",
        "external provider not probed (config-only)",
        False,
    )


def _check_worker_registry(
    settings: Settings,
) -> tuple[ProbeStatus, str, str | None, bool]:
    # Schema/connectivity check plus a policy-gated liveness check. Worker
    # presence is *informational by default*: the API can serve traffic without a
    # worker fleet. Only when ``require_worker_fleet`` is enabled does the absence
    # of a live worker block readiness. Never names worker ids or build metadata.
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    if "worker_registrations" not in tables:
        return (
            ProbeStatus.UNAVAILABLE,
            "worker registry schema is not migrated",
            "missing worker_registrations table",
            False,
        )
    if not settings.require_worker_fleet:
        return (
            ProbeStatus.HEALTHY,
            "worker registry available (fleet presence informational)",
            None,
            False,
        )
    from app.jobs.worker_registry import worker_registry

    with SessionLocal() as db:
        active = worker_registry.active_count(
            db, stale_after_seconds=settings.worker_stale_after_seconds
        )
    if active < 1:
        return (
            ProbeStatus.UNAVAILABLE,
            "no active worker in the fleet",
            "require_worker_fleet is enabled but no worker is ready/busy with a fresh heartbeat",
            True,
        )
    return (ProbeStatus.HEALTHY, "worker fleet has an active worker", None, False)


def _ping_redis(
    settings: Settings, capability: str
) -> tuple[ProbeStatus, str, str | None, bool]:  # pragma: no cover - full mode only
    if not settings.redis_url:
        return (
            ProbeStatus.NOT_CONFIGURED,
            f"{capability} backend is not configured",
            "redis_url is not set",
            False,
        )
    import redis

    client = redis.from_url(settings.redis_url, socket_connect_timeout=1)
    client.ping()
    return (ProbeStatus.HEALTHY, f"{capability} backend reachable", None, False)


def _build_probes(settings: Settings) -> list[ReadinessProbe]:
    return [
        ReadinessProbe("database", required=True, check=_check_database),
        ReadinessProbe("queue", required=True, check=_check_queue),
        ReadinessProbe("durable_queue", required=True, check=_check_durable_queue),
        ReadinessProbe("cache", required=True, check=_check_cache),
        ReadinessProbe("storage", required=True, check=_check_storage),
        ReadinessProbe("vector", required=True, check=_check_vector),
        ReadinessProbe("llm", required=False, check=_check_llm),
        ReadinessProbe(
            "worker_registry",
            required=settings.require_worker_fleet,
            check=_check_worker_registry,
        ),
    ]


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    results: list[ProbeResult]

    @property
    def blocking(self) -> list[ProbeResult]:
        return [r for r in self.results if r.is_blocking]


def run_readiness_probes(settings: Settings | None = None) -> ReadinessReport:
    """Run every probe under strict per-probe and total time budgets.

    Probes run in parallel. Any probe still running when the total deadline
    passes is reported as a timeout rather than being awaited, so the sweep is
    bounded even if several backends hang at once.
    """
    s = settings or get_settings()
    probes = _build_probes(s)
    per_probe = s.readiness_probe_timeout_seconds
    deadline = time.perf_counter() + s.readiness_total_timeout_seconds

    results: dict[str, ProbeResult] = {}
    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futures = {pool.submit(p.run, s, timeout=per_probe): p for p in probes}
        for future, probe in futures.items():
            remaining = max(0.0, deadline - time.perf_counter())
            budget = min(per_probe, remaining) if remaining > 0 else 0.0
            try:
                results[probe.name] = future.result(timeout=budget) if budget > 0 else (
                    _timed_out(probe)
                )
            except FuturesTimeout:
                results[probe.name] = _timed_out(probe)

    ordered = [results[p.name] for p in probes]
    ready = not any(r.is_blocking for r in ordered)
    return ReadinessReport(ready=ready, results=ordered)


def _timed_out(probe: ReadinessProbe) -> ProbeResult:
    return ProbeResult(
        name=probe.name,
        status=ProbeStatus.TIMEOUT,
        required=probe.required,
        summary="check did not complete within the timeout",
        detail="total readiness budget exhausted",
        retryable=True,
        timestamp=datetime.now(UTC).isoformat(),
    )
