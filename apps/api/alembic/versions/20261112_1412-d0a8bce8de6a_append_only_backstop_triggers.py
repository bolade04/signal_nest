"""append-only backstop: PostgreSQL triggers on the guarded tables (D-N4 item 37)

The operator's ruling of 2026-11-12 on D-N4 item 37: "add the PostgreSQL backstop, retaining the
application guard. Cover applicable destructive SQL paths, including TRUNCATE. Keep the
sanctioned privacy-writer exception transaction-scoped and test that it cannot leak through
pooled connections. Document that this does not protect against a deliberate table owner."
It supersedes O-10 ("application-layer append-only; document the database level as separate
infrastructure") for PostgreSQL. It is built under the operator's narrowly scoped
migration-freeze exception for items 36 and 37, which grants no Q-3 change, database-role change
or staging/production migration.

**What it adds (PostgreSQL only).** One trigger function, ``signalnest_append_only_backstop()``,
and two triggers on each of the six tables the application guard covers
(:mod:`app.db.append_only`; the decorated models, pinned by
``test_cf1_append_only_models.test_exactly_the_append_only_models_are_guarded``):
``recommendation_briefs``, ``approvals``, ``audit_logs``, ``creative_drafts``,
``content_reviews`` and ``exports``.

* ``append_only_backstop_row``: ``BEFORE UPDATE OR DELETE … FOR EACH ROW``. It fires for every
  row a statement would change or remove, whatever issued the statement: the ORM, Core, raw SQL,
  a bare connection, a data-modifying CTE, ``MERGE``, the update of an ``INSERT … ON CONFLICT DO
  UPDATE``, or the referential action (``ON DELETE CASCADE`` / ``SET NULL``) of a parent row's
  deletion. A statement that matches no row changes nothing and is not refused.
* ``append_only_backstop_truncate``: ``BEFORE TRUNCATE … FOR EACH STATEMENT`` (PostgreSQL has
  no row-level TRUNCATE trigger). It also fires when the table is truncated through
  ``TRUNCATE <parent> CASCADE``.

The function lets the write through only inside a sanctioned privacy-writer transaction: the
custom setting ``signalnest.privacy_writer`` must equal the current transaction's id
(``pg_current_xact_id()``). :func:`app.privacy.writer.mark_privacy_writer` sets it with
``set_config(…, true)``, so it ends with the transaction; and because it names the transaction,
a value left behind in any other way never matches a later one. Otherwise it raises SQLSTATE
``SNA01`` with a fixed message naming only the operation and the table (no row value). An
INSERT is never refused (append-only means insert-only).

**What it does not protect against.** The runtime role owns these tables, so a deliberate table
owner can disable or drop the triggers (``ALTER TABLE … DISABLE TRIGGER``, ``DROP TRIGGER``),
replace the function, or (with the privilege to set it) run with
``session_replication_role = replica``, which skips ordinary triggers; and any session can set
the mark for its own transaction. The backstop stops accidental code paths, not a deliberate
owner. That would need a separate restricted database role, an infrastructure change that is
not built here. DDL (``DROP TABLE``, ``ALTER TABLE``) is not covered either.

**SQLite (local mode)** gets nothing: the revision is a no-op there, and the application guard
stays the only control.

**Downgrade** drops the triggers and the function. It removes no data.

Revision ID: d0a8bce8de6a
Revises: d5f616c0ec97
Create Date: 2026-11-12 14:12:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d0a8bce8de6a"
down_revision: str | None = "d5f616c0ec97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The guarded tables, in the order the triggers are created. Literal on purpose: a revision
#: never imports the models, whose set may change later.
GUARDED_TABLES: tuple[str, ...] = (
    "recommendation_briefs",
    "approvals",
    "audit_logs",
    "creative_drafts",
    "content_reviews",
    "exports",
)
#: The setting the sanctioned privacy writer sets (``app.privacy.writer.PRIVACY_WRITER_SETTING``).
PRIVACY_WRITER_SETTING = "signalnest.privacy_writer"
#: The SQLSTATE of a refusal (``app.privacy.writer.APPEND_ONLY_SQLSTATE``).
APPEND_ONLY_SQLSTATE = "SNA01"
FUNCTION = "signalnest_append_only_backstop"
ROW_TRIGGER = "append_only_backstop_row"
TRUNCATE_TRIGGER = "append_only_backstop_truncate"

# The function body names only pg_catalog functions, schema-qualified, and the PL/pgSQL TG_
# variables, so it does not depend on the caller's search_path. A row trigger that lets the
# write through returns OLD for a DELETE and NEW for an UPDATE; a statement-level trigger's
# return value is ignored.
CREATE_FUNCTION = """
CREATE FUNCTION signalnest_append_only_backstop() RETURNS trigger
LANGUAGE plpgsql
AS $backstop$
BEGIN
    IF pg_catalog.current_setting('signalnest.privacy_writer', true)
        = CAST(pg_catalog.pg_current_xact_id() AS text) THEN
        IF TG_LEVEL = 'ROW' AND TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        IF TG_LEVEL = 'ROW' THEN
            RETURN NEW;
        END IF;
        RETURN NULL;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'SNA01',
        MESSAGE = 'append-only: ' || TG_OP || ' on ' || TG_TABLE_NAME
            || ' is refused outside a sanctioned privacy-writer transaction',
        HINT = 'Rows of this table are written once. Write a new row instead.';
