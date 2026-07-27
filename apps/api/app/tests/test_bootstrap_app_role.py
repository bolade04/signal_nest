"""Unit tests for the zero-handoff ``signalnest_app`` bootstrap module.

Every test here runs entirely in-process: there are no real AWS calls and no
real database connections. ``boto3.client`` and ``psycopg.connect`` are patched
on the module under test, and ``ClientCursor`` is replaced with a recording
double so the exact SQL template and bound parameters can be inspected without a
server. All identifiers, ARNs, hosts, accounts and passwords are FABRICATED
(RFC 5737 / documentation-style placeholders); nothing addresses a real
resource.

The suite proves the two properties the contract cares about most: the fixed,
unique exit-code map for every failure phase, and the output-hygiene guarantee
that no secret value, URL, hostname, ARN, account id or exception text is ever
emitted to stdout/stderr/logs - only fixed ``bootstrap_app_role:`` lines.
"""

from __future__ import annotations

import importlib
import json
import sys
import urllib.parse

import pytest

import app.db.bootstrap_app_role as bar

# --- Fabricated, non-real fixtures --------------------------------------------

# RFC 5737 / documentation placeholders. None of these address a real resource.
FAKE_ACCOUNT = "111122223333"
FAKE_REGION = "us-east-1"
FAKE_HOST = "db.invalid"
FAKE_PORT = "5432"
FAKE_DB = "signalnest"
FAKE_MASTER_ARN = (
    "arn:aws:secretsmanager:us-east-1:111122223333:secret:master-EXAMPLE"
)
FAKE_TARGET_ARN = (
    "arn:aws:secretsmanager:us-east-1:111122223333:secret:target-EXAMPLE"
)
FAKE_MASTER_USER = "sn_master"
FAKE_MASTER_PW = "master-pw-EXAMPLE"
# A deliberately reserved-character-laden password to exercise URL quoting.
FAKE_APP_PW = "p@ss:w/rd?#[]&= EXAMPLE+"

# Every fabricated value that must NEVER appear in emitted output.
SECRET_STRINGS = (
    FAKE_MASTER_ARN,
    FAKE_TARGET_ARN,
    FAKE_HOST,
    FAKE_ACCOUNT,
    FAKE_MASTER_PW,
    FAKE_APP_PW,
    FAKE_MASTER_USER,
)


def _base_env() -> dict[str, str]:
    return {
        bar._ENV_MASTER_SECRET_ARN: FAKE_MASTER_ARN,
        bar._ENV_TARGET_SECRET_ARN: FAKE_TARGET_ARN,
        bar._ENV_DB_HOST: FAKE_HOST,
        bar._ENV_DB_PORT: FAKE_PORT,
        bar._ENV_DB_NAME: FAKE_DB,
        bar._ENV_EXPECTED_ACCOUNT_ID: FAKE_ACCOUNT,
        bar._ENV_EXPECTED_REGION: FAKE_REGION,
    }


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    # Clear every managed variable first so leakage from the ambient
    # environment cannot make a "missing field" test pass spuriously.
    for name in (
        bar._ENV_MASTER_SECRET_ARN,
        bar._ENV_TARGET_SECRET_ARN,
        bar._ENV_DB_HOST,
        bar._ENV_DB_PORT,
        bar._ENV_DB_NAME,
        bar._ENV_EXPECTED_ACCOUNT_ID,
        bar._ENV_EXPECTED_REGION,
        bar._ENV_MODE,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def _config(**overrides: object) -> dict[str, object]:
    cfg: dict[str, object] = {
        "master_secret_arn": FAKE_MASTER_ARN,
        "target_secret_arn": FAKE_TARGET_ARN,
        "db_host": FAKE_HOST,
        "db_port": int(FAKE_PORT),
        "db_name": FAKE_DB,
        "expected_account_id": FAKE_ACCOUNT,
        "expected_region": FAKE_REGION,
        "mode": bar._MODE_CREATE,
    }
    cfg.update(overrides)
    return cfg


# --- Recording doubles --------------------------------------------------------


class FakeCursor:
    """Records executed statements and answers metadata queries from a plan.

    ``rows`` maps a substring of the executed SQL text to the ``fetchone`` row
    that query should return. This lets one connection double serve role-exists,
    metadata and round-trip probes without a real server.
    """

    def __init__(self, recorder: list[tuple[str, object]], rows: dict[str, object]):
        self._recorder = recorder
        self._rows = rows
        self._last: object = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def _render(self, statement: object) -> str:
        # Composed/SQL render deterministically with %s placeholders intact when
        # given a None context; plain strings pass through unchanged.
        if hasattr(statement, "as_string"):
            return statement.as_string(None)
        return str(statement)

    def execute(self, statement: object, params: object = None) -> None:
        text = self._render(statement)
        self._recorder.append((text, params))
        # Choose the most specific (longest) matching needle so that, e.g., the
        # attribute SELECT (which also contains "FROM pg_roles WHERE rolname")
        # resolves to its own row rather than the role-exists probe's row.
        self._last = None
        best_len = -1
        for needle, row in self._rows.items():
            if needle in text and len(needle) > best_len:
                self._last = row
                best_len = len(needle)

    def fetchone(self) -> object:
        return self._last


class FakeConnection:
    """A psycopg-connection double that hands out :class:`FakeCursor` objects."""

    def __init__(
        self,
        recorder: list[tuple[str, object]],
        rows: dict[str, object],
        close_error: bool = False,
    ):
        self._recorder = recorder
        self._rows = rows
        self._close_error = close_error
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._recorder, self._rows)

    def close(self) -> None:
        if self._close_error:
            raise RuntimeError("close failed")
        self.closed = True


