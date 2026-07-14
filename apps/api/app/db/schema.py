"""Schema-compatibility check (verify, never mutate).

A process must never migrate the database as a side effect of starting up: in a
multi-replica deployment that would race N actors against one schema. Instead a
single migration actor (``python -m app.db.migrate``) owns every schema change,
and each API/worker replica only *verifies* — at startup — that the live schema
is compatible with the code it is running.

The check compares the database's current Alembic revision against the revision
the running code expects (the script head) and classifies the relationship:

* ``compatible``   — the database is exactly at the code's head revision.
* ``ahead``        — the database carries a revision the code does not know. Under
                     the additive-first migration policy an older replica can still
                     run against a newer schema, so this is startup-safe (a rolling
                     deploy briefly runs old code against the new schema).
* ``pending``      — the database is at an ancestor of the code head: migrations
                     the code needs have not been applied. **Not** startup-safe.
* ``uninitialized``— no ``alembic_version`` row at all (fresh database).

This module performs **no** DDL and opens only a short read-only connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.core.config import Settings


def _api_root() -> Path:
    """The API project root (holds ``alembic.ini`` and the ``alembic`` tree).

    Resolved from this file's location (``app/db/schema.py`` -> project root) so it
    is correct regardless of the process working directory or install layout.
    """
    return Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    """An Alembic ``Config`` bound to the project's migration scripts.

    ``script_location`` is pinned to an absolute path so the config resolves from
    any working directory (a container runs from ``/app``); ``env.py`` still injects
    the database URL from :class:`~app.core.config.Settings`.
    """
    root = _api_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


@lru_cache(maxsize=1)
def _revision_order() -> tuple[str, ...]:
    """Revisions from head to base (cached; the migration tree is immutable)."""
    script = ScriptDirectory.from_config(alembic_config())
    return tuple(rev.revision for rev in script.walk_revisions())


def code_head_revision() -> str | None:
    """The single head revision the running code expects."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


class SchemaState(StrEnum):
    COMPATIBLE = "compatible"
    AHEAD = "ahead"
    PENDING = "pending"
    UNINITIALIZED = "uninitialized"


#: States a replica may start under. ``pending``/``uninitialized`` require the
#: single migration actor to run first.
_STARTUP_SAFE = frozenset({SchemaState.COMPATIBLE, SchemaState.AHEAD})


@dataclass(frozen=True)
class SchemaCompatibility:
    """The relationship between the live schema and the running code."""

    state: SchemaState
    db_revision: str | None
    code_head: str | None

    @property
    def is_startup_safe(self) -> bool:
        return self.state in _STARTUP_SAFE


def check_schema_compatibility(engine: Engine) -> SchemaCompatibility:
    """Classify the live schema against the code head. Read-only; never mutates."""
    code_head = code_head_revision()
    insp = inspect(engine)
    if "alembic_version" not in insp.get_table_names():
        return SchemaCompatibility(SchemaState.UNINITIALIZED, None, code_head)

    with engine.connect() as conn:
        db_revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()

    if not db_revision:
        return SchemaCompatibility(SchemaState.UNINITIALIZED, None, code_head)
    if db_revision == code_head:
        return SchemaCompatibility(SchemaState.COMPATIBLE, db_revision, code_head)
    # A revision known to the code but not the head is an ancestor -> the database
    # is behind and migrations are pending. A revision the code does not know is a
    # newer one applied by a later deploy -> additive-first makes it startup-safe.
    if db_revision in _revision_order():
        return SchemaCompatibility(SchemaState.PENDING, db_revision, code_head)
    return SchemaCompatibility(SchemaState.AHEAD, db_revision, code_head)


class SchemaNotReadyError(RuntimeError):
    """The live schema is not compatible with the running code at startup."""


def require_startup_schema(engine: Engine, *, settings: Settings) -> SchemaCompatibility:
    """Verify the schema is startup-safe or raise a clear, actionable error.

    Never migrates. On ``uninitialized``/``pending`` it instructs the operator to
    run the single migration actor rather than silently mutating the database.
    """
    compat = check_schema_compatibility(engine)
    if compat.is_startup_safe:
        return compat
    if compat.state is SchemaState.UNINITIALIZED:
        raise SchemaNotReadyError(
            "Database schema is not initialized. Run migrations with the single "
            "migration actor first:\n  python -m app.db.migrate\n"
            f"(environment={settings.environment})"
        )
    raise SchemaNotReadyError(
        "Database schema is behind the running application "
        f"(db_revision={compat.db_revision}, code_head={compat.code_head}). "
        "Run migrations with the single migration actor before starting replicas:\n"
        "  python -m app.db.migrate"
    )
