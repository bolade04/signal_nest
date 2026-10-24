"""Opaque, tamper-evident keyset cursors for customer list routes (P6-PLAT-12).

A paginated list route keeps its response body -- the same JSON array as before -- and
reports paging in two response headers, so existing clients are unaffected:

* ``X-Total-Count`` -- how many rows match the request's filters (the cursor and the page
  size do not change it). A client shows this as the total, never the page length.
* ``X-Next-Cursor`` -- present only when more rows follow this page. The client sends it
  back unchanged as ``?cursor=`` (with the same filters) to fetch the next page.

Ordering is keyset ordering: the route's sort column(s) followed by the row id, all in
one direction, so ties are broken deterministically and a row inserted while a client
pages through the list can neither repeat an earlier row nor push one off a page.

A cursor is ``base64url(payload) "." base64url(HMAC-SHA256(key, payload))``. The key is
derived from ``secret_key`` under a fixed label, so it is never the token-signing key
itself and needs no new setting. The payload binds the route (``scope``), the workspace,
a digest of the request's filters and the last row's sort key; a cursor that was altered,
or is replayed on another route, another workspace or with other filters, is refused with
``422 "Invalid cursor."``. Every query is still filtered by the request's own workspace,
so even a forged cursor could only position a page inside rows the caller may read.
Clients must treat the cursor as opaque.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Response
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.config import get_settings
from app.core.errors import ValidationDomainError

TOTAL_COUNT_HEADER = "X-Total-Count"
NEXT_CURSOR_HEADER = "X-Next-Cursor"
#: Page size used when a client sends a cursor without ``limit`` on a route whose
#: unpaginated default is "every row".
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
INVALID_CURSOR = "Invalid cursor."
_VERSION = 1
_KEY_LABEL = b"signalnest/pagination-cursor/v1"

#: OpenAPI description of the two headers, for ``responses=`` on each paginated route.
PAGINATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "headers": {
            TOTAL_COUNT_HEADER: {
                "description": "Rows matching the filters, independent of the page.",
                "schema": {"type": "integer"},
            },
            NEXT_CURSOR_HEADER: {
                "description": "Opaque cursor for the next page; absent on the last page.",
                "schema": {"type": "string"},
            },
        }
    }
}


@dataclass(frozen=True)
class Page:
    rows: list[Any]
    total: int
    next_cursor: str | None

    def apply_headers(self, response: Response) -> None:
        response.headers[TOTAL_COUNT_HEADER] = str(self.total)
        if self.next_cursor is not None:
            response.headers[NEXT_CURSOR_HEADER] = self.next_cursor


def _key() -> bytes:
    secret = get_settings().secret_key.encode("utf-8")
    return hmac.new(secret, _KEY_LABEL, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _filters_digest(filters: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {k: v for k, v in filters.items() if v is not None}, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _encode_value(value: Any) -> list:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"unsupported cursor key value: {value!r}")
    if isinstance(value, int):
        return ["i", value]
    if isinstance(value, float):
        return ["f", value]
    if isinstance(value, str):
        return ["s", value]
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return ["t", aware.astimezone(UTC).isoformat()]
    raise TypeError(f"unsupported cursor key value: {value!r}")


def _decode_value(item: Any) -> Any:
    if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)):
        raise ValueError("malformed key value")
    tag, value = item
    if tag == "i" and type(value) is int:
        return value
    if tag == "f" and isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if tag == "s" and isinstance(value, str):
        return value
    if tag == "t" and isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp")
        return parsed
    raise ValueError("malformed key value")


def encode_cursor(
    *, scope: str, workspace_id: str, filters: Mapping[str, Any], key: Sequence[Any]
) -> str:
    payload = json.dumps(
        {
            "v": _VERSION,
            "s": scope,
            "w": workspace_id,
            "f": _filters_digest(filters),
            "k": [_encode_value(v) for v in key],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    mac = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def decode_cursor(
    cursor: str, *, scope: str, workspace_id: str, filters: Mapping[str, Any], arity: int
) -> list[Any]:
    """The sort key a cursor carries, or ``422 "Invalid cursor."`` -- one answer for every
    failure, so the response says nothing about which check refused it."""
    try:
        payload_part, mac_part = cursor.split(".")
        payload = _unb64(payload_part)
        expected = hmac.new(_key(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(mac_part)):
            raise ValueError("bad mac")
        data = json.loads(payload)
        if not isinstance(data, dict) or set(data) != {"v", "s", "w", "f", "k"}:
            raise ValueError("bad shape")
        if (data["v"], data["s"], data["w"], data["f"]) != (
            _VERSION,
            scope,
            workspace_id,
            _filters_digest(filters),
        ):
            raise ValueError("cursor bound elsewhere")
        if not isinstance(data["k"], list) or len(data["k"]) != arity:
            raise ValueError("bad key")
        return [_decode_value(item) for item in data["k"]]
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValidationDomainError(INVALID_CURSOR) from exc


def _after(columns: Sequence[InstrumentedAttribute], values: Sequence[Any], descending: bool):
    """Rows strictly after ``values`` in the ordering ``columns`` (lexicographic)."""
    clauses = []
    for i, column in enumerate(columns):
        equal_prefix = [columns[j] == values[j] for j in range(i)]
        beyond = column < values[i] if descending else column > values[i]
        clauses.append(and_(*equal_prefix, beyond))
    return or_(*clauses)


def paginate(
    db: Session,
    stmt: Select,
    *,
    order: Sequence[InstrumentedAttribute],
    descending: bool,
    scope: str,
    workspace_id: str,
    filters: Mapping[str, Any],
    cursor: str | None,
    limit: int | None,
) -> Page:
    """Run ``stmt`` (already filtered, not yet ordered) as one keyset page.

    ``order`` must end with the primary key so the ordering is total. ``limit=None`` with
    no cursor returns every row (the unpaginated behaviour of routes that had no limit);
    with a cursor it means :data:`DEFAULT_PAGE_SIZE`.
    """
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    page_stmt = stmt
    if cursor is not None:
        values = decode_cursor(
            cursor, scope=scope, workspace_id=workspace_id, filters=filters, arity=len(order)
        )
        page_stmt = page_stmt.where(_after(order, values, descending))
        if limit is None:
            limit = DEFAULT_PAGE_SIZE
    page_stmt = page_stmt.order_by(*(c.desc() if descending else c.asc() for c in order))
    if limit is None:
        return Page(list(db.execute(page_stmt).scalars()), int(total or 0), None)
    rows = list(db.execute(page_stmt.limit(limit + 1)).scalars())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(
            scope=scope,
            workspace_id=workspace_id,
            filters=filters,
            key=[getattr(last, c.key) for c in order],
        )
    return Page(rows, int(total or 0), next_cursor)
