"""Text creative generation (Project Phase 5C, W21 lane P5C) — the only write path for
:class:`~app.creative.models.CreativeDraft` and :class:`~app.creative.models.ContentReview`.

**Request, then a durable job.** :func:`request_generation` (the generate route) checks
everything it can answer at once and enqueues one ``creative.generate`` job
(:class:`~app.creative.schemas.CreativeGeneratePayload`: ids only); :func:`execute_generation`
(the job's handler, :mod:`app.jobs.creative_handlers`) generates, reviews and stores the
draft. The route only enqueues and the client polls the job, then reads the draft.

**Gates, re-checked when the job runs** (a job can wait; the world can change):

* the capability ``creative_generation`` resolves enabled for the workspace (the
  deny-biased resolver; dark by default);
* the brief is the workspace's, and its controlling approval decision (5B: the
  highest ``decision_seq`` for its exact version) is ``approved`` — drafts are generated
  "from an approved recommendation brief" (Phase 5C plan §1);
* a revision's parent is a draft of the same brief and workspace;
* the requester is still a workspace editor (owner, admin or marketer);
* every generation ceiling is configured (:mod:`app.llm.quota`; refused while any is unset)
  and none would be breached, checked under the global and workspace generation locks
  before every provider attempt.

**The model call** is the 5C ``generate_creative`` task through
:meth:`app.llm.service.LLMService.run` with a :class:`~app.llm.quota.CeilingMeter` bound to
the workspace (run cap, monthly budget, usage rows, the daily ceilings) and the output limit
``min(llm_max_output_tokens, llm_max_output_tokens_per_call)``. The inputs are stored context
only — the brief's text, the brand voice, the workspace's **active** approved claims and
required disclaimers — never caller free text. Every model attempt's usage row is kept even
when no draft results (the worker re-writes them after a rollback).

**Screened, then reviewed.** The output is put in its persisted form and screened
(:func:`app.llm.screening.screen_creative`): a link, contact detail, markup or instruction
echo withholds it and nothing is stored. Otherwise the output-side content review runs on
exactly the persisted text (:func:`app.claims.engine.review_against_library`): claim-safety
findings, the workspace's active ``blocked`` claims it contains, and whether every required
disclaimer (the library's, plus the offer calendar's active offers') is carried. The review
is stored with the draft, one row per draft version, and a blocked or failed review makes
the draft unapprovable (:func:`draft_approval_refusal`).

**Provenance from the server.** Prompt name and version, model, ``input_hash`` (computed
here from the task, the prompt identity and the exact inputs — never by a provider), trace
id, ``is_simulated``, the usage snapshot (tokens and ``estimated_cost_usd`` from the price
table; 0 for the mock) and the usage-ledger row of the call; the author and their snapshot.

**Versions.** A draft is never updated: a revision is a new row. ``draft_version`` is the
brief's highest plus one, read under the brief row's lock (``SELECT … FOR UPDATE``) after the
model call, so the lock is never held across it; the unique key turns any remaining clash
into a retryable failure that stores nothing.

**Audited in the same transaction**: ``creative_draft.created`` with ids, versions and flags
only (no generated text, no author identity beyond the actor column).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.approvals.models import Approval
from app.audit.actions import AuditAction
from app.audit.service import record_audit
from app.brands.models import BusinessProfile
from app.briefs.models import RecommendationBrief
from app.campaign_context.models import BrandVoiceProfile, OfferCalendarEntry
from app.capabilities.registry import Capability
from app.capabilities.resolver import resolve_capability
from app.claims.engine import (
    APPROVED_KIND,
    BLOCKED_KIND,
    DISCLAIMER_KIND,
    LibraryEntry,
    LibraryReview,
    active_entries,
    contains_phrase,
    fold,
    review_against_library,
)
from app.claims.library import load_library
from app.core.actor_snapshot import actor_snapshot
from app.core.config import Settings
from app.core.enums import ApprovalDecision, ApprovalSubjectType, Role
from app.core.errors import NotFoundError, SignalNestError, ValidationDomainError
from app.core.logging import get_logger, log_event
from app.creative.models import ContentReview, CreativeDraft
from app.creative.schemas import (
    CREATIVE_BOUNDS,
    ContentReviewOut,
    CreativeDraftCreate,
    CreativeDraftOut,
    CreativeGeneratePayload,
)
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.registry import HandlerContext
from app.jobs.service import enqueue_job
from app.jobs.status import JobErrorCode, JobExecutionError, JobStatus, JobType
from app.llm.base import (
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.metering import LLMBudgetExceededError
from app.llm.models import LLMUsageEvent
from app.llm.prompts import get_prompt
from app.llm.quota import (
    CeilingMeter,
    LLMCeilingExceededError,
    check_ceilings,
    lock_for_generation,
)
from app.llm.screening import screen_creative
from app.organizations.models import OrganizationMember, User

logger = get_logger("signalnest.creative")

#: The model task a draft is generated with (frozen at CF-2).
GENERATE_TASK = "generate_creative"
#: The roles that may generate a draft (the brief-capture editors).
GENERATOR_ROLES: frozenset[Role] = frozenset({Role.OWNER, Role.ADMIN, Role.MARKETER})
#: The persisted text fields and their caps (``CREATIVE_BOUNDS``).
TEXT_LIMITS: dict[str, int] = {
    "headline": CREATIVE_BOUNDS["headline"],
    "body": CREATIVE_BOUNDS["body"],
    "call_to_action": CREATIVE_BOUNDS["call_to_action"],
}
#: Job states in which an enqueued generation is still going to run.
_ACTIVE_JOB_STATES = frozenset(
    {
        JobStatus.PENDING.value,
        JobStatus.SCHEDULED.value,
        JobStatus.CLAIMED.value,
        JobStatus.RUNNING.value,
        JobStatus.RETRY_WAIT.value,
    }
)
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

BRIEF_NOT_FOUND = "Brief not found in this workspace."
DRAFT_NOT_FOUND = "Draft not found in this workspace."


# --------------------------------------------------------------------------- #
# Errors the generate route answers with
# --------------------------------------------------------------------------- #
class BriefNotApprovedError(ValidationDomainError):
    """The brief's current version is not approved: drafts come from approved briefs."""

    code = "brief_not_approved"


