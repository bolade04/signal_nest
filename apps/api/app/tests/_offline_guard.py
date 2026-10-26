"""Project-wide offline guard for the API test session (FU-237-F1). Not itself a test module.

No API test may reach a real provider or any non-loopback host, whatever credential or
provider setting the machine happens to hold. The guard sits at the socket layer, so it
covers every client (httpx, boto3/urllib3, smtplib, redis, asyncio streams) rather than
one library's transport, and needs no third-party plugin.

Allowed: Unix-domain sockets, IPv4 127.0.0.0/8, IPv6 ::1 (and ::ffff:127.x), the literal
host ``localhost`` and the wildcard/unspecified address used when a socket binds locally.
Everything else is refused with :class:`NetworkBlockedError`, naming the test and the
address. Because application code may catch ``OSError`` and carry on, every refusal is
also *recorded*, and the autouse fixture in ``conftest.py`` fails the test at teardown if
any refusal was recorded, so a swallowed error cannot hide an attempt.

DNS: ``socket.getaddrinfo`` / ``gethostbyname*`` are refused for any host that is not an
IP literal or ``localhost`` (a lookup of a public name is itself an outbound leak and
precedes every connect). Name resolution done inside C by ``connect((name, port))`` is
covered by the connect check, which also refuses non-literal names.

Opt-out: ``@pytest.mark.allow_network`` is honoured only when the environment variable
``SIGNALNEST_TESTS_ALLOW_NETWORK=1`` is set; otherwise a test carrying it fails. A
governance test (``test_offline_guard.py``) asserts that no test in the repository
carries the marker.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

ALLOW_ENV = "SIGNALNEST_TESTS_ALLOW_NETWORK"
MARKER = "allow_network"
_NO_TEST = "<outside a test: collection or session setup>"


class NetworkBlockedError(OSError):
    """An API test tried to reach a non-loopback address."""


_state: dict[str, Any] = {"test": _NO_TEST, "allow": False, "violations": []}
_lock = threading.Lock()
_originals: dict[str, Any] = {}
_installed: dict[str, Any] = {}


def set_current_test(nodeid: str | None, *, allow: bool = False) -> None:
    _state["test"] = nodeid or _NO_TEST
    _state["allow"] = allow


def take_violations() -> list[str]:
    with _lock:
        out = list(_state["violations"])
        _state["violations"].clear()
    return out


@contextmanager
def expected_violations() -> Iterator[list[str]]:
    """Let the guard's own negative controls consume the refusals they provoke.

    Only ``test_offline_guard.py`` may use this (asserted by the governance test).
    """
    take_violations()
    seen: list[str] = []
    try:
        yield seen
    finally:
        seen.extend(take_violations())


def _is_loopback_host(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host == "" or host == "localhost":
        return True
    host = host.split("%", 1)[0]  # strip an IPv6 zone id
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _is_ip_literal(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    return True


def _is_dns_free(host: Any) -> bool:
    """No lookup happens for ``localhost`` or an IP literal; the connect check judges the IP."""
    return _is_loopback_host(host) or _is_ip_literal(host)


def _deny(what: str, target: Any) -> None:
    msg = (
        f"offline guard: {what} to {target!r} refused in test {_state['test']} "
        f"(only loopback and Unix sockets are allowed; see app/tests/_offline_guard.py)"
    )
    with _lock:
        _state["violations"].append(msg)
    raise NetworkBlockedError(msg)


def _check_address(what: str, address: Any) -> None:
    if _state["allow"]:
        return
    if isinstance(address, (str, bytes)):  # AF_UNIX path
        return
    if isinstance(address, tuple) and address:
        if _is_loopback_host(address[0]):
            return
        _deny(what, address)
    _deny(what, address)


def _wrap_address_method(name: str) -> Any:
    original = getattr(socket.socket, name)

    def guarded(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        if self.family != socket.AF_UNIX:
            _check_address(name, address)
        return original(self, address, *args, **kwargs)

    guarded.__name__ = name
    guarded._offline_guard = True  # type: ignore[attr-defined]
    _originals[name] = original
    return guarded


def _wrap_sendto() -> Any:
    original = socket.socket.sendto

    def guarded(self: socket.socket, data: Any, *args: Any) -> Any:
        if self.family != socket.AF_UNIX and args:
            _check_address("sendto", args[-1])
        return original(self, data, *args)

    guarded._offline_guard = True  # type: ignore[attr-defined]
    _originals["sendto"] = original
    return guarded


def _wrap_getaddrinfo() -> Any:
    original = socket.getaddrinfo

    def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _state["allow"] and not _is_dns_free(host):
            _deny("DNS lookup (getaddrinfo)", host)
        return original(host, *args, **kwargs)

    guarded._offline_guard = True  # type: ignore[attr-defined]
    _originals["getaddrinfo"] = original
    return guarded


def _wrap_gethostbyname(name: str) -> Any:
    original = getattr(socket, name)

    def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _state["allow"] and not _is_dns_free(host):
            _deny(f"DNS lookup ({name})", host)
        return original(host, *args, **kwargs)

    guarded._offline_guard = True  # type: ignore[attr-defined]
    _originals[name] = original
    return guarded


def install() -> None:
    """Idempotently patch the socket layer. Safe to call repeatedly."""
    if _installed:
        return
    _installed.update(
        connect=_wrap_address_method("connect"),
        connect_ex=_wrap_address_method("connect_ex"),
        sendto=_wrap_sendto(),
        getaddrinfo=_wrap_getaddrinfo(),
        gethostbyname=_wrap_gethostbyname("gethostbyname"),
        gethostbyname_ex=_wrap_gethostbyname("gethostbyname_ex"),
    )
    _apply()


def _apply() -> None:
    for name in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, name, _installed[name])
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        setattr(socket, name, _installed[name])


def is_intact() -> bool:
    return (
        bool(_installed)
        and all(
            getattr(socket.socket, n, None) is _installed[n]
            for n in ("connect", "connect_ex", "sendto")
        )
        and all(
            getattr(socket, n, None) is _installed[n]
            for n in ("getaddrinfo", "gethostbyname", "gethostbyname_ex")
        )
    )


def restore_if_tampered() -> bool:
    """Re-install the guard if something replaced it. Returns True if it had to."""
    if is_intact():
        return False
    _apply()
    return True


def uninstall() -> None:
    for name in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, name, _originals[name])
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        setattr(socket, name, _originals[name])
    _installed.clear()
