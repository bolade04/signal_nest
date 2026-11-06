"""Exact-role read policy for status analytics (Project Phase 5E; frozen at W21 CF-3).

Phase 5E plan §2: analytics reads are gated by ``require_exact_roles(OWNER, ADMIN, MARKETER,
REVIEWER)`` on top of the workspace scope — VIEWER and COMPLIANCE_REVIEWER are not admitted —
because the aggregates include approval latency and generation/review outcomes, the
sensitivity class the audit read route is narrowed for (O-1). The set is named here so it is
reviewable and adjustable by the operator. It reuses the Phase 6 primitive and mounts nothing.

Analytics carries **no flag and no capability**: whether it should is open operator decision
O-12. When 5E-W2 mounts it, it is live (it aggregates existing opportunities).
"""

from __future__ import annotations

from app.auth.dependencies import require_exact_roles
from app.core.enums import Role

#: The roles that may read status analytics (exact membership).
ANALYTICS_READER_ROLES: frozenset[Role] = frozenset(
    {Role.OWNER, Role.ADMIN, Role.MARKETER, Role.REVIEWER}
)

#: The exact-set dependency for every analytics read.
require_analytics_reader = require_exact_roles(*sorted(ANALYTICS_READER_ROLES))


__all__ = ["ANALYTICS_READER_ROLES", "require_analytics_reader"]
