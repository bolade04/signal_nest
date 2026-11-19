"""Status analytics over existing rows (Project Phase 5E, lane 5E-W2).

Read-only aggregates for one workspace, or one of its locations, over six existing entity
families — opportunities, recommendation briefs, approvals, creative drafts, content reviews
and exports (Phase 5E plan §2). No table, no write, no cache, no model call.

**Scope (every query).** The organization and workspace come from the authenticated tenant
context, never from the client. Every family is filtered on ``organization_id`` AND
``workspace_id`` (both columns exist on all six; on the FK-less compliance tables, approvals
and exports, they are the only scope). A ``location_id`` must name a location of this
workspace, active or not, or the read answers the uniform 404 that never says whether the id
exists elsewhere (:func:`resolve_location`).

**Soft deletion (interface sheet P2).** Briefs, drafts and reviews carry ``deleted_at``; a row
with it set is excluded from every figure, and a soft-deleted brief or draft places nothing at
a location. Opportunities, approvals and exports have no such marker.

**Location attribution.**

=================  ==========================================================================
Family             Attributed to location L when
=================  ==========================================================================
opportunities      ``location_id = L``
briefs, drafts     ``location_id = L`` (and the row is live)
reviews            its draft is live and at L (a review has no location of its own)
approvals          its live subject (``brief_id`` for a brief decision, ``draft_id`` for a
                   draft decision) is at L
exports            its live draft (``draft_id``) is at L
=================  ==========================================================================

In the workspace view, approvals and exports count every row of the workspace (they are
compliance records that outlive their subject). A row attributed to none of the workspace's
locations is ``unassigned``. For each compared family the location rows and ``unassigned``
are read by one grouped statement (:func:`location_breakdown`), so they add up to the family's
workspace total as that statement read it, and ``unassigned`` is a count of rows, never a
difference of two readings.

**Null, never a fabricated zero.** Counts are measurements and an empty scope measures 0.
Rates without a denominator, latencies without a decided version, and the ``unassigned`` row
of a location view are ``None`` (:mod:`app.analytics.schemas`).

Consistency: the figures are read in one request transaction, by several statements; on
PostgreSQL (READ COMMITTED) a write landing between two statements can make two figures
disagree by that write: a family card and the comparison's sum for that family, for example.
No figure is derived by subtracting one statement's reading from another's, so such a write
never produces a value outside a field's bound (review R-5E-A r1, A-F2). Nothing is cached, so
the next read is current.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.analytics.schemas import (
    AnalyticsOut,
    ApprovalLatencyOut,
    ApprovalStateCounts,
    ExportStateCounts,
    LatencyOut,
    LocationComparisonOut,
    LocationTotalsOut,
    OpportunityStateCounts,
    OutcomesOut,
    ReviewStateCounts,
    SubjectStateCounts,
)
from app.approvals.models import Approval
from app.briefs.models import RecommendationBrief
from app.core.enums import ApprovalDecision, ApprovalSubjectType, ExportFormat, OpportunityStatus
from app.core.errors import NotFoundError
from app.creative.models import ContentReview, CreativeDraft
from app.db.base import utcnow
from app.exports.models import Export
from app.locations.models import BusinessLocation
from app.opportunities.models import Opportunity

#: The uniform answer for an unknown location or another workspace's (the locations router's).
LOCATION_NOT_FOUND = "Location not found in this workspace."

BRIEF_SUBJECT = ApprovalSubjectType.RECOMMENDATION_BRIEF.value
DRAFT_SUBJECT = ApprovalSubjectType.CREATIVE_DRAFT.value
_APPROVED = ApprovalDecision.APPROVED.value
_REJECTED = ApprovalDecision.REJECTED.value
_REVOKED = ApprovalDecision.REVOKED.value


@dataclass(frozen=True)
class AnalyticsScope:
    """The server-resolved scope of one analytics read."""

    organization_id: str
    workspace_id: str
    #: A location of this workspace (checked), or ``None`` for the whole workspace.
    location_id: str | None = None

    def whole_workspace(self) -> AnalyticsScope:
        return AnalyticsScope(self.organization_id, self.workspace_id, None)


def resolve_location(
    db: Session, *, organization_id: str, workspace_id: str, location_id: str
) -> BusinessLocation:
    """The location, only when it belongs to this workspace; otherwise the uniform 404.

    An unknown id and another workspace's (in this organization or any other) get the same
    answer, so the read never discloses that an id exists elsewhere. A deactivated location
    of the workspace is admitted: its history is still the workspace's.
    """
    location = db.get(BusinessLocation, location_id)
    if (
        location is None
        or location.workspace_id != workspace_id
        or location.organization_id != organization_id
    ):
        raise NotFoundError(LOCATION_NOT_FOUND)
    return location


# --------------------------------------------------------------------------- #
# Scope predicates (one per family; every figure is built from these)
# --------------------------------------------------------------------------- #
def _tenant(model, scope: AnalyticsScope) -> list[ColumnElement[bool]]:
    return [
        model.organization_id == scope.organization_id,
        model.workspace_id == scope.workspace_id,
    ]


def opportunity_scope(scope: AnalyticsScope) -> ColumnElement[bool]:
    preds = _tenant(Opportunity, scope)
    if scope.location_id is not None:
        preds.append(Opportunity.location_id == scope.location_id)
    return and_(*preds)


def brief_scope(scope: AnalyticsScope, brief=RecommendationBrief) -> ColumnElement[bool]:
    preds = [*_tenant(brief, scope), brief.deleted_at.is_(None)]
    if scope.location_id is not None:
        preds.append(brief.location_id == scope.location_id)
    return and_(*preds)


def draft_scope(scope: AnalyticsScope, draft=CreativeDraft) -> ColumnElement[bool]:
    preds = [*_tenant(draft, scope), draft.deleted_at.is_(None)]
    if scope.location_id is not None:
        preds.append(draft.location_id == scope.location_id)
    return and_(*preds)


def review_scope(scope: AnalyticsScope) -> ColumnElement[bool]:
    """A live review whose draft is live and in scope (a review has no location)."""
    draft = aliased(CreativeDraft)
    return and_(
        *_tenant(ContentReview, scope),
        ContentReview.deleted_at.is_(None),
        exists()
        .where(draft.id == ContentReview.draft_id, draft_scope(scope, draft))
        .correlate(ContentReview),
    )


def approval_scope(scope: AnalyticsScope) -> ColumnElement[bool]:
    """The workspace's decision rows; in a location view, those whose live subject is there."""
    preds = _tenant(Approval, scope)
    if scope.location_id is not None:
        brief = aliased(RecommendationBrief)
        draft = aliased(CreativeDraft)
        preds.append(
            or_(
                and_(
                    Approval.subject_type == BRIEF_SUBJECT,
                    exists()
                    .where(brief.id == Approval.brief_id, brief_scope(scope, brief))
                    .correlate(Approval),
                ),
                and_(
                    Approval.subject_type == DRAFT_SUBJECT,
                    exists()
                    .where(draft.id == Approval.draft_id, draft_scope(scope, draft))
                    .correlate(Approval),
                ),
            )
        )
    return and_(*preds)


