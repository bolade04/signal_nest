"""W21 ADDR: the structured business address and its locality confirmation, through real HTTP.

The world is F-1's (``test_location_business_address``): two organizations, three workspaces,
one Dallas, TX location per workspace with a stored rule, and a member of each of the six roles;
SQLite, or PostgreSQL when ``TEST_POSTGRES_URL`` is set. Only ``get_db`` is overridden.

* **A-1** the coverage PUT stores the five fields as entered (trimmed; blank is null) and the GET
  answers them, with what is computed about the town; saving with every field empty works;
* **A-2** an unreadable country, or a state or province that is not one of a US/CA/AU/NG
  country's, is a 422 at its field with a fixed message that never repeats the value; nothing
  is compared with the location there, and nothing is stored;
* **A-3** a PUT that does not name the fields leaves them as they were; an explicit null clears;
* **A-4 / A-5** only owner, admin and marketer confirm; another workspace's location is 404 and
  another organization's workspace 403, with nothing changed;
* **A-6** nothing is filled from the location;
* **A-7 / A-9** the confirmation's four refusals, verbatim, and none records anything; a
  contradiction cannot be confirmed;
* **A-8** confirm and withdraw: the binding, who and when, the computed fields, and the audit
  (coordinator condition OP-1): ``geo_coverage.updated`` naming the confirmation fields that
  changed, never a value; a request that changes nothing records nothing;
* **A-10** a stored confirmation lapses when the town or the region changes, and never stops a
  later contradiction from being refused by the action check;
* **A-11** no answer or published description calls the address or the confirmation verified;
* **A-12 / P-1 / P-3** Locations CRUD is unaffected by empty fields; captured brief, draft and
  review snapshots and every ``jurisdiction_code`` are unchanged by every new write.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select

from app.audit.actions import AuditAction
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.creative.models import ContentReview, CreativeDraft
from app.jurisdiction.business_address import (
    ACTION_CLARIFICATIONS,
    BUSINESS_ADDRESS_CONFLICT,
    BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED,
    CONFIRMATION_REFUSALS,
    resolve_for_action,
)
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.locations.schemas import UNKNOWN_COUNTRY, UNKNOWN_REGION
from app.main import app
from app.tests._auth2_support import pg_database, require_postgres, sqlite_file_engine
from app.tests.test_location_business_address import (
    API,
    EDITORS,
    NOT_A_MEMBER,
    NOT_FOUND_LOCATION,
    NOT_FOUND_RULE,
    ROLES,
    STORED,
    W,
    _error,
    _reset_rate_limiter,
    _snapshots,
    _world,
)


@pytest.fixture(params=["sqlite", "postgresql"])
def w(request, tmp_path) -> Iterator[W]:
    """F-1's world, on SQLite (foreign keys on) or, when ``TEST_POSTGRES_URL`` is set, on a
    PostgreSQL database of the test's own."""
    if request.param == "sqlite":
        yield from _world(sqlite_file_engine(tmp_path / "addr-api.db"))
        return
    require_postgres()
    with pg_database("sn_addrapi") as url:  # pragma: no cover - gated on PostgreSQL
        yield from _world(create_engine(url, future=True))


FIELDS = (
    "business_address_street",
    "business_address_locality",
    "business_address_country",
    "business_address_region",
    "business_address_postcode",
)
CONFIRMATION = (
    "locality_confirmed_at",
    "locality_confirmed_by_user_id",
    "locality_confirmed_city",
    "locality_confirmed_jurisdiction",
    "locality_confirmed_locality",
)
#: Irving, a town of Dallas County: not the location's city, so it asks until confirmed.
IRVING = {
    "business_address_street": "600 E Las Colinas Blvd",
    "business_address_locality": "Irving",
    "business_address_country": "United States",
    "business_address_region": "Texas",
    "business_address_postcode": "75039",
}
SENTINEL = "Zq7Sentinel"
REFUSED = 422, "locality_confirmation_refused"


def _put(w: W, fields: dict, *, loc: str = "loc-a1", ws: str = "ws-a1", who: str = "a-owner"):
    """The stored rule with ``fields`` (its free text, F-1's, names Toronto: a contradiction)."""
    return w.coverage("PUT", ws, loc, {**STORED, **fields}, who=who)


