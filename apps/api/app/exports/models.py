"""Persistence model for audited manual export (Project Phase 5E; frozen at W21 CF-3).

An :class:`Export` row records that a human exported the text of **one exact approved
creative draft version** (Phase 5E plan §2): the draft (``draft_id`` + ``draft_version``),
the approval that authorized it (``approval_id``), the render ``format`` (closed:
:class:`~app.core.enums.ExportFormat`), who exported it and the jurisdiction it was exported
under. Export is manual by definition: the service returns the rendered text inline and
nothing is published, stored as a file or sent anywhere. Nothing writes this table yet (the
export service is lane 5E-W1's, behind ``content_export_enabled``, default ``False``).

* **Append-only** (:func:`app.db.append_only.append_only`; ``created_at`` only — the 5A
  convention). Every ORM update/delete path raises; only :mod:`app.privacy` (erasure,
  retention) and the development-only demo reset write it through Core after insert.
* **Jurisdiction native from birth.** ``jurisdiction_code`` is ``NOT NULL`` and its CHECK has
  no ``IS NULL`` limb and refuses the resolver's ``UNRESOLVED`` (shape bound,
  :mod:`app.jurisdiction.codes`): a NULL-jurisdiction export row is unrepresentable in the
  database. The 5E service additionally refuses a draft or brief whose jurisdiction is NULL
  (precondition 5, defence in depth).
* **A compliance record (operator decision O-6).** ``organization_id`` / ``workspace_id`` are
  FK-less, as on ``approvals`` and ``audit_logs``, so the row survives its scope's deletion,
  anonymised. ``exported_by`` (``SET NULL``) plus the non-FK ``exported_by_snapshot``
  (:mod:`app.core.actor_snapshot`) — the immutable identity at export time — which account
  erasure and organization deletion replace with ``"[erased]"``. O-6 rider: survival is
  subject to the future O-5 policy, never indefinite by default — the row shares the
  ``audit_logs`` retention class (duration unset and undecided).
* **Live links.** ``draft_id`` (``SET NULL`` when the draft goes) and ``approval_id`` (``SET
  NULL`` when the approval is purged). The approval keeps the draft's id as its own
  ``subject_id``, so the chain from an export to what it exported survives the draft.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.actor_snapshot import ACTOR_SNAPSHOT_MAX_LENGTH
from app.core.enums import ExportFormat
from app.db.append_only import append_only
from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.jurisdiction.codes import JURISDICTION_CODE_MAX_LENGTH, jurisdiction_code_check_sql


def export_format_sql() -> str:
    rendered = ", ".join(f"'{v}'" for v in sorted(f.value for f in ExportFormat))
    return f"format IN ({rendered})"


#: The export row's jurisdiction CHECK: NOT NULL and no sentinel (no ``IS NULL`` limb).
EXPORT_JURISDICTION_SQL = jurisdiction_code_check_sql(nullable=False)


@append_only
class Export(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One audited manual export of one approved creative draft version."""

    __tablename__ = "exports"
    __table_args__ = (
        CheckConstraint(export_format_sql(), name="ck_export_format"),
        CheckConstraint(EXPORT_JURISDICTION_SQL, name="ck_export_jurisdiction_code"),
        CheckConstraint("draft_version >= 1", name="ck_export_draft_version"),
    )

    # --- Scope (FK-less by operator decision O-6, as on approvals) -------------------
    organization_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)

    # --- What was exported, and the approval that authorized it ----------------------
    #: The exported draft; NULL once the draft is gone (the approval keeps its id).
    draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("creative_drafts.id", ondelete="SET NULL"), index=True
    )
    #: The exact draft version exported.
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The controlling ``approved`` decision on that version; NULL once it is purged.
    approval_id: Mapped[str | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="SET NULL"), index=True
    )
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The jurisdiction the draft was exported under. Never NULL, never ``UNRESOLVED``.
    jurisdiction_code: Mapped[str] = mapped_column(
        String(JURISDICTION_CODE_MAX_LENGTH), nullable=False
    )

    # --- Who exported it -------------------------------------------------------------
    exported_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: Immutable "<user id> <full name> <<email>>" at export; "[erased]" once erased.
    exported_by_snapshot: Mapped[str] = mapped_column(
        String(ACTOR_SNAPSHOT_MAX_LENGTH), nullable=False
    )
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


__all__ = ["EXPORT_JURISDICTION_SQL", "Export", "export_format_sql"]
