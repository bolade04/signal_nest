"""Recommendation-brief service (Project Phase 5A) — the only write path for
:class:`~app.briefs.models.RecommendationBrief`.

:func:`create_brief` generates one brief for one opportunity's immutable intelligence record
and captures it as a new, immutable version. It owns the invariants that make a brief a
trustworthy compliance record:

* **Scope integrity / IDOR defense.** The intelligence record must be this opportunity's own,
  in the same organization and workspace (422 otherwise, the feedback precedent; the route
  has already answered 404 for a record outside the workspace). An optional ``campaign_id``
  must name a campaign of the same workspace (404 otherwise, uniform with a missing id).
* **Jurisdiction snapshot, or an explicit refusal** (Project Phase 5D §2, lane 5D-W3; the
  S1-5D capture rule as amended by operator ruling Q-4). Before any budget check or model
  call, the record's location is examined (:func:`_capture_jurisdiction`):

  - **No location** (``record.location_id IS NULL``, which includes a record whose location
    was deleted: the reference is ``SET NULL``): the brief is created with
    ``jurisdiction_code`` ``NULL`` (Q-4). Decided from the reference itself, never from the
    resolver's answer. Nothing is inferred or defaulted: no workspace, scout, market or
    other code stands in. S1-5D item 31 (the creative service) refuses drafts from it.
  - **An existing location reference**: it must load inside the opportunity's workspace and
    organization, and resolve with the shared
    :func:`app.jurisdiction.resolver.resolve_jurisdiction` and the coverage rules attached to
    that location. Otherwise — another workspace's or organization's location, or an
    ``UNRESOLVED`` answer — the request is refused (422 ``jurisdiction_unresolved``,
    :class:`BriefJurisdictionUnresolvedError`): no model call, no usage or budget, no brief.
    A resolved code is stored once, at insert, as ``jurisdiction_code``.

  So ``NULL`` is written for exactly one case, a record with no location; every other
  ``NULL`` brief was written before 5D-W3. Existing briefs are never back-filled. The answer
  and the log carry a fixed message and a reason category, never the resolver's evidence or
  any text of the location.
* **Metered model call.** The brief's one model task goes through
  :meth:`app.llm.service.LLMService.run` with the tenant named and an
  :class:`~app.llm.metering.LLMMeter` bound to it: the run cap, the workspace's monthly
  budget (reserved before the call on PostgreSQL), the usage ledger and the opaque tenant
  reference all apply exactly as for a scout run. A spent budget is refused before any call
  (429 ``llm_budget_exhausted``). The task is the existing ``explain_opportunity`` prompt; no
  new prompt task is added in 5A.
* **Screened output.** Model text is screened (:func:`app.llm.screening.screen_explanation`)
  before it can be stored or shown; withheld output stores nothing (422 ``brief_withheld``).
  Content is validated against the frozen :class:`~app.briefs.schemas.BriefContent` bounds.
* **Provenance from the server, never the caller.** ``analysis_version`` /
  ``scoring_version`` / ``location_id`` are copied from the intelligence record, and
  ``jurisdiction_code`` is resolved from that location, or ``NULL`` without one (above); the
  prompt name and version, model, ``input_hash`` (computed by the LLM service), trace id and
  simulation flag come from the response; ``llm_usage_event_id`` is the usage-ledger row of
  the attempt that produced the brief; ``created_by_snapshot`` is the author as they are now.
* **Versioning.** ``brief_version`` is the opportunity's highest version plus one and
  ``parent_brief_id`` the brief it supersedes, read after the opportunity row is locked
  (``SELECT ... FOR UPDATE`` on PostgreSQL), so concurrent captures serialize; a unique-key
  clash that still occurs is a 409 and stores no brief.
* **Append-only.** Inserts only. No function here, or anywhere, updates or deletes a brief
  (``app.db.append_only``); a revision is a new row.
* **Audited in the same transaction.** ``recommendation_brief.created`` is written with the
  brief, carrying identifiers and versions only (no content, no author identity).

Mapping of the task's output onto the brief (facts kept apart from inference):
``why_it_matters`` -> ``summary``; ``ai_inference`` -> ``rationale``; ``recommended_action``
-> ``recommended_actions[0]``; ``suggested_angles`` -> ``suggested_angles``; ``risk_note``
and the screen's claim warnings -> ``risk_notes``; ``evidence_refs`` = the intelligence
record id, then the opportunity's validation-evidence ids (pointers, never copied text).
``who_cares`` is not carried: the brief has no field for it (the opportunity already shows
its audience). List items longer than the frozen item bound (500 characters; the screen
admits up to 600 per text field) are capped with an ellipsis, never withheld.

Failed model calls are still accounted for: when no brief can be produced the meter's usage
rows are in the caller's session (``meter.flush``) and the route commits them before it
answers, so spend is never lost with the error (:class:`BriefNotProducedError`).

Transaction ownership follows the house rule: this service flushes; the caller commits.
"""

