"""Request/response schemas of the recommendation-brief API (Project Phase 5A; frozen at
W21 CF-1).

The 5A routes (``GET``/``POST /workspaces/{workspace_id}/opportunities/{opportunity_id}/
briefs`` and ``GET …/briefs/{brief_id}``, Phase 5A plan §2) return and accept exactly
these shapes. CF-1 mounts no route, so these models are not yet in ``openapi.json``.

Boundary decisions encoded here (the feedback precedent, ``app.feedback.schemas``):

* **Strict request.** :class:`RecommendationBriefCreate` accepts only the caller-owned
  facts — which immutable intelligence record of the opportunity to brief on, and an
  optional campaign (validated against the workspace by the service). Tenant scope,
  ``location_id`` (taken from the intelligence record), version numbering,
  ``parent_brief_id`` (the opportunity's latest brief), the analysis/scoring versions,
  the LLM provenance and the author are all derived server-side. Unknown fields are
  rejected (``extra="forbid"``).
* **Safe response.** :class:`RecommendationBriefOut` exposes the content, the versioning,
  the public-safe provenance (prompt name and version, model identifier, simulation
  flag, analysis/scoring versions) and the author. Internal scope columns, ``input_hash``,
  ``trace_id`` and ``llm_usage_event_id`` are not exposed — the same boundary as the raw
  intelligence ``fingerprint``.
* **Bounded content.** Every text and list is bounded (:data:`BRIEF_BOUNDS`); the service
  validates generated content against :class:`BriefContent` before it is persisted.
* **No status field.** A brief's display status is derived at read time from the approval
  spine (Phase 5A plan §2); no status vocabulary is frozen at CF-1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

#: Content bounds (characters / items), frozen at CF-1.
BRIEF_BOUNDS = {
    "summary": 2000,
    "rationale": 4000,
    "list_items": 10,
    "list_item_chars": 500,
    "evidence_refs": 50,
}

_Item = Annotated[str, Field(min_length=1, max_length=BRIEF_BOUNDS["list_item_chars"])]
_Id = Annotated[str, Field(min_length=1, max_length=32)]


class BriefContent(BaseModel):
    """The bounded content of one brief. Facts (``evidence_refs``) are kept apart from
    inference (everything else)."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=BRIEF_BOUNDS["summary"])
    rationale: str = Field(min_length=1, max_length=BRIEF_BOUNDS["rationale"])
    recommended_actions: list[_Item] = Field(
        default_factory=list, max_length=BRIEF_BOUNDS["list_items"]
    )
    suggested_angles: list[_Item] = Field(
        default_factory=list, max_length=BRIEF_BOUNDS["list_items"]
    )
    risk_notes: list[_Item] = Field(default_factory=list, max_length=BRIEF_BOUNDS["list_items"])
    #: Ids of the evidence the brief rests on (the bound intelligence record and the
    #: opportunity's validation evidence). Pointers only — never copied text.
    evidence_refs: list[_Id] = Field(
        default_factory=list, max_length=BRIEF_BOUNDS["evidence_refs"]
    )


class RecommendationBriefCreate(BaseModel):
    """Strict request body to generate and capture one brief version."""

    model_config = ConfigDict(extra="forbid")

    #: The immutable intelligence record (of this opportunity) to brief on.
    intelligence_record_id: _Id
    #: Optional campaign; must belong to the same workspace (service-validated).
    campaign_id: _Id | None = None


class RecommendationBriefOut(BriefContent):
    """Customer-safe projection of one stored brief version."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: str
    opportunity_id: str
    intelligence_record_id: str
    location_id: str | None
    campaign_id: str | None
    brief_version: int
    parent_brief_id: str | None
    analysis_version: str
    scoring_version: str
    prompt_name: str
    prompt_version: str
    model_identifier: str
    is_simulated: bool
    #: NULL once the author's account is erased.
    created_by: str | None
    #: "<user id> <full name> <<email>>" at capture, or "[erased]".
    created_by_snapshot: str
    created_at: datetime


class RecommendationBriefListOut(BaseModel):
    """Bounded, newest-version-first page of an opportunity's briefs."""

    items: list[RecommendationBriefOut]
    total: int
    limit: int
    offset: int


__all__ = [
    "BRIEF_BOUNDS",
    "BriefContent",
    "RecommendationBriefCreate",
    "RecommendationBriefListOut",
    "RecommendationBriefOut",
]
