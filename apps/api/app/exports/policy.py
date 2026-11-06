"""Exact-role policy for audited manual export (Project Phase 5E; frozen at W21 CF-3).

Named here so they are reviewable and adjustable by the operator (Phase 5E plan §2); both
reuse the Phase 6 primitive :func:`app.auth.dependencies.require_exact_roles` (operator
decision O-1: exact membership, never a rank floor). This module mounts nothing.

* **Exporting** (``POST /workspaces/{ws}/drafts/{draft_id}/exports``, precondition 4):
  exactly OWNER, ADMIN or MARKETER.
* **Reading the export history** (``GET …/exports``): the analytics read set — exactly
  OWNER, ADMIN, MARKETER or REVIEWER — because the history exposes
  ``exported_by_snapshot``, the compliance identity (the plan's "same gate" as analytics).
"""

from __future__ import annotations

from app.auth.dependencies import require_exact_roles
from app.core.enums import Role

#: The roles that may export an approved draft (5E §2 precondition 4).
EXPORTER_ROLES: frozenset[Role] = frozenset({Role.OWNER, Role.ADMIN, Role.MARKETER})
#: The roles that may read a draft's export history (the analytics read set).
EXPORT_HISTORY_READER_ROLES: frozenset[Role] = frozenset(
    {Role.OWNER, Role.ADMIN, Role.MARKETER, Role.REVIEWER}
)

#: The exact-set dependency for the export endpoint.
require_exporter = require_exact_roles(*sorted(EXPORTER_ROLES))
#: The exact-set dependency for the export-history read.
require_export_history_reader = require_exact_roles(*sorted(EXPORT_HISTORY_READER_ROLES))


__all__ = [
    "EXPORTER_ROLES",
    "EXPORT_HISTORY_READER_ROLES",
    "require_export_history_reader",
    "require_exporter",
]