def export_scope(scope: AnalyticsScope) -> ColumnElement[bool]:
    """The workspace's export rows; in a location view, those whose live draft is there."""
    preds = _tenant(Export, scope)
    if scope.location_id is not None:
        draft = aliased(CreativeDraft)
        preds.append(
            exists().where(draft.id == Export.draft_id, draft_scope(scope, draft)).correlate(Export)
        )
    return and_(*preds)


# --------------------------------------------------------------------------- #
# Counts by state
# --------------------------------------------------------------------------- #
def opportunity_counts(db: Session, scope: AnalyticsScope) -> OpportunityStateCounts:
    rows = db.execute(
        select(Opportunity.status, func.count())
        .where(opportunity_scope(scope))
        .group_by(Opportunity.status)
    ).all()
    by_status = {status: int(n) for status, n in rows}
    return OpportunityStateCounts(
        total=sum(by_status.values()),
        new=by_status.get(OpportunityStatus.NEW.value, 0),
        saved=by_status.get(OpportunityStatus.SAVED.value, 0),
        monitoring=by_status.get(OpportunityStatus.MONITORING.value, 0),
        ignored=by_status.get(OpportunityStatus.IGNORED.value, 0),
        actioned=by_status.get(OpportunityStatus.ACTIONED.value, 0),
    )


