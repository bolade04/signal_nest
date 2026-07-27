"""Zero-handoff, noninteractive bootstrap of the ``signalnest_app`` database role.

This module provisions (or, in the separately authorized recovery mode, re-keys)
the least-privilege application login and writes the resulting connection string
directly into an existing, empty ``DATABASE_URL`` Secrets Manager container. The
generated password never leaves the process except as a driver-bound parameter
and as the ciphertext committed to Secrets Manager: it is never rendered into a
log, an exception message, argv, an environment variable, a temporary file, the
shell, or ``psql`` history. There is no interactive credential handoff.

The module is import-safe: nothing runs at import time, it imports only the
Python standard library plus ``boto3``/``botocore`` and ``psycopg`` (v3), and it
never touches ``app.core.config``, ``app.db.session``, or any module that calls
``get_settings()`` at import time. The sole entry point is :func:`main`, invoked
under ``if __name__ == "__main__": sys.exit(main())``.

Sanitized-output guarantee
--------------------------
All process output is produced by :func:`_emit`, which prints only fixed
constant strings prefixed with ``bootstrap_app_role:``. No secret value,
password, connection URL, hostname, ARN, account id, KMS id, exception object,
``repr``, traceback, or AWS/psycopg response body is ever printed or logged.
Configuration *field names* (never their values) may appear in a fixed
"configuration invalid: <field-name>" line. ``logging.disable`` is engaged before
any other work so no dependency can emit a record.

Exit-code map (stable, unique)
------------------------------
==  ==========================================================================
0   SUCCESS
10  CONFIG_INVALID            missing or malformed environment configuration
11  IDENTITY_MISMATCH         STS account id or client region != expected
12  TARGET_NOT_EMPTY          DATABASE_URL container already has AWSCURRENT (create mode)
13  ROLE_EXISTS               signalnest_app already present (create mode; report-only)
14  MASTER_SECRET_FAILURE     master secret access denied / not found / bad JSON shape
15  DB_CONNECT_FAILURE        master TLS connect or session-setup guard failed
16  ROLE_OR_OWNERSHIP_FAILURE role/ownership/metadata step failed before any write
17  SECRET_WRITE_FAILURE      role committed but PutSecretValue failed -> RECOVERY REQUIRED
18  VERIFY_FAILURE            secret written but new-credential round trip failed -> RECOVERY
19  CLEANUP_FAILURE           operation succeeded but a resource failed to close
20  RECOVERY_PRECONDITION     mode=recover but role absent
==  ==========================================================================

Exit codes 17 and 18 indicate that the database role was committed but the
end-to-end state is inconsistent; they require the separately authorized
recovery mode (``SN_BOOTSTRAP_MODE=recover``) to reconcile. Recovery is never
invoked automatically.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import urllib.parse

import boto3
import psycopg
from botocore.exceptions import BotoCoreError, ClientError
from psycopg import ClientCursor, sql

# --- Exit codes ----------------------------------------------------------------

EXIT_SUCCESS = 0
EXIT_CONFIG_INVALID = 10
EXIT_IDENTITY_MISMATCH = 11
EXIT_TARGET_NOT_EMPTY = 12
EXIT_ROLE_EXISTS = 13
EXIT_MASTER_SECRET_FAILURE = 14
EXIT_DB_CONNECT_FAILURE = 15
EXIT_ROLE_OR_OWNERSHIP_FAILURE = 16
EXIT_SECRET_WRITE_FAILURE = 17
EXIT_VERIFY_FAILURE = 18
EXIT_CLEANUP_FAILURE = 19
EXIT_RECOVERY_PRECONDITION = 20

# --- Constants -----------------------------------------------------------------

# The role name is a fixed identifier, not a physical secret identifier, so a
# module constant (rather than configuration) is approved by the contract.
APP_ROLE_NAME = "signalnest_app"

_EMIT_PREFIX = "bootstrap_app_role:"

# Non-secret environment variable names. Values are read from os.environ and are
# never printed; only these NAMES may appear in a "configuration invalid" line.
_ENV_MASTER_SECRET_ARN = "SN_BOOTSTRAP_MASTER_SECRET_ARN"
_ENV_TARGET_SECRET_ARN = "SN_BOOTSTRAP_TARGET_SECRET_ARN"
_ENV_DB_HOST = "SN_BOOTSTRAP_DB_HOST"
_ENV_DB_PORT = "SN_BOOTSTRAP_DB_PORT"
_ENV_DB_NAME = "SN_BOOTSTRAP_DB_NAME"
_ENV_EXPECTED_ACCOUNT_ID = "SN_BOOTSTRAP_EXPECTED_ACCOUNT_ID"
_ENV_EXPECTED_REGION = "SN_BOOTSTRAP_EXPECTED_REGION"
_ENV_MODE = "SN_BOOTSTRAP_MODE"

_DEFAULT_DB_PORT = "5432"
_MODE_CREATE = "create"
_MODE_RECOVER = "recover"

_DB_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_ACCOUNT_ID_RE = re.compile(r"^[0-9]{12}$")

# TLS connect timeout (seconds) for the master and round-trip connections.
_CONNECT_TIMEOUT_SECONDS = 10

# Password entropy: token_urlsafe(48) yields a 48-byte, URL-safe secret.
_PASSWORD_NBYTES = 48


class _ConfigError(Exception):
    """Raised by parse_config with a fixed, non-secret field name in ``args[0]``."""


def _emit(message: str) -> None:
    """Print a single fixed constant status string.

    ``message`` MUST be a compile-time constant string (optionally a config
    field NAME). It must never contain a secret value, ARN, hostname, account
    id, exception text, or response body.
    """
    print(f"{_EMIT_PREFIX} {message}")


def parse_config() -> dict[str, object]:
    """Validate presence and format of every environment input.

    Returns a dict of validated, non-secret configuration. Raises
    :class:`_ConfigError` whose ``args[0]`` is the fixed field NAME (never the
    value) on the first missing/malformed field.
    """
    master_secret_arn = os.environ.get(_ENV_MASTER_SECRET_ARN)
    if not master_secret_arn:
        raise _ConfigError(_ENV_MASTER_SECRET_ARN)

    target_secret_arn = os.environ.get(_ENV_TARGET_SECRET_ARN)
    if not target_secret_arn:
        raise _ConfigError(_ENV_TARGET_SECRET_ARN)

    db_host = os.environ.get(_ENV_DB_HOST)
    if not db_host:
        raise _ConfigError(_ENV_DB_HOST)

    db_port_raw = os.environ.get(_ENV_DB_PORT, _DEFAULT_DB_PORT)
    try:
        db_port = int(db_port_raw)
    except (TypeError, ValueError) as exc:
        raise _ConfigError(_ENV_DB_PORT) from exc
    if not (1 <= db_port <= 65535):
        raise _ConfigError(_ENV_DB_PORT)

    db_name = os.environ.get(_ENV_DB_NAME)
    if not db_name or not _DB_NAME_RE.match(db_name):
        raise _ConfigError(_ENV_DB_NAME)

    expected_account_id = os.environ.get(_ENV_EXPECTED_ACCOUNT_ID)
    if not expected_account_id or not _ACCOUNT_ID_RE.match(expected_account_id):
        raise _ConfigError(_ENV_EXPECTED_ACCOUNT_ID)

    expected_region = os.environ.get(_ENV_EXPECTED_REGION)
    if not expected_region:
        raise _ConfigError(_ENV_EXPECTED_REGION)

    mode = os.environ.get(_ENV_MODE, _MODE_CREATE)
    if mode not in (_MODE_CREATE, _MODE_RECOVER):
        raise _ConfigError(_ENV_MODE)

    return {
        "master_secret_arn": master_secret_arn,
        "target_secret_arn": target_secret_arn,
        "db_host": db_host,
        "db_port": db_port,
        "db_name": db_name,
        "expected_account_id": expected_account_id,
        "expected_region": expected_region,
        "mode": mode,
    }


def validate_identity(config: dict[str, object]) -> bool:
    """Compare the STS caller account and client region to the expected values.

    Returns ``True`` on match, ``False`` on mismatch. Any AWS error is treated
    as a mismatch (fail closed) so the caller maps it to IDENTITY_MISMATCH.
    """
    expected_region = str(config["expected_region"])
    client = boto3.client("sts", region_name=expected_region)
    try:
        identity = client.get_caller_identity()
    except (BotoCoreError, ClientError):
        return False

    account = identity.get("Account")
    if account != config["expected_account_id"]:
        return False

    # The effective region of the client must equal the expected region; the
    # client was constructed with region_name=expected_region above, so a
    # divergence here indicates an environment override we must not trust.
    client_region = client.meta.region_name
    return client_region == expected_region


def check_target_empty(config: dict[str, object]) -> bool:
    """Return ``True`` iff the target container has no AWSCURRENT version staged.

    Uses DescribeSecret (metadata only); the secret value is never read here.
    An access/lookup error is treated as "not empty" (fail closed) so the caller
    does not proceed to write into an unknown container.
    """
    client = boto3.client("secretsmanager", region_name=str(config["expected_region"]))
    try:
        described = client.describe_secret(SecretId=str(config["target_secret_arn"]))
    except (BotoCoreError, ClientError):
        return False

    version_stages = described.get("VersionIdsToStages") or {}
    for stages in version_stages.values():
        if "AWSCURRENT" in stages:
            return False
    return True


def fetch_master_credentials(config: dict[str, object]) -> tuple[str, str]:
    """Fetch and parse the RDS-managed master secret as ``(username, password)``.

    Raises :class:`ValueError` if the payload is missing, not JSON, or lacks the
    required ``username``/``password`` fields. The caller maps any failure to
    MASTER_SECRET_FAILURE. No part of the payload is logged.
    """
    client = boto3.client("secretsmanager", region_name=str(config["expected_region"]))
    response = client.get_secret_value(SecretId=str(config["master_secret_arn"]))
    payload = response.get("SecretString")
    if not payload:
        raise ValueError("master secret has no SecretString")

    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("master secret is not a JSON object")

    username = data.get("username")
    password = data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("master secret missing username/password")
    return username, password


def connect_master(
    config: dict[str, object], username: str, password: str
) -> psycopg.Connection:
    """Open a TLS master connection and install the credential-logging guard.

    Connects with ``sslmode=require`` and a bounded ``connect_timeout``, then -
    BEFORE any credential-bearing statement runs on this connection - issues
    ``SET log_statement TO 'none'`` and ``SET log_min_duration_statement TO -1``
    so no subsequent role statement can be captured by server logging. Any
    failure (connect or guard) propagates; the caller maps it to
    DB_CONNECT_FAILURE.
    """
    conn = psycopg.connect(
        host=str(config["db_host"]),
        port=int(config["db_port"]),
        dbname=str(config["db_name"]),
        user=username,
        password=password,
        sslmode="require",
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        autocommit=True,
    )
    # Session guard: silence statement logging for this session before any
    # password-bearing utility statement is executed.
    with conn.cursor() as cur:
        cur.execute("SET log_statement TO 'none'")
        cur.execute("SET log_min_duration_statement TO -1")
    return conn


def role_exists(conn: psycopg.Connection, role_name: str = APP_ROLE_NAME) -> bool:
    """Return whether ``role_name`` exists, via a parameterized pg_roles query."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        return cur.fetchone() is not None


