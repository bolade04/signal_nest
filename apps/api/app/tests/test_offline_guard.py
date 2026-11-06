"""FU-237-F1: negative controls and governance for the project-wide offline guard.

The guard itself lives in ``_offline_guard.py`` and is wired by ``conftest.py``.
"""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.tests import _offline_guard as guard
from app.tests import _offline_provider as provider
from app.tests._offline_guard import NetworkBlockedError, expected_violations

TESTS_DIR = Path(__file__).resolve().parent
# A documentation-range address (RFC 5737): never routable, so even a guard failure could
# not reach a real host.
PUBLIC_V4 = "192.0.2.1"
PUBLIC_V6 = "2001:db8::1"


def test_guard_is_installed_for_the_session():
    assert guard.is_intact()


def test_httpx_to_a_non_loopback_address_is_refused_with_test_id_and_address():
    with expected_violations() as seen:
        with pytest.raises(httpx.ConnectError) as info:
            httpx.get(f"http://{PUBLIC_V4}:81/", timeout=1)
    assert seen, "the refusal must be recorded, not only raised"
    text = seen[0]
    assert PUBLIC_V4 in text
    assert "test_httpx_to_a_non_loopback_address_is_refused_with_test_id_and_address" in text
    assert "offline guard" in str(info.value)


def test_a_swallowed_refusal_still_fails_the_test_at_teardown():
    """The autouse fixture reads the record, so catching the OSError cannot hide an attempt.

    Proven here by checking the record exists after the error was caught (the fixture's
    teardown then turns the same record into a failure for any test that did not consume it).
    """
    with expected_violations() as seen:
        try:
            socket.create_connection((PUBLIC_V4, 81), timeout=1)
        except OSError:
            pass
    assert len(seen) == 1


@pytest.mark.parametrize("family,host", [(socket.AF_INET, PUBLIC_V4), (socket.AF_INET6, PUBLIC_V6)])
def test_raw_connect_and_connect_ex_are_refused_for_both_families(family, host):
    with expected_violations() as seen:
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.settimeout(0.5)
            with pytest.raises(NetworkBlockedError):
                s.connect((host, 81))
            with pytest.raises(NetworkBlockedError):
                s.connect_ex((host, 81))
        finally:
            s.close()
    assert len(seen) == 2 and all(host in m for m in seen)


def test_udp_sendto_to_a_non_loopback_address_is_refused():
    with expected_violations() as seen:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(NetworkBlockedError):
                s.sendto(b"x", (PUBLIC_V4, 53))
        finally:
            s.close()
    assert len(seen) == 1


def test_udp_sendmsg_with_an_address_is_refused():
    with expected_violations() as seen:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(NetworkBlockedError):
                s.sendmsg([b"x"], [], 0, (PUBLIC_V4, 53))
        finally:
            s.close()
    assert len(seen) == 1


def test_a_non_literal_hostname_is_refused_without_resolving_it():
    with expected_violations() as seen:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(NetworkBlockedError):
                s.connect(("api.anthropic.com", 443))
        finally:
            s.close()
    assert "api.anthropic.com" in seen[0]


@pytest.mark.parametrize(
    "call",
    [
        lambda: socket.getaddrinfo("api.anthropic.com", 443),
        lambda: socket.gethostbyname("api.anthropic.com"),
        lambda: socket.gethostbyname_ex("api.anthropic.com"),
    ],
    ids=["getaddrinfo", "gethostbyname", "gethostbyname_ex"],
)
def test_dns_lookup_of_a_public_name_is_refused(call):
    with expected_violations() as seen:
        with pytest.raises(NetworkBlockedError):
            call()
    assert "api.anthropic.com" in seen[0]


def test_dns_lookup_of_loopback_names_and_literals_still_works():
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo("127.0.0.1", 80)
    assert socket.getaddrinfo("::1", 80)


def test_loopback_testclient_request_still_works():
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as client:
        assert client.get("/ping").json() == {"ok": "yes"}


def test_a_real_loopback_tcp_connection_is_allowed():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
    except PermissionError:  # the reviewer's deny-outbound sandbox profile forbids even local binds
        server.close()
        pytest.skip("sandbox profile forbids binding a local listener")
    server.listen(1)
    port = server.getsockname()[1]
    accepted: list[socket.socket] = []
    t = threading.Thread(target=lambda: accepted.append(server.accept()[0]))
    t.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        with socket.create_connection(("localhost", port), timeout=2):
            pass
    finally:
        t.join(2)
        for a in accepted:
            a.close()
        server.close()


