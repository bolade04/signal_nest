"""Operator-only runtime introspection (``/internal/system/*``).

These endpoints expose the detailed infrastructure topology (per-capability
backend names, configuration gaps) and full readiness diagnostics (durations,
retryability, operator detail). That information is operational and must not be
enumerable by ordinary customers, so every route requires an authenticated
**operator** (``require_operator``). They still never surface secrets — no URLs,
credentials, bucket names or endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_operator
from app.core.config import get_settings
from app.core.metrics import exporter_status, telemetry_failure_count
from app.core.runtime import build_runtime_report
from app.db.session import get_db
from app.jobs.models import Job
from app.jobs.schemas import JobDiagnosticsOut, JobOperatorOut
from app.jobs.worker_registry import worker_registry
from app.jobs.worker_schemas import WorkerFleetDiagnosticsOut, WorkerSummaryOut
from app.organizations.models import User
from app.system.probes import run_readiness_probes

router = APIRouter(prefix="/internal/system", tags=["internal"])


class CapabilityOut(BaseModel):
    name: str
    backend: str
    configured: bool
    is_local: bool
    requires_external: bool
    detail: str | None = None


class CapabilitiesOut(BaseModel):
    app_mode: str
    environment: str
    llm_provider: str
    is_local_mode: bool
    all_configured: bool
    capabilities: list[CapabilityOut]


class TelemetryStatusOut(BaseModel):
    """Operator-safe observability posture. Deliberately free of any endpoint,
    credential, URL, payload, tenant/request/job identifier or token — only
    bounded, enumerable status values and a failure *count*."""

    logging_format: str  # "json" | "console"
    metrics_enabled: bool
    exporter_status: str  # "disabled" | "healthy" | "degraded"
    telemetry_failures: int
    correlation_enabled: bool
    redaction_enabled: bool


class ProbeDiagnosticOut(BaseModel):
    name: str
    status: str
    required: bool
    summary: str
    detail: str | None = None
    duration_ms: float
    retryable: bool
    timestamp: str


class ReadinessDiagnosticsOut(BaseModel):
    ready: bool
    probes: list[ProbeDiagnosticOut]


@router.get("/capabilities", response_model=CapabilitiesOut)
def internal_capabilities(_operator: User = Depends(require_operator)) -> CapabilitiesOut:
    report = build_runtime_report()
    return CapabilitiesOut(**report.to_public_dict())


@router.get("/readiness", response_model=ReadinessDiagnosticsOut)
def internal_readiness(
    _operator: User = Depends(require_operator),
) -> ReadinessDiagnosticsOut:
    report = run_readiness_probes()
    return ReadinessDiagnosticsOut(
        ready=report.ready,
        probes=[ProbeDiagnosticOut(**r.to_operator_dict()) for r in report.results],
    )


@router.get("/telemetry", response_model=TelemetryStatusOut)
def internal_telemetry(_operator: User = Depends(require_operator)) -> TelemetryStatusOut:
    """Operator-only observability posture: logging mode, metrics/exporter health,
    swallowed-telemetry-failure count, and whether correlation + redaction are on.

    Correlation and redaction are structural in this build (the middleware and the
    log formatter are always installed), so both are reported ``True``. Nothing here
    is customer-enumerable and nothing is a secret.
    """
    settings = get_settings()
    return TelemetryStatusOut(
        logging_format=settings.effective_log_format,
        metrics_enabled=settings.metrics_enabled,
        exporter_status=exporter_status(metrics_enabled=settings.metrics_enabled),
        telemetry_failures=telemetry_failure_count(),
        correlation_enabled=True,
        redaction_enabled=True,
    )


@router.get("/jobs", response_model=JobDiagnosticsOut)
def internal_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> JobDiagnosticsOut:
    """Cross-tenant queue diagnostics for operators.

    Surfaces status counts and the most recent jobs with safe worker/lease
    detail. This is operational introspection, so it is not workspace-scoped —
    hence the operator gate — but it still never exposes a raw payload or secret.
    """
    counts = dict(
        db.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    )
    recent = list(
        db.execute(select(Job).order_by(Job.created_at.desc()).limit(limit)).scalars()
    )
    return JobDiagnosticsOut(
        status_counts={str(k): int(v) for k, v in counts.items()},
        recent=[JobOperatorOut.model_validate(j) for j in recent],
    )


@router.get("/workers", response_model=WorkerFleetDiagnosticsOut)
def internal_workers(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> WorkerFleetDiagnosticsOut:
    """Coarse worker-fleet diagnostics for operators.

    Reports the fleet's aggregate health (status counts, active/stale totals)
    and a per-worker lifecycle summary. Stale is derived live from the configured
    threshold so the count is accurate regardless of sweep cadence. This never
    exposes a worker id, build revision, host fingerprint, URL or raw error.
    """
    stale_after = get_settings().worker_stale_after_seconds
    rows = worker_registry.list_workers(db, limit=limit)
    return WorkerFleetDiagnosticsOut(
        status_counts=worker_registry.status_counts(db),
        active_count=worker_registry.active_count(db, stale_after_seconds=stale_after),
        stale_count=worker_registry.stale_count(db, stale_after_seconds=stale_after),
        workers=[WorkerSummaryOut.model_validate(r, from_attributes=True) for r in rows],
    )
