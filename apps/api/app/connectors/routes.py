"""Operator-only live-source registry and takedown (``/internal/system/data-sources``).

``GET`` lists every registry record as its public provenance / terms record
(P6-PRIV-4), whether it can run, any takedown, and how many signals the database holds
for it right now. ``POST …/{source_key}/takedown`` disables a source permanently and
purges its signals in every tenant (:mod:`app.connectors.takedown`), answering with
both what was purged and what remains, recounted after the purge.

The takedown record itself holds only the source key, the time and counts (D-9d). The
operator's free-text reason and own ticket id are kept in a restricted operator case
record (D-9e), readable only through ``GET …/{source_key}/cases``; its access and
retention policy is separately approved and still pending. A customer-visible audit
entry carries a fixed generic explanation and the case's opaque id, never the reason.

Every route requires an authenticated operator (``require_operator``) and is
platform-wide: a source is not a tenant resource.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_operator
from app.connectors.models import SourceTakedown, SourceTakedownCase
from app.connectors.takedown import (
    EXTERNAL_REFERENCE_MAX_LENGTH,
    REASON_MAX_LENGTH,
    known_sources,
    source_signal_counts,
    takedown_source,
    validate_external_reference,
    validate_reason,
)
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationDomainError
from app.db.clock import as_utc
from app.db.session import get_db
from app.organizations.models import User

router = APIRouter(prefix="/internal/system/data-sources", tags=["internal"])


class TakedownRepeatOut(BaseModel):
    """One repeated takedown: when, and the counts it removed. Nothing identifying."""

    at: datetime
    purged: dict[str, int]


class TakedownOut(BaseModel):
    """The permanent suppression record: key, time and counts only (D-9d), plus how many
    operator case records exist and the latest one's opaque id (never the reason)."""

    taken_down_at: datetime
    #: What the first takedown removed; never overwritten by a repeat.
    purge_summary: dict[str, int]
    #: Each repeated takedown, oldest first.
    repeat_purges: list[TakedownRepeatOut]
    #: Operator case records of this source's takedown requests (one per request).
    cases_count: int
    #: Opaque id of the newest case record; its contents are read through the cases route.
    latest_case_id: str | None


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
    """A takedown request. Both fields go ONLY into the restricted operator case record.

    ``reason`` (required, 1–2000 characters) is the operator's free text and
    ``external_reference`` (optional, at most 200 characters, one line) the operator's own
    ticket id. Neither reaches a customer-visible audit entry, export, log or metric: a
    tenant reads its audit log and may only see a generic explanation and the case's
    opaque id (D-9e).
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)
    external_reference: str | None = Field(default=None, max_length=EXTERNAL_REFERENCE_MAX_LENGTH)

    @field_validator("reason")
    @classmethod
    def _private_reason(cls, value: str) -> str:
        try:
            return validate_reason(value)
        except ValidationDomainError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("external_reference")
    @classmethod
    def _private_external_reference(cls, value: str | None) -> str | None:
        try:
            return validate_external_reference(value)
        except ValidationDomainError as exc:
            raise ValueError(str(exc)) from None


class TakedownCaseOut(BaseModel):
    """One operator case record. Operator-only: this is the only place the reason is read."""

    case_id: str
    source_key: str
    reason: str
    external_reference: str | None
    actor_user_id: str | None
    created_at: datetime


class TakedownCasesOut(BaseModel):
    source_key: str
    cases: list[TakedownCaseOut]


class TakedownResultOut(BaseModel):
    source_key: str
    taken_down_at: datetime
    already_taken_down: bool
    purged: dict[str, int]
    remaining: dict[str, int]
    #: True when every remaining count is zero.
    verified: bool
    #: Opaque id of the operator case record this request created; the only reference
    #: the affected workspaces' audit entries carry.
    case_id: str


def _case_summaries(db: Session) -> dict[str, tuple[int, str | None]]:
    """``source_key -> (cases_count, latest case id)``; never the case contents."""
    summary: dict[str, tuple[int, str | None]] = {}
    counts = {
        key: n
        for key, n in db.execute(
            select(SourceTakedownCase.source_key, func.count()).group_by(
                SourceTakedownCase.source_key
            )
        )
    }
    for key in counts:
        latest = db.scalar(
            select(SourceTakedownCase.id)
            .where(SourceTakedownCase.source_key == key)
            .order_by(SourceTakedownCase.created_at.desc(), SourceTakedownCase.id.desc())
            .limit(1)
        )
        summary[key] = (counts[key], latest)
    return summary


def _takedown_out(
    row: SourceTakedown | None, cases: dict[str, tuple[int, str | None]]
) -> TakedownOut | None:
    if row is None:
        return None
    count, latest = cases.get(row.source_key, (0, None))
    return TakedownOut(
        cases_count=count,
        latest_case_id=latest,
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
    cases = _case_summaries(db)
    out: list[DataSourceOut] = []
    for source in known_sources():
        record = source.terms_record()
        out.append(
            DataSourceOut(
                **record,
                activatable=source.is_activatable(),
                takedown=_takedown_out(takedowns.pop(source.source_key, None), cases),
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
                takedown=_takedown_out(row, cases),
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

    The operator's ``reason`` and ``external_reference`` are written to a restricted
    operator case record only (read through the operator-only cases route); each
    affected workspace's audit log gets a fixed generic explanation, the acting operator
    and the case's opaque ``case_id`` — never the reason. The suppression record itself
    holds only the source key, the time and counts.
    """
    result = takedown_source(
        db,
        source_key=source_key,
        reason=body.reason,
        external_reference=body.external_reference,
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
        case_id=result.case_id,
    )


@router.get("/{source_key}/cases", response_model=TakedownCasesOut)
def list_takedown_cases(
    source_key: str,
    _: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> TakedownCasesOut:
    """The operator case records of ``source_key``'s takedown requests, oldest first.

    Operator-only (a customer, including an OWNER, gets the existing non-enumerable
    403). This is the one route that returns the private reason and external reference;
    their retention is pending the operator's separately approved policy.
    """
    rows = list(
        db.scalars(
            select(SourceTakedownCase)
            .where(SourceTakedownCase.source_key == source_key)
            .order_by(SourceTakedownCase.created_at, SourceTakedownCase.id)
        )
    )
    taken_down = db.scalar(select(SourceTakedown.id).where(SourceTakedown.source_key == source_key))
    if not rows and taken_down is None:
        raise NotFoundError("No takedown has been recorded for this source key.")
    return TakedownCasesOut(
        source_key=source_key,
        cases=[
            TakedownCaseOut(
                case_id=c.id,
                source_key=c.source_key,
                reason=c.reason,
                external_reference=c.external_reference,
                actor_user_id=c.actor_user_id,
                created_at=as_utc(c.created_at),
            )
            for c in rows
        ],
    )
