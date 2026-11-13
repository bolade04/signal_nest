"""A complete structured business address for a test location that captures a brief (W21 #313).

Since #313 the brief capture calls :func:`app.jurisdiction.business_address.resolve_for_action`
(W21 ADDR), so a location captures its code only with a coverage rule of its own whose
structured business address is complete and agrees with it (ADDR's list item 6):

* the country;
* the state or province where the jurisdiction is one (the United States, Canada, Australia
  and Nigeria, item 33);
* the town or locality when the location has a city.

These helpers write the location's own fields there, as entered: the country as the country,
the state or province as the region and the city as the town. Nothing is derived, read or
normalised, and nothing else of the rule is set unless a test passes it.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.locations.models import GeoCoverageRule


def complete_address(
    *, city: str | None, state_province: str | None, country: str | None
) -> dict[str, str | None]:
    """The structured business-address fields that agree with a location of these fields."""
    return {
        "business_address_country": country,
        "business_address_region": state_province,
        "business_address_locality": city,
    }


def complete_address_of(location: Mapping[str, str | None]) -> dict[str, str | None]:
    """:func:`complete_address` for a mapping with ``city``, ``state_province`` and ``country``."""
    return complete_address(
        city=location.get("city"),
        state_province=location.get("state_province"),
        country=location.get("country"),
    )


def complete_rule(location: object, **extra: object) -> GeoCoverageRule:
    """A coverage rule of ``location`` (a ``BusinessLocation``) in its own workspace and
    organization, with the complete structured business address of its own fields."""
    fields = complete_address(
        city=getattr(location, "city", None),
        state_province=getattr(location, "state_province", None),
        country=getattr(location, "country", None),
    )
    return GeoCoverageRule(
        organization_id=location.organization_id,
        workspace_id=location.workspace_id,
        location_id=location.id,
        **{**fields, **extra},
    )


__all__ = ["complete_address", "complete_address_of", "complete_rule"]