def _fold_states(rows: Iterable[tuple[object, object, int]]) -> SubjectStateCounts:
    """``(superseded, controlling decision, n)`` groups → the partition (schema order)."""
    awaiting = approved = rejected = superseded = 0
    for is_superseded, latest, n in rows:
        n = int(n)
        if is_superseded:
            superseded += n
        elif latest == _APPROVED:
            approved += n
        elif latest == _REJECTED:
            rejected += n
        else:  # no decision, or the approval was revoked: back in the queue
            awaiting += n
    return SubjectStateCounts(
        total=awaiting + approved + rejected + superseded,
        awaiting_decision=awaiting,
        approved=approved,
        rejected=rejected,
        superseded=superseded,
    )


def _controlling_decision(subject, subject_type: str, version_column):
    """The highest-``decision_seq`` decision on the outer row's exact version (or NULL)."""
    return (
        select(Approval.decision)
        .where(
            Approval.organization_id == subject.organization_id,
            Approval.workspace_id == subject.workspace_id,
            Approval.subject_type == subject_type,
            Approval.subject_id == subject.id,
            Approval.subject_version == version_column,
        )
        .order_by(Approval.decision_seq.desc())
        .limit(1)
        .correlate(subject)
        .scalar_subquery()
    )


def brief_counts(db: Session, scope: AnalyticsScope) -> SubjectStateCounts:
    brief = RecommendationBrief
    newer = aliased(RecommendationBrief)
    superseded = (
        exists()
        .where(
            newer.workspace_id == brief.workspace_id,
            newer.opportunity_id == brief.opportunity_id,
            newer.brief_version > brief.brief_version,
            newer.deleted_at.is_(None),
        )
        .correlate(brief)
    )
    latest = _controlling_decision(brief, BRIEF_SUBJECT, brief.brief_version)
    inner = (
        select(superseded.label("superseded"), latest.label("latest"))
        .select_from(brief)
        .where(brief_scope(scope))
        .subquery()
    )
    rows = db.execute(
        select(inner.c.superseded, inner.c.latest, func.count()).group_by(
            inner.c.superseded, inner.c.latest
        )
    ).all()
    return _fold_states(rows)


def draft_counts(db: Session, scope: AnalyticsScope) -> SubjectStateCounts:
    draft = CreativeDraft
    newer = aliased(CreativeDraft)
    superseded = (
        exists()
        .where(
            newer.workspace_id == draft.workspace_id,
            newer.brief_id == draft.brief_id,
            newer.draft_version > draft.draft_version,
            newer.deleted_at.is_(None),
        )
        .correlate(draft)
    )
    latest = _controlling_decision(draft, DRAFT_SUBJECT, draft.draft_version)
    inner = (
        select(superseded.label("superseded"), latest.label("latest"))
        .select_from(draft)
        .where(draft_scope(scope))
        .subquery()
    )
    rows = db.execute(
        select(inner.c.superseded, inner.c.latest, func.count()).group_by(
            inner.c.superseded, inner.c.latest
        )
    ).all()
    return _fold_states(rows)


def approval_counts(db: Session, scope: AnalyticsScope) -> ApprovalStateCounts:
    rows = db.execute(
        select(Approval.decision, Approval.subject_type, func.count())
        .where(approval_scope(scope))
        .group_by(Approval.decision, Approval.subject_type)
    ).all()
    by_decision: dict[str, int] = {}
    by_subject: dict[str, int] = {}
    total = 0
    for decision, subject_type, n in rows:
        n = int(n)
        total += n
        by_decision[decision] = by_decision.get(decision, 0) + n
        by_subject[subject_type] = by_subject.get(subject_type, 0) + n
    return ApprovalStateCounts(
        total=total,
        approved=by_decision.get(_APPROVED, 0),
        rejected=by_decision.get(_REJECTED, 0),
        revoked=by_decision.get(_REVOKED, 0),
        on_briefs=by_subject.get(BRIEF_SUBJECT, 0),
        on_drafts=by_subject.get(DRAFT_SUBJECT, 0),
    )


