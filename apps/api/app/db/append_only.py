"""Application-layer append-only enforcement for ORM models (Project Phase 5, W21 CF-1).

:func:`append_only` marks a model whose rows are written once; its table becomes a
**guarded table**. Every ORM-enabled write through a ``Session`` that would UPDATE or DELETE a
row of a guarded table raises :class:`AppendOnlyViolation` before any statement is sent (or,
inside a flush, before the flush completes; the caller's transaction is then rolled back),
whichever mapping makes it: the model, a mapped subclass, or a second mapping of the same
table (``__table__ = Model.__table__``, ``registry.map_imperatively(Other, Model.__table__)``).
That is a flush, the legacy bulk API, and an ORM UPDATE, DELETE, upsert or prefixed INSERT
whose target is a guarded table, in a statement passed to ``Session.execute``
(``Query.update/delete`` included): as the statement itself, inside a ``from_statement()``
under any entity, as a CTE attached anywhere in it, or as a CTE that loader-option criteria
add when the statement is compiled. Writes that are not ORM-enabled, or not made through the
``Session``, are listed under "What is not refused, by design". A revision is a new row; a
change of mind is a new row.

**What is refused (every ORM write path except INSERT):**

* the unit of work: an attribute change or ``Session.delete()`` followed by a flush, including
  ``Session.merge()`` of a modified copy and a no-net-change assignment — the model's
  ``before_update`` / ``before_delete`` events, registered with ``propagate=True`` so a
  mapped subclass inherits them, and the same two events registered on ``Mapper`` itself,
  which fire for every mapper and refuse any mapper that maps a guarded table (a second
  mapping of it; CF2-FIX3, W21-REV-CF2-DATA r3 C6);
* ORM-enabled statements run through a ``Session`` — a ``Session`` ``do_orm_execute``
  listener registered for every ``Session``, which sees every statement passed to
  ``Session.execute`` (ORM-enabled or not) before it is compiled. **One rule, wherever the
  element stands** (CF2-FIX2: W21-REV-CF2-DATA r2 C3 and FU-CF1-H2 as extended by the
  coordinator after W21-REV-CF1-DESC r3): the listener walks the whole statement with
  ``sqlalchemy.sql.visitors.iterate`` and refuses every DML element (``UpdateBase``) whose
  **target** is a guarded table — the target table's ``parententity`` annotation names a
  mapper that maps a guarded table: the model, ``aliased(Model)``, ``inspect(Model)``,
  ``Model.__mapper__``, ``class_mapper(Model)``, a mapped subclass or a second mapping
  (CF2-FIX3, r3 C6 and C7) — and that is an update, a delete, an insert carrying an upsert
  clause (``ON CONFLICT DO UPDATE`` or the like), or an insert carrying **any statement
  prefix** (``prefix_with(...)``, e.g. SQLite's ``OR REPLACE``, which deletes the
  conflicting row and inserts a new one). Covered positions: the statement
  itself (WHERE criteria or per-row parameter sets by primary key, ``Query.update()`` /
  ``Query.delete()``, ``lambda_stmt``); the element of ``select(<any entity>).from_statement(…)``
  (the outer entity is not consulted: ``bind_mapper`` names the outer entity, so deciding by it
  let ``select(Other.id).from_statement(update(Model))`` through); and a data-modifying CTE
  (``WITH u AS (UPDATE …)``, which PostgreSQL executes) wherever it is attached — to a
  ``select()``, or to an outer INSERT, UPDATE or DELETE of another table — or selected from at
  any depth (subquery, scalar subquery, ``union``, ``exists``, ``INSERT … FROM SELECT``), and
  any combination of these. **Loader-option criteria** (CF2-FIX3, r3 C5):
  ``with_loader_criteria`` and a relationship's ``.and_()`` inside ``joinedload`` /
  ``selectinload`` / … live in the statement's options, which SQLAlchemy applies only when it
  compiles the statement, so the walk does not see DML referenced from them; when a
  statement carries options, the listener also compiles it for the session's bind and judges
  every top-level CTE of the compiled form by the same rule — that is where such DML runs
  (PostgreSQL executes a data-modifying CTE only at the top level, SQLAlchemy hoists
  non-nesting CTEs there, SQLite runs none), and ``selectinload``'s second statement passes
  through the listener and is compiled and judged in turn. A plain ``insert(Model)`` (``ON
  CONFLICT DO NOTHING`` included) is not refused in any position: append-only means
  insert-only;
* the legacy bulk API: ``Session.bulk_update_mappings()`` for a mapper that maps a guarded
  table, and ``Session.bulk_save_objects()`` with an instance of one that already has an
  identity (an update). These methods emit no event, so this module wraps them on the
  ``Session`` class.

Every check decides by mapper the same way: guarded if its class is, or subclasses, a
decorated model (the MRO), or if any table it maps is a guarded table (a second mapping, or
a mapping over a join that includes one). :func:`is_append_only` itself stays exact, for
enumeration.

The walk reaches what SQLAlchemy's own traversal exposes, and the compile-time check what a
statement with options compiles to as top-level CTEs. A DML statement that has already been
turned into SQL text before execution (``text(str(stmt.compile()))``), or that is held by a
custom ``ClauseElement`` which exposes it neither to traversal nor as a compiled CTE, is raw
SQL to the guard (see below).

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

**What is not refused, by design.** Statements whose **target** is the plain table rather than
a mapped class — ``update(Model.__table__)``, ``delete(table)``, ``table.delete()``, a Core
upsert (``insert(Model.__table__).on_conflict_do_update(...)``), a Core prefixed insert
(``insert(Model.__table__).prefix_with("OR REPLACE")``), a Core DML statement on the table
used as a CTE (``update(Model.__table__).cte()``) — and raw SQL (``INSERT OR REPLACE``,
``REPLACE INTO``, ``WITH u AS (UPDATE …)`` as text) are Core: they carry no target mapper and
reach the database unchanged, whether run through a ``Session`` or a ``Connection``, in any
position (a Core DML statement on the table inside ``from_statement()`` or as a CTE on another
statement included), and whatever their WHERE or VALUES mention: a Core statement whose
criteria name a guarded model's ORM attribute (``update(Approval.__table__).where(Approval.id
== …)``, or a write to another table filtered by ``select(AuditLog.id)``) is judged by its
target (r3 C7). An ORM statement whose target is an unguarded model is not refused either,
whatever its criteria mention. Also not refused: DDL (``DROP TABLE``, ``ALTER TABLE … DROP
COLUMN``, ``TRUNCATE``), as text or as a SQLAlchemy DDL construct; an ORM statement executed
on a bare ``Connection`` (``session.connection().execute(update(Model))``); the database's own
``ON DELETE CASCADE`` / ``SET NULL`` actions when a parent row is deleted (a brief goes with
its workspace, opportunity or evidence; ``approvals.brief_id`` and the actor columns are
nulled); and a conflict clause declared in the table's own DDL (SQLite's ``ON CONFLICT
REPLACE`` through ``sqlite_on_conflict…``), which would make a plain INSERT replace — no
guarded table declares one. Exactly three places write these tables through Core after insert,
all sanctioned:

1. :mod:`app.privacy.erasure` — the O-6 anonymisation that clears values naming an erased
   account or a deleted organization, and the foreign-key walk (``delete_rows``) that deletes
   or nulls rows when their scope is deleted;
2. :mod:`app.privacy.retention` — the time-based purge, through that same walk;
3. :func:`app.db.seed._reset` — the guarded, development-only demo reset that empties every
   table.

``test_cf1_append_only_bulk_paths.py`` is a static **backstop**, not the enforcement layer
(the runtime guard above is). It checks that no other production module builds an UPDATE or
DELETE (function or attribute-call form, method form, ``Query``), an upsert or a prefixed
insert (``.prefix_with(...)``, function or method form) over these models or their tables —
the model given bare, as ``Model.__table__``, as ``Model.__mapper__``, as ``aliased(Model)``,
``inspect(Model)`` or ``class_mapper(Model)``, or through an alias of any of these, following
model import aliases and annotated, tuple and walrus aliases — maps a guarded table a second
time (a class body's ``__table__ = <guarded table>``, ``map_imperatively(Cls, <guarded
table>)``; CF2-FIX3, r3 C6), writes a literal SQL string naming one of the guarded tables
unqualified (``UPDATE [ONLY] <table>``, SQLite's ``UPDATE OR <algorithm> <table>``, ``DELETE
FROM [ONLY] <table>``, ``INSERT OR <algorithm> INTO <table>``, ``REPLACE INTO <table>`` or
``INSERT INTO <table> … DO UPDATE``, the name bare or quoted with ``"…"``, ``'…'`` — SQLite
takes a single-quoted table name — ``[…]`` or backticks), calls the bulk API or calls
``delete_rows``. **Not pinned:** the statement functions (``update``, ``delete``, ``insert``,
dialect inserts, ``prefix_with``, ``aliased``, ``inspect``, ``class_mapper``,
``map_imperatively``) imported or called under another name (e.g. the ``Insert`` class or an
unbound ``Insert.prefix_with(stmt, …)``); models referenced through a module
(``models.Approval``); tables built or reflected by name (``table("approvals")``,
``Table("approvals", …, autoload_with=…)``) or reached through ``Base.metadata``, ``getattr``,
``inspect(...).local_table`` or ``Model.__mapper__.local_table`` — including as the table of a
second mapping; tables passed as parameters; SQL that schema-qualifies the table
(``public.approvals``, ``main.approvals``), puts a comment between the keywords and the table
name, names the table as a PostgreSQL Unicode-escape identifier (``U&"…"``), or uses
``TRUNCATE`` or ``MERGE``; DDL on a guarded table (``DROP TABLE``, ``ALTER TABLE … DROP
COLUMN``); SQL given as a ``bytes`` literal; a conflict clause declared in DDL
(``sqlite_on_conflict…``); and SQL assembled at run time.

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
from sqlalchemy.orm import Mapper, ORMExecuteState, Session
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import UpdateBase

T = TypeVar("T", bound=type)

#: Set on every decorated class, so a test can enumerate the append-only models.
APPEND_ONLY_MARKER = "__append_only__"
#: Set on the ``Session`` class once its bulk methods are wrapped (idempotent import).
_BULK_GUARD_MARKER = "_append_only_bulk_guard"
#: Every class decorated with :func:`append_only`; their tables are the guarded tables.
_DECORATED: list[type] = []


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
    """Class decorator: every ORM path that would update or delete a ``cls`` row raises, and
    ``cls``'s table becomes a guarded table (so a second mapping of it is guarded too)."""
    event.listen(cls, "before_update", _refuse("update"), propagate=True)
    event.listen(cls, "before_delete", _refuse("delete"), propagate=True)
    setattr(cls, APPEND_ONLY_MARKER, True)
    _DECORATED.append(cls)
    return cls