class FakeClientCursor(FakeCursor):
    """Stands in for :class:`psycopg.ClientCursor`.

    ``ClientCursor(conn)`` is constructed positionally in the module, so this
    double accepts the connection and reuses its recorder/plan. It records the
    rendered *template* (with ``%s`` intact) and the params separately, exactly
    as the real client cursor would receive them before local binding.
    """

    def __init__(self, conn: FakeConnection):
        super().__init__(conn._recorder, conn._rows)


# Metadata plan for a correctly-provisioned, least-privilege role. Keys are
# distinctive substrings of each query so the longest-match rule in FakeCursor
# resolves the attribute SELECT, the role-exists probe, the ownership join and
# the round-trip probe to their own rows without cross-matching.
_ROLE_EXISTS_KEY = "SELECT 1 FROM pg_roles WHERE rolname"
_ATTR_KEY = "rolsuper, rolcreaterole"
_OWNER_KEY = "pg_database d JOIN pg_roles r"
_ROUNDTRIP_KEY = "SELECT 1"

_GOOD_ROWS = {
    _ROLE_EXISTS_KEY: (1,),  # role_exists -> present
    _ATTR_KEY: (False, False, False, True, False, False),  # attributes ok
    _OWNER_KEY: (1,),  # ownership ok
    _ROUNDTRIP_KEY: (1,),  # round-trip probe
}


def _rows_for_create() -> dict[str, object]:
    # In create mode role_exists must report absent; metadata/ownership/round
    # trip still succeed once the role has (notionally) been created.
    rows = dict(_GOOD_ROWS)
    rows[_ROLE_EXISTS_KEY] = None
    return rows


class FakeSecretsClient:
    """Records Secrets Manager calls and returns scripted responses."""

    def __init__(self, plan: dict[str, object]):
        self._plan = plan
        self.describe_calls: list[str] = []
        self.get_calls: list[str] = []
        self.put_calls: list[tuple[str, str]] = []

    def describe_secret(self, SecretId: str) -> dict[str, object]:  # noqa: N803
        self.describe_calls.append(SecretId)
        value = self._plan.get("describe")
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]

    def get_secret_value(self, SecretId: str) -> dict[str, object]:  # noqa: N803
        self.get_calls.append(SecretId)
        value = self._plan.get("get")
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]

    def put_secret_value(  # noqa: N803
        self, SecretId: str, SecretString: str
    ) -> dict[str, object]:
        self.put_calls.append((SecretId, SecretString))
        value = self._plan.get("put")
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]


class FakeStsClient:
    def __init__(self, account: str, region: str, error: Exception | None = None):
        self._account = account
        self._error = error
        self.meta = type("Meta", (), {"region_name": region})()

    def get_caller_identity(self) -> dict[str, str]:
        if self._error is not None:
            raise self._error
        return {"Account": self._account}


def _install_aws(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sts: FakeStsClient | None = None,
    secrets_client: FakeSecretsClient | None = None,
) -> None:
    def _factory(service: str, region_name: str | None = None) -> object:
        if service == "sts":
            return sts if sts is not None else FakeStsClient(FAKE_ACCOUNT, FAKE_REGION)
        if service == "secretsmanager":
            if secrets_client is None:
                raise AssertionError("secretsmanager client requested but not provided")
            return secrets_client
        raise AssertionError(f"unexpected boto3 service: {service}")

    monkeypatch.setattr(bar.boto3, "client", _factory)


