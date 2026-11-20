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

Format (frozen at CF-1, with the one exception below): ``"<user id> <full name> <<email>>"``,
at most :data:`ACTOR_SNAPSHOT_MAX_LENGTH` characters. The id leads, so the erasure matcher
finds the account in every snapshot of it.

**The e-mail component: the operator's narrow exception to the frozen format (O-1, register
§49, 2026-11-20).** "Include the email component only when verification applies to the email
value being recorded. Otherwise use a consistent fixed marker." :func:`account_snapshot` is the
one place a snapshot is made from an account: the address is recorded only when
``users.email_verified_at`` is set; otherwise the component is :data:`UNVERIFIED_EMAIL_MARKER`
and the snapshot reads ``"<user id> <full name> <[unverified]>"``. The id, the name, the
``" <…>"`` shape and the bound are unchanged. So an address an account never proved, such as
one an invitee-email erasure leaves on an unverified holder's ``users`` row, is not copied into
a new compliance record. Rows written before this rule are not rewritten.

Why ``email_verified_at`` decides for the value recorded: ``users.email`` is written only when
the account is created and never changed afterwards, and ``email_verified_at`` is set only by
confirming a verification token minted for the stored address or by completing a password
reset mailed to it, and is never cleared. When it is set, it was set for the address the
snapshot records. An account object read earlier in the request can only lag behind a
verification, never run ahead of it, so a stale copy can only record the marker for an address
verified meanwhile, never the address of an account that has not verified it.

**What an API consumer sees.** ``created_by_snapshot`` (the recommendation-brief create, list,
read and approval-queue answers, and the creative-draft list and read answers),
``decided_by_snapshot`` (the brief and draft decision answers and decision histories) and
``exported_by_snapshot`` (the export answer and export history) carry ``<[unverified]>`` in the
e-mail position for every record written, after this rule, by an account whose address is not
verified. The leading id and the name are unchanged, and ``"[erased]"`` is unchanged. The
account and organization privacy exports return the stored values. A consumer that read the
address out of a snapshot reads ``[unverified]``, which is not an address, for those records.

The raw formatter :func:`actor_snapshot` takes the address it is given; production code calls
:func:`account_snapshot` (``app/tests/test_o1_verified_email_snapshot.py`` pins that).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

#: users.id (32) + full_name (200) + email (320) + separators, with headroom.
ACTOR_SNAPSHOT_MAX_LENGTH = 600
#: What an erased account's snapshot becomes. Equal to ``app.privacy.erasure.ERASED``.
ERASED_ACTOR_SNAPSHOT = "[erased]"
#: The e-mail component of a snapshot whose account has not verified its address (O-1,
#: register §49). Not an address, never matched as one by :mod:`app.privacy.matching`, and
#: not :data:`ERASED_ACTOR_SNAPSHOT`.
UNVERIFIED_EMAIL_MARKER = "[unverified]"


class SnapshotAccount(Protocol):
    """What :func:`account_snapshot` reads from a ``users`` row."""

    id: str
    full_name: str
    email: str
    email_verified_at: datetime | None


def actor_snapshot(*, user_id: str, full_name: str, email: str) -> str:
    """Format one snapshot from the values given (the e-mail component as given)."""
    value = f"{user_id} {full_name} <{email}>"
    if len(value) > ACTOR_SNAPSHOT_MAX_LENGTH:
        raise ValueError("actor snapshot exceeds its bound")
    return value


def account_snapshot(account: SnapshotAccount) -> str:
    """The immutable snapshot of ``account`` at the instant it acts: its address only when
    the address is verified, otherwise :data:`UNVERIFIED_EMAIL_MARKER`."""
    verified = account.email_verified_at is not None
    return actor_snapshot(
        user_id=account.id,
        full_name=account.full_name,
        email=account.email if verified else UNVERIFIED_EMAIL_MARKER,
    )


__all__ = [
    "ACTOR_SNAPSHOT_MAX_LENGTH",
    "ERASED_ACTOR_SNAPSHOT",
    "UNVERIFIED_EMAIL_MARKER",
    "SnapshotAccount",
    "account_snapshot",
    "actor_snapshot",
]