def is_append_only(cls: type) -> bool:
    """Whether ``cls`` was decorated with :func:`append_only` (inheritance ignored, so the set
    of decorated models enumerates exactly)."""
    return bool(cls.__dict__.get(APPEND_ONLY_MARKER, False))


def is_guarded(cls: type) -> bool:
    """Whether ``cls`` or any class in its MRO is append-only (a mapped subclass included)."""
    return any(is_append_only(k) for k in cls.__mro__)


def _guarded_table_ids() -> set[int]:
    """``id()`` of each decorated model's table (identity, not ``==``: a Table compares by
    identity, but ``id`` keeps that explicit). Read at call time, so a model decorated or a
    table mapped later is covered."""
    ids = set()
    for cls in _DECORATED:
        table = getattr(cls, "__table__", None)
        if table is not None:
            ids.add(id(table))
    return ids


def _mapper_is_guarded(mapper: Any) -> bool:
    """Whether a mapper writes guarded rows (CF2-FIX3, W21-REV-CF2-DATA r3 C6): its class is
    (or subclasses) a decorated model, or any table it maps is a guarded table — so a second
    mapping of a guarded table (``__table__ = Model.__table__``, ``registry.map_imperatively(
    Other, Model.__table__)``, a mapping over a join that includes it) is guarded too."""
    if is_guarded(mapper.class_):
        return True
    guarded = _guarded_table_ids()
    return any(id(table) in guarded for table in mapper.tables)


