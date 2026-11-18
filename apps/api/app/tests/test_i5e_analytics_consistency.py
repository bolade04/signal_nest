"""Project Phase 5E, I-5E fold of the R-5E r1 reviews: the analytics comparison is one reading.

Two properties of ``GET /workspaces/{ws}/analytics`` (``app.analytics.service``), each on SQLite
and, with ``TEST_POSTGRES_URL``, on PostgreSQL migrated to head (READ COMMITTED):

* **The read never answers 500 because a located row was committed between its statements**
  (review R-5E-A r1, finding A-F2). The family cards are read first, the locations are listed,
  then the comparison is read. A row committed by another connection in between used to make
  ``unassigned`` (the cards minus the location rows) ``-1``, which the response model's
  ``Count`` bound refused: a server error. ``unassigned`` is now a count read by the same
  statement as the location rows (``location_breakdown``). The interleaving is forced here at
  the statement boundary itself (a cursor hook on the request's engine that fires when the
  locations are listed), once per compared family: opportunities, briefs, drafts, approvals
  and exports, in a workspace whose rows of that family are all at one location.
* **The comparison counts only this tenant's decision and export rows** (review R-5E-B r1,
  F-B-3). ``approvals`` and ``exports`` carry FK-less scope columns (operator decision O-6), so
  a row of another workspace or organization can name this workspace's brief or draft. Such
  rows, hand-written here (the application writes none), change no figure of the workspace
  view or of the location view: not the location row, not ``unassigned``.

Both capabilities stay dark globally; the workspace is opened by an override.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event

from app.approvals.models import Approval
from app.core.config import get_settings
from app.exports.models import Export
from app.tests._creative_support import CreativeWorld
from app.tests.fixtures.analytics import T0, Rows, creative_world_on, set_capability

FAMILIES = ("opportunities", "briefs", "drafts", "approvals", "exports")


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world_on(request.param, tmp_path_factory, "i5eac") as w:
        yield w


@pytest.fixture(autouse=True)
def flags_off(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "notifications_analytics_enabled", False)
    monkeypatch.setattr(settings, "content_export_enabled", False)


def _rows(world: CreativeWorld, s, ws: str) -> Rows:
    return Rows(s, organization_id=world.org_of(ws), workspace_id=ws)


def _located_chain(world: CreativeWorld, ws: str, location_id: str) -> dict[str, str]:
    """One row of every compared family at ``location_id`` (committed): an opportunity, its
    brief with one decision, a draft of it with one decision, and one export of the draft."""
    users = world.users
    with world.session() as s:
        rows = _rows(world, s, ws)
        ids = {"opportunity": rows.opportunity(location_id=location_id)}
        ids["brief"] = rows.brief(opportunity_id=ids["opportunity"], author=users["marketer"],
                                  location_id=location_id, code="US-TX", created_at=T0)
        ids["brief_decision"] = rows.decision(
            subject=ids["brief"], kind="brief", decision="approved", seq=1,
            reviewer=users["reviewer"], decided_at=T0 + timedelta(seconds=60))
        ids["draft"] = rows.draft(brief_id=ids["brief"], author=users["marketer"], created_at=T0)
        ids["draft_decision"] = rows.decision(
            subject=ids["draft"], kind="draft", decision="approved", seq=1,
            reviewer=users["reviewer"], decided_at=T0 + timedelta(seconds=120))
        ids["export"] = rows.export(draft_id=ids["draft"], approval_id=ids["draft_decision"],
                                    fmt="json", exporter=users["owner"])
    return ids


def _one_more(world: CreativeWorld, ws: str, location_id: str, family: str,
              chain: dict[str, str]) -> Callable[[], None]:
    """A committed write, from a connection of its own, of one more located row of ``family``."""

    def write() -> None:
        users = world.users
        with world.session() as s:
            rows = _rows(world, s, ws)
            if family == "opportunities":
                rows.opportunity(location_id=location_id)
            elif family == "briefs":
                rows.brief(opportunity_id=rows.opportunity(location_id=None),
                           author=users["marketer"], location_id=location_id, code="US-TX",
                           created_at=T0)
            elif family == "drafts":
                rows.draft(brief_id=chain["brief"], author=users["marketer"], version=2,
                           created_at=T0)
            elif family == "approvals":
                rows.decision(subject=chain["brief"], kind="brief", decision="revoked", seq=2,
                              reviewer=users["reviewer"], decided_at=T0 + timedelta(seconds=300))
            elif family == "exports":
                rows.export(draft_id=chain["draft"], approval_id=chain["draft_decision"],
                            fmt="text/plain", exporter=users["owner"])
            else:  # pragma: no cover - the parameter list above is closed
                raise AssertionError(family)

    return write


@contextmanager
def _between_the_cards_and_the_comparison(world: CreativeWorld, write: Callable[[], None]):
    """Run ``write`` (in a thread of its own, committed before it returns) the first time the
    request's engine is about to list the workspace's locations: after every family card has
    been read and before the comparison is. The hook is on the statement, not on a function of
    the service, so it interleaves the same way whatever the service calls its reads."""
    fired: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany) -> None:
        text = " ".join(statement.split()).lower()
        if fired or not text.startswith("select business_locations.id, business_locations.name"):
            return
        fired.append(text)
        failure: list[BaseException] = []

        def run() -> None:
            try:
                write()
            except BaseException as exc:  # noqa: BLE001 - re-raised in the request's thread
                failure.append(exc)

        writer = threading.Thread(target=run, daemon=True)
        writer.start()
        writer.join(60)
        assert not writer.is_alive(), "the concurrent write did not finish"
        if failure:
            raise failure[0]

    event.listen(world.engine, "before_cursor_execute", before)
    try:
        yield fired
    finally:
        event.remove(world.engine, "before_cursor_execute", before)


def _read(world: CreativeWorld, ws: str, location_id: str | None = None):
    path = f"/workspaces/{ws}/analytics"
    if location_id is not None:
        path += f"?location_id={location_id}"
    return world.send("GET", path, "owner")


def _figures(world: CreativeWorld, ws: str, location_id: str | None = None) -> dict:
    r = _read(world, ws, location_id)
    assert r.status_code == 200, r.text
    body = r.json()
    body.pop("generated_at")
    return body


# --------------------------------------------------------------------------- #
# A-F2: a located row committed between the cards and the comparison
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("family", FAMILIES)
def test_a_located_row_committed_between_the_statements_never_fails_the_read(
    world: CreativeWorld, family: str
):
    ws = world.workspace(f"i5eac {family}")
    set_capability(world, ws, "notifications_analytics")
    with world.session() as s:
        location_id = _rows(world, s, ws).location("Alpha Market", code="US-TX")
    chain = _located_chain(world, ws, location_id)

    # Before: every row of the family is at the location, so nothing is unassigned.
    before = _figures(world, ws)
    [row] = before["locations"]
    card = before[family]["total"]
    assert card >= 1
    assert (row[family], before["unassigned"][family]) == (card, 0)

    with _between_the_cards_and_the_comparison(
        world, _one_more(world, ws, location_id, family, chain)
    ) as fired:
        during = _read(world, ws)  # the test client re-raises a server error
    assert len(fired) == 1, "the read never listed the locations"
    assert during.status_code == 200, during.text
    body = during.json()
    [row] = body["locations"]
    # The card was read before the write and the comparison after it: they may differ by that
    # one row (the disclosed READ COMMITTED limit). The comparison is one reading: its
    # location row has the new row, and `unassigned` is a count, so it is not negative.
    assert body[family]["total"] == card
    assert row[family] == card + 1
    assert body["unassigned"][family] == 0
    assert all(value >= 0 for value in body["unassigned"].values()), body["unassigned"]

    # Nothing is cached: the next read agrees with itself again.
    after = _figures(world, ws)
    [row] = after["locations"]
    assert (after[family]["total"], row[family], after["unassigned"][family]) == (
        card + 1, card + 1, 0
    )


def test_the_comparison_still_adds_up_to_the_cards_in_a_quiet_workspace(world: CreativeWorld):
    """The serial reading the one-statement comparison must keep: location rows plus
    ``unassigned`` equal each family's card, with rows at a location, at none, and whose
    subject is soft-deleted (an approval and an export of a deleted draft are unassigned)."""
    ws = world.workspace("i5eac sums")
    set_capability(world, ws, "notifications_analytics")
    users = world.users
    with world.session() as s:
        rows = _rows(world, s, ws)
        alpha = rows.location("Alpha Market", code="US-TX")
        beta = rows.location("Beta Market", active=False, code="US-CA")
    _located_chain(world, ws, alpha)
    _located_chain(world, ws, beta)
    with world.session() as s:
        rows = _rows(world, s, ws)
        nowhere = rows.opportunity(location_id=None)
        brief = rows.brief(opportunity_id=nowhere, author=users["marketer"], location_id=None,
                           code=None, created_at=T0)
        rows.decision(subject=brief, kind="brief", decision="rejected", seq=1,
                      reviewer=users["reviewer"], decided_at=T0 + timedelta(seconds=10))
        located = rows.opportunity(location_id=alpha)
        gone_brief = rows.brief(opportunity_id=located, author=users["marketer"],
                                location_id=alpha, code="US-TX", created_at=T0)
        gone = rows.draft(brief_id=gone_brief, author=users["marketer"], created_at=T0,
                          deleted=True)
        decided = rows.decision(subject=gone, kind="draft", decision="approved", seq=1,
                                reviewer=users["reviewer"],
                                decided_at=T0 + timedelta(seconds=20))
        rows.export(draft_id=gone, approval_id=decided, fmt="text/markdown",
                    exporter=users["owner"])
    body = _figures(world, ws)
    by_location = {row["location_id"]: row for row in body["locations"]}
    assert set(by_location) == {alpha, beta}
    expected = {
        #             alpha, beta, unassigned
        "opportunities": (2, 1, 1),
        "briefs": (2, 1, 1),
        "drafts": (1, 1, 0),
        "approvals": (2, 2, 2),
        "exports": (1, 1, 1),
    }
    for family, (at_alpha, at_beta, unassigned) in expected.items():
        assert (
            by_location[alpha][family], by_location[beta][family], body["unassigned"][family]
        ) == (at_alpha, at_beta, unassigned), family
        assert body[family]["total"] == at_alpha + at_beta + unassigned, family
    # A location view has no unassigned row and lists that one location with the same figures.
    one = _figures(world, ws, alpha)
    assert one["unassigned"] is None
    [row] = one["locations"]
    assert row == by_location[alpha]


# --------------------------------------------------------------------------- #
# F-B-3: rows of another scope that name this workspace's subjects
# --------------------------------------------------------------------------- #
def test_the_comparison_ignores_foreign_scope_rows_naming_this_workspaces_subjects(
    world: CreativeWorld,
):
    ws = world.workspace("i5eac foreign")
    set_capability(world, ws, "notifications_analytics")
    with world.session() as s:
        location_id = _rows(world, s, ws).location("Alpha Market", code="US-TX")
    chain = _located_chain(world, ws, location_id)
    before, before_at = _figures(world, ws), _figures(world, ws, location_id)
    [row] = before["locations"]
    assert (row["drafts"], row["exports"], row["approvals"]) == (1, 1, 2), row
    assert before["unassigned"] == {
        "opportunities": 0, "briefs": 0, "drafts": 0, "approvals": 0, "exports": 0
    }

    # The sibling workspace and the other organization each hold an export of this
    # workspace's draft, a decision on that draft and a decision on its brief. Only the scope
    # columns are foreign; the draft and brief links are real foreign keys to this workspace.
    for index, other in enumerate((world.ws2, world.ws_b)):
        scoped = {"organization_id": world.org_of(other), "workspace_id": other}
        with world.session() as s:
            s.add(Export(draft_id=chain["draft"], draft_version=1, approval_id=None,
                         format="json", jurisdiction_code="US-TX", exported_by=None,
                         exported_by_snapshot="[erased]", is_simulated=True, **scoped))
            s.add(Approval(subject_type="creative_draft", subject_id=chain["draft"],
                           subject_version=1, draft_id=chain["draft"], decision="rejected",
                           decision_seq=50 + index, decided_by=None,
                           decided_by_snapshot="[erased]", decided_role="reviewer",
                           is_simulated=False, **scoped))
            s.add(Approval(subject_type="recommendation_brief", subject_id=chain["brief"],
                           subject_version=1, brief_id=chain["brief"], decision="revoked",
                           decision_seq=60 + index, decided_by=None,
                           decided_by_snapshot="[erased]", decided_role="reviewer",
                           is_simulated=False, **scoped))
    assert world.count(Export, draft_id=chain["draft"]) == 3
    assert world.count(Approval, draft_id=chain["draft"]) == 3
    assert world.count(Approval, brief_id=chain["brief"]) == 3

    after, after_at = _figures(world, ws), _figures(world, ws, location_id)
    assert after == before
    assert after_at == before_at