def _put_clean(w: W, fields: dict):
    """As :func:`_put`, with the free text left empty, so only the structured fields decide."""
    return _put(w, {"business_address": None, **fields})


def _confirm(w: W, confirmed: bool, *, loc: str = "loc-a1", ws: str = "ws-a1",
             who: str = "a-owner", extra: dict | None = None):
    _reset_rate_limiter()
    url = f"{API}/workspaces/{ws}/locations/{loc}/geo-coverage/locality-confirmation"
    return w.client.put(url, json={"confirmed": confirmed, **(extra or {})}, headers=w.auth[who])


def _audits(w: W) -> list[dict]:
    with w.make() as s:
        rows = s.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.GEO_COVERAGE_UPDATED.value)
            .order_by(AuditLog.created_at, AuditLog.id)
        ).scalars().all()
        return [
            {
                "actor": r.actor_user_id,
                "entity": (r.entity_type, r.entity_id),
                "context": r.context,
                "previous_state": r.previous_state,
                "new_state": r.new_state,
                "reason": r.reason,
            }
            for r in rows
        ]


def _action(w: W, loc: str = "loc-a1", ws: str = "ws-a1"):
    """What a jurisdiction-dependent action would answer for the stored rows (the rules
    selected as #313's capture selects them)."""
    with w.make() as s:
        location = s.get(BusinessLocation, loc)
        rules = s.scalars(
            select(GeoCoverageRule)
            .where(GeoCoverageRule.workspace_id == ws, GeoCoverageRule.location_id == loc)
            .order_by(GeoCoverageRule.created_at, GeoCoverageRule.id)
        ).all()
        return resolve_for_action(location, rules)


def _set_location(w: W, loc: str = "loc-a1", **fields) -> None:
    with w.make() as s:
        location = s.get(BusinessLocation, loc)
        for key, value in fields.items():
            setattr(location, key, value)
        s.commit()


# --------------------------------------------------------------------------- #
# A-1 / A-12: storing and answering; empty fields are fine
# --------------------------------------------------------------------------- #
def test_put_stores_the_fields_as_entered_and_get_answers_them(w: W) -> None:
    padded = {k: f"  {v}  " for k, v in IRVING.items()}
    r = _put(w, padded)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k: body[k] for k in FIELDS} == IRVING
    assert body["locality_matches_city"] is False
    assert body["locality_confirmed_at"] is None and body["locality_confirmation_applies"] is False
    assert w.coverage("GET", "ws-a1", "loc-a1").json() == body
    row = w.row(GeoCoverageRule, "geo-a1")
    assert {k: row[k] for k in FIELDS} == IRVING
    assert {k: row[k] for k in CONFIRMATION} == dict.fromkeys(CONFIRMATION)
    assert row["business_address"] == STORED["business_address"]  # the free text, as it was

    same_town = _put(w, {"business_address_locality": "DALLAS"}).json()
    assert same_town["locality_matches_city"] is True


def test_every_field_may_be_saved_empty(w: W) -> None:
    r = _put(w, {**dict.fromkeys(FIELDS, "   "), "business_address": None})
    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in FIELDS} == dict.fromkeys(FIELDS)
    assert r.json()["locality_matches_city"] is None
    assert {k: w.row(GeoCoverageRule, "geo-a1")[k] for k in FIELDS} == dict.fromkeys(FIELDS)


def test_locations_crud_is_unaffected_by_empty_fields(w: W) -> None:
    _reset_rate_limiter()
    created = w.client.post(f"{API}/workspaces/ws-a1/locations",
                            json={"name": "Plano", "city": "Plano", "country": "US"},
                            headers=w.auth["a-owner"])
    assert created.status_code == 201, created.text
    loc = created.json()["id"]
    assert _put(w, {}, loc=loc).status_code == 200  # first rule, no structured field
    for verb in ("deactivate", "reactivate"):
        _reset_rate_limiter()
        r = w.client.post(f"{API}/workspaces/ws-a1/locations/{loc}/{verb}",
                          headers=w.auth["a-owner"])
        assert r.status_code == 200, r.text
    _reset_rate_limiter()
    updated = w.client.put(f"{API}/workspaces/ws-a1/locations/{loc}",
                           json={"name": "Plano 2", "city": "Plano", "country": "US"},
                           headers=w.auth["a-owner"])
    assert updated.status_code == 200, updated.text