def _is_upsert(statement: Any) -> bool:
    """Whether an INSERT carries a clause that updates an existing row on conflict."""
    clause = getattr(statement, "_post_values_clause", None)
    return clause is not None and type(clause).__name__ != "OnConflictDoNothing"


def _has_prefix(statement: Any) -> bool:
    """Whether an INSERT carries any statement prefix (``prefix_with``), whatever its text or
    dialect: SQLite's ``OR REPLACE`` / ``OR IGNORE`` / … (FU-CF1-H1; see the module docstring
    for why every prefix is refused)."""
    return bool(getattr(statement, "_prefixes", ()))


def _target_mapper(element: UpdateBase) -> Any:
    """The mapper an ORM-enabled INSERT, UPDATE or DELETE writes to, read from its **target**
    table's ``parententity`` annotation — the model, ``aliased(Model)``, ``inspect(Model)``,
    ``Model.__mapper__``, ``class_mapper(Model)`` or a second mapping all set it — else None.

    None means Core DML on a plain ``Table``, whatever its WHERE or VALUES mention
    (CF2-FIX3, W21-REV-CF2-DATA r3 C7): the propagated ``plugin_subject`` is not used,
    because SQLAlchemy also sets it from an ORM attribute in a Core statement's criteria, which
    misnamed (and refused) Core writes, even to an unguarded table."""
    entity = getattr(element.table, "_annotations", {}).get("parententity")
    return getattr(entity, "mapper", None)


