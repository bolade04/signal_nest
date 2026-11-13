"""Exact-role policy for audited manual export (Project Phase 5E; frozen at W21 CF-3).

Named here so they are reviewable and adjustable by the operator (Phase 5E plan §2); both
reuse the Phase 6 primitive :func:`app.auth.dependencies.require_exact_roles` (operator
decision O-1: exact membership, never a rank floor). This module mounts nothing.

* **Exporting** (``POST /workspaces/{ws}/drafts/{draft_id}/exports``, precondition 4):
  exactly OWNER, ADMIN or MARKETER.
* **Reading the export history** (``GET …/exports``): exactly OWNER, ADMIN, MARKETER,
  REVIEWER or COMPLIANCE_REVIEWER — the analytics read set
  (:data:`app.analytics.policy.ANALYTICS_READER_ROLES`) plus COMPLIANCE_REVIEWER. The history
  exposes ``exported_by_snapshot``, the compliance identity, and records what left the
  system, which is the compliance role's concern; the audit read already admits that role.
  Operator ruling D-N4 item 32 (2026-11-12): "add COMPLIANCE_REVIEWER to export history, as
  recommended. Keep export creation and analytics role sets as specified." So the history
  set is no longer the analytics set: export creation and analytics keep their CF-3 sets, and
  only the history read gains COMPLIANCE_REVIEWER. VIEWER is admitted to none of them.
"""

from __future__ import annotations

from app.auth.dependencies import require_exact_roles
from app.core.enums import Role

#: The roles that may export an approved draft (5E §2 precondition 4).
EXPORTER_ROLES: frozenset[Role] = frozenset({Role.OWNER, Role.ADMIN, Role.MARKETER})
#: The roles that may read a draft's export history: the analytics read set plus
#: COMPLIANCE_REVIEWER (operator ruling D-N4 item 32).
EXPORT_HISTORY_READER_ROLES: frozenset[Role] = frozenset(
    {Role.OWNER, Role.ADMIN, Role.MARKETER, Role.REVIEWER, Role.COMPLIANCE_REVIEWER}
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
