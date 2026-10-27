"""D-9d: ``source_takedowns`` is a suppression record free of identifying metadata, and
the suppression outlives every piece of metadata around it (P6-PRIV-4).

Two proofs, on a database of this module's own:

* **Nested identifying metadata.** After a first takedown, a repeat, and the deletion of
  the operator's account, every JSON value in the row (``purge_summary``,
  ``repeat_purges``) is scanned recursively: no key other than ``at`` / ``purged`` / a
  count name, no value equal to any user id, e-mail address or the request reference,
  no string longer than an ISO timestamp. Before D-9d a deleted operator's user id
  survived inside ``repeat_purges`` (the FK column was SET NULL; the JSON copy was not).
* **Suppression persistence.** ``active_takedowns()`` still names the key, the live
  gate (``live_targets_for``) still refuses the source and ``without_taken_down`` still
  drops its signals after each of: the actor's user row deleted; the per-workspace
  audit rows anonymised (actor NULL, context cleared); every audit row for the source
  deleted (audit expiry); ``purge_summary`` / ``repeat_purges`` cleared. Anonymising or
  expiring metadata can never re-enable fetching a taken-down source.

The account-deletion route is not on this branch (#246); the actor is deleted through
the ORM with the schema's own ON DELETE rules (``PRAGMA foreign_keys=ON``), which is
what that route's erasure walk would do to the foreign keys.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.audit.models import AuditLog
from app.brands.models import Brand
from app.connectors.live import SourceTarget, live_targets_for
from app.connectors.models import SourceTakedown
from app.connectors.sources import LegalStatus, SourceRecord
from app.connectors.takedown import (
    AUDIT_ACTION,
    active_takedowns,
    takedown_source,
    validate_reference,
    without_taken_down,
)
from app.core.config import Settings, get_settings
from app.core.enums import ScoutRequestStatus
from app.core.errors import ValidationDomainError
from app.organizations.models import User
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, RawSignal
from app.tests._auth2_support import (
    ALICE,
    BOB,
    EMAIL,
    ORG_A,
    ORG_B,
    WS_A,
    WS_B,
    Env,
    bearer,
    environment,
)

API = get_settings().api_prefix
MARKET = "Toronto, ON"
KEY = "privacy-proof-business"
OTHER = "privacy-proof-food"
REFERENCE = "TKD-PRIV-1"
REPEAT_REFERENCE = "TKD-PRIV-1-repeat"
#: The only keys a repeat entry may carry, and the count names a purge may report.
ENTRY_KEYS = {"at", "purged"}
COUNT_NAMES = {
    "raw_signals",
    "normalized_signals",
    "opportunities_deleted",
    "opportunities_redacted",
    "feedback_removed",
    "clusters_deleted",
}
#: ``datetime.isoformat()`` with microseconds and a UTC offset: the longest JSON string
#: the row may legitimately hold (a timestamp); anything longer is not a count or a time.
ISO_TIMESTAMP_MAX = len(datetime(2026, 1, 1, tzinfo=UTC).replace(microsecond=1).isoformat())
#: The persistent suppression record (D-9d): key, time, counts, row bookkeeping.
MINIMAL_COLUMNS = {
    "id", "source_key", "taken_down_at", "purge_summary", "repeat_purges",
    "created_at", "updated_at",
}


def _record(key: str) -> SourceRecord:
    return SourceRecord(
        source_key=key,
        connector="rss_news",
        display_name=key,
        publisher="Proof Publisher",
        feed_url=f"https://{key}.example/feed.xml",
        markets=(MARKET,),
        tos_url=f"https://{key}.example/terms",
        approval_reference="DEC-PROOF-1",
        approved_on=date(2026, 1, 1),
        legal_status=LegalStatus.APPROVED,
        enabled=True,
    )


TARGETS = [
    SourceTarget(source=_record(k), fetch_url=_record(k).feed_url, delivery="live")
    for k in (KEY, OTHER)
]


def _live_settings() -> Settings:
    return Settings(
        _env_file=None,
        connector_rss_live_enabled=True,
        connector_rss_kill_switch=False,
        connector_rss_replay_origin=None,
        connector_egress_contact_url="https://signalnest.example/bot",
    )


def _seed_tenant(s: Session, org: str, ws: str, slug: str) -> None:
    brand = Brand(organization_id=org, workspace_id=ws, name=slug, industry="coffee")
    s.add(brand)
    s.flush()
    req = ScoutRequest(
        organization_id=org, workspace_id=ws, brand_id=brand.id, name=slug,
        status=ScoutRequestStatus.COMPLETED.value, source_types=[], keywords=["coffee"],
        resolved_market=MARKET,
    )
    s.add(req)
    s.flush()
    for key, n in ((KEY, 2), (OTHER, 1)):
        for i in range(n):
            raw = RawSignal(
                organization_id=org, workspace_id=ws, scout_request_id=req.id,
                source_type="rss_news", content=f"{slug} {key} {i}", is_simulated=False,
                source_key=key, raw_metadata={"provenance": {"source_key": key}},
            )
            s.add(raw)
            s.flush()
            s.add(
                NormalizedSignal(
                    organization_id=org, workspace_id=ws, scout_request_id=req.id,
                    raw_signal_id=raw.id, source_type="rss_news", excerpt=raw.content,
                    content_hash=f"{slug}-{key}-{i}", is_simulated=False, source_key=key,
                    ingest_metadata={"provenance": {"source_key": key}},
                )
            )
    s.flush()


@dataclass
class World:
    env: Env
    factory: sessionmaker

    def session(self) -> Session:
        return self.factory()

    def take_down(self, user_id: str, reference: str) -> dict:
        r = self.env.client.post(
            f"{API}/internal/system/data-sources/{KEY}/takedown",
            headers={"Authorization": f"Bearer {bearer(self.env, user_id)}"},
            json={"reference": reference},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def row(self) -> dict:
        """The persistent record as the database holds it (raw SQL, every column)."""
        with self.env.request_engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM source_takedowns WHERE source_key = :k"), {"k": KEY}
            )
            [values] = result.mappings().all()
        return dict(values)


@pytest.fixture
def world(tmp_path) -> Iterator[World]:
    with environment(tmp_path / "takedown-privacy.db") as env:
        factory = sessionmaker(bind=env.request_engine, expire_on_commit=False, future=True)
        with factory() as s:
            for user_id in (ALICE, BOB):
                s.get(User, user_id).is_operator = True
            _seed_tenant(s, ORG_A, WS_A, "tenant-a")
            _seed_tenant(s, ORG_B, WS_B, "tenant-b")
            s.commit()
        yield World(env=env, factory=factory)


# --------------------------------------------------------------------------- #
# The public reference: one line of visible text
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("TKD-1\u009b", id="C1 control (CSI, Cc)"),
        pytest.param("TKD-\u200b1", id="zero width space (Cf)"),
        pytest.param("TKD-1\u202e", id="right-to-left override (Cf)"),
        pytest.param("TKD-1\u2028", id="line separator (Zl)"),
        pytest.param("TKD-1\u2029", id="paragraph separator (Zp)"),
        pytest.param("TKD-1\ue000", id="private use (Co)"),
        pytest.param("TKD-1\udcff", id="surrogate escape (Cs)"),
        pytest.param("TKD-1\x07", id="C0 control"),
        pytest.param("TKD-1\x7f", id="DEL"),
        pytest.param("TKD-1\nmemo", id="newline"),
        pytest.param("TKD-1\tmemo", id="tab"),
        pytest.param("counsel@example.com", id="address"),
        pytest.param("https://user:secret@tracker.example/T-1", id="URL credentials"),
        pytest.param("x" * 201, id="too long"),
        pytest.param("   ", id="blank"),
    ],
)
def test_a_reference_never_carries_invisible_or_control_characters(bad: str):
    """The reference is shown to tenants (audit log) and operators (registry): no C0/C1
    control, format (zero-width, bidi override), private-use, surrogate or line/paragraph
    separator character may hide in it, on top of the no-'@', one-line, 1-200 rules
    (delta review r6, C-r6-2)."""
    with pytest.raises(ValidationDomainError):
        validate_reference(bad)


@pytest.mark.parametrize(
    "good",
    [
        pytest.param("TKD-2026-0042", id="ticket id"),
        pytest.param("  REQ 7 / Zürich Straße — café  ", id="accented text with spaces"),
        pytest.param("Žádost č. 12 (Ñandú)", id="accented letters and punctuation"),
    ],
)
def test_ordinary_visible_text_is_an_acceptable_reference(good: str):
    assert validate_reference(good) == good.strip()


# --------------------------------------------------------------------------- #
# Nested identifying metadata
# --------------------------------------------------------------------------- #
def _walk(value, path: str = "$") -> Iterator[tuple[str, object]]:
    """Every (path, leaf) and every (path, key) in a JSON value, recursively."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{path}.<key>", k
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, value