def _refused_operation(element: UpdateBase) -> str | None:
    """What the guard refuses about a DML element: an UPDATE, a DELETE, or an INSERT carrying
    an upsert clause or any statement prefix. A plain INSERT (``ON CONFLICT DO NOTHING``
    included) is not refused: append-only means insert-only."""
    if element.is_update:
        return "update"
    if element.is_delete:
        return "delete"
    if element.is_insert and _is_upsert(element):
        return "upsert"
    if element.is_insert and _has_prefix(element):
        return "insert with a statement prefix (e.g. SQLite OR REPLACE)"
    return None


def _judge(element: UpdateBase) -> tuple[str, str] | None:
    """(class name, operation) if ``element`` is a refused DML element on a guarded table:
    its target mapper writes guarded rows and its kind is refused. Else None."""
    mapper = _target_mapper(element)
    if mapper is None or not _mapper_is_guarded(mapper):
        return None
    operation = _refused_operation(element)
    return (mapper.class_.__name__, operation) if operation is not None else None


def _guarded_dml(statement: Any) -> tuple[str, str] | None:
    """The first refused ORM-enabled DML element on a guarded table anywhere in
    ``statement``, as (class name, operation), else None.

    One rule for every position (CF2-FIX2, W21-REV-CF2-DATA r2 C3 and the coordinator's
    FU-CF1-H2 ruling): ``visitors.iterate`` walks the whole statement — the statement itself,
    a ``from_statement()`` element whatever the outer ``select()``'s entity, and every CTE
    wherever it is attached (on a ``select()``, or on an outer INSERT, UPDATE or DELETE of
    another table; selected from at any depth) — and each ``UpdateBase`` element is judged on
    its own target table and kind (:func:`_judge`). The outer statement's ``bind_mapper`` is
    not consulted: for ``select(Other).from_statement(update(Model))`` it names ``Other``."""
    for element in visitors.iterate(statement):
        if not isinstance(element, UpdateBase):
            continue
        found = _judge(element)
        if found is not None:
            name, operation = found
            if element is not statement:
                operation += " nested in another statement (a CTE or from_statement)"
            return name, operation
    return None


