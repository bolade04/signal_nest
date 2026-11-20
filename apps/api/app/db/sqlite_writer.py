"""SQLite's writer lock, taken before a transaction's reads (the 5E SQLite fold).

SQLite has no row locks. Every ``SELECT … FOR UPDATE`` / ``FOR SHARE`` this application issues
compiles to a plain read there, and a read holds nothing once it has returned: with the
application's engine (:func:`app.db.session.build_engine`: rollback-journal mode, the driver's
default transaction control) a transaction begins only at its first write. SQLite's one writer
lock therefore serializes two transactions only from the first write of each, and whatever one of
them read before its own first write can be changed and committed by the other in between.

:func:`hold_sqlite_writer_lock` closes that gap for a caller that must read and then write as
one step: it opens the transaction with ``BEGIN IMMEDIATE``, which takes the writer lock at
once, and the caller's transaction holds it until it commits or rolls back. Its callers:

* :func:`app.exports.service.create_export`, after the role and the capability and before its
  first check, so a decision, a deletion or an erasure is either committed and read by the
  export's checks, or waits until the export has committed (a decision or an erasure at its own
  ``BEGIN IMMEDIATE``, an organization deletion at its first write);
* :func:`app.privacy.erasure.delete_account` and :func:`app.privacy.erasure.erase_invitee_email`,
  before they read which rows name their subject, so an export in flight is either committed
  and found by those reads, or waits until the erasure has committed;
* :func:`app.approvals.service.record_decision` and
  :func:`app.approvals.service.record_draft_decision`, after the request's role and capability
  gates and the decision's scope, author and reason checks, and before the first read the
  decision rests on (the decider's role, D-N4 item 34; for a draft approval the brief's
  approval, item 38; AD-DECISION-SQLITE-01), so a demotion, a removal, a withdrawal of the
  brief's approval, a deletion or an erasure is either committed and read by the decision, or
  waits until the decision has committed.

**The caller's transaction must not be open yet.** On each caller's real entry point it is not
(no statement before the call writes or opens a transaction), so ``BEGIN IMMEDIATE`` is sent;
``app/tests/test_approvals_decision_serialization_sqlite.py`` pins that for every caller. See
:func:`hold_sqlite_writer_lock` for what an already-open transaction does and does not hold.

**What it costs.** The lock is the whole database's, not a row's: while a caller holds it, the
other writers of every tenant wait, and the caller itself first waits for any writer in flight.
A wait lasts at most the connection's busy timeout (``busy_timeout=5000``, five seconds); after
that the waiting statement fails with ``database is locked``, which the application answers as
its generic 500, and the waiting request writes nothing. A writer whose transaction has already
read when it first writes (a job's write phase, or a request inside a ``SAVEPOINT``) does not
wait: its first write fails at once while a decision, an export or an erasure holds the lock. A
decision that would be refused (403, 404 or 409) first waits for a writer in flight, and if the
busy timeout expires it answers the generic 500, writing nothing. Other connections can still
read.

On every other engine the function sends nothing: PostgreSQL's row locks do this work there.
"""

from __future__ import annotations

from sqlalchemy.orm import Session


def hold_sqlite_writer_lock(db: Session) -> None:
    """On SQLite, take the database's writer lock now, for the rest of the caller's transaction.
    On any other engine, do nothing.

    A connection whose transaction is already open sends nothing, because SQLite refuses a
    second ``BEGIN``. Such a transaction holds the writer lock only if it has written: this
    driver opens a transaction by itself only before a write statement, and any write, even one
    that matches no row, takes the lock. A transaction opened without a write, by a
    ``SAVEPOINT`` (``Session.begin_nested``) or an explicit deferred ``BEGIN``, does not hold
    it: another connection can still write and commit until that transaction first reads. From
    its first read the rollback journal's shared lock keeps every other connection from
    committing until it ends, but its own first write then fails at once, without waiting, if
    another connection holds the writer lock. No caller opens such a transaction before it calls
    this (module docstring)."""
    if db.get_bind().dialect.name != "sqlite":
        return
    connection = db.connection()
    if connection.connection.dbapi_connection.in_transaction:
        return
    connection.exec_driver_sql("BEGIN IMMEDIATE")


__all__ = ["hold_sqlite_writer_lock"]
