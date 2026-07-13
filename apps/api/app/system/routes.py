"""System runtime endpoints: liveness, readiness and capability introspection.

Two disclosure tiers:

* **Public / customer tier** (this module, ``/system/*``)
  * ``GET {prefix}/system/health`` — liveness. Cheap, dependency-free, anonymous;
    answers "is the process up?" and is safe to poll frequently.
  * ``GET {prefix}/system/readiness`` — readiness. Anonymous (safe for load
    balancers that cannot authenticate). Runs the bounded active probes and
    returns only coarse, secret-free per-capability status (name + status +
    required) — never hosts, ports, URLs, backends, buckets, paths or exceptions.
  * ``GET {prefix}/system/capabilities`` — a **coarse** runtime summary (mode,
    environment, aggregate readiness). Requires authentication but is safe for
    any customer; it deliberately omits the per-capability backend topology.

* **Operator tier** (``app.system.internal_routes``, ``/internal/system/*``)
  exposes the detailed backend topology and probe diagnostics and requires an
  authenticated **operator**.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.core.config import get_settings
from app.core.runtime import build_runtime_report
from app.organizations.models import User
from app.system.probes import run_readiness_probes

router = APIRouter(prefix="/system", tags=["system"])


class HealthOut(BaseModel):
    status: str
    mode: str


class RuntimeSummaryOut(BaseModel):
    app_mode: str
    environment: str
    is_local_mode: bool
    all_configured: bool


class ReadinessCheckOut(BaseModel):
    name: str
    status: str
    required: bool


class ReadinessOut(BaseModel):
    ready: bool
    schema_migrated: bool
    all_configured: bool
    #: Secret-free list of capability names that are not yet ready.
    unconfigured: list[str]
    reasons: list[str]
    #: Coarse per-capability probe results (name + status + required only).
    checks: list[ReadinessCheckOut]


@router.get("/health", response_model=HealthOut)
def system_health() -> HealthOut:
    return HealthOut(status="ok", mode=get_settings().app_mode)


@router.get("/capabilities", response_model=RuntimeSummaryOut)
def system_capabilities(_user: User = Depends(get_current_user)) -> RuntimeSummaryOut:
    # Coarse summary for any authenticated customer. Detailed backend topology is
    # operator-only and lives at /internal/system/capabilities.
    report = build_runtime_report()
    return RuntimeSummaryOut(**report.to_summary_dict())


@router.get("/readiness", response_model=ReadinessOut)
def system_readiness(response: Response) -> ReadinessOut:
    report = run_readiness_probes()

    schema_migrated = next(
        (r.status.value == "healthy" for r in report.results if r.name == "database"),
        False,
    )
    not_ready = [r.name for r in report.results if r.is_blocking]
    reasons = [f"{r.name} is not ready ({r.status.value})" for r in report.blocking]

    if not report.ready:
        response.status_code = 503

    return ReadinessOut(
        ready=report.ready,
        schema_migrated=schema_migrated,
        all_configured=not not_ready,
        unconfigured=not_ready,
        reasons=reasons,
        checks=[ReadinessCheckOut(**r.to_public_dict()) for r in report.results],
    )