def _compiled_guarded_dml(state: ORMExecuteState) -> tuple[str, str] | None:
    """Loader-option criteria (CF2-FIX3, W21-REV-CF2-DATA r3 C5). ``with_loader_criteria``
    and a relationship's ``.and_()`` inside ``joinedload`` / ``selectinload`` / … live in the
    statement's options and are applied when SQLAlchemy compiles it, so the walk above does
    not see DML referenced from them. When the statement carries options, compile it for the
    session's bind and judge every top-level CTE of the compiled form by the same rule. That
    is where such DML runs: PostgreSQL executes a data-modifying CTE only at the top level,
    SQLAlchemy hoists non-nesting CTEs there, and SQLite runs none. ``selectinload``'s
    criteria reach the database in its own second statement, which passes through this
    listener with its options and is compiled and judged in turn."""
    if not getattr(state.statement, "_with_options", None):
        return None
    bind = state.session.get_bind(**state.bind_arguments)
    compiled = state.statement.compile(dialect=bind.dialect)
    for cte in getattr(compiled, "ctes", None) or ():
        element = getattr(cte, "element", None)
        if isinstance(element, UpdateBase):
            found = _judge(element)
            if found is not None:
                name, operation = found
                return name, operation + " in a CTE added at compile time (loader-option criteria)"
    return None


@event.listens_for(Session, "do_orm_execute")
def _refuse_orm_statements(state: ORMExecuteState) -> None:
    """Refuse an ORM-enabled UPDATE, DELETE, upsert or prefixed INSERT on a guarded table
    wherever it stands in the statement (see :func:`_guarded_dml`), or in a CTE that loader
    options add at compile time (see :func:`_compiled_guarded_dml`).

    This listener runs for every statement passed to ``Session.execute`` (ORM-enabled or
    not), before SQLAlchemy compiles it for execution. A statement whose target is a plain
    ``Table`` (the sanctioned Core paths) has no target mapper and passes.
    """
    found = _guarded_dml(state.statement) or _compiled_guarded_dml(state)
    if found is not None:
        name, operation = found
        raise _refusal(name, operation)


def _refuse_guarded_table(operation: str):
    """A flush listener for every mapper (CF2-FIX3, r3 C6): refuse an update or delete by any
    mapper that writes a guarded table — a second mapping of it included, which the
    class-level listeners of :func:`append_only` cannot see."""

    def listener(mapper: Any, _connection: Any, target: Any) -> None:
        if _mapper_is_guarded(mapper):
            raise _refusal(type(target).__name__, operation)

    return listener


event.listen(Mapper, "before_update", _refuse_guarded_table("update"))
event.listen(Mapper, "before_delete", _refuse_guarded_table("delete"))


def _install_bulk_guard() -> None:
    """Wrap the two legacy bulk methods that can update a row without emitting any event."""
    if getattr(Session, _BULK_GUARD_MARKER, False):
        return
    original_update_mappings = Session.bulk_update_mappings
    original_save_objects = Session.bulk_save_objects

    @wraps(original_update_mappings)
    def bulk_update_mappings(self: Session, mapper: Any, mappings: Any, *args, **kwargs):
        resolved = inspect(mapper).mapper
        if _mapper_is_guarded(resolved):
            raise _refusal(resolved.class_.__name__, "bulk_update_mappings")
        return original_update_mappings(self, mapper, mappings, *args, **kwargs)

    @wraps(original_save_objects)
    def bulk_save_objects(self: Session, objects: Any, *args, **kwargs):
        objects = list(objects)
        for obj in objects:
            state = inspect(obj)
            if state.key is not None and _mapper_is_guarded(state.mapper):
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
