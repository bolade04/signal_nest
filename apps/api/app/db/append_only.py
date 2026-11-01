"""Application-layer append-only enforcement for ORM models (Project Phase 5, W21 CF-1).

:func:`append_only` marks a model whose rows are written once. Every ORM-enabled write
through a ``Session`` that would UPDATE or DELETE such a row — a flush, an ORM statement passed
to ``Session.execute`` (at the top level, through ``from_statement``, or nested anywhere inside
another statement, e.g. as a data-modifying CTE), ``Query.update/delete`` or the legacy bulk
API, for the model or any mapped subclass of it — raises :class:`AppendOnlyViolation` before
any statement is sent (or, inside a flush, before the flush completes; the caller's
transaction is then rolled back). Writes that are not ORM-enabled, or not made through the
``Session``, are listed under "What is not refused, by design". A revision is a new row; a
change of mind is a new row.

**What is refused (every ORM write path except INSERT):**

* the unit of work: an attribute change or ``Session.delete()`` followed by a flush, including
  ``Session.merge()`` of a modified copy and a no-net-change assignment — the mapper's
  ``before_update`` / ``before_delete`` events, registered with ``propagate=True`` so a
  mapped subclass inherits them;
* ORM-enabled statements run through a ``Session``: ``session.execute(update(Model)…)``
  (with WHERE criteria or with per-row parameter sets by primary key),
  ``session.execute(delete(Model)…)``, ``Query.update()``, ``Query.delete()``, an ORM
  ``insert(Model)`` carrying an upsert clause (``ON CONFLICT DO UPDATE`` or the like), and
  an ORM ``insert(Model)`` carrying **any statement prefix** (``prefix_with(...)``, e.g.
  SQLite's ``OR REPLACE``, which deletes the conflicting row and inserts a new one) — each
  also as ``select(Model).from_statement(...)``, which runs the inner statement — and **any
  ORM-enabled INSERT, UPDATE or DELETE on the model nested inside another statement**: a
  data-modifying CTE (``WITH u AS (UPDATE …)``, which PostgreSQL executes), attached with
  ``add_cte`` or selected from, at any depth (subquery, scalar subquery, ``union``,
  ``exists``, ``INSERT … FROM SELECT``, a lambda statement), a plain INSERT included
  (CF2-FIX2, W21-REV-CF2-DATA r2 C3) — a ``Session`` ``do_orm_execute`` listener registered
  for every ``Session``, which sees every statement passed to ``Session.execute`` before it is
  compiled and walks it with ``sqlalchemy.sql.visitors.iterate``;
* the legacy bulk API: ``Session.bulk_update_mappings()`` for the model, and
  ``Session.bulk_save_objects()`` with an instance of it that already has an identity (an
  update). These methods emit no event, so this module wraps them on the ``Session`` class.

The statement listener and both wrappers test the class's MRO, so a mapped subclass of a
guarded model is guarded too (:func:`is_append_only` itself stays exact, for enumeration).

**Why every prefix, not only ``OR REPLACE`` (FU-CF1-H1).** A prefix is free dialect text
(``"OR REPLACE"``, ``"or  replace"``, ``text("OR REPLACE")``, a dialect-restricted prefix), so
matching spellings would be fragile, and the guard cannot know what an arbitrary prefix
does. Each SQLite conflict prefix changes what an INSERT may do to stored data: ``OR
REPLACE`` deletes the existing row; ``OR IGNORE`` silently drops a row that fails a
``CHECK``/``NOT NULL``/unique constraint (a compliance record that is quietly not written);
``OR ROLLBACK`` / ``ABORT`` / ``FAIL`` change what a failing statement leaves behind.
PostgreSQL accepts no INSERT prefix at all, and no code uses one, so refusing them all costs
nothing. Append-only means a plain INSERT; the structured ``on_conflict_do_nothing()`` stays
allowed because it never touches a stored row.

**What is not refused, by design.** Statements built on the table rather than the model —
``update(Model.__table__)``, ``delete(table)``, ``table.delete()``, a Core upsert
(``insert(Model.__table__).on_conflict_do_update(...)``), a Core prefixed insert
(``insert(Model.__table__).prefix_with("OR REPLACE")``), a Core DML statement on the table
used as a CTE (``update(Model.__table__).cte()``) — and raw SQL (``INSERT OR REPLACE``,
``REPLACE INTO``, ``WITH u AS (UPDATE …)`` as text) are Core: they carry no mapper and reach
the database unchanged, whether run through a ``Session`` or a ``Connection``. So does DDL
(``DROP TABLE``, ``ALTER TABLE … DROP COLUMN``, ``TRUNCATE``), as text or as a SQLAlchemy DDL
construct. So does an ORM statement executed on a bare ``Connection``
(``session.connection().execute(update(Model))``), and so do the database's own ``ON DELETE
CASCADE`` / ``SET NULL`` actions when a parent row is deleted (a brief goes with its
workspace, opportunity or evidence; ``approvals.brief_id`` and the actor columns are nulled).
Nor is a conflict clause declared in the table's own DDL (SQLite's ``ON CONFLICT REPLACE``
through ``sqlite_on_conflict…``), which would make a plain INSERT replace; no guarded table
declares one. Exactly three places write these tables through Core after insert, all
sanctioned:

1. :mod:`app.privacy.erasure` — the O-6 anonymisation that clears values naming an erased
   account or a deleted organization, and the foreign-key walk (``delete_rows``) that deletes
   or nulls rows when their scope is deleted;
2. :mod:`app.privacy.retention` — the time-based purge, through that same walk;
3. :func:`app.db.seed._reset` — the guarded, development-only demo reset that empties every
   table.

``test_cf1_append_only_bulk_paths.py`` is a static **backstop**, not the enforcement layer
(the runtime guard above is): it checks that no other production module builds an UPDATE or
DELETE (function or attribute-call form, method form, ``Query``), an upsert or a prefixed
insert (``.prefix_with(...)``, function or method form) over these models or their tables,
writes a literal SQL string naming one of the guarded tables unqualified — ``UPDATE
[ONLY] <table>``, SQLite's ``UPDATE OR <algorithm> <table>``, ``DELETE FROM [ONLY]
<table>``, ``INSERT OR <algorithm> INTO <table>``, ``REPLACE INTO <table>`` or ``INSERT INTO
<table> … DO UPDATE``, the name bare or quoted with ``"…"``, ``'…'`` (SQLite takes a
single-quoted table name), ``[…]`` or backticks — calls the bulk API or calls
``delete_rows``, following **model** import aliases and annotated, tuple and walrus aliases.
**Not pinned:** the statement functions (``update``, ``delete``, ``insert``,
dialect inserts, ``prefix_with``) imported or called under another name (e.g. the
``Insert`` class or an unbound ``Insert.prefix_with(stmt, …)``); models referenced through a
module (``models.Approval``); tables built or reflected by name (``table("approvals")``,
``Table("approvals", …, autoload_with=…)``) or reached through ``Base.metadata``, ``getattr``,
``inspect(...).local_table`` or ``Model.__mapper__.local_table``; tables passed as parameters;
SQL that schema-qualifies the table (``public.approvals``, ``main.approvals``), puts a comment
between the keywords and the table name, names the table as a PostgreSQL Unicode-escape
identifier (``U&"…"``), or uses ``TRUNCATE`` or ``MERGE``; DDL on a guarded table (``DROP
TABLE``, ``ALTER TABLE … DROP COLUMN``); SQL given as a ``bytes`` literal; a conflict clause
declared in DDL (``sqlite_on_conflict…``); and SQL assembled at run time.

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
from sqlalchemy.orm.context import FromStatement
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import UpdateBase

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
    event.listen(cls, "before_update", _refuse("update"), propagate=True)
    event.listen(cls, "before_delete", _refuse("delete"), propagate=True)
    setattr(cls, APPEND_ONLY_MARKER, True)
    return cls


def is_append_only(cls: type) -> bool:
    """Whether ``cls`` was decorated with :func:`append_only` (inheritance ignored, so the set
    of decorated models enumerates exactly)."""
    return bool(cls.__dict__.get(APPEND_ONLY_MARKER, False))


def is_guarded(cls: type) -> bool:
    """Whether ``cls`` or any class in its MRO is append-only (a mapped subclass included)."""
    return any(is_append_only(k) for k in cls.__mro__)


def _is_upsert(statement: Any) -> bool:
    """Whether an INSERT carries a clause that updates an existing row on conflict."""
    clause = getattr(statement, "_post_values_clause", None)
    return clause is not None and type(clause).__name__ != "OnConflictDoNothing"


def _has_prefix(statement: Any) -> bool:
    """Whether an INSERT carries any statement prefix (``prefix_with``), whatever its text or
    dialect: SQLite's ``OR REPLACE`` / ``OR IGNORE`` / … (FU-CF1-H1; see the module docstring
    for why every prefix is refused)."""
    return bool(getattr(statement, "_prefixes", ()))


def _outer_statement(statement: Any) -> Any:
    """The statement SQLAlchemy runs as the outer one. ``select(M).from_statement(X)`` runs
    ``X`` (``ORMExecuteState.is_insert`` / ``is_update`` / ``is_delete`` follow ``X``), so the
    upsert and prefix checks must read ``X`` too (CF2-FIX2: a ``from_statement`` upsert or
    prefixed insert passed before)."""
    return statement.element if isinstance(statement, FromStatement) else statement


def _guarded_dml_class(element: UpdateBase) -> type | None:
    """The guarded class (a decorated model or a mapped subclass) an ORM-enabled INSERT,
    UPDATE or DELETE is built on, else None. Core DML on the table carries no
    ``plugin_subject`` and is never matched (the sanctioned Core paths)."""
    subject = element._propagate_attrs.get("plugin_subject")
    cls = getattr(getattr(subject, "mapper", None), "class_", None)
    return cls if cls is not None and is_guarded(cls) else None


def _nested_guarded_dml(statement: Any) -> str | None:
    """The name of the guarded class of an ORM-enabled INSERT, UPDATE or DELETE nested
    anywhere inside ``statement`` — a data-modifying CTE (``WITH u AS (UPDATE …)``, attached
    with ``add_cte`` or selected from, at any depth: a subquery, a scalar subquery, a
    ``union``, ``exists``, an ``INSERT … FROM SELECT``, a lambda statement) — else None. The
    outer statement (see :func:`_outer_statement`) is judged by the top-level rules instead.

    Every DML kind is refused when nested, a plain INSERT included (coordinator ruling,
    CF2-FIX2, W21-REV-CF2-DATA r2 C3): the guard sees only the outer statement's kind, so it
    refuses a guarded model's DML wherever else it appears rather than re-judging each nested
    one; nothing in the code nests DML, and a plain INSERT keeps its top-level forms."""
    outer = _outer_statement(statement)
    for element in visitors.iterate(statement):
        if element is statement or element is outer or not isinstance(element, UpdateBase):
            continue
        cls = _guarded_dml_class(element)
        if cls is not None:
            return cls.__name__
    return None


@event.listens_for(Session, "do_orm_execute")
def _refuse_orm_statements(state: ORMExecuteState) -> None:
    """Refuse ORM-enabled UPDATE / DELETE / upsert / prefixed-INSERT statements on an
    append-only model, and any ORM-enabled DML on one nested inside another statement.

    ``bind_mapper`` is set only for statements built on a mapped class; a statement built on
    the table (the sanctioned Core paths) has none and passes. This listener runs for every
    statement passed to ``Session.execute`` (ORM-enabled or not) before it is compiled, so the
    nested walk is dialect-independent.
    """
    nested = _nested_guarded_dml(state.statement)
    if nested is not None:
        raise _refusal(nested, "INSERT/UPDATE/DELETE nested in another statement (e.g. a CTE)")
    mapper = state.bind_mapper
    if mapper is None or not is_guarded(mapper.class_):
        return
    name = mapper.class_.__name__
    outer = _outer_statement(state.statement)
    if state.is_update:
        raise _refusal(name, "bulk update")
    if state.is_delete:
        raise _refusal(name, "bulk delete")
    if state.is_insert and _is_upsert(outer):
        raise _refusal(name, "upsert")
    if state.is_insert and _has_prefix(outer):
        raise _refusal(name, "insert with a statement prefix (e.g. SQLite OR REPLACE)")


def _install_bulk_guard() -> None:
    """Wrap the two legacy bulk methods that can update a row without emitting any event."""
    if getattr(Session, _BULK_GUARD_MARKER, False):
        return
    original_update_mappings = Session.bulk_update_mappings
    original_save_objects = Session.bulk_save_objects

    @wraps(original_update_mappings)
    def bulk_update_mappings(self: Session, mapper: Any, mappings: Any, *args, **kwargs):
        cls = inspect(mapper).class_
        if is_guarded(cls):
            raise _refusal(cls.__name__, "bulk_update_mappings")
        return original_update_mappings(self, mapper, mappings, *args, **kwargs)

    @wraps(original_save_objects)
    def bulk_save_objects(self: Session, objects: Any, *args, **kwargs):
        objects = list(objects)
        for obj in objects:
            if is_guarded(type(obj)) and inspect(obj).key is not None:
                raise _refusal(type(obj).__name__, "bulk_save_objects update")
        return original_save_objects(self, objects, *args, **kwargs)

    Session.bulk_update_mappings = bulk_update_mappings  # type: ignore[method-assign]
    Session.bulk_save_objects = bulk_save_objects  # type: ignore[method-assign]
    setattr(Session, _BULK_GUARD_MARKER, True)


_install_bulk_guard()


__all__ = [
    "APPEND_ONLY_MARKER",
    "AppendOnlyViolation",
    "append_only",
    "is_append_only",
    "is_guarded",
]