from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.audit.actions import AuditAction
from app.audit.service import record_audit
from app.briefs.models import RecommendationBrief
from app.briefs.schemas import BRIEF_BOUNDS, BriefContent
from app.campaign_context.models import Campaign
from app.claims.engine import translate_competitor_weakness
from app.core.actor_snapshot import actor_snapshot
from app.core.config import Settings
from app.core.errors import NotFoundError, SignalNestError, ValidationDomainError
from app.core.logging import get_logger, log_event
from app.intelligence.records import SignalIntelligenceRecord
from app.jurisdiction.resolver import resolve_jurisdiction
from app.llm.base import LLMError
from app.llm.metering import (
    LLMBudgetExceededError,
    LLMBudgetExhaustedError,
    LLMMeter,
    ensure_run_allowed,
)
from app.llm.models import LLMUsageEvent
from app.llm.screening import screen_explanation
from app.llm.service import LLMService
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.opportunities.context import build_business_context
from app.opportunities.models import Opportunity, ValidationEvidence
from app.organizations.models import User
from app.signals.models import NormalizedSignal

logger = get_logger("signalnest.briefs")

#: The model task a brief is generated with (an existing prompt; 5A adds none).
BRIEF_TASK = "explain_opportunity"


class BriefNotProducedError(SignalNestError):
    """No brief was stored because the model step did not produce one.

    The caller commits the session before answering (the meter's usage rows are in it), so
    a failed or withheld attempt still counts against the workspace's budget.
    """


class BriefBudgetExhaustedError(BriefNotProducedError):
    """The workspace's monthly AI budget is spent: no model call was made (429, the scout
    run endpoint's code)."""

    status_code = 429
    code = "llm_budget_exhausted"


class BriefGenerationUnavailableError(BriefNotProducedError):
    """The model call failed or was not allowed (provider failure, run cap, unpriced model)."""

    status_code = 503
    code = "brief_generation_unavailable"


class BriefWithheldError(BriefNotProducedError):
    """The generated text failed the safety screen or the content bounds: nothing stored."""

    status_code = 422
    code = "brief_withheld"


class BriefVersionConflictError(BriefNotProducedError):
    """A concurrent capture took this version number: nothing stored (409)."""

    status_code = 409
    code = "conflict"


class BriefJurisdictionUnresolvedError(ValidationDomainError):
    """The record's existing location did not resolve to a jurisdiction, or is not a location
    of the opportunity's workspace and organization (the S1-5D capture rule; a record with no
    location is not refused, Q-4): no model call was made and nothing was stored (422).

    Not a :class:`BriefNotProducedError`: the refusal comes before any model call or budget
    check, so there is no usage to keep."""

    code = "jurisdiction_unresolved"


#: The one answer to an unresolved capture. Fixed: it never quotes the location or the
#: resolver's evidence.
JURISDICTION_UNRESOLVED_MESSAGE = (
    "The jurisdiction of this opportunity's location could not be determined, so no brief "
    "was generated. Complete the location's details, for example add its state or province, "
    "and try again."
)


#: The brief-created log's category for a brief captured without a location (Q-4).
NO_LOCATION = "no_location"


def _refuse_capture(opportunity: Opportunity, reason: str) -> BriefJurisdictionUnresolvedError:
    """Log the capture refusal (a reason category only) and return the error to raise."""
    log_event(
        logger,
        "recommendation_brief_not_generated",
        outcome="refused",
        workspace_id=opportunity.workspace_id,
        opportunity_id=opportunity.id,
        reason=reason,
    )
    return BriefJurisdictionUnresolvedError(JURISDICTION_UNRESOLVED_MESSAGE)


