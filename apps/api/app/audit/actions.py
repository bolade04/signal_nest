"""The closed audit-action registry (Project Phase 5B audit spine; frozen at W21 CF-1).

Every value ``audit_logs.action`` may receive is a member of :class:`AuditAction`, and
:func:`app.audit.service.record_audit` — the single construction seam — refuses anything
else. Pattern: :mod:`app.capabilities.registry` (a closed ``StrEnum`` plus frozen
metadata entries behind a read-only ``MappingProxyType``).

* **Behaviour-preserving.** Every literal action string the code wrote before the
  registry is a member with the identical value, so no stored or newly written row
  changes. The one interpolated action, ``f"opportunity.{status}"``, is replaced by one
  member per :class:`~app.core.enums.OpportunityStatus` (:data:`OPPORTUNITY_STATUS_ACTIONS`)
  with the same five strings.
* **Service-layer enforcement only.** There is no database constraint on
  ``audit_logs.action`` in this round: existing rows keep their values (Phase 5B plan §2).
* **Additions are freeze-owned; nothing is ever removed or renamed.** After CF-1 this module
  is a contract surface: a later milestone adds members only at its CF-* gate, through the
  integration owner. A member is never removed and its value never changes, because stored
  rows keep their values and the typed read (``AuditLogOut.action``) refuses a value outside
  the registry.

Members reserved at CF-1 for the 5A/5B build lanes (vocabulary only; nothing writes
them until those lanes do): ``auth.login_succeeded`` / ``auth.login_failed`` (5B login
auditing), ``approval.approved`` / ``approval.rejected`` / ``approval.revoked`` (one per
:class:`~app.core.enums.ApprovalDecision`, :data:`APPROVAL_DECISION_ACTIONS`) and
``recommendation_brief.created`` (5A capture). Member-role assignment already audits as
``organization_member.role_changed`` (Phase 6, PR #181).

Added at CF-2 (Project Phase 5C): ``claims_library_entry.created`` / ``.updated`` /
``.retired`` — written now, by the protected claims-library routes — and
``creative_draft.created`` (reserved for the 5C lanes).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from app.core.enums import ApprovalDecision, OpportunityStatus


class AuditAction(StrEnum):
    """The closed set of audit actions. The value is the stored ``audit_logs.action``."""

    # --- Accounts and organizations --------------------------------------------------
    AUTH_REGISTER = "auth.register"
    AUTH_LOGIN_SUCCEEDED = "auth.login_succeeded"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    ONBOARDING_COMPLETED = "onboarding.completed"
    BUSINESS_PROFILE_UPDATED = "business_profile.updated"
    ORGANIZATION_MEMBER_ROLE_CHANGED = "organization_member.role_changed"
    ORGANIZATION_MEMBER_REMOVED = "organization_member.removed"
    ORGANIZATION_INVITATION_CREATED = "organization_invitation.created"
    ORGANIZATION_INVITATION_ACCEPTED = "organization_invitation.accepted"
    ORGANIZATION_INVITATION_REVOKED = "organization_invitation.revoked"

    # --- Locations ------------------------------------------------------------------
    LOCATION_CREATED = "location.created"
    LOCATION_DEACTIVATED = "location.deactivated"
    LOCATION_REACTIVATED = "location.reactivated"
    GEO_COVERAGE_UPDATED = "geo_coverage.updated"

    # --- Scouting -------------------------------------------------------------------
    SCOUT_REQUEST_CREATED = "scout_request.created"
    SCOUT_REQUEST_UPDATED = "scout_request.updated"
    SCOUT_REQUEST_PAUSED = "scout_request.paused"
    SCOUT_REQUEST_RESUMED = "scout_request.resumed"
    SCOUT_REQUEST_RUN = "scout_request.run"
    SCOUT_SCHEDULE_CREATED = "scout_schedule.created"
    SCOUT_SCHEDULE_PAUSED = "scout_schedule.paused"
    SCOUT_SCHEDULE_RESUMED = "scout_schedule.resumed"
    SCOUT_SCHEDULE_DELETED = "scout_schedule.deleted"
    SCOUT_ADMISSION_RECONCILED = "scout_admission.reconciled"

    # --- Opportunities (one member per OpportunityStatus) -----------------------------
    OPPORTUNITY_NEW = "opportunity.new"
    OPPORTUNITY_SAVED = "opportunity.saved"
    OPPORTUNITY_MONITORING = "opportunity.monitoring"
    OPPORTUNITY_IGNORED = "opportunity.ignored"
    OPPORTUNITY_ACTIONED = "opportunity.actioned"
    OPPORTUNITY_FEEDBACK_CREATED = "opportunity_feedback.created"

    # --- Capability governance (operator) ---------------------------------------------
    WORKSPACE_CAPABILITY_OVERRIDE_CREATED = "workspace_capability_override.created"
    WORKSPACE_CAPABILITY_OVERRIDE_UPDATED = "workspace_capability_override.updated"
    WORKSPACE_CAPABILITY_OVERRIDE_CLEARED = "workspace_capability_override.cleared"
    WORKSPACE_CAPABILITY_OVERRIDE_REJECTED = "workspace_capability_override.rejected"

    # --- Data sources (operator) -------------------------------------------------------
    DATA_SOURCE_TAKEN_DOWN = "data_source.taken_down"

    # --- Project Phase 5A/5B (reserved at CF-1) ------------------------------------------
    RECOMMENDATION_BRIEF_CREATED = "recommendation_brief.created"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_REVOKED = "approval.revoked"

    # --- Project Phase 5C (CF-2) -----------------------------------------------------
    CLAIMS_LIBRARY_ENTRY_CREATED = "claims_library_entry.created"
    CLAIMS_LIBRARY_ENTRY_UPDATED = "claims_library_entry.updated"
    CLAIMS_LIBRARY_ENTRY_RETIRED = "claims_library_entry.retired"
    CREATIVE_DRAFT_CREATED = "creative_draft.created"


@dataclass(frozen=True, slots=True)
class AuditActionSpec:
    """Immutable metadata for one audit action.

    Attributes:
        action: The member.
        domain: The subsystem that writes it (a coarse, non-secret grouping for readers).
        introduced: Where the action first appeared: ``"pre-registry"`` for every string
            the code already wrote before CF-1, else the milestone that reserved it.
    """

    action: AuditAction
    domain: str
    introduced: str


def _spec(action: AuditAction, introduced: str = "pre-registry") -> AuditActionSpec:
    domain = action.value.split(".", 1)[0]
    return AuditActionSpec(action=action, domain=domain, introduced=introduced)


_RESERVED_5A = frozenset({AuditAction.RECOMMENDATION_BRIEF_CREATED})
_ADDED_5C = frozenset(
    {
        AuditAction.CLAIMS_LIBRARY_ENTRY_CREATED,
        AuditAction.CLAIMS_LIBRARY_ENTRY_UPDATED,
        AuditAction.CLAIMS_LIBRARY_ENTRY_RETIRED,
        AuditAction.CREATIVE_DRAFT_CREATED,
    }
)
_RESERVED_5B = frozenset(
    {
        AuditAction.AUTH_LOGIN_SUCCEEDED,
        AuditAction.AUTH_LOGIN_FAILED,
        AuditAction.APPROVAL_APPROVED,
        AuditAction.APPROVAL_REJECTED,
        AuditAction.APPROVAL_REVOKED,
    }
)



def _introduced(action: AuditAction) -> str:
    if action in _RESERVED_5A:
        return "5A"
    if action in _RESERVED_5B:
        return "5B"
    if action in _ADDED_5C:
        return "5C"
    return "pre-registry"


#: Read-only registry, in declaration order.
AUDIT_ACTION_REGISTRY: Mapping[AuditAction, AuditActionSpec] = MappingProxyType(
    {
        a: _spec(a, _introduced(a))
        for a in AuditAction
    }
)

#: The audit action recorded when an opportunity's status is set (replaces the former
#: ``f"opportunity.{status}"``; same strings).
OPPORTUNITY_STATUS_ACTIONS: Mapping[OpportunityStatus, AuditAction] = MappingProxyType(
    {
        OpportunityStatus.NEW: AuditAction.OPPORTUNITY_NEW,
        OpportunityStatus.SAVED: AuditAction.OPPORTUNITY_SAVED,
        OpportunityStatus.MONITORING: AuditAction.OPPORTUNITY_MONITORING,
        OpportunityStatus.IGNORED: AuditAction.OPPORTUNITY_IGNORED,
        OpportunityStatus.ACTIONED: AuditAction.OPPORTUNITY_ACTIONED,
    }
)

#: The audit action recorded for each approval decision (5B).
APPROVAL_DECISION_ACTIONS: Mapping[ApprovalDecision, AuditAction] = MappingProxyType(
    {
        ApprovalDecision.APPROVED: AuditAction.APPROVAL_APPROVED,
        ApprovalDecision.REJECTED: AuditAction.APPROVAL_REJECTED,
        ApprovalDecision.REVOKED: AuditAction.APPROVAL_REVOKED,
    }
)


class UnknownAuditActionError(ValueError):
    """Raised when a value does not name a member of the closed audit-action registry."""


def audit_action(value: AuditAction | str) -> AuditAction:
    """Strictly convert ``value`` to a registry member; raise for anything else."""
    try:
        return AuditAction(value)
    except ValueError as exc:
        raise UnknownAuditActionError(f"unregistered audit action {value!r}") from exc


def opportunity_status_action(status: OpportunityStatus | str) -> AuditAction:
    """The audit action for setting an opportunity to ``status`` (strict)."""
    return OPPORTUNITY_STATUS_ACTIONS[OpportunityStatus(status)]


__all__ = [
    "APPROVAL_DECISION_ACTIONS",
    "AUDIT_ACTION_REGISTRY",
    "OPPORTUNITY_STATUS_ACTIONS",
    "AuditAction",
    "AuditActionSpec",
    "UnknownAuditActionError",
    "audit_action",
    "opportunity_status_action",
]
