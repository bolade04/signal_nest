"""Live-egress eligibility gate (Phase 3B Batch 2) — fail closed.

This is the single decision point for "may the RSS connector perform *live*
network egress for this request?". It combines, in fail-closed order:

1. the emergency kill switch (``connector_rss_kill_switch``) — if set, nothing runs;
2. the global live flag (``connector_rss_live_enabled``) — off by default;
3. the presence of at least one **activatable** approved source (the registry
   ships empty, so this is false by default);
4. the tenant/workspace/jurisdiction/canary allowlists — empty ⇒ nobody eligible.

Because the registry is empty and every flag defaults off/empty, this module
returns "no live egress" under all default configuration, and there is no
real-egress transport to run even if it did. It exists so the enablement change is
a small, auditable, reversible flip — never an implicit code path.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.connectors.sources import ApprovedSource, ApprovedSourceRegistry, get_registry
from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class LiveEgressDecision:
    """Why live egress is or is not permitted, for observability/audit."""

    permitted: bool
    reason: str


def _rollout_admits(
    settings: Settings, *, tenant_id: str | None, workspace_id: str | None
) -> bool:
    """True only when the request's tenant/workspace is explicitly allowlisted.

    Empty allowlists mean *nobody* is eligible (fail closed), never everybody.
    """
    if not settings.connector_rss_live_tenants or not settings.connector_rss_live_workspaces:
        return False
    if tenant_id is None or workspace_id is None:
        return False
    return (
        tenant_id in set(settings.connector_rss_live_tenants)
        and workspace_id in set(settings.connector_rss_live_workspaces)
    )


def decide_live_egress(
    *,
    market: str | None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    settings: Settings | None = None,
    registry: ApprovedSourceRegistry | None = None,
) -> LiveEgressDecision:
    """Return the fail-closed live-egress decision for a request scope."""
    settings = settings or get_settings()
    registry = registry or get_registry()

    if settings.connector_rss_kill_switch:
        return LiveEgressDecision(False, "kill_switch")
    if not settings.connector_rss_live_enabled:
        return LiveEgressDecision(False, "disabled")

    active = registry.activatable()
    if not active:
        return LiveEgressDecision(False, "no_approved_source")
    if len(active) > settings.connector_rss_live_max_active_sources:
        return LiveEgressDecision(False, "too_many_active_sources")
    if not _rollout_admits(settings, tenant_id=tenant_id, workspace_id=workspace_id):
        return LiveEgressDecision(False, "tenant_not_in_rollout")

    eligible = _eligible_sources(active, settings=settings, market=market)
    if not eligible:
        return LiveEgressDecision(False, "no_source_for_jurisdiction")

    return LiveEgressDecision(True, "permitted")


def _eligible_sources(
    active: tuple[ApprovedSource, ...], *, settings: Settings, market: str | None
) -> tuple[ApprovedSource, ...]:
    """Approved sources whose jurisdiction admits ``market`` and the config allows."""
    allowed_jur = {j.lower() for j in settings.connector_rss_live_jurisdictions}
    result: list[ApprovedSource] = []
    for source in active:
        if not source.permits_market(market):
            continue
        if allowed_jur and market is not None:
            if not any(j in market.lower() for j in allowed_jur):
                continue
        result.append(source)
    return tuple(result)


def live_egress_available(settings: Settings | None = None) -> bool:
    """Coarse, request-independent check: could live egress ever run right now?

    False whenever the kill switch is on, the global flag is off, or the registry
    has no activatable source — i.e. always false under default config.
    """
    settings = settings or get_settings()
    if settings.connector_rss_kill_switch or not settings.connector_rss_live_enabled:
        return False
    return bool(get_registry().activatable())
