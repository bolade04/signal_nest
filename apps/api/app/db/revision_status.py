"""Raw live-revision reader: ``python -m app.db.revision_status``.

Prints **exactly one line** — the database's Alembic revision — to stdout and
exits ``0``, or prints a **fixed, secret-free classification** to stderr and
exits with a dedicated code. Built for machine consumption by an offline
comparator (:mod:`app.db.revision_compare`); it makes no policy decision about
whether the revision is *correct*.

Design constraints (all deliberate):

* Uses the application configuration path only (``DATABASE_URL`` via Settings;
  the staging one-shot task additionally sets ``SN_MIGRATION_MODE=1`` so
  Settings validates without runtime secrets).
* Short-lived ``NullPool`` engine, disposed in ``finally``; never imports
  :mod:`app.db.session` (which builds the process engine at import time).
* Dialect-specific ``connect_args``: PostgreSQL gets a bounded
  ``connect_timeout``; SQLite receives no PostgreSQL-only argument.
* **No logging API, no traceback, no exception message** — a driver error can
  embed the DSN. Output is restricted to the revision line (stdout) and the
  fixed classification tokens below (stderr). A top-level ``BaseException``
  guard keeps every failure away from the default excepthook.

Exit-code map (stable, unique; does not overlap migrate 0-7 or bootstrap 10-20):

* ``30`` configuration failure (bad invocation/arguments, unloadable settings,
  un-constructable engine)
* ``31`` connection failure (engine could not connect)
* ``32`` ``alembic_version`` table missing
* ``33`` zero revision rows
* ``34`` multiple revision rows
* ``35`` malformed revision value
* ``36`` unexpected safe failure
"""

from __future__ import annotations

import re
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

EXIT_OK = 0
EXIT_CONFIG_FAILURE = 30
EXIT_CONNECT_FAILURE = 31
EXIT_TABLE_MISSING = 32
EXIT_NO_ROWS = 33
EXIT_MULTIPLE_ROWS = 34
EXIT_MALFORMED_REVISION = 35
EXIT_UNEXPECTED = 36

#: This repository's Alembic revision ids are 12 lowercase hex characters.
_REVISION_RE = re.compile(r"[0-9a-f]{12}")

#: Bounded PostgreSQL connect timeout (seconds) so a wedged network fails the
#: task instead of hanging it. Never passed to SQLite (unknown DBAPI kwarg).
_PG_CONNECT_TIMEOUT_SECONDS = 10


def _fail(token: str, code: int) -> int:
    print(f"revision-status: {token}", file=sys.stderr)
    return code


def _run(argv: list[str]) -> int:
    if argv:
        return _fail("unexpected-arguments", EXIT_CONFIG_FAILURE)

    try:
        from app.core.config import get_settings

        url = get_settings().database_url
    except Exception:
        return _fail("configuration-failure", EXIT_CONFIG_FAILURE)

    connect_args: dict[str, int] = {}
    if url.startswith("postgresql"):
        connect_args["connect_timeout"] = _PG_CONNECT_TIMEOUT_SECONDS

    try:
        engine = create_engine(url, poolclass=NullPool, connect_args=connect_args)
    except Exception:
        return _fail("configuration-failure", EXIT_CONFIG_FAILURE)

    try:
        try:
            conn = engine.connect()
        except Exception:
            return _fail("connection-failure", EXIT_CONNECT_FAILURE)
        try:
            if not inspect(conn).has_table("alembic_version"):
                return _fail("version-table-missing", EXIT_TABLE_MISSING)
            # Collect ALL rows: a reader that stops at the first row cannot
            # distinguish "exactly one" from "one of several".
            rows = (
                conn.execute(text("SELECT version_num FROM alembic_version"))
                .scalars()
                .all()
            )
        except Exception:
            return _fail("unexpected-failure", EXIT_UNEXPECTED)
        finally:
            conn.close()
    finally:
        engine.dispose()

    if len(rows) == 0:
        return _fail("no-revision-rows", EXIT_NO_ROWS)
    if len(rows) > 1:
        return _fail("multiple-revision-rows", EXIT_MULTIPLE_ROWS)
    revision = rows[0]
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        return _fail("malformed-revision", EXIT_MALFORMED_REVISION)

    print(revision)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point. Never lets any exception reach the default excepthook."""
    try:
        return _run(sys.argv[1:] if argv is None else argv)
    except BaseException:  # noqa: BLE001 - fixed classification, never a traceback
        try:
            print("revision-status: unexpected-failure", file=sys.stderr)
        except BaseException:  # pragma: no cover - stderr itself unusable
            pass
        return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())