# --------------------------------------------------------------------------- #
# A-2: readable values only; fixed messages; no echo; nothing compared with the location
# --------------------------------------------------------------------------- #
def _validation(r) -> list[tuple[list, str]]:
    err = r.json()["error"]
    assert (r.status_code, err["code"], err["message"]) == (
        422, "validation_error", "Request validation failed"
    )
    return [(d["loc"], d["msg"]) for d in err["details"]]


@pytest.mark.parametrize(
    ("fields", "loc", "message"),
    [
        ({"business_address_country": f"Narnia {SENTINEL}"}, "business_address_country",
         UNKNOWN_COUNTRY),
        ({"business_address_country": "US", "business_address_region": f"Bavaria {SENTINEL}"},
         "business_address_region", UNKNOWN_REGION),
        ({"business_address_country": "Nigeria", "business_address_region": SENTINEL},
         "business_address_region", UNKNOWN_REGION),
    ],
)
def test_an_unreadable_value_is_a_fixed_422_at_its_field(w: W, fields, loc, message) -> None:
    before = w.row(GeoCoverageRule, "geo-a1")
    audits = _audits(w)
    r = _put(w, fields)
    assert _validation(r) == [(["body", loc], f"Value error, {message}")]
    assert SENTINEL not in r.text
    assert w.row(GeoCoverageRule, "geo-a1") == before and _audits(w) == audits


def test_the_text_refusal_message_is_worded_as_a_reading() -> None:
    """Review d1 C-7, pinned with the other fixed messages (A-2)."""
    assert ACTION_CLARIFICATIONS["business_address_text_conflict"].startswith(
        "We read the Business address text as naming a different country, or state or province, "
        "from this location's."
    )


def test_the_messages_fit_the_web_clients_bound_whole() -> None:
    """The web client shows one field message clamped to 80 characters (``apps/web/src/api/
    client.ts`` MAX_SEGMENT), "Value error, " included: these fit, so it never cuts them."""
    for message in (UNKNOWN_COUNTRY, UNKNOWN_REGION):
        assert len(f"Value error, {message}") <= 80


def test_two_partial_puts_cannot_store_a_state_of_another_country(w: W) -> None:
    """Review d1 C-4: readability is checked on the values the rule will hold, the stored ones
    merged with those the request names."""
    assert _put(w, {"business_address_region": "Texas"}).status_code == 200  # no country yet
    before, audits = w.row(GeoCoverageRule, "geo-a1"), _audits(w)
    r = _put(w, {"business_address_country": "Nigeria"})
    assert _validation(r) == [
        (["body", "business_address_region"], f"Value error, {UNKNOWN_REGION}")
    ]
    assert w.row(GeoCoverageRule, "geo-a1") == before and _audits(w) == audits
    # Naming both, with a state of that country (or none), is accepted.
    assert _put(w, {"business_address_country": "Nigeria",
                    "business_address_region": "Lagos"}).status_code == 200
    assert _put(w, {"business_address_country": "US",
                    "business_address_region": None}).status_code == 200


def test_a_stored_value_that_no_longer_reads_is_answered_and_asks_at_the_action(w: W) -> None:
    """Review d1 C-4: the response model does not inherit the request's validators. A value
    that does not read (written directly, or after a reader change) is answered by the GET as
    stored, and the action asks for the business address (row 5c)."""
    assert _put_clean(w, {"business_address_region": "Texas",
                          "business_address_country": "US"}).status_code == 200
    with w.make() as s:
        s.get(GeoCoverageRule, "geo-a1").business_address_country = "Narnia"
        s.commit()
    got = w.coverage("GET", "ws-a1", "loc-a1")
    assert got.status_code == 200, got.text
    assert got.json()["business_address_country"] == "Narnia"
    assert _action(w).reason == "business_address_incomplete"


def test_too_long_a_value_is_refused_without_its_text(w: W) -> None:
    r = _put(w, {"business_address_locality": SENTINEL * 20})
    [(where, _msg)] = _validation(r)
    assert where == ["body", "business_address_locality"] and SENTINEL not in r.text