def review_counts(db: Session, scope: AnalyticsScope) -> ReviewStateCounts:
    rows = db.execute(
        select(ContentReview.is_blocked, ContentReview.passed, func.count())
        .where(review_scope(scope))
        .group_by(ContentReview.is_blocked, ContentReview.passed)
    ).all()
    passed = blocked = not_passed = 0
    for is_blocked, has_passed, n in rows:
        n = int(n)
        if is_blocked:
            blocked += n
        elif has_passed:
            passed += n
        else:
            not_passed += n
    return ReviewStateCounts(
        total=passed + blocked + not_passed,
        passed=passed,
        blocked=blocked,
        not_passed=not_passed,
    )


def export_counts(db: Session, scope: AnalyticsScope) -> ExportStateCounts:
    rows = db.execute(
        select(Export.format, func.count()).where(export_scope(scope)).group_by(Export.format)
    ).all()
    by_format = {fmt: int(n) for fmt, n in rows}
    return ExportStateCounts(
        total=sum(by_format.values()),
        plain_text=by_format.get(ExportFormat.TEXT_PLAIN.value, 0),
        markdown=by_format.get(ExportFormat.TEXT_MARKDOWN.value, 0),
        json_document=by_format.get(ExportFormat.JSON.value, 0),
    )


# --------------------------------------------------------------------------- #
# Approval latency
# --------------------------------------------------------------------------- #
def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; every stored timestamp is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def latency_stats(seconds: list[float]) -> LatencyOut:
    """Mean and median of the measured latencies; ``None`` for both when none was measured.

    Not clamped: a negative value (a decision stamped before its subject, which only a clock
    fault or a hand-written row produces) is reported as measured, not hidden.
    """
    if not seconds:
        return LatencyOut(decided=0, mean_seconds=None, median_seconds=None)
    return LatencyOut(
        decided=len(seconds),
        mean_seconds=round(statistics.fmean(seconds), 3),
        median_seconds=round(float(statistics.median(seconds)), 3),
    )


def _latency(
    db: Session, scope: AnalyticsScope, *, subject, subject_type: str, version_column, live
) -> LatencyOut:
    """Creation → first decision, for each decided subject version in scope."""
    first = (
        select(
            Approval.subject_id.label("subject_id"),
            Approval.subject_version.label("subject_version"),
            func.min(Approval.created_at).label("decided_at"),
        )
        .where(*_tenant(Approval, scope), Approval.subject_type == subject_type)
        .group_by(Approval.subject_id, Approval.subject_version)
        .subquery()
    )
    rows = db.execute(
        select(subject.created_at, first.c.decided_at)
        .join(
            first,
            and_(first.c.subject_id == subject.id, first.c.subject_version == version_column),
        )
        .where(live)
    ).all()
    return latency_stats(
        [(_aware(decided) - _aware(created)).total_seconds() for created, decided in rows]
    )


def approval_latency(db: Session, scope: AnalyticsScope) -> ApprovalLatencyOut:
    return ApprovalLatencyOut(
        briefs=_latency(
            db,
            scope,
            subject=RecommendationBrief,
            subject_type=BRIEF_SUBJECT,
            version_column=RecommendationBrief.brief_version,
            live=brief_scope(scope),
        ),
        drafts=_latency(
            db,
            scope,
            subject=CreativeDraft,
            subject_type=DRAFT_SUBJECT,
            version_column=CreativeDraft.draft_version,
            live=draft_scope(scope),
        ),
    )


# --------------------------------------------------------------------------- #
# Generation and review outcomes
# --------------------------------------------------------------------------- #
def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def outcomes(db: Session, scope: AnalyticsScope, reviews: ReviewStateCounts) -> OutcomesOut:
    generated, revisions, simulated = db.execute(
        select(
            func.count(),
            func.count(case((CreativeDraft.parent_draft_id.is_not(None), 1))),
            func.count(case((CreativeDraft.is_simulated.is_(True), 1))),
        ).where(draft_scope(scope))
    ).one()
    return OutcomesOut(
        drafts_generated=int(generated),
        revisions=int(revisions),
        simulated=int(simulated),
        review_pass_rate=_rate(reviews.passed, reviews.total),
        review_block_rate=_rate(reviews.blocked, reviews.total),
    )