END
$backstop$
"""

# One literal statement per table and trigger: no SQL is assembled from values at run time.
CREATE_TRIGGERS: tuple[str, ...] = (
    "CREATE TRIGGER append_only_backstop_row BEFORE UPDATE OR DELETE ON recommendation_briefs"
    " FOR EACH ROW EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_truncate BEFORE TRUNCATE ON recommendation_briefs"
    " FOR EACH STATEMENT EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_row BEFORE UPDATE OR DELETE ON approvals"
    " FOR EACH ROW EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_truncate BEFORE TRUNCATE ON approvals"
    " FOR EACH STATEMENT EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_row BEFORE UPDATE OR DELETE ON audit_logs"
    " FOR EACH ROW EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_truncate BEFORE TRUNCATE ON audit_logs"
    " FOR EACH STATEMENT EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_row BEFORE UPDATE OR DELETE ON creative_drafts"
    " FOR EACH ROW EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_truncate BEFORE TRUNCATE ON creative_drafts"
    " FOR EACH STATEMENT EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_row BEFORE UPDATE OR DELETE ON content_reviews"
    " FOR EACH ROW EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_truncate BEFORE TRUNCATE ON content_reviews"
    " FOR EACH STATEMENT EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_row BEFORE UPDATE OR DELETE ON exports"
    " FOR EACH ROW EXECUTE FUNCTION signalnest_append_only_backstop()",
    "CREATE TRIGGER append_only_backstop_truncate BEFORE TRUNCATE ON exports"
    " FOR EACH STATEMENT EXECUTE FUNCTION signalnest_append_only_backstop()",
)

DROP_TRIGGERS: tuple[str, ...] = (
    "DROP TRIGGER append_only_backstop_truncate ON exports",
    "DROP TRIGGER append_only_backstop_row ON exports",
    "DROP TRIGGER append_only_backstop_truncate ON content_reviews",
    "DROP TRIGGER append_only_backstop_row ON content_reviews",
    "DROP TRIGGER append_only_backstop_truncate ON creative_drafts",
    "DROP TRIGGER append_only_backstop_row ON creative_drafts",
    "DROP TRIGGER append_only_backstop_truncate ON audit_logs",
    "DROP TRIGGER append_only_backstop_row ON audit_logs",
    "DROP TRIGGER append_only_backstop_truncate ON approvals",
    "DROP TRIGGER append_only_backstop_row ON approvals",
    "DROP TRIGGER append_only_backstop_truncate ON recommendation_briefs",
    "DROP TRIGGER append_only_backstop_row ON recommendation_briefs",
)

DROP_FUNCTION = "DROP FUNCTION signalnest_append_only_backstop()"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return  # SQLite (local mode) keeps the application guard only.
    op.execute(CREATE_FUNCTION)
    for statement in CREATE_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    if not _is_postgresql():
        return
    for statement in DROP_TRIGGERS:
        op.execute(statement)
    op.execute(DROP_FUNCTION)
