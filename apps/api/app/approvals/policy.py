"""Exact-role policy for approvals (Project Phase 5B; frozen at W21 CF-1).

Operator decision O-2 (``REC_Role_REVIEWER_plus_org_scoped_assignment``): ``Role.REVIEWER``
is the approver role, granted organization-wide through the existing member-role
assignment (``PUT /organizations/{organization_id}/members/{user_id}/role``). Operator
decision O-1: approval decisions are gated by exact role membership, never by a rank floor.

This module reuses the Phase 6 primitive :func:`app.auth.dependencies.require_exact_roles`
(PR #179) and adds no authorization mechanism. It mounts nothing: the 5B routes attach
:data:`require_approver` to the decision endpoints.
"""

from __future__ import annotations

from app.auth.dependencies import require_exact_roles
from app.core.enums import Role

#: The roles that may record an approval decision: exactly REVIEWER (O-2). OWNER and
#: ADMIN do not inherit it — rank plays no part (O-1).
APPROVER_ROLES: frozenset[Role] = frozenset({Role.REVIEWER})

#: The exact-set dependency for approval-decision endpoints.
require_approver = require_exact_roles(*sorted(APPROVER_ROLES))


__all__ = ["APPROVER_ROLES", "require_approver"]