def _install_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recorder: list[tuple[str, object]],
    rows: dict[str, object],
    connect_error: Exception | None = None,
    roundtrip_error: Exception | None = None,
    close_error: bool = False,
) -> None:
    """Patch ``psycopg.connect`` and ``ClientCursor`` on the module.

    The first ``connect`` is the master connection; the second is the
    independent round-trip probe. ``roundtrip_error`` fails only the latter.
    """
    calls = {"n": 0}

    def _connect(**_kwargs: object) -> FakeConnection:
        calls["n"] += 1
        if calls["n"] == 1:
            if connect_error is not None:
                raise connect_error
            return FakeConnection(recorder, rows, close_error=close_error)
        if roundtrip_error is not None:
            raise roundtrip_error
        return FakeConnection(recorder, rows)

    monkeypatch.setattr(bar.psycopg, "connect", _connect)
    monkeypatch.setattr(bar, "ClientCursor", FakeClientCursor)


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: dict[str, str] | None = None,
    sts: FakeStsClient | None = None,
    secrets_plan: dict[str, object] | None = None,
    rows: dict[str, object] | None = None,
    recorder: list[tuple[str, object]] | None = None,
    connect_error: Exception | None = None,
    roundtrip_error: Exception | None = None,
    close_error: bool = False,
    fixed_password: str | None = FAKE_APP_PW,
) -> int:
    """Drive :func:`main` end-to-end with fully faked AWS/DB collaborators."""
    _set_env(monkeypatch, env if env is not None else _base_env())
    secrets_client = (
        FakeSecretsClient(secrets_plan) if secrets_plan is not None else None
    )
    _install_aws(monkeypatch, sts=sts, secrets_client=secrets_client)
    if recorder is None:
        recorder = []
    if rows is not None:
        _install_db(
            monkeypatch,
            recorder=recorder,
            rows=rows,
            connect_error=connect_error,
            roundtrip_error=roundtrip_error,
            close_error=close_error,
        )
    if fixed_password is not None:
        monkeypatch.setattr(bar, "generate_password", lambda: fixed_password)
    return bar.main()


# A fully-successful create-mode Secrets Manager plan.
def _ok_secrets_plan() -> dict[str, object]:
    return {
        "describe": {"VersionIdsToStages": {}},  # target empty
        "get": {
            "SecretString": json.dumps(
                {"username": FAKE_MASTER_USER, "password": FAKE_MASTER_PW}
            )
        },
        "put": {"VersionId": "v-EXAMPLE", "VersionStages": ["AWSCURRENT"]},
    }


# ============================================================================ #
# Import safety
# ============================================================================ #


def test_import_does_not_pull_app_config_or_session() -> None:
    # Import (or re-import) the module in isolation and assert none of the
    # prohibited app.* modules were dragged in as a side effect.
    for forbidden in ("app.core.config", "app.db.session", "app.db.migrate"):
        sys.modules.pop(forbidden, None)
    importlib.reload(bar)
    for forbidden in ("app.core.config", "app.db.session", "app.db.migrate"):
        assert forbidden not in sys.modules, forbidden


def test_no_bootstrap_runs_on_import() -> None:
    # The only entry point is main(); importing must not have side effects such
    # as opening connections. A clean reload with no patched collaborators is
    # itself the assertion (it would raise if it tried to touch AWS/DB).
    importlib.reload(bar)
    assert callable(bar.main)


# ============================================================================ #
# Configuration validation (per field)
# ============================================================================ #


@pytest.mark.parametrize(
    "drop_field",
    [
        bar._ENV_MASTER_SECRET_ARN,
        bar._ENV_TARGET_SECRET_ARN,
        bar._ENV_DB_HOST,
        bar._ENV_DB_NAME,
        bar._ENV_EXPECTED_ACCOUNT_ID,
        bar._ENV_EXPECTED_REGION,
    ],
)
def test_config_missing_required_field(
    monkeypatch: pytest.MonkeyPatch, drop_field: str
) -> None:
    env = _base_env()
    del env[drop_field]
    _set_env(monkeypatch, env)
    with pytest.raises(bar._ConfigError) as exc:
        bar.parse_config()
    assert exc.value.args[0] == drop_field


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (bar._ENV_DB_PORT, "not-an-int"),
        (bar._ENV_DB_PORT, "0"),
        (bar._ENV_DB_PORT, "65536"),
        (bar._ENV_DB_NAME, "1nvalid"),  # must start with letter/underscore
        (bar._ENV_DB_NAME, "Has-Dash"),  # uppercase + dash rejected
        (bar._ENV_EXPECTED_ACCOUNT_ID, "12345"),  # not 12 digits
        (bar._ENV_EXPECTED_ACCOUNT_ID, "abcdefghijkl"),  # non-numeric
        (bar._ENV_MODE, "destroy"),  # not create/recover
    ],
)
def test_config_malformed_field(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    env = _base_env()
    env[field] = value
    _set_env(monkeypatch, env)
    with pytest.raises(bar._ConfigError) as exc:
        bar.parse_config()
    assert exc.value.args[0] == field


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # Port defaults to 5432 and mode defaults to create when both are absent.
    _set_env(monkeypatch, _base_env())
    cfg = bar.parse_config()
    assert cfg["db_port"] == 5432
    assert cfg["mode"] == bar._MODE_CREATE


def test_config_valid_full(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env()
    env[bar._ENV_MODE] = bar._MODE_RECOVER
    env[bar._ENV_DB_PORT] = "6432"
    _set_env(monkeypatch, env)
    cfg = bar.parse_config()
    assert cfg["mode"] == bar._MODE_RECOVER
    assert cfg["db_port"] == 6432
    assert cfg["db_name"] == FAKE_DB


# ============================================================================ #
# Identity check
# ============================================================================ #


def test_identity_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_aws(
        monkeypatch, sts=FakeStsClient(FAKE_ACCOUNT, FAKE_REGION)
    )
    assert bar.validate_identity(_config()) is True


def test_identity_wrong_account(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_aws(
        monkeypatch, sts=FakeStsClient("999988887777", FAKE_REGION)
    )
    assert bar.validate_identity(_config()) is False


def test_identity_wrong_region(monkeypatch: pytest.MonkeyPatch) -> None:
    # Client's effective region diverges from the expected region.
    _install_aws(
        monkeypatch, sts=FakeStsClient(FAKE_ACCOUNT, "us-west-2")
    )
    assert bar.validate_identity(_config()) is False


def test_identity_aws_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import BotoCoreError

    _install_aws(
        monkeypatch,
        sts=FakeStsClient(FAKE_ACCOUNT, FAKE_REGION, error=BotoCoreError()),
    )
    assert bar.validate_identity(_config()) is False


# ============================================================================ #
# Target-empty precondition
# ============================================================================ #


def test_target_empty_true_when_no_awscurrent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeSecretsClient({"describe": {"VersionIdsToStages": {}}})
    _install_aws(monkeypatch, secrets_client=client)
    assert bar.check_target_empty(_config()) is True
    assert client.describe_calls == [FAKE_TARGET_ARN]


def test_target_not_empty_when_awscurrent_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeSecretsClient(
        {"describe": {"VersionIdsToStages": {"v1": ["AWSCURRENT"]}}}
    )
    _install_aws(monkeypatch, secrets_client=client)
    assert bar.check_target_empty(_config()) is False


def test_target_empty_fails_closed_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "AccessDenied"}}, "DescribeSecret")
    client = FakeSecretsClient({"describe": err})
    _install_aws(monkeypatch, secrets_client=client)
    assert bar.check_target_empty(_config()) is False


