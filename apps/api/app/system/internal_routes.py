"""Operator-only runtime introspection (``/internal/system/*``).

These endpoints expose the detailed infrastructure topology (per-capability
backend names, configuration gaps) and full readiness diagnostics (durations,
retryability, operator detail). That information is operational and must not be
enumerable by ordinary customers, so every route requires an authenticated
**operator** (``require_operator``). They still never surface secrets — no URLs,
credentials, bucket names or endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import require_operator
from app.core.runtime import build_runtime_report
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
