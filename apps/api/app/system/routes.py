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
    workspace?". Since P6-CAP-1 those differ for every capability: enforcement is the
    capability resolver's per-workspace decision, in which a workspace override (or
    the safety ceiling) outranks the flag. The customer-facing, workspace-effective
    answers live on their own routes —
    ``GET /workspaces/{workspace_id}/feedback-capability`` and
    ``GET /workspaces/{workspace_id}/scheduling-capability`` — and the customer UI
    reads those, not these.
    """

    #: The RAW global opportunity-feedback flag. It does NOT decide availability
    #: for a workspace holding an honored enable override — the resolver returns
    #: the override before it reaches this flag — and the feedback UI no longer
    #: reads this field at all: it reads the workspace-effective reflection at
    #: ``GET /workspaces/{workspace_id}/feedback-capability`` (P6-UI-005).
    opportunity_feedback_enabled: bool

    #: The RAW global scout-scheduling flag. Since P6-CAP-1 it does NOT decide
    #: availability for a workspace holding an honored override; the schedule panel
    #: reads the workspace-effective reflection at
    #: ``GET /workspaces/{workspace_id}/scheduling-capability`` instead (P6-UI-006).
    scout_scheduling_enabled: bool

    #: The RAW global RSS-connector flag, reflected for parity with the capability
    #: registry. Since P6-CAP-1 connector selection is the resolver's per-workspace
    #: decision (a disable override or the ceiling can turn RSS off for one
    #: workspace even while this reads ``true``); reflection alone selects no
    #: connector and performs no fetch.
    connector_rss_enabled: bool

    #: The RAW global recommendation-briefs flag (Project Phase 5A, CF-1), reflected
    #: per operator decision O-4 (``ALT_expose_all``). Like the others it does NOT
    #: decide availability for a workspace: the briefs/approvals gate will decide
    #: through the capability resolver, where a workspace override outranks it.
    recommendation_briefs_enabled: bool

    #: The RAW global creative-generation flag (Project Phase 5C, CF-2), reflected per
    #: operator decision O-4 (``ALT_expose_all``). It does not decide availability for a
    #: workspace: the 5C gate will decide through the capability resolver.
    creative_generation_enabled: bool


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
        # Raw global flags, one field per registered capability (P6-CAP-2).
        # Deliberately NOT resolver-derived: this endpoint has no workspace context,
        # so it cannot express a per-workspace decision.
        #
        # Since P6-CAP-1 every capability *enforces* per workspace through the
        # resolver, so these keys are no longer what any gate decides on. The
        # customer UI reads the workspace-effective routes instead —
        # `GET /workspaces/{workspace_id}/feedback-capability` (P6-UI-005) and
        # `GET /workspaces/{workspace_id}/scheduling-capability` (P6-UI-006) — which
        # share one resolver call site with their gates. These fields keep their
        # raw-global meaning so no existing reader is silently repurposed.
        features=FeatureFlagsOut(
            opportunity_feedback_enabled=settings.opportunity_feedback_enabled,
            scout_scheduling_enabled=settings.scout_scheduling_enabled,
            connector_rss_enabled=settings.connector_rss_enabled,
            recommendation_briefs_enabled=settings.recommendation_briefs_enabled,
            creative_generation_enabled=settings.creative_generation_enabled,
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
