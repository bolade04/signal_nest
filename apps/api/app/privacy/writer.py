"""The sanctioned privacy writer's transaction marker (D-N4 item 37: the append-only backstop).

On PostgreSQL, Alembic revision ``d0a8bce8de6a`` puts a trigger on every table the
application guard (:mod:`app.db.append_only`) covers. The trigger refuses every UPDATE, DELETE
and TRUNCATE of those tables unless the transaction has been marked as a **sanctioned
privacy-writer transaction**. Exactly four writers in this package mark their transaction, after
their own checks have passed and before their first write:

* :func:`app.privacy.erasure.erase_organization` (organization deletion, and an organization
  deleted with its only member's account): the O-6 anonymisation of the organization's audit
  records, approvals and exports, then the foreign-key walk;
* :func:`app.privacy.erasure.delete_account` (account deletion): the anonymisation of the
  values that name the account, then the foreign-key walk;
* :func:`app.privacy.erasure.erase_invitee_email` (operator erasure of a person with no
  account): the anonymisation of the values that name the address;
* :func:`app.privacy.retention.purge_batch` (the retention purge, from the sweep or the
  ``retention.purge`` job): the foreign-key walk over expired rows.

**The mark is transaction-scoped, and bound to the transaction.** :func:`mark_privacy_writer`
runs ``set_config('signalnest.privacy_writer', pg_current_xact_id()::text, true)``:

* ``is_local = true`` (the ``SET LOCAL`` form): the value lasts until the end of the current
  transaction. COMMIT and ROLLBACK both restore it, and so does a ROLLBACK TO a savepoint taken
  before the mark. A pooled connection therefore never carries it into the next checkout;
* the value is the current transaction's id, and the trigger compares it with
  ``pg_current_xact_id()`` (a 64-bit id, never reused). A value left behind in another way — a
  session-level ``SET`` of the same name, or ``'on'`` — never matches a later transaction, so it
  grants nothing.

Within the marked transaction the exception covers every later statement, not only the
writer's own: the routes, the CLI, the sweep and the job runner that own these transactions
commit straight after the writer and run no other destructive statement on a guarded table.

**What the mark is not.** A custom setting is readable and settable by any session, so the mark
is a statement of intent, not an authentication. The backstop stops accidental code paths (raw
SQL, a bare connection, a data-modifying CTE, ``MERGE``, an upsert, ``TRUNCATE``, a referential
action of a parent's deletion, or an ORM form the application guard does not know), not a
deliberate actor: such an actor can set the mark for its own transaction, and the role that owns
the tables can disable or drop the trigger. Protection against a deliberate table owner needs a
separate restricted database role, an infrastructure change that is not built here.
``test_append_only_backstop_postgres.py`` pins statically that only these four functions call
:func:`mark_privacy_writer` and that only this module names the setting.

On SQLite (local mode) there is no trigger; the call does nothing and the application guard is
the only control.
"""

from __future__ import annotations

from sqlalchemy import Text, cast, func, select
from sqlalchemy.orm import Session

#: The custom setting the trigger reads (``current_setting(..., true)``). Alembic revision
#: ``d0a8bce8de6a`` carries the same literal; the backstop's tests pin that the two agree.
PRIVACY_WRITER_SETTING = "signalnest.privacy_writer"
#: The SQLSTATE the trigger raises when it refuses a write (a code of its own, so it is never
#: mistaken for an integrity error such as a unique-key clash).
APPEND_ONLY_SQLSTATE = "SNA01"


def mark_privacy_writer(db: Session) -> None:
    """Mark the session's current transaction as a sanctioned privacy-writer transaction.

    PostgreSQL: one statement, ``set_config(<setting>, <current transaction id>, true)``; it
    begins the session's transaction if none is open. SQLite: nothing (no trigger exists).
    Call it after the writer's own checks and before its first write.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(
        select(func.set_config(PRIVACY_WRITER_SETTING, cast(func.pg_current_xact_id(), Text), True))
    )


__all__ = ["APPEND_ONLY_SQLSTATE", "PRIVACY_WRITER_SETTING", "mark_privacy_writer"]