# ============================================================================ #
# Master credential parsing
# ============================================================================ #


def test_fetch_master_credentials_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeSecretsClient(
        {
            "get": {
                "SecretString": json.dumps(
                    {"username": FAKE_MASTER_USER, "password": FAKE_MASTER_PW}
                )
            }
        }
    )
    _install_aws(monkeypatch, secrets_client=client)
    user, pw = bar.fetch_master_credentials(_config())
    assert user == FAKE_MASTER_USER
    assert pw == FAKE_MASTER_PW
    assert client.get_calls == [FAKE_MASTER_ARN]


@pytest.mark.parametrize(
    "payload",
    [
        {"SecretString": "not-json{"},
        {"SecretString": json.dumps(["not", "an", "object"])},
        {"SecretString": json.dumps({"username": "u"})},  # missing password
        {"SecretString": json.dumps({"password": "p"})},  # missing username
        {"SecretString": ""},  # empty payload
        {},  # no SecretString at all
    ],
)
def test_fetch_master_credentials_bad_payload(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    client = FakeSecretsClient({"get": payload})
    _install_aws(monkeypatch, secrets_client=client)
    with pytest.raises(ValueError):
        bar.fetch_master_credentials(_config())


# ============================================================================ #
# Password generation
# ============================================================================ #


def test_generate_password_uses_token_urlsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, int] = {}

    def _fake_token_urlsafe(nbytes: int) -> str:
        seen["nbytes"] = nbytes
        return "generated-EXAMPLE"

    monkeypatch.setattr(bar.secrets, "token_urlsafe", _fake_token_urlsafe)
    result = bar.generate_password()
    assert seen["nbytes"] == 48
    assert result == "generated-EXAMPLE"


def test_generate_password_is_url_safe_and_long() -> None:
    pw = bar.generate_password()
    # token_urlsafe(48) yields base64url of 48 bytes -> 64 chars.
    assert len(pw) >= 64
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert set(pw) <= allowed
    # No URL-structural or shell-meaningful characters leak in.
    for bad in ("/", "+", "=", ":", "@", "?", "#", " "):
        assert bad not in pw


# ============================================================================ #
# Role creation SQL discipline
# ============================================================================ #


def test_create_app_role_parameterizes_password() -> None:
    recorder: list[tuple[str, object]] = []
    conn = FakeConnection(recorder, {})
    # Swap in the recording ClientCursor for this direct-function test.
    original = bar.ClientCursor
    bar.ClientCursor = FakeClientCursor
    try:
        bar.create_app_role(conn, FAKE_APP_PW)
    finally:
        bar.ClientCursor = original

    assert len(recorder) == 1
    template, params = recorder[0]
    # The password is a bound parameter, not interpolated text.
    assert "%s" in template
    assert FAKE_APP_PW not in template
    assert params == (FAKE_APP_PW,)
    # Restrictive attribute clause is present verbatim.
    assert "LOGIN" in template
    assert "NOSUPERUSER" in template
    assert "NOCREATEDB" in template
    assert "NOCREATEROLE" in template
    assert "NOREPLICATION" in template
    assert "NOBYPASSRLS" in template
    # Role identifier is quoted, not a bare/interpolated token.
    assert '"signalnest_app"' in template