def _capture_jurisdiction(
    db: Session, opportunity: Opportunity, record: SignalIntelligenceRecord
) -> str | None:
    """The jurisdiction code a new brief from ``record`` captures (Phase 5D §2): a code,
    ``None`` for a record with no location (Q-4), or :class:`BriefJurisdictionUnresolvedError`.

    * **No location** is exactly ``record.location_id IS NULL`` (a deleted location's
      reference is ``SET NULL``, so it is included). It is decided from the reference itself,
      before and without the resolver, and answers ``None``: nothing is inferred or defaulted.
    * **An existing location reference** must load inside the opportunity's workspace and
      organization; one that does not (another workspace's or organization's location, or a
      row that is gone) is refused, never treated as no location, so a tenant-boundary fault
      can never become a ``NULL`` brief. A loaded location is resolved with its coverage
      rules, the ones attached to it in the workspace (``GeoCoverageRule.location_id``), the
      rule set the scout pipeline and the geo-coverage routes already choose for a location;
      workspace-level rules (``location_id`` NULL) are not the location's and are not read.
      ``UNRESOLVED`` is refused.

    Every refusal logs a reason category only, never location text or resolver evidence.
    """
    if record.location_id is None:
        return None
    location = db.get(BusinessLocation, record.location_id)
    if (
        location is None
        or location.workspace_id != opportunity.workspace_id
        or location.organization_id != opportunity.organization_id
    ):
        raise _refuse_capture(opportunity, "jurisdiction_location_outside_workspace")
    rules = list(
        db.scalars(
            select(GeoCoverageRule)
            .where(
                GeoCoverageRule.workspace_id == opportunity.workspace_id,
                GeoCoverageRule.location_id == location.id,
            )
            .order_by(GeoCoverageRule.created_at, GeoCoverageRule.id)
        ).all()
    )
    resolution = resolve_jurisdiction(location, rules)
    if not resolution.is_resolved:
        raise _refuse_capture(opportunity, "jurisdiction_location_unresolved")
    return resolution.code


def _brief_inputs(
    db: Session, opportunity: Opportunity, record: SignalIntelligenceRecord
) -> tuple[dict, str | None]:
    """The task inputs (every template field a key) and the workspace's industry."""
    ctx = build_business_context(db, opportunity.workspace_id)
    signal = db.get(NormalizedSignal, record.normalized_signal_id)
    pain_point = signal.pain_point_dna if signal is not None else None
    inputs = {
        "resolved_market": opportunity.resolved_market,
        "audience_fit": opportunity.audience_fit,
        "pain_point_dna": pain_point,
        "safe_positioning": translate_competitor_weakness(pain_point, ctx.industry),
    }
    return inputs, ctx.industry


def _evidence_refs(db: Session, opportunity: Opportunity, record_id: str) -> list[str]:
    """Pointers to the evidence: the bound record first, then the validation evidence."""
    evidence_ids = db.scalars(
        select(ValidationEvidence.id)
        .where(
            ValidationEvidence.workspace_id == opportunity.workspace_id,
            ValidationEvidence.opportunity_id == opportunity.id,
        )
        .order_by(ValidationEvidence.created_at, ValidationEvidence.id)
        .limit(BRIEF_BOUNDS["evidence_refs"] - 1)
    ).all()
    return [record_id, *evidence_ids]


def _cap(text: str) -> str:
    limit = BRIEF_BOUNDS["list_item_chars"]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _content(
    screened: dict, warnings: list[str], evidence_refs: list[str]
) -> BriefContent:
    """The screened task output as bounded brief content, or :class:`BriefWithheldError`."""
    risk_notes = [screened.get("risk_note") or "", *warnings]
    try:
        return BriefContent(
            summary=screened.get("why_it_matters") or "",
            rationale=screened.get("ai_inference") or "",
            recommended_actions=[
                _cap(a) for a in [screened.get("recommended_action") or ""] if a
            ],
            suggested_angles=[a for a in screened.get("suggested_angles") or [] if a][
                : BRIEF_BOUNDS["list_items"]
            ],
            risk_notes=[_cap(n) for n in risk_notes if n][: BRIEF_BOUNDS["list_items"]],
            evidence_refs=evidence_refs,
        )
    except ValidationError:
        # An empty summary or rationale, or text outside the frozen bounds. The error's own
        # text quotes the model output, so it is not chained or logged.
        raise BriefWithheldError(
            "The generated brief was incomplete, so nothing was stored. Try again."
        ) from None


