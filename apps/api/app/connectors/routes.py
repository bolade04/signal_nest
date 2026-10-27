"""Operator-only live-source registry and takedown (``/internal/system/data-sources``).

``GET`` lists every registry record as its public provenance / terms record
(P6-PRIV-4), whether it can run, any takedown, and how many signals the database holds
for it right now. ``POST …/{source_key}/takedown`` disables a source permanently and
purges its signals in every tenant (:mod:`app.connectors.takedown`), answering with
both what was purged and what remains, recounted after the purge.

The takedown record itself holds only the source key, the time and counts (D-9d). The
reason for a takedown is kept in the referenced request record, not in the product;
the product keeps attribution (the acting operator) and the public ``reference`` in
each affected workspace's audit log.

Both routes require an authenticated operator (``require_operator``) and are
platform-wide: a source is not a tenant resource.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_operator
from app.connectors.models import SourceTakedown
from app.connectors.takedown import (
    REFERENCE_MAX_LENGTH,
    known_sources,
    source_signal_counts,
    takedown_source,
    validate_reference,
)
from app.core.config import get_settings
from app.core.errors import ValidationDomainError
from app.db.clock import as_utc
from app.db.session import get_db
from app.organizations.models import User

router = APIRouter(prefix="/internal/system/data-sources", tags=["internal"])


class TakedownRepeatOut(BaseModel):
    """One repeated takedown: when, and the counts it removed. Nothing identifying."""

    at: datetime
    purged: dict[str, int]


class TakedownOut(BaseModel):
    """The permanent suppression record: key, time and counts only (D-9d). Who took the
    source down and the public reference are in each affected workspace's audit log."""

    taken_down_at: datetime
    #: What the first takedown removed; never overwritten by a repeat.
    purge_summary: dict[str, int]
    #: Each repeated takedown, oldest first.
    repeat_purges: list[TakedownRepeatOut]


class DataSourceOut(BaseModel):
    source_key: str
    connector: str | None
    display_name: str | None
    publisher: str | None
    feed_url: str | None
    markets: list[str]
    tos_url: str | None
    robots_url: str | None
    robots_disposition: str | None
    approval_reference: str | None
    approved_on: str | None
    legal_status: str | None
    enabled: bool
    #: Enabled, approved and well formed (live egress still needs the global switch).
    activatable: bool
    takedown: TakedownOut | None
    #: Signals the database holds for this source, counted at request time.
    stored: dict[str, int]


class DataSourcesOut(BaseModel):
    live_egress_enabled: bool
    kill_switch: bool
    replay_mode: bool
    sources: list[DataSourceOut]


class TakedownIn(BaseModel):
    """A takedown request: the public id of the request record it answers, nothing else.

    There is deliberately no free-text field. The reason for a takedown is kept in the
    referenced request record, not in the product; a tenant reads its own audit log, so
    no operator text may reach it (adversarial r2-MINOR-2; D-9e interim).
    """

    model_config = ConfigDict(extra="forbid")

    #: Public ticket / request id (1–200 printable characters, one line, no ``@``: never
    #: an address or a URL with credentials). Copied into every affected workspace's
    #: audit entry.
    reference: str = Field(min_length=1, max_length=REFERENCE_MAX_LENGTH)

    @field_validator("reference")
    @classmethod
    def _public_reference(cls, value: str) -> str:
        try:
            return validate_reference(value)
        except ValidationDomainError as exc:
            raise ValueError(str(exc)) from None


class TakedownResultOut(BaseModel):
    source_key: str
    taken_down_at: datetime
    already_taken_down: bool
    purged: dict[str, int]
    remaining: dict[str, int]
    #: True when every remaining count is zero.
    verified: bool


def _takedown_out(row: SourceTakedown | None) -> TakedownOut | None:
    if row is None:
        return None
    return TakedownOut(
        taken_down_at=as_utc(row.taken_down_at),
        purge_summary=dict(row.purge_summary or {}),
        repeat_purges=[TakedownRepeatOut(**entry) for entry in (row.repeat_purges or [])],
    )


@router.get("", response_model=DataSourcesOut)
def list_data_sources(
    _: User = Depends(require_operator), db: Session = Depends(get_db)
) -> DataSourcesOut:
    settings = get_settings()
    takedowns = {t.source_key: t for t in db.scalars(select(SourceTakedown))}
    out: list[DataSourceOut] = []
    for source in known_sources():
        record = source.terms_record()
        out.append(
            DataSourceOut(
                **record,
                activatable=source.is_activatable(),
                takedown=_takedown_out(takedowns.pop(source.source_key, None)),
                stored=source_signal_counts(db, source.source_key),
            )
        )
    # Taken-down keys whose record has since left the registry stay visible.
    for key, row in sorted(takedowns.items()):
        out.append(
            DataSourceOut(
                source_key=key,
                connector=None,
                display_name=None,
                publisher=None,
                feed_url=None,
                markets=[],
                tos_url=None,
                robots_url=None,
                robots_disposition=None,
                approval_reference=None,
                approved_on=None,
                legal_status=None,
                enabled=False,
                activatable=False,
                takedown=_takedown_out(row),
                stored=source_signal_counts(db, key),
            )
        )
    return DataSourcesOut(
        live_egress_enabled=settings.connector_rss_live_enabled,
        kill_switch=settings.connector_rss_kill_switch,
        replay_mode=bool(settings.connector_rss_replay_origin),
        sources=out,
    )


@router.post("/{source_key}/takedown", response_model=TakedownResultOut)
def take_down_data_source(
    source_key: str,
    body: TakedownIn,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> TakedownResultOut:
    """Take a live source down permanently and purge its signals in every tenant.

    The reason for a takedown is kept in the referenced request record, not in the
    product; the product keeps attribution and the public reference in each affected
    workspace's audit log. The suppression record itself holds only the source key, the
    time and counts.
    """
    result = takedown_source(
        db,
        source_key=source_key,
        reference=body.reference,
        actor_user_id=user.id,
    )
    db.commit()
    return TakedownResultOut(
        source_key=source_key,
        taken_down_at=as_utc(result.takedown.taken_down_at),
        already_taken_down=result.already_taken_down,
        purged=result.purged,
        remaining=result.remaining,
        verified=result.verified,
    )
