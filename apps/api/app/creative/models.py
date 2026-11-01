"""Persistence models for text creative generation (Project Phase 5C; frozen at W21 CF-2).

**:class:`CreativeDraft`** — one immutable, versioned marketing-text draft generated from a
recommendation brief (Phase 5C plan §2, "the brief pattern"):

* **Append-only.** Every ORM path that would update or delete a row raises (unit of work,
  ORM UPDATE/DELETE statements and ``Query.update/delete``, the bulk API, upserts; a mapped
  subclass included — :func:`app.db.append_only.append_only`). Not refused: Core statements
  and Core upserts on the table, ORM statements on a bare ``Connection``, raw SQL and the
  database's own FK actions; only :mod:`app.privacy` and the development-only demo reset
  write that way. ``created_at`` only. A revision is a new row: ``draft_version`` is
  monotonic per (workspace, brief) (``uq_creative_draft_version``) and ``parent_draft_id``
  points at the draft it revises.
* **Text only.** ``channel`` is a closed vocabulary of text channels
  (:class:`~app.core.enums.CreativeChannel`); the content is ``headline``, ``body`` and
  ``call_to_action``, bounded at the API boundary (:mod:`app.creative.schemas`).
* **Full LLM provenance on the row** (Phase 5C plan §2; exit criterion 8).
  ``prompt_name``, ``prompt_version``, ``model_identifier``, ``input_hash`` (computed in the
  service layer), ``trace_id``, ``is_simulated`` and an immutable **usage snapshot** of the
  call — ``input_tokens``, ``output_tokens``, ``estimated_cost_usd`` (0 for the mock) —
  copied when the draft is written. The accounting authority stays the Phase 6 usage
  ledger: every call is a ``llm_usage_events`` row, reserved and settled by the #269 budget
  ledger, and quotas and ceilings read the ledger, never these columns.
  ``llm_usage_event_id`` references the ledger row of the call (coordinator decision I-4: no
  ``llm_usage_records`` table); the snapshot keeps the draft's provenance complete even
  after that link is nulled.
* **Authorship for author≠approver.** ``created_by`` (``SET NULL``) plus the non-FK
  ``created_by_snapshot`` (:mod:`app.core.actor_snapshot`); on account erasure the snapshot
  is anonymised and the row kept (O-6, as for briefs; :mod:`app.privacy.erasure`).
* **Tenancy.** ``organization_id`` / ``workspace_id`` / ``brief_id`` / ``opportunity_id``
  cascade, so a draft goes with its brief, opportunity and workspace; ``location_id`` and
  ``campaign_id`` are ``SET NULL``.

**:class:`ContentReview`** — the output-side review of exactly one draft version: the
persisted ``ClaimSafetyResult`` (``risk_level``, ``findings``, ``warnings``), the workspace
blocked-claims hits, the required-disclaimer resolution and ``passed``. **Explicit tenant
columns** (``organization_id``, ``workspace_id``) so a query never has to join through
``creative_drafts`` to scope it. A draft whose review ``is_blocked`` cannot be approved — the
5B approval service enforces it for ``subject_type == creative_draft``. Append-only like the
draft. Retention: the review cascades with its draft (it is never kept beyond the draft it
judges), so its survival is bounded by the draft's — not indefinite (the O-6 rider).

Portable types only; CHECKs rendered from the Python vocabularies.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.actor_snapshot import ACTOR_SNAPSHOT_MAX_LENGTH
from app.core.enums import ClaimRisk, CreativeChannel
from app.db.append_only import append_only
from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.llm.models import USD


def _in_list(column: str, values) -> str:
    rendered = ", ".join(f"'{v}'" for v in sorted(str(v) for v in values))
    return f"{column} IN ({rendered})"


def channel_sql() -> str:
    return _in_list("channel", (c.value for c in CreativeChannel))


def risk_level_sql() -> str:
    return _in_list("risk_level", (r.value for r in ClaimRisk))


#: The usage snapshot is never negative.
DRAFT_USAGE_SQL = "input_tokens >= 0 AND output_tokens >= 0 AND estimated_cost_usd >= 0"
#: ``is_blocked`` is exactly "the risk level is blocked" (``ClaimSafetyResult.is_blocked``).
REVIEW_BLOCKED_SQL = (
    "(is_blocked AND risk_level = 'blocked') OR (NOT is_blocked AND risk_level <> 'blocked')"
)
#: A review passes only when nothing blocks and every required disclaimer is resolved.
REVIEW_PASSED_SQL = "NOT passed OR (NOT is_blocked AND disclaimers_resolved)"


@append_only
class CreativeDraft(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One immutable version of a generated marketing-text draft."""

    __tablename__ = "creative_drafts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "brief_id", "draft_version", name="uq_creative_draft_version"
        ),
        CheckConstraint("draft_version >= 1", name="ck_creative_draft_version"),
        CheckConstraint(channel_sql(), name="ck_creative_draft_channel"),
        CheckConstraint(DRAFT_USAGE_SQL, name="ck_creative_draft_usage"),
    )

    # --- Scope -------------------------------------------------------------------
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The recommendation brief (exact version) this draft was generated from.
    brief_id: Mapped[str] = mapped_column(
        ForeignKey("recommendation_briefs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_locations.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )

    # --- Versioning ----------------------------------------------------------------
    #: Monotonic per (workspace, brief), starting at 1.
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The draft this version revises; NULL for a first draft.
    parent_draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("creative_drafts.id", ondelete="SET NULL"), index=True
    )

    # --- Content (text only) ---------------------------------------------------------
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    headline: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    call_to_action: Mapped[str | None] = mapped_column(String(200))

    # --- LLM provenance --------------------------------------------------------------
    prompt_name: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    model_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    #: SHA-256 hex of the model inputs, computed in the service layer.
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Immutable usage snapshot of the call (provenance only; the ledger is the authority).
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    #: From the versioned price table; 0 for the mock.
    estimated_cost_usd: Mapped[Decimal] = mapped_column(USD, nullable=False)
    #: The Phase 6 usage-ledger row of the call (the accounting authority).
    llm_usage_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("llm_usage_events.id", ondelete="SET NULL"), index=True
    )

    # --- Authorship ------------------------------------------------------------------
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: Immutable "<user id> <full name> <<email>>" at generation; "[erased]" once erased.
    created_by_snapshot: Mapped[str] = mapped_column(
        String(ACTOR_SNAPSHOT_MAX_LENGTH), nullable=False
    )


