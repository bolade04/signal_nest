"""Request/response schemas of the approval API (Project Phase 5B; frozen at W21 CF-1).

The 5B decision endpoints accept and return exactly these shapes. CF-1 mounts no route,
so these models are not yet in ``openapi.json``. Every closed vocabulary is enum-typed
(``ApprovalDecision``, ``ApprovalReason``, ``ApprovalSubjectType``, ``Role``) so it
reaches ``schema.d.ts`` as a closed union (Project Phase 5 plan §6.2.1).

* **Strict request.** :class:`ApprovalDecisionCreate` carries the decision, the exact
  subject version it is made on (a stale version is refused by the service rather than
  silently re-targeted), an optional polarity-checked reason and a bounded note. The
  subject comes from the route path; scope, decider, decided role, ``decision_seq`` and
  ``is_simulated`` are derived server-side. Unknown fields are rejected.
* **Response.** :class:`ApprovalOut` projects one stored decision. The FK-less scope
  column ``organization_id`` is not exposed.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.approvals.models import APPROVAL_NOTE_MAX_LENGTH
from app.approvals.reasons import ApprovalReason, reason_allowed
from app.core.enums import ApprovalDecision, ApprovalSubjectType, Role


class ApprovalDecisionCreate(BaseModel):
    """Strict request body recording one decision on one exact subject version."""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    #: The version the decider looked at; must be the subject's current version.
    subject_version: int = Field(ge=1)
    reason_code: ApprovalReason | None = None
    note: str | None = Field(default=None, max_length=APPROVAL_NOTE_MAX_LENGTH)

    @model_validator(mode="after")
    def _reason_matches_decision(self) -> ApprovalDecisionCreate:
        # The same polarity the database CHECK enforces, refused at the boundary (422).
        if not reason_allowed(self.decision, self.reason_code):
            raise ValueError("reason_code does not match the decision")
        return self


class ApprovalOut(BaseModel):
    """One stored, immutable decision."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    subject_type: ApprovalSubjectType
    subject_id: str
    subject_version: int
    #: NULL once the brief is gone (the decision survives it, O-6).
    brief_id: str | None
    decision: ApprovalDecision
    decision_seq: int
    reason_code: ApprovalReason | None
    note: str | None
    #: NULL once the decider's account is erased.
    decided_by: str | None
    #: "<user id> <full name> <<email>>" at decision time, or "[erased]".
    decided_by_snapshot: str
    decided_role: Role
    is_simulated: bool
    created_at: datetime


class ApprovalHistoryOut(BaseModel):
    """Bounded page of the decisions on one subject, newest ``decision_seq`` first."""

    items: list[ApprovalOut]
    total: int
    limit: int
    offset: int


__all__ = ["ApprovalDecisionCreate", "ApprovalHistoryOut", "ApprovalOut"]
