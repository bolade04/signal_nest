"""Dedicated live database revision reader — the whole program.

Reads the live Alembic revision from ``alembic_version`` and prints it. Nothing else.

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
not an independent check — a defect in the shared module would corrupt the measurement and
the measured object identically. It imports no ``app`` module, no SQLAlchemy, no Alembic,
no pydantic. Standard library plus psycopg only.

HONEST LIMIT (do not soften): these controls bound what CODE exists and what can RUN. They
do not bound the CREDENTIAL. While the injected DSN is the application role — which owns
the database and can therefore write and perform DDL — "read-only" is a property of this
program's behaviour, not of the identity it connects as. A dedicated PostgreSQL role with
SELECT on ``alembic_version`` and nothing else is the only unconditional control, and it
requires its own secret under separate authorization.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse

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

#: The ONLY SQL this program issues. A fixed literal: no interpolation, no parameters,
#: no user input reaches it. Tests assert this module contains exactly one SQL string.
_QUERY = "SELECT version_num FROM alembic_version"

#: libpq reads PG* environment variables (PGOPTIONS, PGSSLMODE, PGHOST, …). ECS
#: containerOverrides CAN set environment, so those variables are attacker-influencable
#: at RunTask time. Scrub them all before connecting and pass every parameter explicitly,
#: so nothing environmental can weaken TLS or clear the read-only setting.
_PG_ENV_PREFIX = "PG"

#: The application writes DATABASE_URL with SQLAlchemy's driver-suffixed scheme
#: (bootstrap_app_role.py composes ``postgresql+psycopg://…``). libpq does NOT understand
#: that suffix, so it must be stripped — without this the reader cannot connect to the
#: real database at all. The base scheme is still allowlisted afterwards.
_ALLOWED_SCHEMES = frozenset({"postgresql", "postgres"})

#: sslmode values that actually require TLS. Checked as an exact parsed value rather than
#: a substring: ``sslmode=requireXXX`` contains "sslmode=require" but is not a TLS
#: guarantee, and ``prefer``/``allow``/``disable`` must never pass.
_TLS_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})

#: THE PORT PIN, and why it is a security control rather than tidiness.
#:
#: ``containerOverrides.environment`` is caller-settable at RunTask time, so a caller who
#: can reach RunTask may be able to supply a DATABASE_URL naming a host they control. The
#: reader's security group permits egress to 0.0.0.0/0 on 443 (ECR pull, Secrets Manager,
#: CloudWatch Logs — unavoidable without VPC endpoints), and the PostgreSQL wire protocol
#: does not care which port it runs on: a redirected DSN pointing at ``attacker:443``
#: would otherwise be reachable, could answer the one query, and would make the reader
#: report an attacker-chosen revision with exit 0. That defeats the VERIFICATION without
#: ever touching the entrypoint hardening.
#:
#: Pinning the port composes with the security group to close that path: outbound 5432 is
#: permitted ONLY to the RDS security group, so a DSN restricted to 5432 can only reach
#: the intended database. This lives in the image — which is digest-pinned and has no
#: override channel — precisely because the environment does.
_ALLOWED_PORT = 5432


def _fail(code: int) -> int:
    """Emit one fixed classification token. Never an exception message or traceback."""
    print(f"revision-reader: {_TOKENS[code]}", file=sys.stderr)
    return code


def _scrub_pg_environment() -> None:
    for key in [k for k in os.environ if k.startswith(_PG_ENV_PREFIX)]:
        del os.environ[key]


def _normalise_dsn(dsn: str) -> str | None:
    """Validate the DSN and return a libpq-acceptable form, or ``None`` to reject.

    Rejection is deliberately silent about *why*: the caller maps every ``None`` to one
    fixed configuration-failure token, so a probing caller learns nothing about which
    constraint they tripped.
    """
    try:
        parts = urllib.parse.urlsplit(dsn)
    except ValueError:
        return None

    # ``postgresql+psycopg`` -> ``postgresql``. Only the driver suffix is dropped; an
    # unrelated scheme still fails the allowlist below.
    base_scheme = parts.scheme.lower().partition("+")[0]
    if base_scheme not in _ALLOWED_SCHEMES:
        return None

    if not parts.hostname:
        return None

    # THE AUTHORITY MUST DENOTE EXACTLY ONE HOST, UNAMBIGUOUSLY. Both checks below exist
    # because this program and libpq parse the authority independently, and every place
    # they could disagree is a place where one destination is validated and a different
    # one is connected to.
    #
    # '@' — each parser decides where userinfo ends by locating an '@'. Rather than reason
    # about whose convention wins, refuse more than one. This cannot reject a legitimate
    # DSN: bootstrap_app_role.compose_database_url percent-encodes both credentials with
    # quote(safe=""), so a real '@' inside a password arrives as %40 and is not counted.
    if parts.netloc.count("@") != 1:
        return None
    # ',' — libpq accepts MULTI-HOST URIs and tries each host in turn, but urlsplit knows
    # nothing about that syntax. It reports the whole comma-joined string as `hostname`,
    # and takes everything after the FIRST colon as the port (CPython's `_hostinfo` uses
    # `partition(':')`, not `rpartition`). So a single trailing port on a comma-joined
    # authority reads as a clean 5432, and the portless form reads as unspecified: BOTH
    # satisfy the port pin while libpq would try the attacker's host first. Only the
    # two-colon shape was ever caught, and only because "443,db:5432" fails int() — an
    # accident, not a guard. The port pin alone was never sufficient here; rejecting ','
    # is what makes the single-destination guarantee total.
    if "," in parts.netloc:
        return None
    # '%' in the HOST — the same ambiguity one decoding-stage later. urlsplit does NOT
    # percent-decode the host, so the two checks above see `evil.invalid%2Cdb.invalid` as
    # a single comma-free hostname and `db.invalid%3A443` as having no port at all. libpq
    # DOES percent-decode URI components, so if it decodes before splitting on ',' those
    # become a multi-host list and a redirected port respectively. Which side of the split
    # libpq decodes on is not something this program can establish, so it refuses the
    # question: a legitimate RDS endpoint is plain ASCII letters, digits, dots and hyphens
    # and never needs an escape. Credentials are unaffected — they live in the netloc but
    # not in `hostname`, so compose_database_url's quote(safe="") output still passes.
    if "%" in parts.hostname:
        return None

    try:
        port = parts.port  # raises ValueError on a non-numeric or out-of-range port
    except ValueError:
        return None
    # None means "unspecified", which libpq resolves to 5432 — the same destination the
    # security group permits, so it is accepted rather than requiring a redundant literal.
    if port is not None and port != _ALLOWED_PORT:
        return None

    # A fragment is not part of a libpq URI, so anything after '#' would be interpreted
    # differently by the two parsers. Refuse rather than guess.
    if parts.fragment:
        return None

    # THE QUERY STRING IS AN ALLOWLIST, and this is load-bearing rather than fastidious.
    # A libpq URI accepts connection KEYWORDS in its query string, so checking only the
    # port in its positional slot is not enough: `?host=evil&port=443` would satisfy a
    # positional check while libpq connected somewhere else entirely. `service=` and
    # `passfile=` can pull an entire connection definition in from a file, and `options=`
    # can push server settings. The reader needs exactly one parameter, so permit exactly
    # one and refuse every other keyword outright.
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if set(query) != {"sslmode"}:
        return None
    ssl_values = query["sslmode"]
    if len(ssl_values) != 1 or ssl_values[0] not in _TLS_SSLMODES:
        return None

    return urllib.parse.urlunsplit((base_scheme, parts.netloc, parts.path, parts.query, ""))


def _run(argv: list[str]) -> int:
    # FIRST action, before reading any environment and before connecting. Under the fixed
    # ENTRYPOINT an override `command` arrives here as argv, so this is the point at which
    # an attempted command override is refused — and exit 50 is its distinct fingerprint,
    # deliberately NOT merged with the config-failure code.
    if argv:
        return _fail(EXIT_ARGV_REJECTED)

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.strip():
        return _fail(EXIT_CONFIG_FAILED)
    # Scheme, host, PORT and TLS are all ASSERTED here rather than trusted from
    # provisioning discipline — because the environment this DSN arrives in is exactly
    # the channel a RunTask caller can influence. See _ALLOWED_PORT for why the port
    # constraint is the one that closes the redirect path.
    dsn = _normalise_dsn(dsn.strip())
    if dsn is None:
        return _fail(EXIT_CONFIG_FAILED)

    _scrub_pg_environment()

    try:
        import psycopg
    except Exception:  # noqa: BLE001 — fixed classification, never a traceback
        return _fail(EXIT_CONFIG_FAILED)

    conn = None
    try:
        try:
            conn = psycopg.connect(
                dsn,
                connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                autocommit=False,
                # Read-only enforced by the SERVER, before the first statement runs, and
                # reasserted at session level below. A write then raises server-side
                # rather than relying on this program's restraint.
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

    # Exactly one line on stdout. No comparison happens here: the expected head never
    # enters the container, so the reader cannot be argued into agreeing with itself.
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
# `/usr/bin/python3.11 -m revision_reader.reader`; without it the module would import,
# do nothing, and exit 0 — a reader that always "succeeds" while reading nothing, which
# is the worst possible failure mode for a verification instrument.
if __name__ == "__main__":  # pragma: no cover - exercised by the in-image CI band
    raise SystemExit(main())