@append_only
class ContentReview(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """The output-side content review of one draft version."""

    __tablename__ = "content_reviews"
    __table_args__ = (
        UniqueConstraint("draft_id", name="uq_content_review_draft"),
        CheckConstraint("draft_version >= 1", name="ck_content_review_draft_version"),
        CheckConstraint(risk_level_sql(), name="ck_content_review_risk_level"),
        CheckConstraint(REVIEW_BLOCKED_SQL, name="ck_content_review_blocked"),
        CheckConstraint(REVIEW_PASSED_SQL, name="ck_content_review_passed"),
    )

    # --- Explicit tenant scope (never only through creative_drafts) ------------------
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("creative_drafts.id", ondelete="CASCADE"), nullable=False
    )
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- The persisted ClaimSafetyResult -----------------------------------------------
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: ``[{"category", "risk", "excerpt"}]`` (``app.claims.engine.ClaimFinding``).
    findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    warnings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: Ids of the workspace's (active) claims-library ``blocked`` entries the draft hit.
    blocked_claim_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: Required disclaimers (offer-calendar ``required_disclaimer``) the draft had to carry.
    required_disclaimers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    disclaimers_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)


__all__ = [
    "DRAFT_USAGE_SQL",
    "REVIEW_BLOCKED_SQL",
    "REVIEW_PASSED_SQL",
    "ContentReview",
    "CreativeDraft",
    "channel_sql",
    "risk_level_sql",
]