def generate_password() -> str:
    """Return a fresh 48-byte URL-safe password (``secrets.token_urlsafe``)."""
    return secrets.token_urlsafe(_PASSWORD_NBYTES)


def create_app_role(conn: psycopg.Connection, password: str) -> None:
    """Create the least-privilege application login.

    The password is bound as a client-side ``%s`` parameter through
    :class:`psycopg.ClientCursor`. Utility statements such as ``CREATE ROLE``
    accept no server-side parameters, so ClientCursor performs the escaping and
    quoting locally; the password is NEVER interpolated via f-string, ``format``,
    or concatenation. The role identifier is composed with
    :class:`psycopg.sql.Identifier`. Attribute clause is fully restrictive:
    ``LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS``.
    """
    statement = sql.SQL(
        "CREATE ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS PASSWORD %s"
    ).format(role=sql.Identifier(APP_ROLE_NAME))
    with ClientCursor(conn) as cur:
        cur.execute(statement, (password,))


def recover_app_role(conn: psycopg.Connection, password: str) -> None:
    """Re-key the existing application login (recovery mode only).

    Same client-side ``%s`` password binding discipline as
    :func:`create_app_role`, but issues ``ALTER ROLE ... WITH PASSWORD`` against
    the already-existing role. Never auto-invoked.
    """
    statement = sql.SQL("ALTER ROLE {role} WITH PASSWORD %s").format(
        role=sql.Identifier(APP_ROLE_NAME)
    )
    with ClientCursor(conn) as cur:
        cur.execute(statement, (password,))