# --------------------------------------------------------------------------- #
# Per-location comparison
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LocationBreakdown:
    """One consistent reading of the comparison: the listed locations' rows and the rest."""

    #: Per-family totals of each listed location (0 where nothing is attributed to it).
    rows: dict[str, LocationTotalsOut]
    #: Per family, every workspace row attributed to none of the listed locations.
    unassigned: LocationTotalsOut


def _attribution(db: Session, stmt) -> dict[str | None, int]:
    """``(attributed location id or NULL, count)`` groups of one family, from one statement."""
    return {location_id: int(n) for location_id, n in db.execute(stmt).all()}


def _split(groups: dict[str | None, int], location_ids: list[str]) -> tuple[dict[str, int], int]:
    """A family's groups as (count per listed location, count attributed to none of them).

    Both come from the same statement, so the second is a count of rows and never a
    difference of two readings: it cannot be negative, whatever is written meanwhile."""
    listed = {location_id: groups.get(location_id, 0) for location_id in location_ids}
    rest = sum(n for location_id, n in groups.items() if location_id not in listed)
    return listed, rest


def location_breakdown(
    db: Session, scope: AnalyticsScope, location_ids: list[str]
) -> LocationBreakdown:
    """The workspace's rows of each compared family, grouped by attributed location.

    One grouped statement per family over the family's whole-workspace scope (the rules of
    the module docstring): a row is attributed to its own location (opportunities, briefs,
    drafts) or to its live subject's (approvals, exports), and to none (NULL) otherwise. The
    rows of a listed location and ``unassigned`` (every other group: no location, a subject
    that is gone or soft-deleted, a location that is not listed) are derived from that one
    result, so within a family they are consistent with each other by construction (review
    R-5E-A r1, A-F2: as two statements, a located row committed between them made
    ``unassigned`` negative and the read fail).
    """
    workspace = scope.whole_workspace()
    brief = aliased(RecommendationBrief)
    draft = aliased(CreativeDraft)
    export_draft = aliased(CreativeDraft)
    opportunities, opportunities_rest = _split(
        _attribution(
            db,
            select(Opportunity.location_id, func.count())
            .where(opportunity_scope(workspace))
            .group_by(Opportunity.location_id),
        ),
        location_ids,
    )
    briefs, briefs_rest = _split(
        _attribution(
            db,
            select(RecommendationBrief.location_id, func.count())
            .where(brief_scope(workspace))
            .group_by(RecommendationBrief.location_id),
        ),
        location_ids,
    )
    drafts, drafts_rest = _split(
        _attribution(
            db,
            select(CreativeDraft.location_id, func.count())
            .where(draft_scope(workspace))
            .group_by(CreativeDraft.location_id),
        ),
        location_ids,
    )
    # A decision is attributed to its live subject's location: the brief's for a brief
    # decision, the draft's for a draft decision. The subject must be this workspace's own
    # (``brief_scope`` / ``draft_scope``) AND the decision row must be (``_tenant``): the
    # scope columns of ``approvals`` are FK-less, so a row of another scope can name this
    # workspace's subject.
    decided = (
        select(
            case(
                (Approval.subject_type == BRIEF_SUBJECT, brief.location_id),
                (Approval.subject_type == DRAFT_SUBJECT, draft.location_id),
                else_=None,
            ).label("location_id")
        )
        .select_from(Approval)
        .outerjoin(brief, and_(brief.id == Approval.brief_id, brief_scope(workspace, brief)))
        .outerjoin(draft, and_(draft.id == Approval.draft_id, draft_scope(workspace, draft)))
        .where(*_tenant(Approval, workspace))
        .subquery()
    )
    approvals, approvals_rest = _split(
        _attribution(
            db, select(decided.c.location_id, func.count()).group_by(decided.c.location_id)
        ),
        location_ids,
    )
    # An export is attributed to its live draft's location; its own scope columns are
    # FK-less too, so the same two predicates apply.
    exports, exports_rest = _split(
        _attribution(
            db,
            select(export_draft.location_id, func.count())
            .select_from(Export)
            .outerjoin(
                export_draft,
                and_(export_draft.id == Export.draft_id, draft_scope(workspace, export_draft)),
            )
            .where(*_tenant(Export, workspace))
            .group_by(export_draft.location_id),
        ),
        location_ids,
    )
    return LocationBreakdown(
        rows={
            location_id: LocationTotalsOut(
                opportunities=opportunities[location_id],
                briefs=briefs[location_id],
                drafts=drafts[location_id],
                approvals=approvals[location_id],
                exports=exports[location_id],
            )
            for location_id in location_ids
        },
        unassigned=LocationTotalsOut(
            opportunities=opportunities_rest,
            briefs=briefs_rest,
            drafts=drafts_rest,
            approvals=approvals_rest,
            exports=exports_rest,
        ),
    )


