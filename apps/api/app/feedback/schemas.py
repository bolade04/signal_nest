"""Request/response schemas for the feature-gated opportunity-feedback API (3C-C).

These are the *only* HTTP shapes the feedback subsystem exposes. They deliberately
encode the owner-approved product decisions at the boundary:

* **Strict request** — :class:`FeedbackCreate` accepts *only* the caller-owned facts:
  which immutable intelligence record the judgement is about, the binary
  ``is_useful`` verdict, and an *optional* structured reason from the closed
  :class:`~app.core.enums.FeedbackReason` vocabulary (no free text). Everything else
  — tenant scope, actor, and the version provenance — is derived server-side and can
  never be supplied by a client. ``extra="forbid"`` rejects any unknown field
  (including attempts to smuggle ``organization_id`` / ``workspace_id`` /
  ``opportunity_id`` / ``fingerprint`` / ``submitted_by_user_id``) with a 422.
* **Safe response** — :class:`FeedbackOut` projects a stored row into a customer-safe
  view. It exposes the judgement plus the *public-safe* version provenance
  (``analysis_version`` / ``scoring_version``) but never the raw ``fingerprint`` (kept
  internal, mirroring the intelligence provenance boundary) nor the internal tenant
  scope columns.
* **Bounded history** — :class:`FeedbackHistoryOut` is the standard limit/offset page
  envelope used across the API.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.core.enums import FeedbackReason


class FeedbackCreate(BaseModel):
    """Strict request body for capturing one opportunity-feedback judgement.

    The client supplies only ``intelligence_record_id`` (which immutable record the
    judgement is about), the binary ``is_useful`` verdict, and an optional
    ``reason_code``. Tenant scope, actor attribution and version provenance are all
    derived server-side. Unknown fields are rejected (``extra="forbid"``), so a caller
    can never inject scope, provenance or attribution.
    """

    model_config = ConfigDict(extra="forbid")

    intelligence_record_id: str
    is_useful: bool
    reason_code: FeedbackReason | None = None


class FeedbackOut(BaseModel):
    """Customer-safe projection of a stored :class:`OpportunityFeedback` row.

    Exposes the judgement, its optional reason, the actor (if still known) and the
    *public-safe* version provenance — never the raw ``fingerprint`` or the internal
    ``organization_id`` / ``workspace_id`` scope columns.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    opportunity_id: str
    intelligence_record_id: str
    is_useful: bool
    reason_code: FeedbackReason | None
    submitted_by_user_id: str | None
    analysis_version: str
    scoring_version: str
    created_at: datetime


class FeedbackHistoryOut(BaseModel):
    """Bounded, reverse-chronological page of an opportunity's feedback history."""

    items: list[FeedbackOut]
    total: int
    limit: int
    offset: int