@pytest.mark.parametrize(
    "fields",
    [
        {"business_address_country": "Canada", "business_address_region": "Ontario"},
        {"business_address_country": "United Kingdom", "business_address_region": "Bavaria"},
        {"business_address_region": "Bavaria"},  # no country to read it in
        {"business_address_country": "US", "business_address_region": "Alaska",
         "business_address_locality": "Anchorage"},
    ],
    ids=["another-country", "country-class-region", "region-without-country",
         "another-state"],
)
def test_storing_never_compares_with_the_location(w: W, fields) -> None:
    r = _put(w, fields)
    assert r.status_code == 200, r.text
    assert {k: w.row(GeoCoverageRule, "geo-a1")[k] for k in fields} == fields


# --------------------------------------------------------------------------- #
# A-3 / A-6: unnamed fields preserved; nothing filled from the location
# --------------------------------------------------------------------------- #
def test_a_put_that_does_not_name_the_fields_leaves_them(w: W) -> None:
    assert _put(w, IRVING).status_code == 200
    r = w.coverage("PUT", "ws-a1", "loc-a1", {**STORED, "radius_miles": 5})  # an older client
    assert r.status_code == 200, r.text
    row = w.row(GeoCoverageRule, "geo-a1")
    assert {k: row[k] for k in FIELDS} == IRVING and row["radius_miles"] == 5
    cleared = _put(w, {"business_address_postcode": None})
    assert cleared.json()["business_address_postcode"] is None
    assert {k: w.row(GeoCoverageRule, "geo-a1")[k] for k in FIELDS} == {
        **IRVING, "business_address_postcode": None
    }


def test_nothing_is_filled_from_the_location(w: W) -> None:
    location = w.row(BusinessLocation, "loc-a1")
    assert (location["city"], location["state_province"], location["country"]) == (
        "Dallas", "TX", "US"
    )
    for fields in ({}, {"business_address_region": "TX"}, {"business_address_locality": "Dallas"}):
        assert _put(w, fields).status_code == 200
        row = w.row(GeoCoverageRule, "geo-a1")
        assert row["business_address_country"] is None
        assert row["business_address_street"] is None and row["business_address_postcode"] is None
    # A new location's first rule too.
    _reset_rate_limiter()
    loc = w.client.post(f"{API}/workspaces/ws-a1/locations",
                        json={"name": "Austin", "city": "Austin", "state_province": "TX",
                              "country": "US"}, headers=w.auth["a-owner"]).json()["id"]
    body = _put(w, {}, loc=loc).json()
    assert {k: body[k] for k in FIELDS} == dict.fromkeys(FIELDS)


# --------------------------------------------------------------------------- #
# A-4 / A-5: who may confirm, and where
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ROLES, ids=[r.value for r in ROLES])
def test_only_owner_admin_and_marketer_confirm(w: W, role) -> None:
    assert _put(w, IRVING).status_code == 200
    who = f"a-{role.value}"
    before, audits = w.row(GeoCoverageRule, "geo-a1"), _audits(w)
    r = _confirm(w, True, who=who)
    if role in EDITORS:
        assert r.status_code == 200, r.text
        assert w.row(GeoCoverageRule, "geo-a1")["locality_confirmed_by_user_id"] == who
    else:
        assert _error(r) == (403, "permission_denied",
                             f"Role '{role.value}' is not permitted for this action.")
        assert w.row(GeoCoverageRule, "geo-a1") == before and _audits(w) == audits


def test_another_workspaces_location_is_404_and_another_organization_403(w: W) -> None:
    for loc in ("loc-a2", "loc-b1"):
        with w.make() as s:
            rule = s.get(GeoCoverageRule, f"geo-{loc[4:]}")
            for k, v in IRVING.items():
                setattr(rule, k, v)
            s.commit()
    before = {r: w.row(GeoCoverageRule, r) for r in ("geo-a2", "geo-b1")}
    audits = _audits(w)
    assert _error(_confirm(w, True, loc="loc-a2")) == NOT_FOUND_LOCATION
    assert _error(_confirm(w, True, loc="loc-b1")) == NOT_FOUND_LOCATION
    assert _error(_confirm(w, True, ws="ws-b1", loc="loc-b1")) == NOT_A_MEMBER
    assert _error(_confirm(w, False, ws="ws-b1", loc="loc-b1")) == NOT_A_MEMBER
    assert {r: w.row(GeoCoverageRule, r) for r in before} == before and _audits(w) == audits
    # Positive control: the same owner confirms in the other workspace through its own path.
    _reset_rate_limiter()
    assert _confirm(w, True, ws="ws-a2", loc="loc-a2").status_code == 200