class CreativeGenerationUnconfiguredError(SignalNestError):
    """A generation ceiling is not configured: generation is refused until all are."""

    status_code = 503
    code = "creative_generation_unconfigured"


class CreativeQuotaExceededError(SignalNestError):
    """A daily generation ceiling is reached (typed; ``reason`` names which)."""

    status_code = 429
    code = "llm_quota_exceeded"


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def creative_capability(
    db: Session, settings: Settings, organization_id: str, workspace_id: str
):
    """The deny-biased resolution of ``creative_generation`` for one workspace."""
    return resolve_capability(
        session=db,
        settings=settings,
        capability=Capability.CREATIVE_GENERATION,
        organization_id=organization_id,
        workspace_id=workspace_id,
    )


def get_scoped_brief(db: Session, workspace_id: str, brief_id: str) -> RecommendationBrief:
    """The brief within this workspace, or 404 (uniform with a missing id)."""
    brief = db.get(RecommendationBrief, brief_id)
    if brief is None or brief.workspace_id != workspace_id:
        raise NotFoundError(BRIEF_NOT_FOUND)
    return brief


def get_scoped_draft(db: Session, workspace_id: str, draft_id: str) -> CreativeDraft:
    """The draft within this workspace, or 404 (uniform with a missing id)."""
    draft = db.get(CreativeDraft, draft_id)
    if draft is None or draft.workspace_id != workspace_id:
        raise NotFoundError(DRAFT_NOT_FOUND)
    return draft


def brief_is_approved(db: Session, brief: RecommendationBrief) -> bool:
    """Whether the controlling decision on the brief's exact version is ``approved`` (5B:
    the highest ``decision_seq`` controls; never "an approved row exists")."""
    latest = db.scalar(
        select(Approval.decision)
        .where(
            Approval.organization_id == brief.organization_id,
            Approval.workspace_id == brief.workspace_id,
            Approval.subject_type == ApprovalSubjectType.RECOMMENDATION_BRIEF.value,
            Approval.subject_id == brief.id,
            Approval.subject_version == brief.brief_version,
        )
        .order_by(Approval.decision_seq.desc())
        .limit(1)
    )
    return latest == ApprovalDecision.APPROVED.value


