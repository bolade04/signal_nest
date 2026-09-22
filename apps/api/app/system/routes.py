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


class FeatureFlagsOut(BaseModel):
    """Coarse, read-only product-capability booleans.

    These are *reflections* of raw server feature flags — never a customer-settable
    toggle. Secret-free: only booleans, never backend topology or configuration
    values.

    They answer "is this flag on?", NOT "is this capability available to my
    workspace?". For ``scout_scheduling`` and ``connector_rss`` those coincide,
    because enforcement reads the raw flag. For ``opportunity_feedback`` they do
    not: an honored workspace override outranks the flag, so this field can read
    ``false`` while the feedback endpoints serve normally. The customer-facing,
    workspace-effective answer lives on its own route,
    ``GET /workspaces/{workspace_id}/feedback-capability``.
    """

    #: The RAW global opportunity-feedback flag. It does NOT decide availability
    #: for a workspace holding an honored enable override — the resolver returns
    #: the override before it reaches this flag — and the feedback UI no longer
    #: reads this field at all: it reads the workspace-effective reflection at
    #: ``GET /workspaces/{workspace_id}/feedback-capability`` (P6-UI-005).
    opportunity_feedback_enabled: bool

    #: Whether scout scheduling is enabled server-side. While false the schedule
    #: *mutation* endpoints answer 503 while the schedule *read* stays available,
    #: so the UI keeps an existing schedule visible and withholds only the
    #: mutation affordances.
    scout_scheduling_enabled: bool

    #: Whether the RSS connector is enabled server-side. Reflected for parity
    #: with the capability registry so a client can tell the capability is dark
    #: without probing; reflection alone selects no connector and performs no
    #: fetch.
    connector_rss_enabled: bool


class RuntimeSummaryOut(BaseModel):
    app_mode: str
    environment: str
    is_local_mode: bool
    all_configured: bool
    #: Read-only product-capability reflections (see FeatureFlagsOut).
    features: FeatureFlagsOut


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
    settings = get_settings()
    report = build_runtime_report(settings)
    return RuntimeSummaryOut(
        **report.to_summary_dict(),
        # Raw global flags, one field per registered capability. Deliberately NOT
        # resolver-derived, and this is settled rather than pending: scheduling
        # enforcement reads the raw setting, so a per-workspace effective value
        # here would not describe what the mutation endpoints actually do.
        #
        # `P6-UI-005` shipped in 6U-1H and deliberately did NOT convert these
        # fields. The customer workspace-effective feedback reflection lives on
        # its own route (`GET /workspaces/{workspace_id}/feedback-capability`),
        # precisely so this endpoint could keep raw-global meaning for a key the
        # UI already consumes. Making scheduling resolver-backed is a different
        # problem — `P6-UI-006` / `P6-CAP-1` — and remains open.
        features=FeatureFlagsOut(
            opportunity_feedback_enabled=settings.opportunity_feedback_enabled,
            scout_scheduling_enabled=settings.scout_scheduling_enabled,
            connector_rss_enabled=settings.connector_rss_enabled,
        ),
    )


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
