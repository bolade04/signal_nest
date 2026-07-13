"""System runtime endpoints: liveness, readiness and capability introspection.

* ``GET {prefix}/system/health`` — liveness. Cheap, dependency-free; answers "is the
  process up?" and is safe to poll frequently.
* ``GET {prefix}/system/readiness`` — readiness. Answers "can this instance serve
  traffic?": the database schema must be migrated and every selected backend must be
  configured. Returns ``503`` when not ready so orchestrators hold traffic.
* ``GET {prefix}/system/capabilities`` — a **secret-free** view of which backend each
  capability is bound to and whether it is configured. Never exposes URLs, keys, bucket
  names or endpoints.

These endpoints are unauthenticated on purpose (no tenant data is returned) and expose
no secrets, so they are safe for load balancers and platform probes.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import inspect

from app.core.config import get_settings
from app.core.runtime import build_runtime_report
from app.db.session import engine

router = APIRouter(prefix="/system", tags=["system"])


class HealthOut(BaseModel):
    status: str
    mode: str


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


class ReadinessOut(BaseModel):
    ready: bool
    schema_migrated: bool
    all_configured: bool
    #: Secret-free list of capability names that are not yet configured.
    unconfigured: list[str]
    reasons: list[str]


def _schema_migrated() -> bool:
    """True when the authoritative Alembic schema is present (users + version table)."""
    tables = set(inspect(engine).get_table_names())
    return "users" in tables and "alembic_version" in tables


@router.get("/health", response_model=HealthOut)
def system_health() -> HealthOut:
    return HealthOut(status="ok", mode=get_settings().app_mode)


@router.get("/capabilities", response_model=CapabilitiesOut)
def system_capabilities() -> CapabilitiesOut:
    report = build_runtime_report()
    return CapabilitiesOut(**report.to_public_dict())


@router.get("/readiness", response_model=ReadinessOut)
def system_readiness(response: Response) -> ReadinessOut:
    report = build_runtime_report()
    schema_migrated = _schema_migrated()

    reasons: list[str] = []
    if not schema_migrated:
        reasons.append("database schema not migrated")
    for cap in report.unconfigured:
        reasons.append(f"{cap.name} backend '{cap.backend}' is not configured")

    ready = schema_migrated and report.all_configured
    if not ready:
        response.status_code = 503

    return ReadinessOut(
        ready=ready,
        schema_migrated=schema_migrated,
        all_configured=report.all_configured,
        unconfigured=[c.name for c in report.unconfigured],
        reasons=reasons,
    )
