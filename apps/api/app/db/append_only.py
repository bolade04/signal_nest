"""Application-layer append-only enforcement for ORM models (Project Phase 5, W21 CF-1).

:func:`append_only` marks a model whose rows are written once. Every ORM path that would
UPDATE or DELETE such a row raises :class:`AppendOnlyViolation` before any statement is sent
(or, inside a flush, before the flush completes; the caller's transaction is then rolled
back). A revision is a new row; a change of mind is a new row.

**What is refused (every ORM write path except INSERT):**

* the unit of work: an attribute change or ``Session.delete()`` followed by a flush, including
  ``Session.merge()`` of a modified copy and a no-net-change assignment — the mapper's
  ``before_update`` / ``before_delete`` events;
* ORM-enabled statements run through a ``Session``: ``session.execute(update(Model)…)``
  (with WHERE criteria or with per-row parameter sets by primary key),
  ``session.execute(delete(Model)…)``, ``Query.update()``, ``Query.delete()``, and an ORM
  ``insert(Model)`` carrying an upsert clause (``ON CONFLICT DO UPDATE`` or the like) —
  a ``Session`` ``do_orm_execute`` listener registered for every ``Session``;
* the legacy bulk API: ``Session.bulk_update_mappings()`` for the model, and
  ``Session.bulk_save_objects()`` with an instance of it that already has an identity (an
  update). These methods emit no event, so this module wraps them on the ``Session`` class.

**What is not refused, by design.** Statements built on the table rather than the model —
``update(Model.__table__)``, ``delete(table)``, ``table.delete()`` — and raw SQL are Core:
they carry no mapper and reach the database unchanged, whether run through a ``Session`` or a
``Connection``. So does an ORM statement executed on a bare ``Connection``. Exactly three
places write these tables that way after insert, all sanctioned:

1. :mod:`app.privacy.erasure` — the O-6 anonymisation that clears values naming an erased
   account or a deleted organization, and the foreign-key walk (``delete_rows``) that deletes
   or nulls rows when their scope is deleted;
2. :mod:`app.privacy.retention` — the time-based purge, through that same walk;
3. :func:`app.db.seed._reset` — the guarded, development-only demo reset that empties every
   table.

``test_cf1_append_only_models.py`` pins that no other production module builds an UPDATE or
DELETE over these models or their tables, or calls the bulk API on them.

**Enforcement level (operator decision O-10,
``REC_app_layer_append_only_document_db_level_as_separate_infra``).** This is an
application-layer control only. It is not database-level immutability: the runtime role owns
the database, so a restricted role or a trigger is a separate infrastructure change, not
built here.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, TypeVar

from sqlalchemy import event, inspect
from sqlalchemy.orm import ORMExecuteState, Session

T = TypeVar("T", bound=type)

#: Set on every decorated class, so a test can enumerate the append-only models.
APPEND_ONLY_MARKER = "__append_only__"
#: Set on the ``Session`` class once its bulk methods are wrapped (idempotent import).
_BULK_GUARD_MARKER = "_append_only_bulk_guard"


class AppendOnlyViolation(RuntimeError):
    """An ORM path tried to update or delete a row of an append-only model."""


def _refusal(cls_name: str, operation: str) -> AppendOnlyViolation:
    return AppendOnlyViolation(
        f"{cls_name} is append-only: an ORM {operation} is refused. Write a new row instead."
    )


def _refuse(operation: str):
    def listener(_mapper: Any, _connection: Any, target: Any) -> None:
        raise _refusal(type(target).__name__, operation)

    return listener


def append_only(cls: T) -> T:
    """Class decorator: every ORM path that would update or delete a ``cls`` row raises."""
    event.listen(cls, "before_update", _refuse("update"))
    event.listen(cls, "before_delete", _refuse("delete"))
    setattr(cls, APPEND_ONLY_MARKER, True)
    return cls


def is_append_only(cls: type) -> bool:
    """Whether ``cls`` was decorated with :func:`append_only` (inheritance ignored)."""
    return bool(cls.__dict__.get(APPEND_ONLY_MARKER, False))


def _is_upsert(statement: Any) -> bool:
    """Whether an INSERT carries a clause that updates an existing row on conflict."""
    clause = getattr(statement, "_post_values_clause", None)
    return clause is not None and type(clause).__name__ != "OnConflictDoNothing"


@event.listens_for(Session, "do_orm_execute")
def _refuse_orm_statements(state: ORMExecuteState) -> None:
    """Refuse ORM-enabled UPDATE / DELETE / upsert statements on an append-only model.

    ``bind_mapper`` is set only for statements built on a mapped class; a statement built on
    the table (the sanctioned Core paths) has none and passes.
    """
    mapper = state.bind_mapper
    if mapper is None or not is_append_only(mapper.class_):
        return
    name = mapper.class_.__name__
    if state.is_update:
        raise _refusal(name, "bulk update")
    if state.is_delete:
        raise _refusal(name, "bulk delete")
    if state.is_insert and _is_upsert(state.statement):
        raise _refusal(name, "upsert")


def _install_bulk_guard() -> None:
    """Wrap the two legacy bulk methods that can update a row without emitting any event."""
    if getattr(Session, _BULK_GUARD_MARKER, False):
        return
    original_update_mappings = Session.bulk_update_mappings
    original_save_objects = Session.bulk_save_objects

    @wraps(original_update_mappings)
    def bulk_update_mappings(self: Session, mapper: Any, mappings: Any, *args, **kwargs):
        cls = inspect(mapper).class_
        if is_append_only(cls):
            raise _refusal(cls.__name__, "bulk_update_mappings")
        return original_update_mappings(self, mapper, mappings, *args, **kwargs)

    @wraps(original_save_objects)
    def bulk_save_objects(self: Session, objects: Any, *args, **kwargs):
        objects = list(objects)
        for obj in objects:
            if is_append_only(type(obj)) and inspect(obj).key is not None:
                raise _refusal(type(obj).__name__, "bulk_save_objects update")
        return original_save_objects(self, objects, *args, **kwargs)

    Session.bulk_update_mappings = bulk_update_mappings  # type: ignore[method-assign]
    Session.bulk_save_objects = bulk_save_objects  # type: ignore[method-assign]
    setattr(Session, _BULK_GUARD_MARKER, True)


_install_bulk_guard()


__all__ = ["APPEND_ONLY_MARKER", "AppendOnlyViolation", "append_only", "is_append_only"]
