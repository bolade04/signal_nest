"""The closed approval reason-code vocabulary and its polarity (Project Phase 5B; W21 CF-1).

Metadata-bearing vocabularies live with their domain, not in ``app.core.enums`` (Project
Phase 5 plan §6.2.1): the reason codes carry a **polarity** — each code may accompany
exactly one :class:`~app.core.enums.ApprovalDecision` — following the
``FeedbackReason`` / ``ck_opportunity_feedback_reason_polarity`` precedent. The three
sets partition the vocabulary (disjoint, together complete), and the database CHECK on
``approvals`` is rendered from them, so the storable pairs are exactly these.

A reason is optional: a NULL ``reason_code`` is allowed with every decision (as for
feedback). Free text goes in the bounded ``note``; the ``*_other`` codes let a decider
flag an unmodelled reason without inventing a code.

The code list itself is a W21 CF-1 implementation proposal (the Phase 5B plan fixes the
pattern, not the codes) and is recorded for coordinator review.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from app.core.enums import ApprovalDecision


class ApprovalReason(StrEnum):
    """Why a decider approved, rejected or revoked a subject version."""

    # Approved
    EVIDENCE_SUFFICIENT = "evidence_sufficient"
    ALIGNED_WITH_STRATEGY = "aligned_with_strategy"
    APPROVED_OTHER = "approved_other"

    # Rejected
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    OFF_STRATEGY = "off_strategy"
    BRAND_OR_COMPLIANCE_RISK = "brand_or_compliance_risk"
    INACCURATE = "inaccurate"
    REJECTED_OTHER = "rejected_other"

    # Revoked
    SUPERSEDED = "superseded"
    CIRCUMSTANCES_CHANGED = "circumstances_changed"
    APPROVED_IN_ERROR = "approved_in_error"
    REVOKED_OTHER = "revoked_other"


#: Codes that may accompany ``approved`` only.
APPROVAL_REASONS: frozenset[ApprovalReason] = frozenset(
    {
        ApprovalReason.EVIDENCE_SUFFICIENT,
        ApprovalReason.ALIGNED_WITH_STRATEGY,
        ApprovalReason.APPROVED_OTHER,
    }
)
#: Codes that may accompany ``rejected`` only.
REJECTION_REASONS: frozenset[ApprovalReason] = frozenset(
    {
        ApprovalReason.INSUFFICIENT_EVIDENCE,
        ApprovalReason.OFF_STRATEGY,
        ApprovalReason.BRAND_OR_COMPLIANCE_RISK,
        ApprovalReason.INACCURATE,
        ApprovalReason.REJECTED_OTHER,
    }
)
#: Codes that may accompany ``revoked`` only.
REVOCATION_REASONS: frozenset[ApprovalReason] = frozenset(
    {
        ApprovalReason.SUPERSEDED,
        ApprovalReason.CIRCUMSTANCES_CHANGED,
        ApprovalReason.APPROVED_IN_ERROR,
        ApprovalReason.REVOKED_OTHER,
    }
)

#: The polarity partition: decision → the codes that may accompany it.
REASONS_BY_DECISION: Mapping[ApprovalDecision, frozenset[ApprovalReason]] = MappingProxyType(
    {
        ApprovalDecision.APPROVED: APPROVAL_REASONS,
        ApprovalDecision.REJECTED: REJECTION_REASONS,
        ApprovalDecision.REVOKED: REVOCATION_REASONS,
    }
)


def reason_allowed(decision: ApprovalDecision, reason: ApprovalReason | None) -> bool:
    """Whether ``reason`` may accompany ``decision`` (NULL always may)."""
    return reason is None or reason in REASONS_BY_DECISION[decision]


def _quoted(values) -> str:
    return ", ".join(f"'{v.value}'" for v in sorted(values, key=lambda v: v.value))


def reason_polarity_sql() -> str:
    """The portable CHECK expression on ``approvals``, rendered from the partition.

    Deterministic (decisions and codes sorted by value) so the model and the migration
    render it identically.
    """
    limbs = ["reason_code IS NULL"]
    for decision in sorted(REASONS_BY_DECISION, key=lambda d: d.value):
        limbs.append(
            f"(decision = '{decision.value}'"
            f" AND reason_code IN ({_quoted(REASONS_BY_DECISION[decision])}))"
        )
    return " OR ".join(limbs)


__all__ = [
    "APPROVAL_REASONS",
    "REASONS_BY_DECISION",
    "REJECTION_REASONS",
    "REVOCATION_REASONS",
    "ApprovalReason",
    "reason_allowed",
    "reason_polarity_sql",
]
