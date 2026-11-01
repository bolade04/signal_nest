"""Application-layer append-only enforcement for ORM models (Project Phase 5, W21 CF-1).

:func:`append_only` makes the ORM refuse to UPDATE or DELETE a row of the decorated model:
the unit of work raises :class:`AppendOnlyViolation` from the mapper's ``before_update`` /
``before_delete`` event, before any statement is sent. A revision is a new row; a change
of mind is a new row.

**Enforcement level (stated precisely; operator decision O-10,
``REC_app_layer_append_only_document_db_level_as_separate_infra``).** This is an
application-layer control on the ORM unit of work only:

* it does not see Core statements (``update(Model.__table__)``, ``delete(table)``) or raw
  SQL. Exactly two Core paths write these tables after insert, both sanctioned and both in
  :mod:`app.privacy`: the erasure anonymisation that clears values naming an erased account
  or organization, and the foreign-key walk that deletes or nulls rows when their scope is
  deleted or their retention class is purged;
* it is not database-level immutability. The runtime role owns the database, so a
  restricted role or a trigger is a separate infrastructure change (O-10), not built here.
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import event

T = TypeVar("T", bound=type)

#: Set on every decorated class, so a test can enumerate the append-only models.
APPEND_ONLY_MARKER = "__append_only__"


class AppendOnlyViolation(RuntimeError):
    """An ORM flush tried to update or delete a row of an append-only model."""


def _refuse(operation: str):
    def listener(_mapper: Any, _connection: Any, target: Any) -> None:
        raise AppendOnlyViolation(
            f"{type(target).__name__} is append-only: an ORM {operation} is refused. "
            "Write a new row instead."
        )

    return listener


def append_only(cls: T) -> T:
    """Class decorator: the ORM refuses every UPDATE and DELETE of ``cls`` rows."""
    event.listen(cls, "before_update", _refuse("update"))
    event.listen(cls, "before_delete", _refuse("delete"))
    setattr(cls, APPEND_ONLY_MARKER, True)
    return cls


def is_append_only(cls: type) -> bool:
    """Whether ``cls`` was decorated with :func:`append_only` (inheritance ignored)."""
    return bool(cls.__dict__.get(APPEND_ONLY_MARKER, False))


__all__ = ["APPEND_ONLY_MARKER", "AppendOnlyViolation", "append_only", "is_append_only"]
