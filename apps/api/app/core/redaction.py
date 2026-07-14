"""Reusable secret redaction for structured logging and diagnostics.

One layer, applied *before* any structured value is serialized, so a careless
``extra_fields`` (a database URL, an ``Authorization`` header, a lease or
generation token, …) can never reach a log sink verbatim. The contract:

* **Key-based.** A mapping key whose name matches a sensitive pattern
  (case-insensitively) has its value replaced with :data:`REDACTED`, regardless
  of the value's type — the whole subtree is dropped, not walked.
* **URL/DSN-based.** Any string is scrubbed of ``user:pass@`` credentials and of
  sensitive query-parameter values, so a non-secret-keyed field that happens to
  hold a connection string still cannot leak.
* **Bounded + cycle-safe.** Recursion depth, container width and string length are
  all bounded, and already-seen containers are short-circuited, so a hostile or
  cyclic structure cannot exhaust memory or the stack.
* **Never raises.** Redaction is defensive infrastructure: any internal error
  degrades to :data:`REDACTION_ERROR` instead of propagating into the caller.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Matches ``scheme://userinfo@`` anywhere in a string (even embedded in a larger
#: message such as a driver error), so credentials are masked regardless of context.
_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")

#: Placeholder substituted for a redacted value.
REDACTED = "[REDACTED]"
#: Emitted when the redactor itself fails (it must never raise into callers).
REDACTION_ERROR = "[REDACTION_ERROR]"
#: Emitted when a structure is deeper/wider than the bounds below.
TRUNCATED = "[TRUNCATED]"

#: Substrings that mark a mapping key as sensitive (matched case-insensitively).
#: Deliberately specific to avoid redacting benign fields (e.g. ``author``).
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "cookie",
    "credential",
    "private_key",
    "client_secret",
    "access_key",
    "session_key",
    "database_url",
    "redis_url",
    "dsn",
    "jwt",
    "signed_url",
    "connection_string",
)

#: Query-parameter names whose *values* are stripped from any scrubbed URL.
SENSITIVE_QUERY_PARTS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "signature",
    "sig",
    "key",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "x-amz-credential",
    "x-amz-signature",
    "x-amz-security-token",
)

_MAX_DEPTH = 8
_MAX_ITEMS = 200
_MAX_STR_LEN = 2048


def _key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _scrub_url_string(value: str) -> str:
    """Remove ``user:pass@`` credentials and sensitive query values from a URL.

    A non-URL string is returned unchanged. Parsing is best-effort; anything that
    does not look like a ``scheme://`` URL is left alone (but still length-bounded
    by the caller).
    """
    if "://" not in value:
        return value

    # Mask any ``scheme://userinfo@`` first — this catches URLs embedded inside a
    # larger message (e.g. a driver error) that would not parse as a whole URL.
    value = _URL_CRED_RE.sub(rf"\1{REDACTED}@", value)

    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or not parts.netloc:
        return value

    netloc = parts.netloc

    query = parts.query
    if query:
        scrubbed_pairs = [
            (k, REDACTED if any(p in k.lower() for p in SENSITIVE_QUERY_PARTS) else v)
            for k, v in parse_qsl(query, keep_blank_values=True)
        ]
        query = urlencode(scrubbed_pairs)

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _truncate(value: str) -> str:
    if len(value) <= _MAX_STR_LEN:
        return value
    return value[:_MAX_STR_LEN] + "...[truncated]"


def _redact(value: Any, *, depth: int, seen: set[int]) -> Any:
    if depth > _MAX_DEPTH:
        return TRUNCATED

    if isinstance(value, str):
        return _truncate(_scrub_url_string(value))

    if isinstance(value, (bool, int, float)) or value is None:
        return value

    if isinstance(value, bytes):
        # Never emit raw bytes (may be binary or secret material).
        return f"[bytes:{len(value)}]"

    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            return TRUNCATED
        seen.add(marker)
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                out["..."] = TRUNCATED
                break
            key = str(k)
            out[key] = REDACTED if _key_is_sensitive(key) else _redact(
                v, depth=depth + 1, seen=seen
            )
        seen.discard(marker)
        return out

    if isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in seen:
            return TRUNCATED
        seen.add(marker)
        out_list: list[Any] = []
        for i, item in enumerate(value):
            if i >= _MAX_ITEMS:
                out_list.append(TRUNCATED)
                break
            out_list.append(_redact(item, depth=depth + 1, seen=seen))
        seen.discard(marker)
        return out_list

    # Unknown object: stringify safely (its repr may carry secrets, so scrub+bound).
    try:
        return _truncate(_scrub_url_string(str(value)))
    except Exception:  # pragma: no cover - pathological __str__
        return REDACTION_ERROR


def redact(value: Any) -> Any:
    """Return a redacted copy of ``value`` safe to serialize into a log/diagnostic.

    Handles nested mappings/sequences, strips URL credentials and sensitive query
    parameters, bounds depth/width/length, tolerates cycles, and never raises.
    """
    try:
        return _redact(value, depth=0, seen=set())
    except Exception:  # pragma: no cover - defensive: redaction must never raise
        return REDACTION_ERROR


def redact_text(value: str) -> str:
    """Scrub and bound a single string (URL credentials/query values, length)."""
    try:
        return _truncate(_scrub_url_string(value))
    except Exception:  # pragma: no cover
        return REDACTION_ERROR


def sanitize_exception(exc: BaseException) -> dict[str, str]:
    """Return a safe ``{error_class, error_message}`` for logging.

    The message is redacted (a driver error can embed a connection URL/credential),
    so only the exception *type* is guaranteed verbatim.
    """
    try:
        return {
            "error_class": type(exc).__name__,
            "error_message": redact_text(str(exc)),
        }
    except Exception:  # pragma: no cover
        return {"error_class": REDACTION_ERROR, "error_message": REDACTION_ERROR}


__all__ = [
    "REDACTED",
    "REDACTION_ERROR",
    "TRUNCATED",
    "redact",
    "redact_text",
    "sanitize_exception",
    "SENSITIVE_KEY_PARTS",
    "SENSITIVE_QUERY_PARTS",
]