def _json(value):
    """A JSON column as raw SQL returns it (text on SQLite, decoded on PostgreSQL)."""
    return json.loads(value) if isinstance(value, str) else value


def _assert_nothing_identifying(row: dict, *, forbidden: set[str], repeats: int) -> None:
    assert set(row) == MINIMAL_COLUMNS, sorted(row)
    assert row["source_key"] == KEY
    # Column values: nothing equal to a user id, an address or a reference.
    for column, value in row.items():
        assert value not in forbidden, (column, value)
    summary = _json(row["purge_summary"])
    repeat_purges = _json(row["repeat_purges"])
    assert set(summary) <= COUNT_NAMES and all(isinstance(v, int) for v in summary.values())
    assert len(repeat_purges) == repeats
    for entry in repeat_purges:
        assert set(entry) == ENTRY_KEYS, entry
        assert set(entry["purged"]) <= COUNT_NAMES, entry
        datetime.fromisoformat(entry["at"])  # a timestamp, nothing else
    for path, leaf in _walk({"purge_summary": summary, "repeat_purges": repeat_purges}):
        if path.endswith(".<key>"):
            assert leaf in ENTRY_KEYS | COUNT_NAMES | {"purge_summary", "repeat_purges"}, (
                path, leaf,
            )
            continue
        assert leaf not in forbidden, (path, leaf)
        if isinstance(leaf, str):
            assert len(leaf) <= ISO_TIMESTAMP_MAX, (path, leaf)
        else:
            assert isinstance(leaf, int) and not isinstance(leaf, bool), (path, leaf)


