"""W21 ADDR: privacy of the structured business address and its locality confirmation.

On the P6-PRIV-1 world (``_privacy_support``: three organizations, every tenant table populated,
one coverage rule per workspace), each test gives organization A's rule ``a1-geo`` a structured
address confirmed by CAROL and organization B's ``b1-geo`` one confirmed by BOB:

* **V-1 organization export:** the ten new columns are exported with the rule, for the
  organization's own rules only;
* **V-2 account export:** an account's export lists the confirmations it made (the rule, its
  location and the instant), never the address, and never another account's;
* **V-3 account deletion:** deleting CAROL sets only ``locality_confirmed_by_user_id`` to NULL
  (counted in the receipt), keeps the confirmation and the address, leaves BOB's untouched, and
  the independent verifier passes (before the deletion it finds the reference);
* **V-4 organization deletion:** A's rules go with A; B's stay; the verifier passes;
* **V-5 content verification:** the new text columns are scanned (a planted address naming
  the subject is found);
* **V-7 inventory:** no new table and no new retention class; the rule stays tenant content.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from app.locations.models import GeoCoverageRule
from app.privacy.inventory import TABLES, Category
from app.privacy.verification import verify_organization_erased, verify_subject_erased
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    DAVE,
    EMAIL,
    NAME,
    ORG_A,
    ORG_B,
    PASSWORD,
    WORKSPACES,
    PrivacyEnv,
    privacy_world,
)

NEW = (
    "business_address_street",
    "business_address_locality",
    "business_address_region",
    "business_address_postcode",
    "business_address_country",
    "locality_confirmed_locality",
    "locality_confirmed_city",
    "locality_confirmed_jurisdiction",
    "locality_confirmed_at",
    "locality_confirmed_by_user_id",
)
AT = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "addr-privacy.db") as e:
        yield e


def _address(street: str, town: str, by: str) -> dict:
    return {
        "business_address_street": street,
        "business_address_locality": town,
        "business_address_region": "Lagos",
        "business_address_postcode": "100001",
        "business_address_country": "Nigeria",
        "locality_confirmed_locality": town,
        "locality_confirmed_city": "Lagos",
        "locality_confirmed_jurisdiction": "NG-LA",
        "locality_confirmed_at": AT,
        "locality_confirmed_by_user_id": by,
    }


def _confirm_rows(env: PrivacyEnv) -> None:
    with env.engine.begin() as conn:
        for rule, street, town, by in (
            ("a1-geo", "1 Allen Avenue", "Ikeja", CAROL),
            ("b1-geo", "2 Awolowo Road", "Ikoyi", BOB),
        ):
            conn.execute(
                update(GeoCoverageRule.__table__)
                .where(GeoCoverageRule.__table__.c.id == rule)
                .values(_address(street, town, by))
            )


def _rule(env: PrivacyEnv, rule_id: str) -> dict:
    [row] = env.rows(GeoCoverageRule, GeoCoverageRule.id == rule_id)
    return row


# --------------------------------------------------------------------------- #
# V-1 / V-2: exports
# --------------------------------------------------------------------------- #
def test_the_organization_export_carries_the_new_columns_of_its_own_rules(env) -> None:
    _confirm_rows(env)
    r = env.client.get(f"{API}/organizations/{ORG_A}/export", headers=env.auth(ALICE))
    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()["tables"]["geo_coverage_rules"]["rows"]}
    assert set(rows) == {"a1-geo", "a2-geo"}
    assert set(NEW) <= set(rows["a1-geo"])
    assert rows["a1-geo"]["business_address_locality"] == "Ikeja"
    assert rows["a1-geo"]["locality_confirmed_by_user_id"] == CAROL
    assert rows["a2-geo"]["business_address_locality"] is None
    assert "Ikoyi" not in json.dumps(r.json())  # B's address is not A's


def test_the_account_export_lists_the_confirmations_made_without_the_address(env) -> None:
    _confirm_rows(env)
    carol = env.client.get(f"{API}/account/export", headers=env.auth(CAROL))
    assert carol.status_code == 200, carol.text
    made = carol.json()["locality_confirmations_made"]
    assert made == [{
        "id": "a1-geo", "organization_id": ORG_A, "workspace_id": WORKSPACES[ORG_A][0],
        "location_id": "a1-loc", "locality_confirmed_at": AT.isoformat(),
    }]
    blob = json.dumps(made)
    for text in ("Ikeja", "1 Allen Avenue", "Lagos", "NG-LA", "Nigeria"):
        assert text not in blob
    bob = env.client.get(f"{API}/account/export", headers=env.auth(BOB)).json()
    assert [m["id"] for m in bob["locality_confirmations_made"]] == ["b1-geo"]
    dave = env.client.get(f"{API}/account/export", headers=env.auth(DAVE)).json()
    assert dave["locality_confirmations_made"] == []


# --------------------------------------------------------------------------- #
# V-3 / V-4: deletion
# --------------------------------------------------------------------------- #
def test_account_deletion_forgets_who_confirmed_and_keeps_the_confirmation(env) -> None:
    _confirm_rows(env)
    with env.session() as s:
        before = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert ("geo_coverage_rules", "locality_confirmed_by_user_id") in {
        (f.table, f.column) for f in before.reference_findings
    }
    carol_rule, bob_rule = _rule(env, "a1-geo"), _rule(env, "b1-geo")

    r = env.client.post(
        f"{API}/account/delete",
        headers=env.auth(CAROL),
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["counts"]["nulled"]["geo_coverage_rules.locality_confirmed_by_user_id"] == 1
    assert _rule(env, "a1-geo") == {
        **carol_rule, "locality_confirmed_by_user_id": None,
        "updated_at": _rule(env, "a1-geo")["updated_at"],
    }
    assert _rule(env, "b1-geo") == bob_rule
    with env.session() as s:
        after = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert after.verified, after.as_dict()


def test_organization_deletion_takes_the_organizations_rules_only(env) -> None:
    _confirm_rows(env)
    bob_rule = _rule(env, "b1-geo")
    r = env.client.post(
        f"{API}/organizations/{ORG_A}/delete",
        headers=env.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )
    assert r.status_code == 200, r.text
    assert env.rows(GeoCoverageRule, GeoCoverageRule.organization_id == ORG_A) == []
    assert _rule(env, "b1-geo") == bob_rule
    assert {row["organization_id"] for row in env.rows(GeoCoverageRule)} == {
        ORG_B, *(o for o in NAME if o not in (ORG_A, ORG_B))
    }
    with env.session() as s:
        full = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A]
        )
    assert full.verified, full.as_dict()


# --------------------------------------------------------------------------- #
# V-5 / V-7: the verifier scans the new columns; the inventory is unchanged
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("column", [c for c in NEW if c not in (
    "locality_confirmed_at", "locality_confirmed_by_user_id", "locality_confirmed_jurisdiction",
)])
def test_the_content_verifier_scans_the_new_text_columns(env, column) -> None:
    """Positive control: an address that names the subject, typed into a new column of a
    tenant row (which account deletion does not reach), is found by the content scan."""
    _confirm_rows(env)  # a whole binding, so a binding column can carry the planted text
    with env.engine.begin() as conn:
        conn.execute(
            update(GeoCoverageRule.__table__)
            .where(GeoCoverageRule.__table__.c.id == "b1-geo")
            .values({column: f"c/o {EMAIL[CAROL]}"})
        )
    with env.session() as s:
        report = verify_subject_erased(s, email=EMAIL[CAROL])
    assert ("geo_coverage_rules", column) in {(f.table, f.column) for f in report.content_findings}


def test_the_inventory_entry_is_unchanged() -> None:
    entry = TABLES["geo_coverage_rules"]
    assert entry.category is Category.TENANT_CONTENT
    assert entry.exported is True and entry.excluded_columns == ()
    assert entry.retention_class is None
