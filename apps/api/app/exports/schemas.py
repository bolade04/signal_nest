"""Request/response schemas of the audited manual export API (Project Phase 5E, lane 5E-W1).

The routes (:mod:`app.exports.routes`; 5E interface sheet P4) accept and return these shapes:

    POST /workspaces/{workspace_id}/drafts/{draft_id}/exports   ExportIn  -> 201 ExportOut
    GET  /workspaces/{workspace_id}/drafts/{draft_id}/exports             -> 200 ExportHistoryOut
    GET  /workspaces/{workspace_id}/export-capability                     -> 200 ExportCapabilityOut

* **Strict request.** :class:`ExportIn` carries only the render format, enum-typed over the
  closed :class:`~app.core.enums.ExportFormat` set. The draft comes from the path; its version,
  the controlling approval, the jurisdiction and the exporter are derived server-side. Unknown
  fields are rejected.
* **The rendered text is returned inline** (:attr:`ExportOut.content`), once, in the answer to
  the export. It is never stored, signed into a URL or written to a file store (5E plan §2):
  the history (:class:`ExportHistoryItemOut`) lists the records without it.
* **Safe projection.** The export record's scope ids and its ``exported_by`` foreign key are
  not exposed; ``exported_by_snapshot`` is the compliance identity at export (``"[erased]"``
  once the account is erased), which the history's role set (item 32) may read.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.core.enums import ExportFormat


class ExportIn(BaseModel):
    """Export one approved draft version in one format."""

    model_config = ConfigDict(extra="forbid")

    format: ExportFormat


class ExportHistoryItemOut(BaseModel):
    """One audited export record of one draft version (no rendered text)."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: str
    #: NULL once the draft is gone (the approval keeps its id).
    draft_id: str | None
    #: The exact draft version exported.
    draft_version: int
    #: The controlling ``approved`` decision the export rested on; NULL once it is purged.
    approval_id: str | None
    format: ExportFormat
    #: The draft's captured jurisdiction, copied at export (never re-resolved).
    jurisdiction_code: str
    #: "<user id> <full name> <<email>>" at export ("[unverified]" in place of an address the
    #: exporter has not verified; app.core.actor_snapshot), or "[erased]".
    exported_by_snapshot: str
    is_simulated: bool
    created_at: datetime


class ExportOut(ExportHistoryItemOut):
    """The export just recorded, with its rendered text."""

    #: The draft's text rendered in ``format`` (:mod:`app.exports.render`). Returned once,
    #: here; never stored.
    content: str


class ExportHistoryOut(BaseModel):
    """A bounded page of one draft version's exports, newest first."""

    items: list[ExportHistoryItemOut]
    total: int
    limit: int
    offset: int


class ExportCapabilityOut(BaseModel):
    """Customer-visible export availability for one workspace.

    Exactly one field (the ``RecommendationBriefCapabilityOut`` precedent): no governance
    metadata (deciding rule, global flag, override) reaches a customer. Returned by
    ``GET /workspaces/{workspace_id}/export-capability`` with 200 while export is dark
    (interface sheet P7), so the UI can show the dark state without calling an export route.
    """

    #: Whether ``content_export`` resolves enabled for this workspace: the export routes
    #: answer 503 ``capability_unavailable`` while it is false.
    enabled: bool


__all__ = [
    "ExportCapabilityOut",
    "ExportHistoryItemOut",
    "ExportHistoryOut",
    "ExportIn",
    "ExportOut",
]
