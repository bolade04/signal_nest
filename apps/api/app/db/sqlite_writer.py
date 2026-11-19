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
  export's checks, or waits at its own first write until the export has committed;
* :func:`app.privacy.erasure.delete_account` and :func:`app.privacy.erasure.erase_invitee_email`,
  before they read which rows name their subject, so an export in flight is either committed
  and found by those reads, or waits until the erasure has committed.

**What it costs.** The lock is the whole database's, not a row's: while a caller holds it, every
other writer of every tenant waits, and the caller itself first waits for any writer in flight.
A wait lasts at most the connection's busy timeout (``busy_timeout=5000``, five seconds); after
that the waiting statement fails with ``database is locked``, which the application answers as
its generic 500, and the waiting request writes nothing. Other connections can still read.

On every other engine the function sends nothing: PostgreSQL's row locks do this work there.
"""

from __future__ import annotations

from sqlalchemy.orm import Session


def hold_sqlite_writer_lock(db: Session) -> None:
    """On SQLite, take the database's writer lock now, for the rest of the caller's transaction.
    On any other engine, do nothing.

    A connection whose transaction is already open sends nothing: this driver opens one only
    before a write statement, so that transaction already holds the writer lock, and SQLite
    refuses a second ``BEGIN``."""
    if db.get_bind().dialect.name != "sqlite":
        return
    connection = db.connection()
    if connection.connection.dbapi_connection.in_transaction:
        return
    connection.exec_driver_sql("BEGIN IMMEDIATE")


__all__ = ["hold_sqlite_writer_lock"]