def _meter(db: Session, settings: Settings, opportunity: Opportunity) -> LLMMeter:
    """A meter for this one capture, bound to the opportunity's tenant.

    The session factory is built from the request's own bind (the durable handler's
    pattern), so on PostgreSQL the shared ledger reserves and settles in short transactions
    of its own and on SQLite the usage rows join the request's transaction at ``flush``.
    """
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False, future=True)
    return LLMMeter(settings, session_factory=factory).bind(
        db,
        organization_id=opportunity.organization_id,
        workspace_id=opportunity.workspace_id,
        scout_request_id=None,
    )


def _next_version(db: Session, opportunity: Opportunity) -> tuple[int, str | None]:
    """``(brief_version, parent_brief_id)`` for a new brief, under the opportunity's lock."""
    # Serialize captures for one opportunity: lock its row (READ COMMITTED on PostgreSQL)
    # BEFORE reading the highest version. SQLite ignores FOR UPDATE; it has one writer, and
    # the unique key turns any remaining clash into a 409 below.
    db.execute(
        select(Opportunity.id).where(Opportunity.id == opportunity.id).with_for_update()
    )
    latest = db.execute(
        select(RecommendationBrief.id, RecommendationBrief.brief_version)
        .where(
            RecommendationBrief.workspace_id == opportunity.workspace_id,
            RecommendationBrief.opportunity_id == opportunity.id,
        )
        .order_by(RecommendationBrief.brief_version.desc())
        .limit(1)
    ).first()
    if latest is None:
        return 1, None
    return latest.brief_version + 1, latest.id