def test_a_unix_socket_connection_is_allowed():
    path = str(Path(tempfile.mkdtemp(dir="/tmp")) / "s")
    us = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        us.bind(path)
    except PermissionError:  # deny-outbound sandbox profile
        us.close()
        pytest.skip("sandbox profile forbids binding a Unix socket")
    us.listen(1)
    uc = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        uc.connect(path)
    except (
        PermissionError
    ):  # deny-outbound sandbox profile (not the guard: NetworkBlockedError differs)
        pytest.skip("sandbox profile forbids connecting a Unix socket")
    finally:
        uc.close()
        us.close()
        Path(path).unlink(missing_ok=True)
        Path(path).parent.rmdir()


async def test_asyncio_open_connection_to_a_non_loopback_address_is_refused():
    import asyncio

    with expected_violations() as seen:
        with pytest.raises(OSError):
            await asyncio.wait_for(asyncio.open_connection(PUBLIC_V4, 81), 2)
    assert seen and PUBLIC_V4 in seen[0]


def test_a_test_that_replaces_the_guard_is_detected_and_it_is_restored():
    original = socket.socket.connect
    socket.socket.connect = lambda self, address: None  # type: ignore[method-assign]
    try:
        assert not guard.is_intact()
        assert guard.restore_if_tampered() is True
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]
    assert guard.is_intact()


def test_the_opt_out_marker_is_registered_but_only_usable_with_the_env_var(pytestconfig):
    registered = "\n".join(pytestconfig.getini("markers"))
    assert guard.MARKER in registered and provider.MARKER in registered
    assert provider.ALLOW_ENV in registered
    assert guard.ALLOW_ENV in registered


def test_pin_state_no_provider_credential_and_mock_service():
    """Asserts the F1 part-2 pin; the subprocess control below runs it under an ambient key."""
    from app.core.config import get_settings
    from app.jobs import pipeline
    from app.llm import service as service_module

    assert os.environ["LLM_PROVIDER"] == "mock"
    assert not [k for k in os.environ if k.upper().startswith(("LLM_API", "ANTHROPIC_", "OPENAI_"))]
    assert get_settings().llm_provider == "mock" and not get_settings().llm_api_key
    assert service_module.get_llm_service().provider_name == "mock"
    assert service_module.get_llm_service() is service_module.llm_service
    # Phase 5A: the pipeline reaches the service only through the accessor; it holds no
    # binding of its own that the pin could miss.
    assert not hasattr(pipeline, "llm_service")
    assert pipeline.get_llm_service is service_module.get_llm_service


def test_an_ambient_real_provider_environment_is_pinned_to_the_mock():
    env = {
        **os.environ,
        "LLM_PROVIDER": "anthropic",
        "LLM_API_KEY": "fake-not-a-real-key",
        "ANTHROPIC_API_KEY": "fake-not-a-real-key",
        "OPENAI_API_KEY": "fake-not-a-real-key",
    }
    env.pop(provider.ALLOW_ENV, None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{Path(__file__).name}::test_pin_state_no_provider_credential_and_mock_service",
        ],
        cwd=TESTS_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-1000:]
    assert "fake-not-a-real-key" not in result.stdout + result.stderr


def _py_files() -> list[Path]:
    root = TESTS_DIR.parents[1]  # apps/api
    return [
        p
        for base in (root / "app", root / "tests")
        if base.is_dir()
        for p in base.rglob("*.py")
        if ".venv" not in p.parts and p.name != "_offline_guard.py"
    ]


@pytest.mark.parametrize("marker", [guard.MARKER, provider.MARKER])
def test_no_test_in_the_repository_carries_an_opt_out_marker(marker):
    offenders: list[str] = []
    for path in _py_files():
        if path.name in {"conftest.py", "_offline_provider.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == marker:
                offenders.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Constant) and node.value == marker:
                if path.name != Path(__file__).name:
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"{marker} is forbidden: {offenders}"


def test_governance_scan_is_not_vacuous():
    files = _py_files()
    assert files, "governance scan found no files"
    assert Path(__file__).resolve() in {p.resolve() for p in files}
    assert any(p.name == "conftest.py" for p in files)


def test_expected_violations_helper_is_used_only_by_this_module():
    offenders = [
        str(p)
        for p in _py_files()
        if p.name not in {Path(__file__).name, "conftest.py"}
        and "expected_violations" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders
