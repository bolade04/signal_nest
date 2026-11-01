"""Request/response schemas of the creative-generation API (Project Phase 5C; frozen at W21
CF-2).

The 5C routes — generate ``POST /workspaces/{workspace_id}/briefs/{brief_id}/drafts``, list
``GET /workspaces/{workspace_id}/briefs/{brief_id}/drafts``, fetch one
``GET /workspaces/{workspace_id}/drafts/{draft_id}`` (Phase 5C plan §2) — accept and return
these shapes. CF-2 mounts no route, so they are not yet in ``openapi.json``.

* **Strict request.** :class:`CreativeDraftCreate` carries the channel and, for a revision,
  the draft being revised. The brief comes from the path; scope, version numbering, the
  model inputs, provenance and the author are derived server-side. Unknown fields are
  rejected. No free text is accepted: everything the model sees comes from stored context.
* **Generation is a durable job.** The generate route only enqueues
  (:class:`CreativeGenerationQueuedOut`, then poll the job) — the job's payload contract is
  :class:`CreativeGeneratePayload`.
* **Safe response.** :class:`CreativeDraftOut` exposes the text, versioning, the public-safe
  provenance and the author, with the content review embedded; it never exposes scope ids,
  ``input_hash``, ``trace_id``, the usage snapshot or the usage-ledger link (the CF-1 brief
  boundary).
* **Closed vocabularies are enum-typed** (``CreativeChannel``, ``ClaimRisk``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ClaimRisk, CreativeChannel

#: Content bounds (characters / items), frozen at CF-2.
CREATIVE_BOUNDS = {
    "headline": 200,
    "body": 4000,
    "call_to_action": 200,
    "review_items": 50,
    "review_item_chars": 500,
}

_Id = Annotated[str, Field(min_length=1, max_length=32)]


class GeneratedCreative(BaseModel):
    """The bounded text one generation produces (the ``generate_creative`` task output is
    validated by ``app.llm.schemas.GenerateCreativeOut``; this is what a draft stores)."""

    model_config = ConfigDict(extra="forbid")

    headline: str | None = Field(default=None, max_length=CREATIVE_BOUNDS["headline"])
    body: str = Field(min_length=1, max_length=CREATIVE_BOUNDS["body"])
    call_to_action: str | None = Field(
        default=None, max_length=CREATIVE_BOUNDS["call_to_action"]
    )


class CreativeDraftCreate(BaseModel):
    """Strict request body to generate one draft version from a brief."""

    model_config = ConfigDict(extra="forbid")

    channel: CreativeChannel
    #: For a revision: the draft (of the same brief and workspace) being revised.
    parent_draft_id: _Id | None = None


class CreativeGeneratePayload(BaseModel):
    """The ``creative.generate`` durable job's payload (ids only; no content)."""

    model_config = ConfigDict(extra="forbid")

    brief_id: _Id
    channel: CreativeChannel
    parent_draft_id: _Id | None = None
    requested_by: _Id


class CreativeGenerationQueuedOut(BaseModel):
    """The generate route's answer: the durable job to poll."""

    job_id: str


class ContentFindingOut(BaseModel):
    category: str
    risk: ClaimRisk
    excerpt: str


class ContentReviewOut(BaseModel):
    """The output-side review of one draft version."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    draft_id: str
    draft_version: int
    risk_level: ClaimRisk
    is_blocked: bool
    findings: list[ContentFindingOut]
    warnings: list[str]
    blocked_claim_ids: list[str]
    required_disclaimers: list[str]
    disclaimers_resolved: bool
    passed: bool
    created_at: datetime


class CreativeDraftOut(GeneratedCreative):
    """Customer-safe projection of one stored draft version, with its review."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: str
    brief_id: str
    opportunity_id: str
    location_id: str | None
    campaign_id: str | None
    draft_version: int
    parent_draft_id: str | None
    channel: CreativeChannel
    prompt_name: str
    prompt_version: str
    model_identifier: str
    is_simulated: bool
    #: NULL once the author's account is erased.
    created_by: str | None
    #: "<user id> <full name> <<email>>" at generation, or "[erased]".
    created_by_snapshot: str
    created_at: datetime
    #: NULL only while the review has not been written.
    review: ContentReviewOut | None = None


class CreativeDraftListOut(BaseModel):
    """Bounded, newest-version-first page of a brief's drafts."""

    items: list[CreativeDraftOut]
    total: int
    limit: int
    offset: int


class CreativeGenerationCapabilityOut(BaseModel):
    """Customer-visible creative-generation availability for one workspace.

    Exactly one field (the ``FeedbackCapabilityOut`` precedent): no governance metadata
    reaches a customer.
    """

    enabled: bool


__all__ = [
    "CREATIVE_BOUNDS",
    "ContentFindingOut",
    "ContentReviewOut",
    "CreativeDraftCreate",
    "CreativeDraftListOut",
    "CreativeDraftOut",
    "CreativeGeneratePayload",
    "CreativeGenerationCapabilityOut",
    "CreativeGenerationQueuedOut",
    "GeneratedCreative",
]
