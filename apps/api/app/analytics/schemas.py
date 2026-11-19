"""Response schemas of the status-analytics read (Project Phase 5E, lane 5E-W2).

``GET /workspaces/{workspace_id}/analytics`` (optional ``location_id``) answers exactly
:class:`AnalyticsOut`. Every figure is derived at read time from rows that already exist
(opportunities, recommendation briefs, approvals, creative drafts, content reviews, exports);
analytics owns no table (Phase 5E plan §2).

Boundary decisions encoded here:

* **Null, never a fabricated zero (the ``RunStats`` discipline, 5E plan §2).** A count is an
  ``int``: it is a measurement over the rows in scope, and an empty scope measures ``0``. A
  figure that needs a basis the scope does not have is ``None``: a rate with no denominator,
  a latency with no decided subject, and the ``unassigned`` comparison row of a view already
  narrowed to one location (it has no meaning there). No figure is ever filled in.
* **No attribution or advertising-performance field** (definition-audit operator decision
  D-05: "include operational metrics and customer-facing status analytics; defer
  advertising-performance attribution"). Nothing here measures clicks, impressions,
  conversions, revenue, spend or any outcome outside the product; the LLM usage snapshot on a
  draft (tokens, estimated cost) is not exposed either.
* **Closed shapes.** Every family has its own model with named fields, so the generated web
  types are closed (no open index signature lets a wrong key compile). Unknown fields are
  never added to the response (``extra="forbid"`` on every model).
* **No identity.** No author, decider or exporter, and no snapshot, appears in any figure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: A count of rows in scope: never negative, never null.
Count = Annotated[int, Field(ge=0)]
#: A share in [0, 1]; ``None`` where it has no denominator.
Rate = Annotated[float, Field(ge=0, le=1)]
#: A duration in seconds; ``None`` where nothing was measured. No lower bound: a negative
#: latency (a decision stamped before its subject, which only a clock fault or a hand-written
#: row produces) is reported as measured (``service.latency_stats``), so one such row never
#: fails the read.
Seconds = float


class OpportunityStateCounts(_Closed):
    """Opportunities in scope by their workflow ``status`` (``OpportunityStatus``).

    ``total`` counts every row in scope; each named state counts the rows carrying it.
    """

    total: Count
    new: Count
    saved: Count
    monitoring: Count
    ignored: Count
    actioned: Count


class SubjectStateCounts(_Closed):
    """Brief or draft versions in scope by their derived state (no status column exists).

    A partition, evaluated in this order (the approval queue's reading,
    ``app.approvals.service._awaiting_decision_select``):

    * ``superseded`` — a newer version of the same chain exists (a brief: the same
      opportunity; a draft: the same brief); it is decided through its successor.
    * ``approved`` / ``rejected`` — the controlling decision (the highest ``decision_seq``
      on that exact version) is that decision.
    * ``awaiting_decision`` — no decision yet, or the controlling decision is ``revoked``.

    ``total`` = the sum of the four. Soft-deleted versions (``deleted_at`` set) are excluded.
    """

    total: Count
    awaiting_decision: Count
    approved: Count
    rejected: Count
    superseded: Count


class ApprovalStateCounts(_Closed):
    """Approval decision rows in scope (append-only; a change of mind is a new row).

    By decision (``approved`` + ``rejected`` + ``revoked`` = ``total``) and by subject
    (``on_briefs`` + ``on_drafts`` = ``total``).
    """

    total: Count
    approved: Count
    rejected: Count
    revoked: Count
    on_briefs: Count
    on_drafts: Count


class ReviewStateCounts(_Closed):
    """Content reviews in scope (one per draft version) by outcome; a partition.

    ``passed``: nothing blocks and every required disclaimer is carried. ``blocked``: the
    claim-safety result is blocked. ``not_passed``: neither (in practice a required
    disclaimer is missing).
    """

    total: Count
    passed: Count
    blocked: Count
    not_passed: Count


class ExportStateCounts(_Closed):
    """Manual exports in scope by render format (``ExportFormat``); a partition.

    ``plain_text`` = ``text/plain``, ``markdown`` = ``text/markdown``, ``json_document`` =
    ``json`` (a field named ``json`` would shadow ``BaseModel.json``).
    """

    total: Count
    plain_text: Count
    markdown: Count
    json_document: Count


class LatencyOut(_Closed):
    """Time from a subject version's creation to its first decision, over decided versions.

    ``decided`` counts the versions in scope with at least one decision. ``mean_seconds`` and
    ``median_seconds`` are ``None`` while ``decided`` is 0: there is nothing to measure.
    """

    decided: Count
    mean_seconds: Seconds | None
    median_seconds: Seconds | None


class ApprovalLatencyOut(_Closed):
    """Approval latency, for briefs and for drafts separately."""

    briefs: LatencyOut
    drafts: LatencyOut


class OutcomesOut(_Closed):
    """Generation and review outcomes.

    Generation: ``drafts_generated`` versions in scope, of which ``revisions`` revise an
    earlier version and ``simulated`` came from the simulated generator. Review: the share of
    content reviews that passed / blocked; each rate is ``None`` while no review is in scope.
    """

    drafts_generated: Count
    revisions: Count
    simulated: Count
    review_pass_rate: Rate | None
    review_block_rate: Rate | None


class LocationTotalsOut(_Closed):
    """Per-family totals for one comparison row (the same scope rules as the families)."""

    opportunities: Count
    briefs: Count
    drafts: Count
    approvals: Count
    exports: Count


class LocationComparisonOut(LocationTotalsOut):
    """One location of the workspace in the per-location comparison."""

    location_id: str
    name: str
    is_active: bool


class AnalyticsOut(_Closed):
    """Status analytics for one workspace, or one of its locations."""

    workspace_id: str
    #: The location the figures are narrowed to; ``None`` for the whole workspace.
    location_id: str | None
    #: When the figures were computed (UTC).
    generated_at: datetime

    opportunities: OpportunityStateCounts
    briefs: SubjectStateCounts
    approvals: ApprovalStateCounts
    drafts: SubjectStateCounts
    reviews: ReviewStateCounts
    exports: ExportStateCounts

    approval_latency: ApprovalLatencyOut
    outcomes: OutcomesOut

    #: Every location of the workspace (active or not), by name; only the requested one when
    #: the view is narrowed to a location.
    locations: list[LocationComparisonOut]
    #: Rows that name no location; ``None`` when the view is narrowed to a location.
    unassigned: LocationTotalsOut | None


__all__ = [
    "AnalyticsOut",
    "Count",
    "Rate",
    "Seconds",
    "ApprovalLatencyOut",
    "ApprovalStateCounts",
    "ExportStateCounts",
    "LatencyOut",
    "LocationComparisonOut",
    "LocationTotalsOut",
    "OpportunityStateCounts",
    "OutcomesOut",
    "ReviewStateCounts",
    "SubjectStateCounts",
]