def test_recover_app_role_uses_alter_with_password() -> None:
    recorder: list[tuple[str, object]] = []
    conn = FakeConnection(recorder, {})
    original = bar.ClientCursor
    bar.ClientCursor = FakeClientCursor
    try:
        bar.recover_app_role(conn, FAKE_APP_PW)
    finally:
        bar.ClientCursor = original

    assert len(recorder) == 1
    template, params = recorder[0]
    assert "ALTER ROLE" in template
    assert "WITH PASSWORD %s" in template
    assert FAKE_APP_PW not in template
    assert params == (FAKE_APP_PW,)


def test_transfer_ownership_statement() -> None:
    recorder: list[tuple[str, object]] = []
    conn = FakeConnection(recorder, {})
    bar.transfer_ownership(conn, FAKE_DB)
    assert len(recorder) == 1
    template, _params = recorder[0]
    assert "ALTER DATABASE" in template
    assert "OWNER TO" in template
    assert f'"{FAKE_DB}"' in template
    assert '"signalnest_app"' in template


# ============================================================================ #
# Role metadata verification
# ============================================================================ #


def test_verify_role_metadata_ok() -> None:
    recorder: list[tuple[str, object]] = []
    conn = FakeConnection(recorder, _GOOD_ROWS)
    assert bar.verify_role_metadata(conn, FAKE_DB) is True


@pytest.mark.parametrize(
    "attrs",
    [
        (True, False, False, True, False, False),  # rolsuper set
        (False, True, False, True, False, False),  # rolcreaterole set
        (False, False, True, True, False, False),  # rolcreatedb set
        (False, False, False, False, False, False),  # rolcanlogin false
        (False, False, False, True, True, False),  # rolreplication set
        (False, False, False, True, False, True),  # rolbypassrls set
    ],
)
def test_verify_role_metadata_rejects_bad_attributes(
    attrs: tuple[bool, ...],
) -> None:
    rows = dict(_GOOD_ROWS)
    rows[_ATTR_KEY] = attrs
    conn = FakeConnection([], rows)
    assert bar.verify_role_metadata(conn, FAKE_DB) is False


def test_verify_role_metadata_rejects_wrong_owner() -> None:
    rows = dict(_GOOD_ROWS)
    rows[_OWNER_KEY] = None
    conn = FakeConnection([], rows)
    assert bar.verify_role_metadata(conn, FAKE_DB) is False


# ============================================================================ #
# URL composition + quoting
# ============================================================================ #


def test_compose_database_url_shape_and_quoting() -> None:
    url = bar.compose_database_url(_config(), FAKE_APP_PW)
    expected_pw = urllib.parse.quote(FAKE_APP_PW, safe="")
    assert url == (
        f"postgresql+psycopg://signalnest_app:{expected_pw}"
        f"@{FAKE_HOST}:{int(FAKE_PORT)}/{FAKE_DB}?sslmode=require"
    )
    # Reserved characters were percent-encoded, so the raw password does not
    # appear inside the URL and cannot break its structure.
    assert FAKE_APP_PW not in url
    assert "%40" in url  # '@'
    assert "%2F" in url  # '/'
    assert "%3A" in url  # ':'
    assert "sslmode=require" in url


def test_compose_database_url_quotes_username() -> None:
    # The username is quoted with safe="" (defense in depth) even though the
    # constant role name has no reserved characters.
    url = bar.compose_database_url(_config(), "simple")
    assert "://signalnest_app:" in url


# ============================================================================ #
# Target secret write
# ============================================================================ #


def test_write_target_secret_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeSecretsClient(
        {"put": {"VersionId": "v-EXAMPLE", "VersionStages": ["AWSCURRENT"]}}
    )
    _install_aws(monkeypatch, secrets_client=client)
    url = bar.compose_database_url(_config(), FAKE_APP_PW)
    assert bar.write_target_secret(_config(), url) is True
    # Exactly one put, addressed to the TARGET ARN, carrying the URL payload.
    assert len(client.put_calls) == 1
    secret_id, secret_string = client.put_calls[0]
    assert secret_id == FAKE_TARGET_ARN
    assert secret_string == url


@pytest.mark.parametrize(
    "response",
    [
        {"VersionStages": ["AWSCURRENT"]},  # missing VersionId
        {"VersionId": "v-EXAMPLE"},  # missing VersionStages
        {"VersionId": "v-EXAMPLE", "VersionStages": ["AWSPENDING"]},  # no CURRENT
        {"VersionId": "", "VersionStages": ["AWSCURRENT"]},  # empty VersionId
        {},  # empty response
    ],
)
def test_write_target_secret_invalid_response(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, object]
) -> None:
    client = FakeSecretsClient({"put": response})
    _install_aws(monkeypatch, secrets_client=client)
    url = bar.compose_database_url(_config(), FAKE_APP_PW)
    assert bar.write_target_secret(_config(), url) is False