def create_brief(
    db: Session,
    *,
    opportunity: Opportunity,
    intelligence_record: SignalIntelligenceRecord,
    campaign_id: str | None,
    author: User,
    llm: LLMService,
    settings: Settings,
) -> RecommendationBrief:
    """Generate and capture one immutable brief version; returns the flushed row.

    Raises :class:`~app.core.errors.ValidationDomainError` (record not this opportunity's),
    :class:`~app.core.errors.NotFoundError` (campaign not in the workspace),
    :class:`BriefJurisdictionUnresolvedError` (the record's existing location is not the
    workspace's or did not resolve; raised before any budget check or model call; a record
    with no location is not refused, its brief's ``jurisdiction_code`` is ``NULL``), and
    :class:`BriefNotProducedError` subclasses (budget, model failure, withheld output, a
    concurrent capture that took the version).
    """
    record = intelligence_record
    if (
        record.opportunity_id != opportunity.id
        or record.organization_id != opportunity.organization_id
        or record.workspace_id != opportunity.workspace_id
    ):
        raise ValidationDomainError(
            "The intelligence record does not belong to this opportunity."
        )
    if campaign_id is not None:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.workspace_id != opportunity.workspace_id:
            raise NotFoundError("Campaign not found in this workspace.")

    # The S1-5D capture rule (amended by Q-4): resolve first, refuse an existing location that
    # is not the workspace's or is UNRESOLVED before any budget or model use; no location is
    # NULL.
    jurisdiction_code = _capture_jurisdiction(db, opportunity, record)

    try:
        ensure_run_allowed(db, settings, opportunity.workspace_id)
    except LLMBudgetExhaustedError:
        raise BriefBudgetExhaustedError(
            "This workspace has used its AI budget for this month, so no brief can be "
            "generated now."
        ) from None

    inputs, industry = _brief_inputs(db, opportunity, record)
    meter = _meter(db, settings, opportunity)
    try:
        out, response = llm.run(
            BRIEF_TASK,
            inputs,
            organization_id=opportunity.organization_id,
            workspace_id=opportunity.workspace_id,
            meter=meter,
        )
    except LLMBudgetExceededError as exc:
        meter.flush(db)
        log_event(
            logger,
            "recommendation_brief_not_generated",
            outcome="refused",
            workspace_id=opportunity.workspace_id,
            opportunity_id=opportunity.id,
            reason=exc.reason,
        )
        if exc.reason == "workspace_budget":
            raise BriefBudgetExhaustedError(
                "This workspace has used its AI budget for this month, so no brief can be "
                "generated now."
            ) from None
        raise BriefGenerationUnavailableError(
            "A brief cannot be generated right now. Try again later."
        ) from None
    except LLMError as exc:
        meter.flush(db)
        log_event(
            logger,
            "recommendation_brief_not_generated",
            outcome="error",
            workspace_id=opportunity.workspace_id,
            opportunity_id=opportunity.id,
            error_class=type(exc).__name__,
        )
        raise BriefGenerationUnavailableError(
            "A brief cannot be generated right now. Try again later."
        ) from None
    meter.flush(db)

    screened = screen_explanation(out.model_dump(), industry)
    if screened.output is None:
        log_event(
            logger,
            "recommendation_brief_withheld",
            outcome="withheld",
            workspace_id=opportunity.workspace_id,
            opportunity_id=opportunity.id,
            reason=screened.withheld_reason,
        )
        raise BriefWithheldError(
            "The generated brief did not pass the safety screen, so nothing was stored."
        )
    content = _content(
        screened.output, screened.warnings, _evidence_refs(db, opportunity, record.id)
    )

    # The usage row exists by now (settled on PostgreSQL, flushed on SQLite); a row a
    # vanished tenant prevented is not referenced.
    usage_event_id = response.usage_event_id
    if usage_event_id is not None and db.get(LLMUsageEvent, usage_event_id) is None:
        usage_event_id = None

    version, parent_id = _next_version(db, opportunity)
    brief = RecommendationBrief(
        organization_id=opportunity.organization_id,
        workspace_id=opportunity.workspace_id,
        opportunity_id=opportunity.id,
        intelligence_record_id=record.id,
        location_id=record.location_id,
        # Captured once, here (Phase 5D §2); NULL only for a record with no location (Q-4).
        jurisdiction_code=jurisdiction_code,
        campaign_id=campaign_id,
        brief_version=version,
        parent_brief_id=parent_id,
        # Provenance — copied from the immutable record and the response, never the caller.
        analysis_version=record.analysis_version,
        scoring_version=record.scoring_version,
        prompt_name=response.prompt_name,
        prompt_version=response.prompt_version,
        model_identifier=response.model,
        input_hash=response.input_hash,
        trace_id=response.trace_id,
        is_simulated=response.is_simulated,
        llm_usage_event_id=usage_event_id,
        created_by=author.id,
        created_by_snapshot=actor_snapshot(
            user_id=author.id, full_name=author.full_name, email=author.email
        ),
        **content.model_dump(),
    )
    try:
        with db.begin_nested():
            db.add(brief)
            db.flush()
    except IntegrityError:
        raise BriefVersionConflictError(
            "Another brief for this opportunity was saved at the same time. Try again."
        ) from None

    record_audit(
        db,
        organization_id=brief.organization_id,
        workspace_id=brief.workspace_id,
        actor_user_id=author.id,
        action=AuditAction.RECOMMENDATION_BRIEF_CREATED,
        entity_type="recommendation_brief",
        entity_id=brief.id,
        new_state={
            "opportunity_id": brief.opportunity_id,
            "intelligence_record_id": brief.intelligence_record_id,
            "brief_version": brief.brief_version,
            "parent_brief_id": brief.parent_brief_id,
            "prompt": f"{brief.prompt_name}@{brief.prompt_version}",
            "is_simulated": brief.is_simulated,
        },
    )
    log_event(
        logger,
        "recommendation_brief_created",
        outcome="success",
        workspace_id=brief.workspace_id,
        opportunity_id=brief.opportunity_id,
        brief_id=brief.id,
        brief_version=brief.brief_version,
        is_simulated=brief.is_simulated,
        # A category, never the code or the location: whether a jurisdiction was captured.
        jurisdiction="captured" if jurisdiction_code is not None else NO_LOCATION,
    )
    return brief


__all__ = [
    "BRIEF_TASK",
    "JURISDICTION_UNRESOLVED_MESSAGE",
    "BriefBudgetExhaustedError",
    "BriefGenerationUnavailableError",
    "BriefJurisdictionUnresolvedError",
    "BriefNotProducedError",
    "BriefVersionConflictError",
    "BriefWithheldError",
    "create_brief",
]