def test_the_takedown_row_never_holds_identifying_metadata_even_nested(world: World):
    forbidden = {ALICE, BOB, *EMAIL.values(), REFERENCE, REPEAT_REFERENCE}
    with world.session() as s:
        forbidden |= set(s.scalars(select(User.id))) | set(s.scalars(select(User.email)))

    # (1) first takedown, by ALICE
    first = world.take_down(ALICE, REFERENCE)
    assert first["verified"] and first["purged"]["raw_signals"] == 4
    _assert_nothing_identifying(world.row(), forbidden=forbidden, repeats=0)
    with world.session() as s:
        entries = s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all()
        # Attribution and the public reference are in the tenant audit rows, nowhere else.
        assert {(e.organization_id, e.workspace_id) for e in entries} == {
            (ORG_A, WS_A), (ORG_B, WS_B),
        }
        assert all(e.actor_user_id == ALICE and e.context["reference"] == REFERENCE
                   for e in entries)

    # (2) a repeat, by ALICE again
    second = world.take_down(ALICE, REPEAT_REFERENCE)
    assert second["already_taken_down"]
    _assert_nothing_identifying(world.row(), forbidden=forbidden, repeats=1)
    before = world.row()

    # (3) the operator's account is deleted (no account-deletion route on this branch:
    # the ORM delete with the schema's ON DELETE rules — audit_logs.actor_user_id SET NULL)
    with world.session() as s:
        s.execute(delete(User).where(User.id == ALICE))
        s.commit()
    with world.session() as s:
        assert s.get(User, ALICE) is None
        entries = s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all()
        assert entries and all(e.actor_user_id is None for e in entries)
        assert all(e.context["reference"] == REFERENCE for e in entries)  # evidence kept
    after = world.row()
    assert after == before  # nothing in the suppression record referred to the account
    _assert_nothing_identifying(after, forbidden=forbidden, repeats=1)