# ============================================================================ #
# Round trip
# ============================================================================ #


def test_verify_round_trip_success(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: list[tuple[str, object]] = []
    monkeypatch.setattr(
        bar.psycopg,
        "connect",
        lambda **_k: FakeConnection(recorder, {"SELECT 1": (1,)}),
    )
    assert bar.verify_round_trip(_config(), FAKE_APP_PW) is True


def test_verify_round_trip_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_k: object) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(bar.psycopg, "connect", _boom)
    assert bar.verify_round_trip(_config(), FAKE_APP_PW) is False


def test_verify_round_trip_wrong_result(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: list[tuple[str, object]] = []
    monkeypatch.setattr(
        bar.psycopg,
        "connect",
        lambda **_k: FakeConnection(recorder, {"SELECT 1": (0,)}),
    )
    assert bar.verify_round_trip(_config(), FAKE_APP_PW) is False


# ============================================================================ #
# main() - full-phase exit codes
# ============================================================================ #


def test_main_create_success(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: list[tuple[str, object]] = []
    code = _run_main(
        monkeypatch,
        secrets_plan=_ok_secrets_plan(),
        rows=_rows_for_create(),
        recorder=recorder,
    )
    assert code == bar.EXIT_SUCCESS


def test_main_config_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env()
    del env[bar._ENV_DB_HOST]
    code = _run_main(monkeypatch, env=env)
    assert code == bar.EXIT_CONFIG_INVALID


def test_main_identity_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    code = _run_main(
        monkeypatch,
        sts=FakeStsClient("999988887777", FAKE_REGION),
        secrets_plan=_ok_secrets_plan(),
    )
    assert code == bar.EXIT_IDENTITY_MISMATCH


def test_main_target_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _ok_secrets_plan()
    plan["describe"] = {"VersionIdsToStages": {"v1": ["AWSCURRENT"]}}
    code = _run_main(monkeypatch, secrets_plan=plan)
    assert code == bar.EXIT_TARGET_NOT_EMPTY


def test_main_role_exists_create_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # role_exists reports present in create mode -> report-only 13.
    rows = dict(_GOOD_ROWS)  # role present
    code = _run_main(
        monkeypatch,
        secrets_plan=_ok_secrets_plan(),
        rows=rows,
    )
    assert code == bar.EXIT_ROLE_EXISTS


def test_main_master_secret_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _ok_secrets_plan()
    plan["get"] = {"SecretString": "definitely-not-json{"}
    code = _run_main(monkeypatch, secrets_plan=plan)
    assert code == bar.EXIT_MASTER_SECRET_FAILURE


def test_main_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    code = _run_main(
        monkeypatch,
        secrets_plan=_ok_secrets_plan(),
        rows=_rows_for_create(),
        connect_error=OSError("TLS handshake failed"),
    )
    assert code == bar.EXIT_DB_CONNECT_FAILURE


def test_main_role_create_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make create_app_role raise so the role/ownership phase fails BEFORE write.
    def _boom(_conn: object, _pw: str) -> None:
        raise RuntimeError("permission denied for CREATE ROLE")

    monkeypatch.setattr(bar, "create_app_role", _boom)
    recorder: list[tuple[str, object]] = []
    secrets_client_holder: dict[str, FakeSecretsClient] = {}

    plan = _ok_secrets_plan()
    client = FakeSecretsClient(plan)
    secrets_client_holder["c"] = client
    _set_env(monkeypatch, _base_env())
    _install_aws(monkeypatch, secrets_client=client)
    _install_db(monkeypatch, recorder=recorder, rows=_rows_for_create())
    monkeypatch.setattr(bar, "generate_password", lambda: FAKE_APP_PW)

    code = bar.main()
    assert code == bar.EXIT_ROLE_OR_OWNERSHIP_FAILURE
    # No secret was written because the failure preceded the write phase.
    assert client.put_calls == []


def test_main_metadata_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Role created but metadata check fails -> 16, still before any write.
    rows = _rows_for_create()
    rows[_ATTR_KEY] = (True, False, False, True, False, False)  # rolsuper set
    plan = _ok_secrets_plan()
    client = FakeSecretsClient(plan)
    _set_env(monkeypatch, _base_env())
    _install_aws(monkeypatch, secrets_client=client)
    _install_db(monkeypatch, recorder=[], rows=rows)
    monkeypatch.setattr(bar, "generate_password", lambda: FAKE_APP_PW)
    code = bar.main()
    assert code == bar.EXIT_ROLE_OR_OWNERSHIP_FAILURE
    assert client.put_calls == []


def test_main_secret_write_failure_no_role_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Role committed, then PutSecretValue returns an invalid response -> 17.
    plan = _ok_secrets_plan()
    plan["put"] = {"VersionId": "v-EXAMPLE", "VersionStages": ["AWSPENDING"]}
    recorder: list[tuple[str, object]] = []
    code = _run_main(
        monkeypatch,
        secrets_plan=plan,
        rows=_rows_for_create(),
        recorder=recorder,
    )
    assert code == bar.EXIT_SECRET_WRITE_FAILURE
    # Recovery-required path must NOT roll the role back: no DROP ROLE issued.
    executed = " ".join(text for text, _ in recorder)
    assert "DROP ROLE" not in executed.upper()


def test_main_secret_write_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import ClientError

    plan = _ok_secrets_plan()
    plan["put"] = ClientError({"Error": {"Code": "AccessDenied"}}, "PutSecretValue")
    recorder: list[tuple[str, object]] = []
    code = _run_main(
        monkeypatch,
        secrets_plan=plan,
        rows=_rows_for_create(),
        recorder=recorder,
    )
    assert code == bar.EXIT_SECRET_WRITE_FAILURE
    executed = " ".join(text for text, _ in recorder)
    assert "DROP ROLE" not in executed.upper()


def test_main_verify_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Everything succeeds through the write, but the round-trip connect fails.
    recorder: list[tuple[str, object]] = []
    code = _run_main(
        monkeypatch,
        secrets_plan=_ok_secrets_plan(),
        rows=_rows_for_create(),
        recorder=recorder,
        roundtrip_error=OSError("auth failed for new role"),
    )
    assert code == bar.EXIT_VERIFY_FAILURE


def test_main_cleanup_failure_only_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A close error on an otherwise-successful run maps to 19.
    _set_env(monkeypatch, _base_env())
    client = FakeSecretsClient(_ok_secrets_plan())
    _install_aws(monkeypatch, secrets_client=client)
    _install_db(
        monkeypatch,
        recorder=[],
        rows=_rows_for_create(),
        close_error=True,
    )
    monkeypatch.setattr(bar, "generate_password", lambda: FAKE_APP_PW)
    code = bar.main()
    assert code == bar.EXIT_CLEANUP_FAILURE


def test_main_cleanup_failure_does_not_mask_prior_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A close error must NOT override a recovery-required code (17 here).
    plan = _ok_secrets_plan()
    plan["put"] = {"VersionId": "v-EXAMPLE", "VersionStages": ["AWSPENDING"]}
    _set_env(monkeypatch, _base_env())
    client = FakeSecretsClient(plan)
    _install_aws(monkeypatch, secrets_client=client)
    _install_db(
        monkeypatch,
        recorder=[],
        rows=_rows_for_create(),
        close_error=True,
    )
    monkeypatch.setattr(bar, "generate_password", lambda: FAKE_APP_PW)
    code = bar.main()
    assert code == bar.EXIT_SECRET_WRITE_FAILURE


# ============================================================================ #
# Recovery mode
# ============================================================================ #


def test_main_recover_role_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env()
    env[bar._ENV_MODE] = bar._MODE_RECOVER
    rows = _rows_for_create()  # role absent
    code = _run_main(
        monkeypatch,
        env=env,
        secrets_plan=_ok_secrets_plan(),
        rows=rows,
    )
    assert code == bar.EXIT_RECOVERY_PRECONDITION


def test_main_recover_role_present_uses_alter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _base_env()
    env[bar._ENV_MODE] = bar._MODE_RECOVER
    recorder: list[tuple[str, object]] = []
    # Recovery ignores the target-empty precondition, so leave describe as if a
    # version already existed to prove 12 is not enforced in recover mode.
    plan = _ok_secrets_plan()
    plan["describe"] = {"VersionIdsToStages": {"v1": ["AWSCURRENT"]}}
    code = _run_main(
        monkeypatch,
        env=env,
        secrets_plan=plan,
        rows=_GOOD_ROWS,  # role present
        recorder=recorder,
        fixed_password="recover-pw-EXAMPLE-value",
    )
    assert code == bar.EXIT_SUCCESS
    executed = [text for text, _ in recorder]
    joined = " ".join(executed)
    # ALTER path taken, not CREATE; new password bound as a %s parameter.
    assert any("ALTER ROLE" in t and "WITH PASSWORD %s" in t for t in executed)
    assert "CREATE ROLE" not in joined
    # The new password appears only as a bound param, never in template text.
    alter_params = [
        params for text, params in recorder if "ALTER ROLE" in text
    ]
    assert alter_params == [("recover-pw-EXAMPLE-value",)]
    assert "recover-pw-EXAMPLE-value" not in joined


def test_main_recover_target_not_empty_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Explicit: a populated target does not block recover mode (no exit 12).
    env = _base_env()
    env[bar._ENV_MODE] = bar._MODE_RECOVER
    plan = _ok_secrets_plan()
    plan["describe"] = {"VersionIdsToStages": {"v1": ["AWSCURRENT"]}}
    code = _run_main(
        monkeypatch,
        env=env,
        secrets_plan=plan,
        rows=_GOOD_ROWS,
    )
    assert code != bar.EXIT_TARGET_NOT_EMPTY
    assert code == bar.EXIT_SUCCESS


# ============================================================================ #
# Exit-code map uniqueness / stability
# ============================================================================ #


def test_exit_codes_unique_and_stable() -> None:
    codes = {
        "EXIT_SUCCESS": bar.EXIT_SUCCESS,
        "EXIT_CONFIG_INVALID": bar.EXIT_CONFIG_INVALID,
        "EXIT_IDENTITY_MISMATCH": bar.EXIT_IDENTITY_MISMATCH,
        "EXIT_TARGET_NOT_EMPTY": bar.EXIT_TARGET_NOT_EMPTY,
        "EXIT_ROLE_EXISTS": bar.EXIT_ROLE_EXISTS,
        "EXIT_MASTER_SECRET_FAILURE": bar.EXIT_MASTER_SECRET_FAILURE,
        "EXIT_DB_CONNECT_FAILURE": bar.EXIT_DB_CONNECT_FAILURE,
        "EXIT_ROLE_OR_OWNERSHIP_FAILURE": bar.EXIT_ROLE_OR_OWNERSHIP_FAILURE,
        "EXIT_SECRET_WRITE_FAILURE": bar.EXIT_SECRET_WRITE_FAILURE,
        "EXIT_VERIFY_FAILURE": bar.EXIT_VERIFY_FAILURE,
        "EXIT_CLEANUP_FAILURE": bar.EXIT_CLEANUP_FAILURE,
        "EXIT_RECOVERY_PRECONDITION": bar.EXIT_RECOVERY_PRECONDITION,
    }
    values = list(codes.values())
    assert len(set(values)) == len(values), "exit codes must be unique"
    # Stability: assert the exact documented numbers.
    assert values == [0, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]


# ============================================================================ #
# Output hygiene - no secrets/values/exceptions in any output
# ============================================================================ #


def _assert_output_clean(captured: pytest.CaptureFixture[str]) -> None:
    out = captured.readouterr()
    combined = out.out + out.err
    for line in combined.splitlines():
        if not line.strip():
            continue
        assert line.startswith(bar._EMIT_PREFIX), f"unexpected output line: {line!r}"
    for secret in SECRET_STRINGS:
        assert secret not in combined, f"leaked value in output: {secret!r}"
    # No raw exception text either.
    for token in ("Traceback", "Error:", "Exception", "connection refused"):
        assert token not in combined


def test_output_hygiene_across_failure_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scenarios = []

    # 1. identity mismatch
    def s_identity() -> None:
        _run_main(
            monkeypatch,
            sts=FakeStsClient("999988887777", FAKE_REGION),
            secrets_plan=_ok_secrets_plan(),
        )

    # 2. master secret bad json
    def s_master() -> None:
        plan = _ok_secrets_plan()
        plan["get"] = {"SecretString": "not-json{"}
        _run_main(monkeypatch, secrets_plan=plan)

    # 3. connect failure
    def s_connect() -> None:
        _run_main(
            monkeypatch,
            secrets_plan=_ok_secrets_plan(),
            rows=_rows_for_create(),
            connect_error=OSError("connection refused"),
        )

    # 4. secret write failure
    def s_write() -> None:
        plan = _ok_secrets_plan()
        plan["put"] = {"VersionId": "v", "VersionStages": ["AWSPENDING"]}
        _run_main(
            monkeypatch,
            secrets_plan=plan,
            rows=_rows_for_create(),
            recorder=[],
        )

    # 5. verify failure
    def s_verify() -> None:
        _run_main(
            monkeypatch,
            secrets_plan=_ok_secrets_plan(),
            rows=_rows_for_create(),
            recorder=[],
            roundtrip_error=OSError("connection refused"),
        )

    scenarios = [s_identity, s_master, s_connect, s_write, s_verify]
    for scenario in scenarios:
        scenario()
        _assert_output_clean(capsys)


def test_output_hygiene_config_field_name_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env()
    del env[bar._ENV_DB_HOST]
    code = _run_main(monkeypatch, env=env)
    assert code == bar.EXIT_CONFIG_INVALID
    out = capsys.readouterr()
    combined = out.out + out.err
    # The field NAME may appear; its VALUE (the host) may not.
    assert bar._ENV_DB_HOST in combined
    assert FAKE_HOST not in combined


def test_output_hygiene_success_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_main(
        monkeypatch,
        secrets_plan=_ok_secrets_plan(),
        rows=_rows_for_create(),
        recorder=[],
    )
    _assert_output_clean(capsys)


def test_logging_disabled_no_records(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # main() disables logging first; no log records should be captured across a
    # representative failure run.
    with caplog.at_level(0):
        _run_main(
            monkeypatch,
            sts=FakeStsClient("999988887777", FAKE_REGION),
            secrets_plan=_ok_secrets_plan(),
        )
    assert caplog.records == []
