"""PostgreSQL sessions run in UTC: pinned at connect, verified on every new connection.

Why
---
psycopg returns a ``timestamptz`` as an aware ``datetime`` in the *session's* time zone.
Python compares and subtracts two aware datetimes that share one ``tzinfo`` as wall-clock
times, ignoring ``fold`` (PEP 495). In a zone with daylight saving time the repeated hour
therefore reorders instants: 01:20 CST (07:20Z) compares as earlier than 01:30 CDT (06:30Z),
and a difference taken across a change is off by 3600 s. The lease and deadline predicates
(``attempt_holds_authority``, ``is_eligible``) and the blocked-Scout ages compare exactly such
values. A UTC session has no fold and no offset change, so each of those comparisons is a
comparison of instants. (Review W21-REV-BLOCKEDOBS r1, NOTE-1.)

What
----
* **Pin.** :func:`utc_connect_args` adds the libpq startup option ``-c timezone=UTC`` after
  any ``options`` already given (in ``connect_args``, else in the URL's query, else in
  ``PGOPTIONS``, which an explicit ``options`` would otherwise replace), so it is the last
  ``-c timezone`` and wins. The server applies it before the first statement. It outranks the
  server default (``postgresql.conf``, ``ALTER SYSTEM``) and ``ALTER DATABASE`` / ``ALTER
  ROLE ... SET timezone``, and ``RESET ALL`` / ``DISCARD ALL`` return to it.
* **Verify (defence in depth).** :func:`enforce_utc_session` installs a ``connect`` listener,
  :func:`refuse_non_utc_session`, that reads the ``TimeZone`` the server reported for the new
  session (psycopg's ParameterStatus: client side, no round trip) and refuses the connection
  with :class:`SessionTimezoneError` unless it is exactly ``UTC``. ``PGTZ`` in the process
  environment outranks the startup option (libpq sends it as a startup parameter of its own,
  which the server applies after ``options``), so a ``PGTZ`` other than ``UTC`` is refused,
  not silently obeyed. A refused connection fails the API's startup schema gate, the worker's
  startup validation, the readiness probe and each CLI's first connection: a **new** session
  that is not in UTC is never used. The check runs when a connection is opened; a
  session-level ``SET TIME ZONE`` committed later would persist in the pool (no tracked code
  issues one).

Every engine that reads or writes application rows is built through here: the process
engine (``app.db.session.build_engine``: the API, the worker and the operator CLIs that use
it), Alembic's online migrations (``alembic/env.py``) and
``app.campaign_context.location_refs``. Not built here, because they read no application row
and no temporal value: ``app.db.migrate``'s revision read-back and downgrade-token engines
(``alembic_version`` and server identity), ``app.db.revision_status`` (``alembic_version``),
``app.db.bootstrap_app_role`` (role DDL; import-isolated by design) and the separate
``apps/revision-reader`` package (``alembic_version``, read-only).

SQLite is untouched: it has no session time zone (its datetimes come back naive, and the
app reads them as UTC).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine, make_url

#: The libpq startup option every PostgreSQL connection carries, last.
UTC_SESSION_OPTION = "-c timezone=UTC"
#: The only ``TimeZone`` a session may report.
SESSION_TIMEZONE = "UTC"


class SessionTimezoneError(RuntimeError):
    """A new PostgreSQL session reported a time zone other than UTC, and was refused."""


def _is_postgres(url: str | URL) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def _given_options(url: str | URL, connect_args: Mapping[str, Any]) -> str | None:
    """The ``options`` libpq would otherwise use, in libpq's own precedence."""
    if connect_args.get("options") is not None:
        return str(connect_args["options"])
    query = make_url(url).query.get("options")
    if query is not None:
        return " ".join(query) if isinstance(query, tuple) else str(query)
    return os.environ.get("PGOPTIONS")


def utc_connect_args(
    url: str | URL, connect_args: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """``connect_args`` for ``create_engine`` with the session time zone pinned to UTC.

    PostgreSQL only; any other backend gets ``connect_args`` back unchanged (as a new dict).
    """
    args = dict(connect_args or {})
    if not _is_postgres(url):
        return args
    given = (_given_options(url, args) or "").strip()
    args["options"] = f"{given} {UTC_SESSION_OPTION}" if given else UTC_SESSION_OPTION
    return args


def reported_timezone(dbapi_connection: Any) -> str | None:
    """The ``TimeZone`` the server reported for this session (psycopg), or ``None``."""
    info = getattr(dbapi_connection, "info", None)
    parameter_status = getattr(info, "parameter_status", None)
    if parameter_status is None:
        return None
    return parameter_status("TimeZone")


def refuse_non_utc_session(dbapi_connection: Any, _connection_record: Any) -> None:
    """``connect`` listener: refuse a new session that is not in UTC.

    Raising here makes SQLAlchemy close the new connection and fail the checkout.
    """
    reported = reported_timezone(dbapi_connection)
    if reported != SESSION_TIMEZONE:
        raise SessionTimezoneError(
            f"PostgreSQL session time zone is {reported!r}, not {SESSION_TIMEZONE!r}. "
            f"Connections are opened with '{UTC_SESSION_OPTION}'; a client setting overrides "
            "it (PGTZ in the environment?). The connection is refused."
        )


def enforce_utc_session(engine: Engine) -> Engine:
    """Install :func:`refuse_non_utc_session` on a PostgreSQL engine (idempotent)."""
    if engine.dialect.name == "postgresql" and not event.contains(
        engine, "connect", refuse_non_utc_session
    ):
        event.listen(engine, "connect", refuse_non_utc_session)
    return engine


def create_utc_engine(url: str | URL, **kwargs: Any) -> Engine:
    """``create_engine`` with the session time zone pinned to UTC and verified (PostgreSQL)."""
    kwargs["connect_args"] = utc_connect_args(url, kwargs.get("connect_args"))
    return enforce_utc_session(create_engine(url, **kwargs))
