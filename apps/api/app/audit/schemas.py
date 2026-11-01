"""Typed response schema of the audit read API (Project Phase 5B; frozen at W21 CF-1).

:class:`AuditLogOut` is what the typed audit read route will return (5B-W2 wires it onto
``GET /workspaces/{workspace_id}/audit-logs`` together with its filters; CF-1 changes no
route). ``action`` is typed by the closed :class:`~app.audit.actions.AuditAction`
registry, so the vocabulary reaches ``schema.d.ts`` as a closed union rather than a bare
string (Project Phase 5 plan §6.2.1), and ``previous_state`` / ``new_state`` are surfaced.

Never exposed: ``organization_id`` (the caller's own organization, implied by the route)
and ``updated_at`` (an anonymisation marker, see :mod:`app.audit.models`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.audit.actions import AuditAction


class AuditLogOut(BaseModel):
    """One audit record as an owner, admin or compliance reviewer reads it."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    #: NULL for an organization-level record (no workspace).
    workspace_id: str | None
    action: AuditAction
    #: NULL when the actor's account was erased (or the action had no actor).
    actor_user_id: str | None
    entity_type: str | None
    entity_id: str | None
    reason: str | None
    previous_state: dict[str, Any] | None
    new_state: dict[str, Any] | None
    #: ``{}`` when empty; NULL is possible (the column is nullable) though never written.
    context: dict[str, Any] | None
    created_at: datetime


__all__ = ["AuditLogOut"]