def transfer_ownership(conn: psycopg.Connection, db_name: str) -> None:
    """Set the target database owner to the application role.

    Both the database name and the role name are composed with
    :class:`psycopg.sql.Identifier`; no value is interpolated as text.
    """
    statement = sql.SQL("ALTER DATABASE {db} OWNER TO {role}").format(
        db=sql.Identifier(db_name),
        role=sql.Identifier(APP_ROLE_NAME),
    )
    with conn.cursor() as cur:
        cur.execute(statement)


def verify_role_metadata(conn: psycopg.Connection, db_name: str) -> bool:
    """Confirm the role attributes and database ownership before any secret write.

    Requires every ``pg_roles`` privilege attribute to be false except
    ``rolcanlogin`` (which must be true), and the ``pg_database`` owner of
    ``db_name`` to be the application role. Returns ``True`` iff both hold.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, "
            "rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = %s",
            (APP_ROLE_NAME,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolbypassrls = row
        if rolsuper or rolcreaterole or rolcreatedb or rolreplication or rolbypassrls:
            return False
        if not rolcanlogin:
            return False

        cur.execute(
            "SELECT 1 FROM pg_database d JOIN pg_roles r ON d.datdba = r.oid "
            "WHERE d.datname = %s AND r.rolname = %s",
            (db_name, APP_ROLE_NAME),
        )
        return cur.fetchone() is not None


def compose_database_url(config: dict[str, object], password: str) -> str:
    """Build the ``postgresql+psycopg://`` URL with defensive percent-encoding.

    Both the (constant) username and the generated password are passed through
    ``urllib.parse.quote(..., safe="")`` so any reserved character cannot break
    URL structure. The resulting string is secret-bearing and is only ever
    written to Secrets Manager or bound as a driver parameter - never logged.
    """
    user = urllib.parse.quote(APP_ROLE_NAME, safe="")
    pw = urllib.parse.quote(password, safe="")
    host = str(config["db_host"])
    port = int(config["db_port"])
    db_name = str(config["db_name"])
    return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db_name}?sslmode=require"


def write_target_secret(config: dict[str, object], database_url: str) -> bool:
    """Write the composed URL as a new version of the target secret.

    Issues exactly one ``put_secret_value``. Returns ``True`` iff the response
    carries a ``VersionId`` and lists ``AWSCURRENT`` in ``VersionStages``. The
    response body is never logged. The target secret is NOT read back afterward.
    """
    client = boto3.client("secretsmanager", region_name=str(config["expected_region"]))
    response = client.put_secret_value(
        SecretId=str(config["target_secret_arn"]),
        SecretString=database_url,
    )
    version_id = response.get("VersionId")
    version_stages = response.get("VersionStages") or []
    return bool(version_id) and "AWSCURRENT" in version_stages


def verify_round_trip(config: dict[str, object], password: str) -> bool:
    """Open a fresh, independent connection as the app role and run ``SELECT 1``.

    Uses a brand-new ``psycopg.connect`` (not the master connection) with
    ``sslmode=require``. Returns ``True`` iff the round trip succeeds.
    """
    conn = None
    try:
        conn = psycopg.connect(
            host=str(config["db_host"]),
            port=int(config["db_port"]),
            dbname=str(config["db_name"]),
            user=APP_ROLE_NAME,
            password=password,
            sslmode="require",
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            autocommit=True,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
        return row is not None and row[0] == 1
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


def main() -> int:
    """Run the bootstrap and return a fixed exit code (see module docstring).

    Logging is disabled first so no dependency can emit a record. Each phase is
    guarded by try/except that maps any failure to a fixed exit code without
    surfacing the underlying value or exception text.
    """
    logging.disable(logging.CRITICAL)

    # Phase 1: configuration.
    try:
        config = parse_config()
    except _ConfigError as exc:
        _emit(f"configuration invalid: {exc.args[0]}")
        return EXIT_CONFIG_INVALID
    except Exception:
        _emit("configuration invalid")
        return EXIT_CONFIG_INVALID

    mode = str(config["mode"])

    # Phase 2: identity.
    try:
        identity_ok = validate_identity(config)
    except Exception:
        identity_ok = False
    if not identity_ok:
        _emit("identity check: mismatch")
        return EXIT_IDENTITY_MISMATCH
    _emit("identity check: match")

    # Phase 3: target-empty precondition (create mode only).
    if mode == _MODE_CREATE:
        try:
            target_empty = check_target_empty(config)
        except Exception:
            target_empty = False
        if not target_empty:
            _emit("target secret not empty")
            return EXIT_TARGET_NOT_EMPTY

    # Phase 4: master credentials.
    try:
        master_user, master_pw = fetch_master_credentials(config)
    except Exception:
        _emit("master secret failure")
        return EXIT_MASTER_SECRET_FAILURE

    conn: psycopg.Connection | None = None
    password: str | None = None
    database_url: str | None = None
    # ``result`` holds the exit code determined by phases 5-9. CLEANUP_FAILURE
    # (phase 10) may only override a SUCCESS result: a prior failure code
    # (15-18) must survive a subsequent close error so recovery signalling is
    # never masked.
    result = EXIT_SUCCESS
    try:
        # Phase 5: master connection + session guard.
        try:
            conn = connect_master(config, master_user, master_pw)
        except Exception:
            _emit("database connect failure")
            result = EXIT_DB_CONNECT_FAILURE
            return result
        finally:
            # The master password reference is no longer needed once the
            # connection is established (or has failed). Python strings are
            # immutable, so this only drops our reference; the value is not
            # zeroed in memory.
            master_pw = ""

        # Phase 6: role existence / mode precondition.
        try:
            exists = role_exists(conn)
        except Exception:
            _emit("role or ownership failure")
            result = EXIT_ROLE_OR_OWNERSHIP_FAILURE
            return result

        if mode == _MODE_CREATE and exists:
            _emit("role already exists")
            result = EXIT_ROLE_EXISTS
            return result
        if mode == _MODE_RECOVER and not exists:
            _emit("recovery precondition failed")
            result = EXIT_RECOVERY_PRECONDITION
            return result

        # Phase 7: password + role create/alter + ownership + metadata check.
        try:
            password = generate_password()
            if mode == _MODE_RECOVER:
                recover_app_role(conn, password)
            else:
                create_app_role(conn, password)
            transfer_ownership(conn, str(config["db_name"]))
            metadata_ok = verify_role_metadata(conn, str(config["db_name"]))
        except Exception:
            _emit("role or ownership failure")
            result = EXIT_ROLE_OR_OWNERSHIP_FAILURE
            return result
        if not metadata_ok:
            _emit("role or ownership failure")
            result = EXIT_ROLE_OR_OWNERSHIP_FAILURE
            return result

        # Phase 8: compose URL + write target secret. From here on the role is
        # committed, so a write failure requires recovery.
        try:
            database_url = compose_database_url(config, password)
            write_ok = write_target_secret(config, database_url)
        except Exception:
            _emit("secret write failure: recovery required")
            result = EXIT_SECRET_WRITE_FAILURE
            return result
        if not write_ok:
            _emit("secret write failure: recovery required")
            result = EXIT_SECRET_WRITE_FAILURE
            return result

        # Phase 9: independent new-credential round trip.
        try:
            verified = verify_round_trip(config, password)
        except Exception:
            verified = False
        if not verified:
            _emit("verify failure: recovery required")
            result = EXIT_VERIFY_FAILURE
            return result

    finally:
        # Phase 10: cleanup. Close the master connection and drop secret-bearing
        # references. Python strings are immutable, so deleting the names only
        # releases our references; nothing is scrubbed from memory.
        password = None
        database_url = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                # A close error is only material when the operation otherwise
                # succeeded; a prior failure code (15-18) must not be masked by
                # CLEANUP_FAILURE. This return runs inside finally and therefore
                # overrides the return value from the try body, so it is guarded
                # to fire only on an otherwise-successful result.
                if result == EXIT_SUCCESS:
                    _emit("cleanup failure")
                    return EXIT_CLEANUP_FAILURE

    _emit("success")
    return EXIT_SUCCESS


if __name__ == "__main__":
    # Belt-and-suspenders: nothing may ever reach the default excepthook, so no
    # traceback of any kind can be emitted (KeyboardInterrupt and I/O failures
    # inside print() do not pass through main()'s Exception guards). Not a new
    # documented failure mode; re-uses the cleanup-failure classification.
    try:
        _code = main()
    except BaseException:  # noqa: B036 - deliberate traceback suppression
        _code = EXIT_CLEANUP_FAILURE
    sys.exit(_code)
