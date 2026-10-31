"""Finding identity: one current card per finding across a Scout's runs (W16, PROPOSED).

Contract W16-CUSTOMER-REPAIR-CONTRACT-01 §A / §I.1-§I.4. A card is identified, within its Scout
(``organization_id``, ``workspace_id``, ``scout_request_id``), by the set of evidence items it was
built from - never by its title, cluster label, scores or any model-generated text.

* **Item identity** ``(source_type, source identity, content digest)``, computed from the stored
  ``normalized_signals`` row (so a run and a later reading of an older card agree). The source
  identity is the item's provenance key where one is recorded: ``source_key`` plus the item's own
  id (its feed guid, else its link); else its feed guid; else its URL. A URL alone is never the
  whole identity: the content digest (``content_hash``, SHA-256 of the normalized text) is always
  part of it, because most simulated items share one URL.
* **Finding key** ``f1:`` + SHA-256 over the sorted item identities. The prefix versions the rule:
  a later rule gives different keys (a new card), never a false match.

Nothing here writes; :mod:`app.jobs.pipeline` stores the key and the sightings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.opportunities.models import FINDING_KEY_PREFIX, Opportunity, OpportunityObservation
from app.signals.models import NormalizedSignal


def item_identity(
    *,
    source_type: str,
    source_key: str | None,
    source_url: str | None,
    content_hash: str,
    ingest_metadata: dict | None,
) -> tuple[str, str, str]:
    """The identity of one evidence item, from the fields its ``normalized_signals`` row stores."""
    provenance = (ingest_metadata or {}).get("provenance") or {}
    guid = provenance.get("item_guid")
    if source_key:
        item = guid or provenance.get("source_url") or source_url or ""
        source_identity = f"key:{source_key}|{item}"
    elif guid:
        source_identity = f"guid:{guid}"
    else:
        source_identity = f"url:{source_url or ''}"
    return (source_type, source_identity, content_hash)


def identity_of(signal: Any) -> tuple[str, str, str]:
    """:func:`item_identity` of a ``NormalizedSignal`` (or a row with the same attributes)."""
    return item_identity(
        source_type=signal.source_type,
        source_key=signal.source_key,
        source_url=signal.source_url,
        content_hash=signal.content_hash,
        ingest_metadata=signal.ingest_metadata,
    )


def finding_key(members: Iterable[Any]) -> str | None:
    """The finding key of a set of member signals; ``None`` for an empty set."""
    items = sorted(
        json.dumps(list(identity_of(m)), separators=(",", ":"), ensure_ascii=True) for m in members
    )
    if not items:
        return None
    return FINDING_KEY_PREFIX + hashlib.sha256("\n".join(items).encode()).hexdigest()


def legacy_cards_by_key(
    db: Session, *, organization_id: str, workspace_id: str, scout_request_id: str
) -> dict[str, str]:
    """The Scout's identity-less cards (created before the finding identity existed), keyed by
    the finding key their OWN stored member rows give: key -> the OLDEST such card's id.

    A card whose cluster or members are gone (evidence retention) cannot be keyed and is left
    out. Two statements, read-only."""
    cards = db.execute(
        select(Opportunity.id, Opportunity.cluster_id)
        .where(
            Opportunity.organization_id == organization_id,
            Opportunity.workspace_id == workspace_id,
            Opportunity.scout_request_id == scout_request_id,
            Opportunity.finding_key.is_(None),
            Opportunity.cluster_id.is_not(None),
        )
        .order_by(Opportunity.created_at, Opportunity.id)
    ).all()
    if not cards:
        return {}
    members: dict[str, list[Any]] = {}
    for row in db.execute(
        select(
            NormalizedSignal.cluster_id,
            NormalizedSignal.source_type,
            NormalizedSignal.source_key,
            NormalizedSignal.source_url,
            NormalizedSignal.content_hash,
            NormalizedSignal.ingest_metadata,
        ).where(
            NormalizedSignal.organization_id == organization_id,
            NormalizedSignal.workspace_id == workspace_id,
            NormalizedSignal.scout_request_id == scout_request_id,
            NormalizedSignal.cluster_id.in_([c.cluster_id for c in cards]),
        )
    ).all():
        members.setdefault(row.cluster_id, []).append(row)
    out: dict[str, str] = {}
    for card in cards:  # oldest first: the first card seen for a key is kept
        key = finding_key(members.get(card.cluster_id, ()))
        if key is not None:
            out.setdefault(key, card.id)
    return out


def sightings(
    db: Session, *, workspace_id: str, opportunity_ids: list[str]
) -> dict[str, tuple[int, datetime | None]]:
    """``id -> (seen_count, last_seen_at)`` for a page of cards, in ONE statement.

    ``seen_count`` counts the card's recorded sightings, plus one when it has no ``first_seen``
    row (a card created before sightings were recorded, its creation being its first sighting).
    ``last_seen_at`` is the latest sighting, ``None`` when there is none. A card with no row is
    absent from the result (seen once, at its creation)."""
    if not opportunity_ids:
        return {}
    rows = db.execute(
        select(
            OpportunityObservation.opportunity_id,
            func.count(),
            func.max(OpportunityObservation.observed_at),
            func.sum(case((OpportunityObservation.kind == "first_seen", 1), else_=0)),
        )
        .where(
            OpportunityObservation.workspace_id == workspace_id,
            OpportunityObservation.opportunity_id.in_(opportunity_ids),
        )
        .group_by(OpportunityObservation.opportunity_id)
    ).all()
    return {card_id: (int(n) + (0 if first else 1), last) for card_id, n, last, first in rows}


def with_sightings(db: Session, workspace_id: str, cards: list[Opportunity], model):
    """The cards as ``model`` (``OpportunityCard`` or a subclass), with their sightings."""
    seen = sightings(db, workspace_id=workspace_id, opportunity_ids=[c.id for c in cards])
    out = []
    for card in cards:
        item = model.model_validate(card)
        if card.id in seen:
            item.seen_count, item.last_seen_at = seen[card.id]
        out.append(item)
    return out


__all__ = [
    "finding_key",
    "identity_of",
    "item_identity",
    "legacy_cards_by_key",
    "sightings",
    "with_sightings",
]