def test_the_service_records_a_repeat_as_time_and_counts_only(world: World):
    """Direct service calls (the migration and PostgreSQL proofs use them) write the same
    minimal entry as the route: no actor, no reference, whatever the caller passes."""
    with world.session() as s:
        takedown_source(s, source_key=KEY, reference=REFERENCE, actor_user_id=ALICE)
        s.commit()
    with world.session() as s:
        result = takedown_source(s, source_key=KEY, reference=REPEAT_REFERENCE, actor_user_id=BOB)
        s.commit()
    assert result.already_taken_down
    [entry] = result.takedown.repeat_purges
    assert set(entry) == ENTRY_KEYS and entry["purged"] == result.purged
    _assert_nothing_identifying(
        world.row(), forbidden={ALICE, BOB, REFERENCE, REPEAT_REFERENCE}, repeats=1
    )


# --------------------------------------------------------------------------- #
# Suppression persistence
# --------------------------------------------------------------------------- #
def _signal(key: str) -> SimpleNamespace:
    return SimpleNamespace(attribution={"source_key": key}, content=key)


def _assert_still_suppressed(s: Session, *, step: str) -> None:
    takedowns = active_takedowns(s)
    assert KEY in takedowns and OTHER not in takedowns, step
    # The live gate (app.connectors.live) refuses the source and nothing else.
    allowed = live_targets_for(
        market=MARKET, source_types=(), takedowns=takedowns, settings=_live_settings(),
        targets=TARGETS,
    )
    assert [t.source.source_key for t in allowed] == [OTHER], step
    # A run in flight re-checks under the lock and drops the source's signals.
    kept = without_taken_down(s, [_signal(KEY), _signal(OTHER), _signal(KEY)])
    assert [sig.content for sig in kept] == [OTHER], step


def test_suppression_survives_erasure_anonymisation_and_expiry_of_every_metadata(
    world: World,
):
    world.take_down(ALICE, REFERENCE)
    world.take_down(ALICE, REPEAT_REFERENCE)
    with world.session() as s:
        _assert_still_suppressed(s, step="after the takedown and a repeat")

    # 1. The actor's user row is deleted.
    with world.session() as s:
        s.execute(delete(User).where(User.id == ALICE))
        s.commit()
    with world.session() as s:
        _assert_still_suppressed(s, step="actor deleted")

    # 2. The per-workspace audit rows are anonymised (actor NULL, context cleared).
    with world.session() as s:
        s.execute(
            update(AuditLog)
            .where(AuditLog.action == AUDIT_ACTION)
            .values(actor_user_id=None, context={}, new_state={}, reason=None)
        )
        s.commit()
    with world.session() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all()
        _assert_still_suppressed(s, step="audit rows anonymised")

    # 3. Every audit row for the source is deleted (audit expiry).
    with world.session() as s:
        s.execute(delete(AuditLog).where(AuditLog.action == AUDIT_ACTION))
        s.execute(delete(AuditLog).where(AuditLog.entity_type == "source_takedown"))
        s.commit()
    with world.session() as s:
        assert s.scalars(select(AuditLog)).all() == []
        _assert_still_suppressed(s, step="audit rows expired")

    # 4. The counts on the record itself are cleared.
    with world.session() as s:
        s.execute(
            update(SourceTakedown)
            .where(SourceTakedown.source_key == KEY)
            .values(purge_summary={}, repeat_purges=[])
        )
        s.commit()
    with world.session() as s:
        row = s.scalar(select(SourceTakedown).where(SourceTakedown.source_key == KEY))
        assert row.purge_summary == {} and row.repeat_purges == []
        _assert_still_suppressed(s, step="counts cleared")

    # The registry still shows the takedown (to a remaining operator), with nothing
    # identifying in it, and the key cannot be taken down "for the first time" again.
    listing = world.env.client.get(
        f"{API}/internal/system/data-sources",
        headers={"Authorization": f"Bearer {bearer(world.env, BOB)}"},
    )
    assert listing.status_code == 200, listing.text
    entry = {e["source_key"]: e for e in listing.json()["sources"]}[KEY]
    assert entry["takedown"] == {
        "taken_down_at": entry["takedown"]["taken_down_at"],
        "purge_summary": {},
        "repeat_purges": [],
    }
    assert entry["activatable"] is False and entry["stored"]["raw_signals"] == 0
    again = world.take_down(BOB, "TKD-PRIV-2")
    assert again["already_taken_down"] is True