def location_totals(
    db: Session, scope: AnalyticsScope, location_ids: list[str]
) -> dict[str, LocationTotalsOut]:
    """Per-family totals for each listed location of the workspace (same rules as above)."""
    return location_breakdown(db, scope, location_ids).rows


# --------------------------------------------------------------------------- #
# The read
# --------------------------------------------------------------------------- #
def workspace_analytics(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    location_id: str | None = None,
) -> AnalyticsOut:
    """Status analytics for the workspace, or for one of its locations (404 otherwise).

    ``organization_id`` and ``workspace_id`` must be the authenticated tenant context's;
    ``location_id`` is the caller's and is checked against that workspace before any figure
    is read.
    """
    locations_select = select(
        BusinessLocation.id, BusinessLocation.name, BusinessLocation.is_active
    ).where(
        BusinessLocation.organization_id == organization_id,
        BusinessLocation.workspace_id == workspace_id,
    )
    if location_id is not None:
        resolve_location(
            db, organization_id=organization_id, workspace_id=workspace_id, location_id=location_id
        )
        locations_select = locations_select.where(BusinessLocation.id == location_id)
    scope = AnalyticsScope(organization_id, workspace_id, location_id)

    opportunities = opportunity_counts(db, scope)
    briefs = brief_counts(db, scope)
    approvals = approval_counts(db, scope)
    drafts = draft_counts(db, scope)
    reviews = review_counts(db, scope)
    exports = export_counts(db, scope)

    listed = db.execute(locations_select.order_by(BusinessLocation.name, BusinessLocation.id)).all()
    breakdown = location_breakdown(db, scope, [row.id for row in listed])
    comparison = [
        LocationComparisonOut(
            location_id=row.id,
            name=row.name,
            is_active=bool(row.is_active),
            **breakdown.rows[row.id].model_dump(),
        )
        for row in listed
    ]
    # A count read with the location rows, never the family cards minus them (A-F2).
    unassigned = None if location_id is not None else breakdown.unassigned

    return AnalyticsOut(
        workspace_id=workspace_id,
        location_id=location_id,
        generated_at=utcnow(),
        opportunities=opportunities,
        briefs=briefs,
        approvals=approvals,
        drafts=drafts,
        reviews=reviews,
        exports=exports,
        approval_latency=approval_latency(db, scope),
        outcomes=outcomes(db, scope, reviews),
        locations=comparison,
        unassigned=unassigned,
    )


__all__ = [
    "LOCATION_NOT_FOUND",
    "AnalyticsScope",
    "LocationBreakdown",
    "approval_counts",
    "approval_latency",
    "approval_scope",
    "brief_counts",
    "brief_scope",
    "draft_counts",
    "draft_scope",
    "export_counts",
    "export_scope",
    "latency_stats",
    "location_breakdown",
    "location_totals",
    "opportunity_counts",
    "opportunity_scope",
    "outcomes",
    "resolve_location",
    "review_counts",
    "review_scope",
    "workspace_analytics",
]
