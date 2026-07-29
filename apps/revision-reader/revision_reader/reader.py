"""Dedicated live database revision reader — the whole program.

Reads the live Alembic revision from ``public.alembic_version`` and prints it. Nothing else.

WHY THIS EXISTS AS A SEPARATE ARTIFACT (Gate 4J): the application worker image has no
ENTRYPOINT, contains a shell, contains ``app.db.migrate`` (upgrade AND downgrade), and
receives the application ``DATABASE_URL``. A RunTask holder could therefore override the
container command and execute arbitrary code against a database-owner credential. ECS
``ContainerOverride`` exposes {name, command, environment, environmentFiles, cpu, memory,
memoryReservation, resourceRequirements} — it has **no entryPoint member**. So a fixed
exec-form ENTRYPOINT cannot be replaced at RunTask time, and an override ``command``
becomes mere argv to this program, which rejects all argv as its first action.

DELIBERATELY NOT SHARED WITH apps/api. This is a verification INSTRUMENT for the
application's schema state; an instrument that shares code with the thing it measures is
not an independent check. It imports no ``app`` module, no SQLAlchemy, no Alembic, no
pydantic. Standard library plus psycopg only.

DESTINATION AUTHENTICITY (Gate 4J.1): the network configuration of a RunTask call —
subnets, security groups, assignPublicIp — is caller-supplied and has NO IAM condition key,
and the injected ``DATABASE_URL`` environment can be shadowed by a ``containerOverrides``
entry. So NEITHER the network path NOR the DSN's host may be trusted to decide which server
is read. Instead the host, database, and role are BAKED into the image (``_pinned``) and the
connection is made with ``sslmode=verify-full`` against a committed AWS RDS CA bundle. The
DSN is used for exactly one value — the password — and the authority is never parsed for a
destination nor handed to libpq. The AWS RDS CA signs every customer's instance, so
verify-full ALONE would only prove "some RDS server"; the baked host is what says "ours".

HONEST LIMIT (do not soften): these controls bound what CODE exists, what can RUN, and WHICH
server is contacted. They do not bound the CREDENTIAL. While the injected DSN is the
application role — which owns the database and can therefore write and perform DDL —
"read-only" is a property of this program's behaviour, not of the identity it connects as. A
dedicated PostgreSQL role with SELECT on ``alembic_version`` and nothing else is the only
unconditional control, and it requires its own secret under separate authorization.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse

from revision_reader import _pinned

# 12 lowercase hex — this repository's Alembic revision format.
_REVISION_RE = re.compile(r"[0-9a-f]{12}")

# Bounded, so a wedged network fails the task instead of hanging it.
_CONNECT_TIMEOUT_SECONDS = 10
# Overall watchdog: bounds a hang without needing ecs:StopTask (which could only be
# cluster-scoped and would therefore also permit stopping API/worker service tasks).
_STATEMENT_TIMEOUT_MS = 10_000

EXIT_OK = 0
EXIT_ARGV_REJECTED = 50
EXIT_CONFIG_FAILED = 51
EXIT_CONNECT_FAILED = 52
EXIT_TABLE_MISSING = 53
EXIT_NO_ROWS = 54
EXIT_MULTIPLE_ROWS = 55
EXIT_MALFORMED_REVISION = 56
EXIT_UNEXPECTED = 57

_TOKENS = {
    EXIT_ARGV_REJECTED: "READER-ARGV-REJECTED",
    EXIT_CONFIG_FAILED: "READER-CONFIG-FAILED",
    EXIT_CONNECT_FAILED: "READER-CONNECTION-FAILED",
    EXIT_TABLE_MISSING: "READER-VERSION-TABLE-MISSING",
    EXIT_NO_ROWS: "READER-ZERO-REVISIONS",
    EXIT_MULTIPLE_ROWS: "READER-MULTIPLE-REVISIONS",
    EXIT_MALFORMED_REVISION: "READER-REVISION-MALFORMED",
    EXIT_UNEXPECTED: "READER-UNEXPECTED-FAILED",
}

#: The ONLY SQL this program issues. A fixed literal, schema-qualified to ``public`` so a
#: caller who shadows the DSN role (and thus its ``search_path``) cannot steer the read to a
#: different schema's ``alembic_version`` in the same database. Tests assert this module
#: contains exactly one SQL string.
_QUERY = "SELECT version_num FROM public.alembic_version"

#: Fixed TLS destination port. Never taken from the DSN.
_ALLOWED_PORT = 5432

#: The base schemes libpq understands once SQLAlchemy's ``+driver`` suffix is stripped.
#: bootstrap_app_role.py composes ``postgresql+psycopg://…``; libpq rejects the suffix.
_ALLOWED_SCHEMES = frozenset({"postgresql", "postgres"})

#: The DECODED password must be printable ASCII with no control byte. This is checked AFTER
#: percent-decoding, never on the raw DSN: a percent-encoded NUL (``%00``) is invisible to a
#: raw-string check but, once unquoted into a psycopg keyword value, truncates libpq's
#: conninfo at the C ``char*`` boundary — silently dropping every later parameter, including
#: ``sslmode`` and ``sslrootcert`` (a TLS downgrade to libpq's ``prefer`` default). The
#: generated application password is ``secrets.token_urlsafe`` output, wholly within this set.
_PASSWORD_RE = re.compile(r"[\x21-\x7e]{1,256}")

#: The baked expected host must be a plausible lowercase DNS name with at least one dot. The
#: committed sentinel (empty string) fails this and fails the reader closed.
_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))+\Z"
)

#: libpq reads PG* environment variables (PGSSLMODE, PGSSLROOTCERT, PGHOST, PGSERVICE,
#: PGSERVICEFILE, PGPASSFILE, PGHOSTADDR, …) and HOME (its default ``sslrootcert`` is
#: ``~/.postgresql/root.crt`` and default passfile ``~/.pgpass``). ECS ``containerOverrides``
#: can set environment, so all of these are attacker-influencable at RunTask time. They are
#: scrubbed as the reader's FIRST action, before the DSN is even read, so nothing
#: environmental can weaken TLS, redirect the host, or clear the read-only setting.
_SCRUBBED_ENV_PREFIXES = ("PG",)
_SCRUBBED_ENV_EXACT = ("HOME",)


def _fail(code: int) -> int:
    """Emit one fixed classification token to stderr. Never an exception or traceback."""
    print(f"revision-reader: {_TOKENS[code]}", file=sys.stderr)
    return code


def _scrub_connection_environment() -> None:
    """Delete every connection-influencing environment variable. ``del os.environ[k]`` calls
    ``unsetenv`` at the C level, so libpq (loaded later) sees nothing."""
    for key in [k for k in os.environ if k.startswith(_SCRUBBED_ENV_PREFIXES)]:
        del os.environ[key]
    for key in _SCRUBBED_ENV_EXACT:
        os.environ.pop(key, None)


def _password_from_dsn(dsn: str) -> str | None:
    """Return the DECODED, validated password — the ONLY value taken from the DSN — or None.

    The host, port, database and role are baked into the image and never read from the DSN,
    so the authority is never parsed for a destination and never handed to libpq. Rejection
    is deliberately silent about *why*; the caller maps every None to one fixed
    configuration-failure token.
    """
    try:
        parts = urllib.parse.urlsplit(dsn)
    except ValueError:
        return None

    base_scheme = parts.scheme.lower().partition("+")[0]
    if base_scheme not in _ALLOWED_SCHEMES:
        return None

    # Exactly one '@' so the userinfo/host boundary is unambiguous and the password we read
    # is the password libpq would read. compose_database_url percent-encodes credentials, so
    # a literal '@' in a real password arrives as %40 and is not counted.
    if parts.netloc.count("@") != 1:
        return None

    # Reject any bracketed authority outright — never needed for a DNS RDS endpoint, and
    # urlsplit's bracket/host handling has diverged across CPython patch levels (the source
    # of the Gate 4J bracketed-authority bypass). We do not use the host, but refusing the
    # shape keeps the userinfo split unambiguous regardless of interpreter.
    if "[" in parts.netloc or "]" in parts.netloc:
        return None

    raw = parts.password
    if raw is None:
        return None
    # Percent-DECODE first, then gate: a percent-encoded control byte only becomes dangerous
    # after unquote, and only then can it truncate a psycopg keyword value.
    password = urllib.parse.unquote(raw)
    if _PASSWORD_RE.fullmatch(password) is None:
        return None
    return password


def _dsn_host_or_none(dsn: str) -> str | None:
    """Best-effort host for the tamper detector only. Never used to connect."""
    try:
        return urllib.parse.urlsplit(dsn).hostname
    except ValueError:
        return None


def _run(argv: list[str]) -> int:
    # FIRST action: scrub connection-influencing environment (PG* and HOME) before the DSN is
    # read, so a RunTask environment override cannot weaken TLS or redirect the connection.
    _scrub_connection_environment()

    # Under the fixed ENTRYPOINT an override `command` arrives here as argv; reject all of it
    # (exit 50), deliberately NOT merged with the config-failure code.
    if argv:
        return _fail(EXIT_ARGV_REJECTED)

    # The baked pins decide the destination and are unreachable by any RunTask parameter. A
    # placeholder image (unbaked sentinels) fails here and can never certify a head.
    host = _pinned.EXPECTED_DB_HOST
    dbname = _pinned.EXPECTED_DB_NAME
    user = _pinned.EXPECTED_DB_USER
    ca_path = _pinned.CA_BUNDLE_PATH
    if _HOST_RE.fullmatch(host) is None or not dbname or not user:
        return _fail(EXIT_CONFIG_FAILED)
    if not os.path.isfile(ca_path):
        return _fail(EXIT_CONFIG_FAILED)

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        return _fail(EXIT_CONFIG_FAILED)
    password = _password_from_dsn(dsn)
    if password is None:
        return _fail(EXIT_CONFIG_FAILED)

    # TAMPER DETECTOR (evidence quality, NOT the control): the control is that we connect to
    # the baked host regardless of the DSN. But if the DSN names a host at all, it must equal
    # the baked host byte-for-byte (exact ASCII — never case-folded or NFKC-normalised, which
    # collapse distinct hosts). A mismatch means the injected secret was tampered with; fail
    # closed and visibly rather than silently ignoring it.
    dsn_host = _dsn_host_or_none(dsn)
    if dsn_host is not None and dsn_host != host:
        return _fail(EXIT_CONFIG_FAILED)

    try:
        import psycopg
    except Exception:  # noqa: BLE001 — fixed classification, never a traceback
        return _fail(EXIT_CONFIG_FAILED)

    conn = None
    try:
        try:
            conn = psycopg.connect(
                # TLS parameters FIRST: defence in depth on conninfo ordering. host/dbname/
                # user are baked constants; password is the only DSN-derived value and is
                # already gated to printable ASCII, so no value can truncate the conninfo.
                sslmode="verify-full",
                sslrootcert=ca_path,
                host=host,
                port=_ALLOWED_PORT,
                dbname=dbname,
                user=user,
                password=password,
                connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                autocommit=False,
                # Read-only enforced by the SERVER before the first statement, reasserted at
                # session level below. A write then raises server-side.
                options=(
                    "-c default_transaction_read_only=on "
                    f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}"
                ),
            )
        except Exception:  # noqa: BLE001
            return _fail(EXIT_CONNECT_FAILED)

        try:
            conn.read_only = True
        except Exception:  # noqa: BLE001
            return _fail(EXIT_CONNECT_FAILED)

        try:
            with conn.cursor() as cur:
                cur.execute(_QUERY)
                rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            # Distinguish "table absent" without echoing any driver text. psycopg exposes
            # the SQLSTATE; 42P01 is undefined_table.
            if getattr(exc, "sqlstate", None) == "42P01":
                return _fail(EXIT_TABLE_MISSING)
            return _fail(EXIT_UNEXPECTED)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — disposal must never mask the outcome
                pass

    # Complete row collection: a reader that stopped at the first row could not tell
    # "exactly one" from "one of several".
    if len(rows) == 0:
        return _fail(EXIT_NO_ROWS)
    if len(rows) > 1:
        return _fail(EXIT_MULTIPLE_ROWS)

    revision = rows[0][0]
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        return _fail(EXIT_MALFORMED_REVISION)

    # Exactly one line on stdout. No comparison happens here: the expected head never enters
    # the container, so the reader cannot be argued into agreeing with itself.
    print(revision)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point. Never lets any exception reach the default excepthook."""
    try:
        return _run(sys.argv[1:] if argv is None else argv)
    except BaseException:  # noqa: BLE001 — fixed classification, never a traceback
        try:
            print(f"revision-reader: {_TOKENS[EXIT_UNEXPECTED]}", file=sys.stderr)
        except BaseException:  # pragma: no cover — stderr itself unusable
            pass
        return EXIT_UNEXPECTED


def _console_main() -> None:  # pragma: no cover - console-script shim
    """Console-script entry point named by pyproject's [project.scripts]."""
    raise SystemExit(main())


# THE IMAGE ENTRYPOINT DEPENDS ON THIS GUARD. The container runs
# `/usr/bin/python3.11 -m revision_reader.reader`; without it the module would import, do
# nothing, and exit 0 — a reader that always "succeeds" while reading nothing, which is the
# worst possible failure mode for a verification instrument.
if __name__ == "__main__":  # pragma: no cover - exercised by the in-image CI band
    raise SystemExit(main())