def test_a_location_without_a_rule_is_the_existing_404(w: W) -> None:
    _reset_rate_limiter()
    loc = w.client.post(f"{API}/workspaces/ws-a1/locations", json={"name": "Bare"},
                        headers=w.auth["a-owner"]).json()["id"]
    assert _error(_confirm(w, True, loc=loc)) == NOT_FOUND_RULE
    assert _error(_confirm(w, False, loc=loc)) == NOT_FOUND_RULE


# --------------------------------------------------------------------------- #
# A-7 / A-9: the four refusals, verbatim; nothing recorded
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("fields", "location", "cause"),
    [
        (IRVING, {"state_province": None}, "location"),
        ({**IRVING, "business_address_country": "Canada",
          "business_address_region": "Ontario"}, {}, "conflict"),
        ({**IRVING, "business_address_region": "Oklahoma"}, {}, "conflict"),
        ({**IRVING, "business_address_region": None}, {}, "incomplete"),
        ({**IRVING, "business_address_country": None}, {}, "incomplete"),
        ({**IRVING, "business_address_locality": None}, {}, "incomplete"),  # a city: town required
        ({**IRVING, "business_address_locality": "dallas"}, {}, "nothing"),
        (IRVING, {"city": None}, "nothing"),
    ],
    ids=["location-unresolved", "another-country", "another-state", "no-region", "no-country",
         "no-town", "same-town", "no-city"],
)
def test_the_refusals_are_verbatim_and_record_nothing(w: W, fields, location, cause) -> None:
    assert _put(w, fields).status_code == 200
    _set_location(w, **location)
    before, audits = w.row(GeoCoverageRule, "geo-a1"), _audits(w)
    r = _confirm(w, True)
    assert _error(r) == (*REFUSED, CONFIRMATION_REFUSALS[cause])
    assert w.row(GeoCoverageRule, "geo-a1") == before and _audits(w) == audits


def test_the_confirmation_binds_what_is_saved_never_what_is_sent(w: W) -> None:
    assert _put(w, IRVING).status_code == 200
    r = _confirm(w, True, extra={"business_address_locality": "Dallas",
                                 "locality_confirmed_city": "Irving"})
    assert r.status_code == 200, r.text
    row = w.row(GeoCoverageRule, "geo-a1")
    assert (row["locality_confirmed_locality"], row["locality_confirmed_city"],
            row["locality_confirmed_jurisdiction"]) == ("Irving", "Dallas", "US-TX")


# --------------------------------------------------------------------------- #
# A-8: confirm and withdraw; the audit names fields, never values (coordinator OP-1)
# --------------------------------------------------------------------------- #
def test_confirm_and_withdraw_with_their_audit(w: W) -> None:
    assert _put_clean(w, IRVING).status_code == 200
    assert _action(w).reason == BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED
    audits = _audits(w)

    r = _confirm(w, True, who="a-marketer")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locality_confirmation_applies"] is True
    assert body["locality_matches_city"] is False and body["locality_confirmed_at"]
    row = w.row(GeoCoverageRule, "geo-a1")
    assert (row["locality_confirmed_locality"], row["locality_confirmed_city"],
            row["locality_confirmed_jurisdiction"], row["locality_confirmed_by_user_id"]) == (
        "Irving", "Dallas", "US-TX", "a-marketer"
    )
    assert w.coverage("GET", "ws-a1", "loc-a1").json() == body
    assert _action(w).code == "US-TX"  # the action now resolves, labelled customer-confirmed

    new = _audits(w)[len(audits):]
    assert new == [{
        "actor": "a-marketer", "entity": ("geo_coverage_rule", "geo-a1"),
        "context": {"locality_confirmation": "confirmed", "fields_changed": list(CONFIRMATION)},
        "previous_state": None, "new_state": None, "reason": None,
    }]
    text = json.dumps(new)
    for value in (*IRVING.values(), "Dallas", "US-TX"):
        assert value not in text, value

    # The same member confirms again: only who/when may change; the history keeps both.
    again = _confirm(w, True, who="a-owner")
    assert again.status_code == 200
    latest = _audits(w)[-1]
    assert latest["actor"] == "a-owner"
    assert latest["context"] == {
        "locality_confirmation": "confirmed",
        "fields_changed": ["locality_confirmed_at", "locality_confirmed_by_user_id"],
    }

    withdrawn = _confirm(w, False, who="a-admin")
    assert withdrawn.status_code == 200
    assert withdrawn.json()["locality_confirmation_applies"] is False
    assert withdrawn.json()["locality_confirmed_at"] is None
    assert {k: w.row(GeoCoverageRule, "geo-a1")[k] for k in CONFIRMATION} == dict.fromkeys(
        CONFIRMATION
    )
    assert _audits(w)[-1]["context"] == {
        "locality_confirmation": "withdrawn", "fields_changed": list(CONFIRMATION)
    }
    count = len(_audits(w))
    assert _confirm(w, False).status_code == 200  # nothing to withdraw: nothing recorded
    assert len(_audits(w)) == count
    assert _action(w).reason == BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED


# --------------------------------------------------------------------------- #
# A-10: the binding lapses; a confirmation never overrides a later contradiction
# --------------------------------------------------------------------------- #
def test_a_confirmation_lapses_and_never_overrides_a_contradiction(w: W) -> None:
    assert _put_clean(w, IRVING).status_code == 200
    assert _confirm(w, True).status_code == 200
    moved = _put_clean(w, {"business_address_locality": "Plano"})
    assert moved.json()["locality_confirmation_applies"] is False
    assert _action(w).reason == BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED
    back = _put_clean(w, {"business_address_locality": "IRVING"})
    assert back.json()["locality_confirmation_applies"] is True  # the same town again
    assert _action(w).code == "US-TX"

    contradicted = _put_clean(w, {"business_address_region": "Oklahoma"})
    assert contradicted.json()["locality_confirmation_applies"] is False
    assert _action(w).reason == BUSINESS_ADDRESS_CONFLICT

    assert _put_clean(w, {"business_address_region": "Texas"}).status_code == 200
    _set_location(w, city="Fort Worth")
    assert w.coverage("GET", "ws-a1", "loc-a1").json()["locality_confirmation_applies"] is False
    assert _action(w).reason == BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED


# --------------------------------------------------------------------------- #
# A-11: never "verified"
# --------------------------------------------------------------------------- #
def test_nothing_calls_the_address_or_the_confirmation_verified(w: W) -> None:
    doc = app.openapi()
    schemas = doc["components"]["schemas"]
    published = json.dumps([schemas["GeoCoverageBase"], schemas["GeoCoverageOut"],
                            schemas["LocalityConfirmationIn"],
                            doc["paths"][f"{API}/workspaces/{{workspace_id}}/locations/"
                                         f"{{location_id}}/geo-coverage/locality-confirmation"]])
    lowered = published.lower()
    assert lowered.count("verified") == lowered.count("not verified") > 0
    assert _put(w, IRVING).status_code == 200
    assert "verified" not in _confirm(w, True).text.lower()
    for message in CONFIRMATION_REFUSALS.values():
        assert "verified" not in message.lower()


# --------------------------------------------------------------------------- #
# P-1 / P-3: snapshots and jurisdiction values are never touched
# --------------------------------------------------------------------------- #
def test_every_new_write_keeps_captured_snapshots_and_jurisdiction_values(w: W) -> None:
    _snapshots(w)
    with w.make() as s:
        s.get(BusinessLocation, "loc-a1").jurisdiction_code = "US-TX"
        s.get(GeoCoverageRule, "geo-a1").jurisdiction_code = "US-TX"
        s.commit()
    rows = {(m, rid): w.row(m, rid) for m, rid in (
        (RecommendationBrief, "brief-a1"), (CreativeDraft, "draft-a1"),
        (ContentReview, "review-a1"), (BusinessLocation, "loc-a1"),
    )}
    assert _put(w, IRVING).status_code == 200
    assert _confirm(w, True).status_code == 200
    assert _put(w, {"business_address_region": "Oklahoma"}).status_code == 200
    assert _confirm(w, False).status_code == 200
    for (model, rid), before in rows.items():
        assert w.row(model, rid) == before, model.__tablename__
    assert w.row(GeoCoverageRule, "geo-a1")["jurisdiction_code"] == "US-TX"
