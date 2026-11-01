"""Audit logging service for sensitive actions — the single construction seam.

**Append-only guarantee and its enforcement level (Project Phase 5B audit spine, W21
CF-1; operator decision O-10, ``REC_app_layer_append_only_document_db_level_as_
separate_infra``):**

* :func:`record_audit` is the only place an :class:`~app.audit.models.AuditLog` is
  constructed in application code (pinned by ``test_cf1_audit_action_registry.py``), and
  it accepts only a member of the closed :class:`~app.audit.actions.AuditAction` registry.
* The model is :func:`~app.db.append_only.append_only`: every ORM path that would update or
  delete an audit record raises — the unit of work (attribute change or ``delete`` + flush,
  ``merge``), ORM ``update(AuditLog)`` / ``delete(AuditLog)`` statements, ``Query.update()``
  and ``Query.delete()``, ``bulk_update_mappings`` / ``bulk_save_objects`` updates and ORM
  upserts (``test_cf1_append_only_models.py``, ``test_cf1_append_only_bulk_paths.py``).
* Not refused, by design: statements on ``AuditLog.__table__`` (Core) and raw SQL. The
  sanctioned writers after insert are the privacy obligations in :mod:`app.privacy` — the
  erasure anonymisation (values naming an erased account or a deleted organization are
  cleared, the row is kept — O-6) and the time-based purge of the ``audit_logs`` retention
  class (duration undecided — O-5) — and the guarded, development-only demo reset
  (``app.db.seed``). A static test pins that no other production module builds an UPDATE or
  DELETE over the model or its table.
* Database-level immutability (a restricted role or a trigger) is **not** in place: the
  runtime role owns the database. That is a separate infrastructure change (O-10).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit.actions import AuditAction, audit_action
from app.audit.models import AuditLog


def record_audit(
    db: Session,
    *,
    organization_id: str,
    action: AuditAction,
    actor_user_id: str | None = None,
    workspace_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    reason: str | None = None,
    previous_state: dict | None = None,
    new_state: dict | None = None,
    context: dict | None = None,
) -> AuditLog:
    """Append one audit record. ``action`` must be a registry member.

    Raises :class:`~app.audit.actions.UnknownAuditActionError` (a ``ValueError``) before
    anything is written when ``action`` is not registered. The stored value is the
    member's string, unchanged from before the registry existed.
    """
    registered = audit_action(action)
    log = AuditLog(
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action=registered.value,
        entity_type=entity_type,
        entity_id=entity_id,
        reason=reason,
        previous_state=previous_state,
        new_state=new_state,
        context=context or {},
    )
    db.add(log)
    db.flush()
    return log
