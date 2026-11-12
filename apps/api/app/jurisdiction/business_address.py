"""The structured business address before a jurisdiction-dependent action (W21 ADDR).

The operator's instruction of 2026-11-12 ("Authorize a separate structured-business-address
package"): a location's business address is stored as street, town or locality, state or
province, postcode and country (``GeoCoverageRule.business_address_*``), and "missing or
inconsistent information must return UNRESOLVED with an actionable clarification message before
jurisdiction-dependent brief or draft actions". :func:`resolve_for_action` is that check. It is
pure, like :mod:`app.jurisdiction.resolver`, and it is for jurisdiction-dependent actions only:
Scout searches, Locations CRUD and every other path never call it.

**The steps, in a fixed order; the first that fails decides** (design v2 §4.1):

1. **The location.** :func:`~app.jurisdiction.resolver.resolve_jurisdiction` with no rules: its
   country, and its state or province where the jurisdiction is one (item 33). Otherwise
   :data:`LOCATION_UNRESOLVED`.
2. **Structured contradiction.** A rule's country, read with
   :func:`~app.jurisdiction.resolver.country_code_of`, is another country than the location's;
   or, in a country of :data:`~app.jurisdiction.subdivisions.SUBDIVISION_JURISDICTIONS`, its
   state or province, read with :func:`~app.jurisdiction.resolver.subdivision_code_of`, is
   another subdivision. A refusal (:data:`BUSINESS_ADDRESS_CONFLICT`); never confirmable.
3. **Free-text refusal.** The merged free-text reader (``resolve_jurisdiction`` with the rules)
   refused the text as an explicit contradiction: :data:`BUSINESS_ADDRESS_TEXT_CONFLICT`. It
   runs whether or not the fields are complete; it can only refuse, never make the action
   resolve, so a known contradiction is never turned into a request to fill something in.
4. **Completeness.** No rule at all, or a field of :data:`MANDATORY_FIELDS` missing, or the
   country or a required state or province unreadable: :data:`BUSINESS_ADDRESS_INCOMPLETE`.
   Empty fields stop here: the text's agreement or silence is never consulted for them ("Empty
   new fields must not fall back to “town alone states nothing.”").
5. **Free-text clarification.** The reader's ambiguity category, unchanged (the structured
   fields do not settle it: RULING-D1-C3), with the remedy sentence of
   :data:`ACTION_CLARIFICATIONS`.
6. **The town.** The business town and the location's city differ after
   :func:`~app.geography.markets.fold` and no applicable customer confirmation binds them:
   :data:`BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED`. Consulted last, so a confirmation can never
   override a contradiction.
7. **Resolved**, to the location's own code.

**What is read, and what is not.** The country and the state or province are read only as a
country and as a subdivision of that country, by the readers that read the location's own
fields. The street and the postcode are never read. The town is compared with the location's
city for equality after ``fold`` and is never parsed: "Dallas TX" is not "Dallas", and its
"TX" is not read as a code. A state or province with no country is never read in the
location's country (the location's jurisdiction is never copied into the address). No city
table, alias list or dataset exists here.

**A customer's confirmation** (``GeoCoverageRule.locality_confirmed_*``) answers only step 6:
that the business town lies within the location's area. It binds the town, the location's city
and the jurisdiction it was made for, and lapses when any of them changes. It is
customer-confirmed, never verified.

Every UNRESOLVED answer carries a category of :data:`ACTION_CLARIFICATIONS`, whose messages are
fixed and never contain an address, other customer text or the resolver's evidence; neither does
any evidence line this module adds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, Protocol

from app.geography.markets import fold
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import (
    CLARIFICATIONS,
    JurisdictionResolution,
    LocationFacts,
    country_code_of,
    resolve_jurisdiction,
    subdivision_code_of,
)
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS

# --- The categories (closed and stable) ---------------------------------------------------
#: The location's own country, or its required state or province, is missing or unreadable.
LOCATION_UNRESOLVED: Final = "location_unresolved"
#: A required business-address field is missing or unreadable, or there is no rule at all.
BUSINESS_ADDRESS_INCOMPLETE: Final = "business_address_incomplete"
#: The business country, or state or province, is another than the location's (a refusal).
BUSINESS_ADDRESS_CONFLICT: Final = "business_address_conflict"
#: The free-text business address explicitly names another place (a refusal).
BUSINESS_ADDRESS_TEXT_CONFLICT: Final = "business_address_text_conflict"
#: The business town differs from the location's city, unconfirmed (confirmable).
BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED: Final = "business_address_locality_unconfirmed"

#: This package's categories and their fixed messages (design v2 §4.6, verbatim).
ADDRESS_CLARIFICATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        LOCATION_UNRESOLVED: (
            "This location's country, or its state or province, is missing or not recognised. "
            "Open the location and, on its Details tab, enter its country and, for a location in "
            "the United States, Canada, Australia or Nigeria, its state or province. Then try "
            "again."
        ),
        BUSINESS_ADDRESS_INCOMPLETE: (
            "This location's business address is incomplete. Open the location and, on its "
            "Service area tab, enter the business country and, for a business in the United "
            "States, Canada, Australia or Nigeria, the business state or province. Then try again."
        ),
        BUSINESS_ADDRESS_CONFLICT: (
            "The business country, or the business state or province, is not this location's "
            "own. Correct the business address on the location's Service area tab, or the "
            "location's country or state or province on its Details tab, so that both name the "
            "same place. This cannot be confirmed instead of corrected."
        ),
        BUSINESS_ADDRESS_TEXT_CONFLICT: (
            "The Business address text names a different country, or state or province, from "
            "this location's. Correct that text on the location's Service area tab, or leave it "
            "empty and use the business address fields. This cannot be confirmed instead of "
            "corrected."
        ),
        BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED: (
            "The business town or locality is not this location's city. If the business is in "
            "the location's city, enter the same name in both. If the town or locality lies "
            "within this location's area, for example a suburb, confirm that on the location's "
            "Service area tab. Otherwise correct the business address or the location's city."
        ),
    }
)

#: Appended to each free-text reader category in this vocabulary. True whenever such a category
#: is returned, because step 5 runs only after step 4 has passed.
TEXT_REMEDY: Final = (
    " The business address fields are complete, so you can instead leave the Business address "
    "text empty."
)

#: The closed vocabulary of :func:`resolve_for_action`: this package's categories, and every
#: category of the merged free-text reader (``resolver.CLARIFICATIONS``, read here, so a
#: category the geography lane adds joins without an edit) with :data:`TEXT_REMEDY`.
ACTION_CLARIFICATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        **ADDRESS_CLARIFICATIONS,
        **{category: message + TEXT_REMEDY for category, message in CLARIFICATIONS.items()},
    }
)

#: The categories that refuse a known contradiction: no confirmation can answer them.
REFUSALS: Final = frozenset({BUSINESS_ADDRESS_CONFLICT, BUSINESS_ADDRESS_TEXT_CONFLICT})

# --- What is mandatory (one table; coordinator ruling OP-3) --------------------------------
#: The business-address fields a jurisdiction-dependent action requires, by the class of the
#: location's country: "the existing jurisdiction requirements" (item 33). The country always;
#: the state or province where the jurisdiction is a subdivision. A different operator ruling
#: (for example, a required town) is a one-line change here.
MANDATORY_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "subdivision": ("business_address_country", "business_address_region"),
        "country": ("business_address_country",),
    }
)


def country_class(country: str) -> str:
    """``"subdivision"`` for a country of ``SUBDIVISION_JURISDICTIONS``, else ``"country"``."""
    return "subdivision" if country in SUBDIVISION_JURISDICTIONS else "country"


class StructuredAddressFacts(Protocol):
    """What the check reads from a coverage rule (a ``GeoCoverageRule`` row satisfies it).
    Attributes missing from an object read as empty, the state of every row after the
    migration."""

    @property
    def business_address(self) -> str | None: ...


def _text(obj: object, name: str) -> str | None:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) and value.strip() else None


def location_code(location: LocationFacts | None) -> str | None:
    """The location's jurisdiction from its own country and state or province, exactly as
    :func:`resolve_jurisdiction` reads them (item 33), or ``None``.

    For the Locations routes, which must not call the resolver (register gate G0 attaches to
    ``resolve_jurisdiction``'s first caller outside tests). It does not read the location's
    captured ``jurisdiction_code``, which nothing writes; :func:`resolve_for_action` does."""
    if location is None:
        return None
    country = country_code_of(getattr(location, "country", None))
    if country is None:
        return None
    if country not in SUBDIVISION_JURISDICTIONS:
        return country
    return subdivision_code_of(country, getattr(location, "state_province", None))


def structured_status(rule: object, code: str) -> str:
    """``"conflict"``, ``"incomplete"`` or ``"agree"``: one rule's structured fields against
    the location's ``code`` (steps 2 and 4). Only the rule's own fields are read."""
    country = code.split("-", 1)[0]
    cls = country_class(country)
    stated_country = None
    country_text = _text(rule, "business_address_country")
    if country_text is not None:
        stated_country = country_code_of(country_text)
        if stated_country is not None and stated_country != country:
            return "conflict"
    region_text = _text(rule, "business_address_region")
    stated_region = None
    if cls == "subdivision" and stated_country == country and region_text is not None:
        stated_region = subdivision_code_of(country, region_text)
        if stated_region is not None and stated_region != code:
            return "conflict"
    if any(_text(rule, field) is None for field in MANDATORY_FIELDS[cls]):
        return "incomplete"
    if country_text is not None and stated_country is None:
        return "incomplete"  # a country that does not read
    if cls == "subdivision" and region_text is not None and stated_region is None:
        return "incomplete"  # a state or province that does not read (or no country to read it in)
    return "agree"


def locality_differs(locality: str | None, city: str | None) -> bool | None:
    """Whether the business town and the location's city differ after ``fold`` (the only
    established mapping); ``None`` when either is empty (nothing to compare)."""
    town, place = fold(locality), fold(city)
    if not town or not place:
        return None
    return town != place


def confirmation_applies(rule: object, city: str | None, code: str | None) -> bool:
    """Whether the rule's stored locality confirmation binds what holds now: its current town,
    the location's current ``city``, and the jurisdiction ``code`` that the location and the
    business address both read as (the address agreeing with the location), all as they were
    when it was made."""
    if getattr(rule, "locality_confirmed_at", None) is None or code is None:
        return False
    town = _text(rule, "business_address_locality")
    return (
        bool(fold(town))
        and fold(_text(rule, "locality_confirmed_locality")) == fold(town)
        and fold(_text(rule, "locality_confirmed_city")) == fold(city)
        and getattr(rule, "locality_confirmed_jurisdiction", None) == code
        and structured_status(rule, code) == "agree"
    )


#: The fixed answers of the locality-confirmation route when it records nothing (422
#: ``locality_confirmation_refused``), by cause, in the order they are checked (design v2 §4.6).
CONFIRMATION_REFUSALS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "location": (
            "Enter this location's country and, for a location in the United States, Canada, "
            "Australia or Nigeria, its state or province on its Details tab, and save, before "
            "confirming the town or locality."
        ),
        "conflict": (
            "This town or locality cannot be confirmed: the business country, or the business "
            "state or province, is not this location's own. Correct it first."
        ),
        "incomplete": (
            "Enter the business country and, for a business in the United States, Canada, "
            "Australia or Nigeria, the business state or province, and save, before confirming "
            "the town or locality."
        ),
        "nothing": (
            "There is nothing to confirm. A confirmation is needed only when the saved business "
            "town or locality and the location's city are both filled in and differ."
        ),
    }
)


def confirmation_refusal(rule: object, location: LocationFacts) -> str | None:
    """Why a customer may not confirm the stored town now (a key of
    :data:`CONFIRMATION_REFUSALS`), or ``None``: the location reads, its stored structured
    address neither contradicts it nor is incomplete, and the stored town and the location's
    city both exist and differ. A confirmation can never be recorded over a contradiction."""
    code = location_code(location)
    if code is None:
        return "location"
    status = structured_status(rule, code)
    if status != "agree":
        return status  # "conflict" or "incomplete"
    city = getattr(location, "city", None)
    if not locality_differs(_text(rule, "business_address_locality"), city):
        return "nothing"
    return None


def _unresolved(evidence: Sequence[str], category: str, why: str) -> JurisdictionResolution:
    return JurisdictionResolution(
        code=UNRESOLVED,
        codes=(),
        confidence=0.0,
        evidence=(*evidence, f"unresolved: {why}"),
        reason=category,
    )


def resolve_for_action(
    location: LocationFacts | None, coverage_rules: Sequence[StructuredAddressFacts] = ()
) -> JurisdictionResolution:
    """The jurisdiction a jurisdiction-dependent action may use, or UNRESOLVED with a category
    of :data:`ACTION_CLARIFICATIONS` (the module docstring gives the steps). ``coverage_rules``
    are the location's own rules, selected by the caller within its tenant."""
    base = resolve_jurisdiction(location, ())
    if not base.is_resolved:
        return _unresolved(base.evidence[:-1], LOCATION_UNRESOLVED, "the location's own fields")
    code = base.code
    evidence = list(base.evidence)
    rules = list(coverage_rules)

    statuses = [structured_status(rule, code) for rule in rules]
    if "conflict" in statuses:
        return _unresolved(evidence, BUSINESS_ADDRESS_CONFLICT, "structured address conflicts")

    text = None
    if any(_text(rule, "business_address") for rule in rules):
        text = resolve_jurisdiction(location, rules)
        if not text.is_resolved and text.reason is None:
            return _unresolved(
                evidence, BUSINESS_ADDRESS_TEXT_CONFLICT, "business address text contradicts"
            )

    if not rules or "incomplete" in statuses:
        return _unresolved(evidence, BUSINESS_ADDRESS_INCOMPLETE, "structured address incomplete")
    evidence.append("structured business address agrees")

    if text is not None and not text.is_resolved:
        return _unresolved(evidence, text.reason, "business address text is ambiguous")
    if text is not None:
        evidence.append("business address text states nothing against it")

    city = getattr(location, "city", None)
    for rule in rules:
        if locality_differs(_text(rule, "business_address_locality"), city):
            if not confirmation_applies(rule, city, code):
                return _unresolved(
                    evidence, BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED, "town differs, unconfirmed"
                )
            evidence.append(
                "town or locality customer-confirmed within the location's area (not verified)"
            )
    return JurisdictionResolution(
        code=code, codes=base.codes, confidence=base.confidence, evidence=tuple(evidence)
    )


__all__ = [
    "ACTION_CLARIFICATIONS",
    "ADDRESS_CLARIFICATIONS",
    "BUSINESS_ADDRESS_CONFLICT",
    "BUSINESS_ADDRESS_INCOMPLETE",
    "BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED",
    "BUSINESS_ADDRESS_TEXT_CONFLICT",
    "CONFIRMATION_REFUSALS",
    "LOCATION_UNRESOLVED",
    "MANDATORY_FIELDS",
    "REFUSALS",
    "TEXT_REMEDY",
    "confirmation_applies",
    "confirmation_refusal",
    "country_class",
    "location_code",
    "locality_differs",
    "resolve_for_action",
    "structured_status",
]
