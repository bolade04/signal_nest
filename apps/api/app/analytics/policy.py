"""Exact-role read policy for status analytics (Project Phase 5E; frozen at W21 CF-3).

Phase 5E plan §2: analytics reads are gated by ``require_exact_roles(OWNER, ADMIN, MARKETER,
REVIEWER)`` on top of the workspace scope — VIEWER and COMPLIANCE_REVIEWER are not admitted —
because the aggregates include approval latency and generation/review outcomes, the
sensitivity class the audit read route is narrowed for (O-1). The set is named here so it is
reviewable and adjustable by the operator. It reuses the Phase 6 primitive and mounts nothing.

The capability gate is separate from this role set. Operator decision O-12 was ruled on
2026-11-12: "Choose O-12(B): one additional flag for the notifications and analytics surfaces,
off by default. Explicitly amend O-4’s three-flag limit. Gate the backend routes, customer UI
and applicable notification emission consistently." The analytics route
(``app.analytics.routes``) therefore answers 503 while ``notifications_analytics`` does not
resolve enabled for the workspace, after this role check.
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
