"""Persistence model for approvals (Project Phase 5B; frozen at W21 CF-1).

An :class:`Approval` row is one **append-only** decision — ``approved``, ``rejected`` or
``revoked`` — on one exact immutable subject version (Phase 5B plan §2):

* **Append-only.** A change of mind is a new row (the ``OpportunityFeedback``
  discipline). Every ORM path that would update or delete a row raises (unit of work, ORM
  UPDATE/DELETE statements and ``Query.update/delete``, the bulk API, upserts;
  :func:`app.db.append_only.append_only`). Core statements on the table are not refused:
  only :mod:`app.privacy` (erasure, retention) and the development-only demo reset write that
  way, pinned by a static test. The table carries ``created_at`` only.
* **Bound to an exact version.** ``subject_type`` / ``subject_id`` / ``subject_version``
  name the decided version without a foreign key, so the binding survives the subject's
  deletion. For a ``recommendation_brief`` subject, ``brief_id`` is the live foreign key
  (``SET NULL`` when the brief goes) and, while set, equals ``subject_id``
  (``ck_approval_brief_subject``).
* **The latest decision controls.** ``decision_seq`` is monotonic per subject version
  (``uq_approval_decision_seq``); its allocation is serialized per subject by the 5B
  service under a row lock (the ``capabilities/service.py`` ``_workspace_lock_select``
  pattern). The controlling decision for a subject version is the row with the highest
  ``decision_seq`` — consumers gate on that row, never on "an approved row exists".
* **Who decided, provably.** ``decided_by`` (``SET NULL``) plus the non-FK
  ``decided_by_snapshot`` (:mod:`app.core.actor_snapshot`) and the ``decided_role``
  snapshot. Separation of duties (approver ≠ author of the subject version) is enforced in
  the 5B service.
* **Closed vocabularies, DB-enforced.** ``subject_type`` (:class:`~app.core.enums.
  ApprovalSubjectType`), ``decision`` (:class:`~app.core.enums.ApprovalDecision`),
  ``decided_role`` (:class:`~app.core.enums.Role`) and the ``reason_code`` polarity
  (:mod:`app.approvals.reasons`) are portable CHECKs rendered from those Python sources.

**Survival — operator decision O-6** (``REC_compliance_records_survive_FKless_scope_
columns``): ``organization_id`` and ``workspace_id`` carry **no** foreign key, exactly like
``audit_logs``, so an approval survives the deletion of its workspace and organization.
Deleting the organization anonymises its approvals (decider, snapshot and note cleared;
the rest kept), and deleting the deciding account clears every value naming it
(:mod:`app.privacy.erasure`). Per the O-6 rider, survival is subject to the future O-5
retention policy and is **not** indefinite by default: approvals belong to the
``audit_logs`` retention class, whose duration is unset (undecided) and without which
production refuses to start (:mod:`app.privacy.inventory`).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.approvals.reasons import reason_polarity_sql
from app.core.actor_snapshot import ACTOR_SNAPSHOT_MAX_LENGTH
from app.core.enums import ApprovalDecision, ApprovalSubjectType, Role
from app.db.append_only import append_only
from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

#: Bound of the free-text note (characters).
APPROVAL_NOTE_MAX_LENGTH = 1000


def _in_list(column: str, values) -> str:
    rendered = ", ".join(f"'{v}'" for v in sorted(str(v) for v in values))
    return f"{column} IN ({rendered})"


def subject_type_sql() -> str:
    return _in_list("subject_type", (t.value for t in ApprovalSubjectType))


def decision_sql() -> str:
    return _in_list("decision", (d.value for d in ApprovalDecision))


def decided_role_sql() -> str:
    return _in_list("decided_role", (r.value for r in Role))


#: While ``brief_id`` is set, the row decides on that brief.
BRIEF_SUBJECT_SQL = (
    "brief_id IS NULL OR (subject_type = 'recommendation_brief' AND brief_id = subject_id)"
)


@append_only
class Approval(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One append-only decision on one exact subject version."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint(
            "subject_type",
            "subject_id",
            "subject_version",
            "decision_seq",
            name="uq_approval_decision_seq",
        ),
        CheckConstraint(subject_type_sql(), name="ck_approval_subject_type"),
        CheckConstraint(decision_sql(), name="ck_approval_decision"),
        CheckConstraint(reason_polarity_sql(), name="ck_approval_reason_polarity"),
        CheckConstraint(decided_role_sql(), name="ck_approval_decided_role"),
        CheckConstraint(BRIEF_SUBJECT_SQL, name="ck_approval_brief_subject"),
        CheckConstraint("subject_version >= 1", name="ck_approval_subject_version"),
        CheckConstraint("decision_seq >= 1", name="ck_approval_decision_seq"),
    )

    # --- Scope (FK-less by operator decision O-6; see the module docstring) ----------
    organization_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)

    # --- Subject (the exact immutable version decided on) ----------------------------
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The live link for a ``recommendation_brief`` subject; NULL once the brief is gone.
    brief_id: Mapped[str | None] = mapped_column(
        ForeignKey("recommendation_briefs.id", ondelete="SET NULL"), index=True
    )

    # --- Decision ------------------------------------------------------------------
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Monotonic per subject version; the highest is the controlling decision.
    decision_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(String(APPROVAL_NOTE_MAX_LENGTH))

    # --- Decider -------------------------------------------------------------------
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: Immutable "<user id> <full name> <<email>>" at decision time; "[erased]" once the
    #: account is erased or the organization deleted.
    decided_by_snapshot: Mapped[str] = mapped_column(
        String(ACTOR_SNAPSHOT_MAX_LENGTH), nullable=False
    )
    #: The decider's organization role at decision time (a snapshot).
    decided_role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


__all__ = [
    "APPROVAL_NOTE_MAX_LENGTH",
    "BRIEF_SUBJECT_SQL",
    "Approval",
    "decided_role_sql",
    "decision_sql",
    "subject_type_sql",
]
