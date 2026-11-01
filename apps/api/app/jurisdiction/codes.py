"""The jurisdiction-code vocabulary (Project Phase 5D; frozen at W21 CF-3).

A **jurisdiction code** is an ISO 3166-1 alpha-2 country code, optionally followed by ``-``
and an ISO 3166-2 subdivision part of one to three letters or digits: ``GB``, ``KE``,
``US-TX``, ``NG-LA``, ``GB-ENG``. It is the same shape as the region identity the Phase 6
market engine already uses (``app.geography.markets``: ``"US-TX"``, ``"GB-ENG"``), so a
resolver can derive one from the other without a second spelling. Upper case only.

**``UNRESOLVED`` is a value of the resolver's result, never of a column** (Phase 5D plan §2,
the reconciled nullability decision). It means "the resolver ran and failed", and every
consumer treats it as *deny*. A stored ``NULL`` means "no resolution was ever attempted or
captured", a different state; the sentinel is therefore refused by every jurisdiction
column's CHECK, so the two states cannot be collapsed in storage.

**Two layers.**

* :data:`JURISDICTION_CODE_PATTERN` / :func:`is_jurisdiction_code` / :data:`JurisdictionCode`
  are the exact grammar. Schemas and services validate with them.
* :func:`jurisdiction_code_check_sql` renders the database CHECK every jurisdiction column
  carries. It is a **portable shape bound**, not the full grammar: SQLite and PostgreSQL
  share no regular-expression operator, so it asserts upper case, a length of 2, or 4 to 6
  with ``-`` third. That refuses the sentinel (10 characters), lower case, ``USA``, ``US_TX``
  and an empty string; it does not refuse every malformed code (``U1``, ``US--X`` pass it),
  which the grammar above does.

The resolver (``resolve_jurisdiction``) and its result type belong to lane 5D-W2.
"""

from __future__ import annotations

import re
from typing import Annotated, Final

from pydantic import StringConstraints

#: The resolver's "ran and failed" result. Never stored (see the module docstring).
UNRESOLVED: Final = "UNRESOLVED"

#: The longest code: two letters, ``-`` and three subdivision characters.
JURISDICTION_CODE_MAX_LENGTH: Final = 6

#: The exact grammar of a jurisdiction code.
JURISDICTION_CODE_PATTERN: Final = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$")

#: A jurisdiction code as a Pydantic field type (the exact grammar).
JurisdictionCode = Annotated[
    str,
    StringConstraints(
        pattern=JURISDICTION_CODE_PATTERN.pattern, max_length=JURISDICTION_CODE_MAX_LENGTH
    ),
]


def is_jurisdiction_code(value: object) -> bool:
    """True when ``value`` is a jurisdiction code (never for :data:`UNRESOLVED`)."""
    return isinstance(value, str) and JURISDICTION_CODE_PATTERN.fullmatch(value) is not None


def jurisdiction_code_check_sql(column: str = "jurisdiction_code", *, nullable: bool) -> str:
    """The portable CHECK every jurisdiction column carries (a shape bound; see above).

    ``nullable=False`` (the ``exports`` table, born jurisdiction-aware) drops the ``IS NULL``
    limb (the column's ``NOT NULL`` is what refuses a NULL: a CHECK that evaluates to NULL is
    satisfied).
    """
    shape = (
        f"upper({column}) = {column} AND (length({column}) = 2 OR "
        f"(length({column}) BETWEEN 4 AND {JURISDICTION_CODE_MAX_LENGTH} "
        f"AND substr({column}, 3, 1) = '-'))"
    )
    return f"{column} IS NULL OR ({shape})" if nullable else shape


__all__ = [
    "JURISDICTION_CODE_MAX_LENGTH",
    "JURISDICTION_CODE_PATTERN",
    "UNRESOLVED",
    "JurisdictionCode",
    "is_jurisdiction_code",
    "jurisdiction_code_check_sql",
]
