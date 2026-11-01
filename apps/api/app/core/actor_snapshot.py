"""The actor snapshot carried by Phase 5 compliance records (W21 CF-1).

``recommendation_briefs.created_by_snapshot`` and ``approvals.decided_by_snapshot`` store
who acted, as they were at that instant, in one immutable string: the account id plus its
display identity. The foreign key beside each (``created_by`` / ``decided_by``) is
``ON DELETE SET NULL``; the snapshot is what still says "this account, then known as …"
after the account's name or address changes.

**On erasure (operator decision O-6 as Phase 6 already encodes it for ``audit_logs``):**
the record survives, and every value naming an erased account is cleared. Deleting the
account replaces the whole snapshot with :data:`ERASED_ACTOR_SNAPSHOT` (the same marker
``app.privacy.erasure.ERASED`` writes into audit records); the row, its decision, the
decided role and its timestamps are kept. See :mod:`app.privacy.erasure`.

Format (frozen at CF-1): ``"<user id> <full name> <<email>>"``, at most
:data:`ACTOR_SNAPSHOT_MAX_LENGTH` characters. The id leads, so the erasure matcher finds the
account in every snapshot of it.
"""

from __future__ import annotations

#: users.id (32) + full_name (200) + email (320) + separators, with headroom.
ACTOR_SNAPSHOT_MAX_LENGTH = 600
#: What an erased account's snapshot becomes. Equal to ``app.privacy.erasure.ERASED``.
ERASED_ACTOR_SNAPSHOT = "[erased]"


def actor_snapshot(*, user_id: str, full_name: str, email: str) -> str:
    """The immutable snapshot of one account at the instant it acts."""
    value = f"{user_id} {full_name} <{email}>"
    if len(value) > ACTOR_SNAPSHOT_MAX_LENGTH:
        raise ValueError("actor snapshot exceeds its bound")
    return value


__all__ = ["ACTOR_SNAPSHOT_MAX_LENGTH", "ERASED_ACTOR_SNAPSHOT", "actor_snapshot"]