def reviews_by_draft(db: Session, drafts: Iterable[CreativeDraft]) -> dict[str, ContentReview]:
    """Each draft's review, read by the review's own tenant columns and the draft id."""
    drafts = list(drafts)
    if not drafts:
        return {}
    workspace_id = drafts[0].workspace_id
    rows = db.scalars(
        select(ContentReview).where(
            ContentReview.workspace_id == workspace_id,
            ContentReview.draft_id.in_([d.id for d in drafts]),
        )
    ).all()
    return {r.draft_id: r for r in rows}


def draft_out(draft: CreativeDraft, review: ContentReview | None) -> CreativeDraftOut:
    out = CreativeDraftOut.model_validate(draft)
    if review is not None:
        out = out.model_copy(update={"review": ContentReviewOut.model_validate(review)})
    return out


def list_brief_drafts(
    db: Session, brief: RecommendationBrief, *, limit: int, offset: int
) -> tuple[list[CreativeDraft], int]:
    """A bounded page of the brief's drafts, newest version first, and the total."""
    base = select(CreativeDraft).where(
        CreativeDraft.workspace_id == brief.workspace_id,
        CreativeDraft.brief_id == brief.id,
    )
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = db.scalars(
        base.order_by(CreativeDraft.draft_version.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), total


def draft_approval_refusal(db: Session, draft: CreativeDraft) -> str | None:
    """Why ``draft`` cannot be approved, or None (Phase 5C plan §2; exit criterion 6).

    The 5B approval service calls this when ``subject_type == creative_draft`` and refuses
    while it returns a reason. Refused when the draft has no review, when its review is
    blocked or names a blocked claim, when a required disclaimer is unresolved (the review
    did not pass), or when the text contains a ``blocked`` claim that is active in the
    workspace's library *now* (one added after the draft was reviewed). A retired claim
    blocks nothing.
    """
    review = db.scalar(
        select(ContentReview).where(
            ContentReview.workspace_id == draft.workspace_id,
            ContentReview.draft_id == draft.id,
        )
    )
    if review is None:
        return "review_missing"
    if review.is_blocked or review.blocked_claim_ids:
        return "review_blocked"
    if not review.disclaimers_resolved or not review.passed:
        return "review_not_passed"
    text = fold(_draft_text(draft))
    for entry in active_entries(load_library(db, draft.workspace_id)):
        if entry.kind == BLOCKED_KIND and contains_phrase(text, fold(entry.text)):
            return "blocked_claim"
    return None


def _draft_text(draft: CreativeDraft) -> str:
    return "\n".join(t for t in (draft.headline, draft.body, draft.call_to_action) if t)


# --------------------------------------------------------------------------- #
# The generate request (route)
# --------------------------------------------------------------------------- #
def _check_parent(db: Session, brief: RecommendationBrief, parent_draft_id: str | None) -> None:
    if parent_draft_id is None:
        return
    parent = db.get(CreativeDraft, parent_draft_id)
    if parent is None or parent.workspace_id != brief.workspace_id or parent.brief_id != brief.id:
        raise NotFoundError(DRAFT_NOT_FOUND)


def _next_version_number(db: Session, brief: RecommendationBrief) -> int:
    latest = db.scalar(
        select(func.max(CreativeDraft.draft_version)).where(
            CreativeDraft.workspace_id == brief.workspace_id,
            CreativeDraft.brief_id == brief.id,
        )
    )
    return int(latest or 0) + 1


def _idempotency_prefix(
    brief: RecommendationBrief, body: CreativeDraftCreate, requested_by: str, next_version: int
) -> str:
    """Requests that would produce the same next draft share a prefix (one per brief state,
    channel, parent and requester), so a repeated submit joins the job already queued."""
    digest = hashlib.sha256(
        f"{body.channel.value}|{body.parent_draft_id or ''}|{requested_by}".encode()
    ).hexdigest()[:24]
    return f"creative:{brief.id}:v{next_version}:{digest}"


def request_generation(
    db: Session,
    *,
    brief: RecommendationBrief,
    body: CreativeDraftCreate,
    requested_by: User,
    settings: Settings,
    now: datetime | None = None,
) -> Job:
    """Validate a generate request and enqueue (or join) its ``creative.generate`` job.

    Raises :class:`BriefNotApprovedError` (422), :class:`~app.core.errors.NotFoundError`
    (a parent draft not of this brief, 404), :class:`CreativeGenerationUnconfiguredError`
    (503) and :class:`CreativeQuotaExceededError` (429). The capability and role gates are
    the route's; the job re-checks every gate under the generation locks.
    """
    now = now or datetime.now(UTC)
    if not brief_is_approved(db, brief):
        raise BriefNotApprovedError(
            "Approve this brief before generating drafts from it."
        )
    _check_parent(db, brief, body.parent_draft_id)
    if settings.creative_generation_ceilings_unset():
        raise CreativeGenerationUnconfiguredError(
            "Creative generation is not configured yet, so no draft can be generated."
        )
    try:
        # Advisory (no lock): the job re-checks before every attempt, under the locks.
        check_ceilings(
            db, settings, workspace_id=brief.workspace_id, task=GENERATE_TASK, now=now
        )
    except LLMCeilingExceededError as exc:
        # The reason is logged, not answered: "global" would disclose platform-wide spend.
        _log_not_generated(brief.workspace_id, brief.id, "refused", exc.reason)
        raise CreativeQuotaExceededError(
            "The daily limit for generated drafts has been reached. Try again tomorrow."
        ) from None

    prefix = _idempotency_prefix(brief, body, requested_by.id, _next_version_number(db, brief))
    prior = db.scalars(
        select(Job)
        .where(
            Job.organization_id == brief.organization_id,
            Job.workspace_id == brief.workspace_id,
            Job.job_type == JobType.CREATIVE_GENERATE.value,
            Job.idempotency_key.startswith(prefix + ":", autoescape=True),
        )
        .order_by(Job.created_at.desc(), Job.id)
    ).all()
    for job in prior:
        if job.status in _ACTIVE_JOB_STATES:
            return job  # the same request is already queued or running: join it
    payload = CreativeGeneratePayload(
        brief_id=brief.id,
        channel=body.channel,
        parent_draft_id=body.parent_draft_id,
        requested_by=requested_by.id,
    )
    return enqueue_job(
        db,
        job_type=JobType.CREATIVE_GENERATE,
        context=ExecutionContext(
            organization_id=brief.organization_id,
            workspace_id=brief.workspace_id,
            location_id=brief.location_id,
            campaign_id=brief.campaign_id,
        ),
        payload=payload.model_dump(mode="json"),
        location_id=brief.location_id,
        idempotency_key=f"{prefix}:{len(prior)}",
        now=now,
    )


# --------------------------------------------------------------------------- #
# The job: generate, screen, review, store
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GenerationContext:
    """Everything the model call and the review read, from stored context only."""

    inputs: dict[str, Any]
    industry: str | None
    library: list[LibraryEntry]
    offer_disclaimers: list[str]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _offer_disclaimers(db: Session, brief: RecommendationBrief, now: datetime) -> list[str]:
    """The ``required_disclaimer`` of every offer that applies now: active, started, not
    ended, and eligible for the brief's location (an offer with no location list applies
    everywhere)."""
    rows = db.scalars(
        select(OfferCalendarEntry)
        .where(
            OfferCalendarEntry.workspace_id == brief.workspace_id,
            OfferCalendarEntry.is_active.is_(True),
        )
        .order_by(OfferCalendarEntry.created_at, OfferCalendarEntry.id)
    ).all()
    found: list[str] = []
    for offer in rows:
        text = (offer.required_disclaimer or "").strip()
        if not text:
            continue
        start, end = _aware(offer.start_date), _aware(offer.end_date)
        if (start is not None and start > now) or (end is not None and end < now):
            continue
        eligible = offer.eligible_location_ids or []
        if eligible and brief.location_id not in eligible:
            continue
        found.append(text)
    return found


def _brand_voice(db: Session, workspace_id: str) -> str | None:
    rows = db.scalars(
        select(BrandVoiceProfile)
        .where(BrandVoiceProfile.workspace_id == workspace_id)
        .order_by(BrandVoiceProfile.created_at, BrandVoiceProfile.id)
    ).all()
    parts: list[str] = []
    for voice in rows:
        for label, values in (
            ("tone", voice.tone),
            ("personality", voice.personality),
            ("use", voice.do_use),
            ("avoid", voice.avoid),
        ):
            items = [str(v) for v in (values or []) if str(v).strip()]
            if items:
                parts.append(f"{label}: {', '.join(items)}")
    return "; ".join(parts) or None


def generation_context(
    db: Session, brief: RecommendationBrief, channel: str, now: datetime
) -> GenerationContext:
    """The ``generate_creative`` inputs (every template field a key) and the review's
    library, from the brief and the workspace's stored context."""
    library = load_library(db, brief.workspace_id)
    active = active_entries(library)
    offer_disclaimers = _offer_disclaimers(db, brief, now)
    disclaimers = [e.text for e in active if e.kind == DISCLAIMER_KIND] + offer_disclaimers
    profile = db.scalar(
        select(BusinessProfile).where(BusinessProfile.workspace_id == brief.workspace_id)
    )
    inputs = {
        "channel": channel,
        "summary": brief.summary,
        "rationale": brief.rationale,
        "recommended_actions": list(brief.recommended_actions or []),
        "suggested_angles": list(brief.suggested_angles or []),
        "brand_voice": _brand_voice(db, brief.workspace_id),
        "approved_claims": [e.text for e in active if e.kind == APPROVED_KIND],
        "required_disclaimers": disclaimers,
    }
    return GenerationContext(
        inputs=inputs,
        industry=profile.industry if profile else None,
        library=library,
        offer_disclaimers=offer_disclaimers,
    )


def compute_input_hash(task: str, inputs: dict[str, Any]) -> str:
    """SHA-256 hex of the canonical JSON of the task, its prompt identity and the exact
    inputs — computed here, never taken from a provider."""
    prompt = get_prompt(task)
    canonical = json.dumps(
        {"task": task, "prompt": f"{prompt.name}@{prompt.version}", "inputs": inputs},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cap_items(items: Iterable[Any]) -> list[Any]:
    limit, chars = CREATIVE_BOUNDS["review_items"], CREATIVE_BOUNDS["review_item_chars"]
    capped: list[Any] = []
    for item in list(items)[:limit]:
        capped.append(item[:chars] if isinstance(item, str) else item)
    return capped


def _review_row(draft: CreativeDraft, review: LibraryReview) -> ContentReview:
    chars = CREATIVE_BOUNDS["review_item_chars"]
    findings = [
        {"category": f.category, "risk": f.risk.value, "excerpt": f.excerpt[:chars]}
        for f in review.safety.findings
    ]
    return ContentReview(
        organization_id=draft.organization_id,
        workspace_id=draft.workspace_id,
        draft_id=draft.id,
        draft_version=draft.draft_version,
        risk_level=review.safety.risk_level.value,
        is_blocked=review.is_blocked,
        findings=_cap_items(findings),
        warnings=_cap_items(review.safety.warnings),
        blocked_claim_ids=_cap_items(review.blocked_claim_ids),
        required_disclaimers=_cap_items(review.required_disclaimers),
        disclaimers_resolved=review.disclaimers_resolved,
        passed=review.passed,
    )


def _fail(code: JobErrorCode, summary: str) -> JobExecutionError:
    return JobExecutionError(code, summary)


def _requester(db: Session, organization_id: str, user_id: str) -> User:
    user = db.get(User, user_id)
    role = db.scalar(
        select(OrganizationMember.role).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
    )
    if user is None or role is None or Role(role) not in GENERATOR_ROLES:
        raise _fail(JobErrorCode.AUTHORIZATION, "requester_not_permitted")
    return user


def _usage_ids(db: Session, job_id: str) -> set[str]:
    return set(db.scalars(select(LLMUsageEvent.id).where(LLMUsageEvent.job_id == job_id)))


def _call_usage_event_id(db: Session, job_id: str, before: set[str]) -> str | None:
    """The usage-ledger row of the attempt that produced the output: the one new ``ok``
    generation row of this job since ``before`` (written at settlement or by ``flush``)."""
    rows = db.execute(
        select(LLMUsageEvent.id, LLMUsageEvent.outcome, LLMUsageEvent.task).where(
            LLMUsageEvent.job_id == job_id
        )
    ).all()
    new_ok = [
        r.id
        for r in rows
        if r.id not in before and r.outcome == "ok" and r.task == GENERATE_TASK
    ]
    return new_ok[0] if len(new_ok) == 1 else None


def _next_version(db: Session, brief: RecommendationBrief) -> int:
    """The next ``draft_version`` for ``brief``, read under the brief row's lock."""
    db.execute(
        select(RecommendationBrief.id)
        .where(RecommendationBrief.id == brief.id)
        .with_for_update()
    )
    return _next_version_number(db, brief)


def execute_generation(
    ctx: HandlerContext, *, settings: Settings, llm, meter_clock=None
) -> dict[str, Any]:
    """Run one ``creative.generate`` attempt; returns the job's safe result summary.

    Raises :class:`~app.jobs.status.JobExecutionError`: ``VALIDATION`` (payload, brief,
    approval or parent), ``AUTHORIZATION`` (requester), ``POLICY`` (capability, ceilings,
    budget), ``TRANSIENT`` (the model stayed unavailable after the service's retries, or a
    version clash; the worker retries), ``NON_RETRYABLE`` (a permanent model failure or
    withheld output). Nothing is stored on any of them except the usage rows.
    """
    db = ctx.db
    try:
        payload = CreativeGeneratePayload.model_validate(ctx.payload)
    except ValidationError:
        raise _fail(JobErrorCode.VALIDATION, "invalid_payload") from None
    org_id, ws_id = ctx.context.organization_id, ctx.context.workspace_id

    brief = db.get(RecommendationBrief, payload.brief_id)
    if brief is None or brief.workspace_id != ws_id or brief.organization_id != org_id:
        raise _fail(JobErrorCode.VALIDATION, "brief_not_found")
    if not creative_capability(db, settings, org_id, ws_id).effective_enabled:
        raise _fail(JobErrorCode.POLICY, "capability_unavailable")
    if not brief_is_approved(db, brief):
        raise _fail(JobErrorCode.VALIDATION, "brief_not_approved")
    try:
        _check_parent(db, brief, payload.parent_draft_id)
    except NotFoundError:
        raise _fail(JobErrorCode.VALIDATION, "parent_not_found") from None
    author = _requester(db, org_id, payload.requested_by)
    unset = settings.creative_generation_ceilings_unset()
    if unset:
        raise _fail(JobErrorCode.POLICY, "ceilings_unset")
    if ctx.is_cancelled():
        raise _fail(JobErrorCode.CANCELLED, "cancelled before generation")

    # Held until the attempt's transaction ends: every ceiling check, the call and its
    # usage rows happen inside it (app.llm.quota).
    if not lock_for_generation(db, ws_id):
        raise _fail(JobErrorCode.VALIDATION, "workspace_not_found")
    now = ctx.now if ctx.now.tzinfo is not None else ctx.now.replace(tzinfo=UTC)
    context = generation_context(db, brief, payload.channel.value, now)

    meter = CeilingMeter(
        settings,
        db=db,
        task=GENERATE_TASK,
        clock=meter_clock,
        job_id=ctx.job_id,
        attempt=ctx.attempt,
        session_factory=sessionmaker(
            bind=db.get_bind(), autoflush=False, expire_on_commit=False, future=True
        ),
    ).bind(db, organization_id=org_id, workspace_id=ws_id, scout_request_id=None)
    ctx.after_rollback.append(meter.flush)
    before = _usage_ids(db, ctx.job_id)
    try:
        out, response = llm.run(
            GENERATE_TASK,
            context.inputs,
            meter=meter,
            max_output_tokens=settings.llm_max_output_tokens_per_call,
        )
    except LLMCeilingExceededError as exc:
        _log_not_generated(ws_id, brief.id, "refused", exc.reason)
        raise _fail(JobErrorCode.POLICY, f"ceiling:{exc.reason}") from None
    except LLMBudgetExceededError as exc:
        _log_not_generated(ws_id, brief.id, "refused", exc.reason)
        raise _fail(JobErrorCode.POLICY, f"budget:{exc.reason}") from None
    except (LLMTimeoutError, LLMRateLimitError, LLMUnavailableError) as exc:
        _log_not_generated(ws_id, brief.id, "error", type(exc).__name__)
        raise _fail(JobErrorCode.TRANSIENT, "model_unavailable") from None
    except LLMError as exc:
        _log_not_generated(ws_id, brief.id, "error", type(exc).__name__)
        raise _fail(JobErrorCode.NON_RETRYABLE, f"model_failed:{type(exc).__name__}") from None
    meter.flush(db)

    screened = screen_creative(out.model_dump(), TEXT_LIMITS)
    if screened.output is None:
        _log_not_generated(ws_id, brief.id, "withheld", screened.withheld_reason)
        raise _fail(JobErrorCode.NON_RETRYABLE, f"withheld:{screened.withheld_reason}")
    review = review_against_library(
        screened.screening_text, context.industry, context.library, context.offer_disclaimers
    )

    version = _next_version(db, brief)
    usage = response.usage
    draft = CreativeDraft(
        organization_id=org_id,
        workspace_id=ws_id,
        brief_id=brief.id,
        opportunity_id=brief.opportunity_id,
        location_id=brief.location_id,
        campaign_id=brief.campaign_id,
        draft_version=version,
        parent_draft_id=payload.parent_draft_id,
        channel=payload.channel.value,
        headline=screened.output["headline"] or None,
        body=screened.output["body"],
        call_to_action=screened.output["call_to_action"] or None,
        # Provenance — from the response and this service, never the caller.
        prompt_name=response.prompt_name,
        prompt_version=response.prompt_version,
        model_identifier=response.model,
        input_hash=compute_input_hash(GENERATE_TASK, context.inputs),
        trace_id=response.trace_id,
        is_simulated=response.is_simulated,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        estimated_cost_usd=Decimal(str(usage.estimated_cost_usd)),
        llm_usage_event_id=_call_usage_event_id(db, ctx.job_id, before),
        created_by=author.id,
        created_by_snapshot=actor_snapshot(
            user_id=author.id, full_name=author.full_name, email=author.email
        ),
    )
    db.add(draft)
    try:
        db.flush()
    except IntegrityError:
        raise _fail(JobErrorCode.TRANSIENT, "version_conflict") from None
    db.add(_review_row(draft, review))
    db.flush()

    record_audit(
        db,
        organization_id=org_id,
        workspace_id=ws_id,
        actor_user_id=author.id,
        action=AuditAction.CREATIVE_DRAFT_CREATED,
        entity_type="creative_draft",
        entity_id=draft.id,
        new_state={
            "brief_id": brief.id,
            "brief_version": brief.brief_version,
            "draft_version": draft.draft_version,
            "parent_draft_id": draft.parent_draft_id,
            "channel": draft.channel,
            "prompt": f"{draft.prompt_name}@{draft.prompt_version}",
            "is_simulated": draft.is_simulated,
            "review_passed": review.passed,
            "is_blocked": review.is_blocked,
        },
    )
    log_event(
        logger,
        "creative_draft_created",
        outcome="success",
        workspace_id=ws_id,
        brief_id=brief.id,
        draft_id=draft.id,
        draft_version=draft.draft_version,
        is_simulated=draft.is_simulated,
        review_passed=review.passed,
    )
    return {
        "outcome": "draft_created",
        "draft_id": draft.id,
        "draft_version": draft.draft_version,
        "review_passed": review.passed,
        "is_blocked": review.is_blocked,
    }


def _log_not_generated(workspace_id: str, brief_id: str, outcome: str, reason: str | None):
    log_event(
        logger,
        "creative_draft_not_generated",
        outcome=outcome,
        workspace_id=workspace_id,
        brief_id=brief_id,
        reason=reason,
    )


__all__ = [
    "BRIEF_NOT_FOUND",
    "DRAFT_NOT_FOUND",
    "GENERATE_TASK",
    "GENERATOR_ROLES",
    "BriefNotApprovedError",
    "CreativeGenerationUnconfiguredError",
    "CreativeQuotaExceededError",
    "GenerationContext",
    "brief_is_approved",
    "compute_input_hash",
    "creative_capability",
    "draft_approval_refusal",
    "draft_out",
    "execute_generation",
    "generation_context",
    "get_scoped_brief",
    "get_scoped_draft",
    "list_brief_drafts",
    "request_generation",
    "reviews_by_draft",
]
