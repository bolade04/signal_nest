"""Explicit tenant/location execution context for background jobs.

Phase 1-2 enforced tenant + per-location isolation inside HTTP request handlers
(``app.auth.dependencies.TenantContext``). Jobs, however, received only a bare
``{"scout_request_id": ...}`` dict and re-derived tenancy from the row. This module
makes the tenant/location scope of a job **explicit and carried with the work**, so a
job can never be executed without knowing which organization / workspace / location /
campaign it belongs to.

The context is a plain, JSON-serializable value object (no ORM instances) so it can be
transported across the in-process queue today and a durable queue later without change.
"""

from __future__ import annotations

from pydantic import BaseModel


class ExecutionContext(BaseModel):
    """Immutable tenant + location scope carried alongside a job.

    ``organization_id`` and ``workspace_id`` are always required; ``location_id`` and
    ``campaign_id`` are optional because not every job is location- or campaign-scoped.
    Correlation ids are optional and used only for tracing, never for authorization.
    """

    model_config = {"frozen": True}

    organization_id: str
    workspace_id: str
    location_id: str | None = None
    campaign_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None

    @classmethod
    def for_scout_request(
        cls,
        *,
        organization_id: str,
        workspace_id: str,
        location_id: str | None = None,
        campaign_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> ExecutionContext:
        return cls(
            organization_id=organization_id,
            workspace_id=workspace_id,
            location_id=location_id,
            campaign_id=campaign_id,
            request_id=request_id,
            trace_id=trace_id,
        )

    @property
    def isolation_key(self) -> tuple[str, str, str | None]:
        """The tuple that must never blend across markets: (org, workspace, location)."""
        return (self.organization_id, self.workspace_id, self.location_id)


class JobContextError(Exception):
    """Raised when a job payload lacks the required tenant execution context."""


def scope_matches(context: ExecutionContext, *, organization_id: str, workspace_id: str) -> bool:
    """Guard used by handlers to verify the loaded row matches the declared scope.

    Isolation is non-negotiable: if a job's declared context does not match the entity
    it loaded, the handler must refuse rather than process cross-tenant data.
    """
    return (
        context.organization_id == organization_id
        and context.workspace_id == workspace_id
    )


__all__ = ["ExecutionContext", "JobContextError", "scope_matches"]
