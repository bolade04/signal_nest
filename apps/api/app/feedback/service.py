"""Opportunity feedback service — append-only capture (Phase 3C, 3C-B).

This is the *only* write path for :class:`~app.feedback.models.OpportunityFeedback`.
It owns the invariants that make captured feedback trustworthy and inert:

* **Scope integrity / IDOR defense** — the target intelligence record must actually
  belong to the given opportunity's exact tenant scope (same organization, same
  workspace, and attached to that opportunity). A caller can never bind feedback for
  opportunity *X* to an intelligence record from a different opportunity or tenant.
* **Provenance from the record, never the caller** — ``analysis_version`` /
  ``scoring_version`` / ``fingerprint`` are *copied from the immutable target record*
  at capture time, so the feedback is forever interpretable against the exact
  analysis it was given on. These are never accepted from the caller.
* **Polarity** — an optional reason code must match the usefulness it accompanies
  (positive codes only with useful, negative only with not-useful). Enforced here in
  the domain layer *and* by a DB check constraint as a backstop.
* **Append-only** — every call inserts a *new* row. Nothing is ever updated or
  overwritten; a change of mind is simply another row.

Capture-only by contract: this service never scores, rescores, ranks, trains, or
emits any cross-workspace / cross-market signal. Role gating (editor-only) and the
``opportunity_feedback_enabled`` feature gate live at the future API boundary (3C-C),
mirroring how the scouting-schedule service leaves role/feature gating to its route.

Transaction ownership follows the house rule: this service ``flush``es but never
commits — the caller owns the transaction.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.core.enums import (
    NEGATIVE_FEEDBACK_REASONS,
    POSITIVE_FEEDBACK_REASONS,
    FeedbackReason,
)
from app.core.errors import ValidationDomainError
from app.core.logging import get_logger, log_event
from app.feedback.models import OpportunityFeedback
from app.intelligence.records import SignalIntelligenceRecord
from app.opportunities.models import Opportunity

logger = get_logger("signalnest.feedback")


def _coerce_reason(reason: FeedbackReason | str | None) -> FeedbackReason | None:
    """Normalize an optional reason to a :class:`FeedbackReason` or ``None``."""
    if reason is None:
        return None
    try:
        return FeedbackReason(reason)
    except ValueError as exc:
        raise ValidationDomainError(
            "Unsupported feedback reason code."
        ) from exc


def _validate_polarity(is_useful: bool, reason: FeedbackReason | None) -> None:
    """Reject a reason whose polarity contradicts the useful/not-useful judgement.

    A ``None`` reason is always valid. A positive code is valid only with useful
    feedback; a negative code only with not-useful feedback. This is the domain-layer
    guard that backs (and pre-empts) the DB check constraint.
    """
    if reason is None:
        return
    if is_useful and reason not in POSITIVE_FEEDBACK_REASONS:
        raise ValidationDomainError(
            "This reason cannot accompany useful feedback."
        )
    if not is_useful and reason not in NEGATIVE_FEEDBACK_REASONS:
        raise ValidationDomainError(
            "This reason cannot accompany not-useful feedback."
        )


def create_feedback(
    db: Session,
    *,
    opportunity: Opportunity,
    intelligence_record: SignalIntelligenceRecord,
    is_useful: bool,
    reason: FeedbackReason | str | None = None,
    submitted_by_user_id: str | None = None,
) -> OpportunityFeedback:
    """Append one immutable feedback row for an opportunity + intelligence record.

    Validates that ``intelligence_record`` belongs to ``opportunity``'s exact tenant
    scope, normalizes and polarity-checks the optional reason, copies the version
    provenance from the record, attributes the acting user, and inserts a new row.
    Returns the flushed (id-bearing) row; the caller owns the commit.
    """
    reason_enum = _coerce_reason(reason)
    _validate_polarity(is_useful, reason_enum)

    # Scope integrity + IDOR defense: the record must be this opportunity's own,
    # in the same organization and workspace. Feedback inherits the record's scope
    # rather than trusting any caller-supplied scope.
    if (
        intelligence_record.opportunity_id != opportunity.id
        or intelligence_record.organization_id != opportunity.organization_id
        or intelligence_record.workspace_id != opportunity.workspace_id
    ):
        raise ValidationDomainError(
            "The intelligence record does not belong to this opportunity."
        )

    feedback = OpportunityFeedback(
        organization_id=opportunity.organization_id,
        workspace_id=opportunity.workspace_id,
        opportunity_id=opportunity.id,
        intelligence_record_id=intelligence_record.id,
        submitted_by_user_id=submitted_by_user_id,
        is_useful=is_useful,
        reason_code=reason_enum.value if reason_enum is not None else None,
        # Provenance snapshot — copied from the immutable record, never caller-supplied.
        analysis_version=intelligence_record.analysis_version,
        scoring_version=intelligence_record.scoring_version,
        fingerprint=intelligence_record.fingerprint,
    )
    db.add(feedback)
    db.flush()

    record_audit(
        db,
        organization_id=feedback.organization_id,
        workspace_id=feedback.workspace_id,
        actor_user_id=submitted_by_user_id,
        action="opportunity_feedback.created",
        entity_type="opportunity_feedback",
        entity_id=feedback.id,
    )
    log_event(
        logger,
        "opportunity_feedback_created",
        outcome="success",
        workspace_id=feedback.workspace_id,
        opportunity_id=feedback.opportunity_id,
        intelligence_record_id=feedback.intelligence_record_id,
        is_useful=feedback.is_useful,
        reason_code=feedback.reason_code,
    )
    return feedback


__all__ = ["create_feedback"]
