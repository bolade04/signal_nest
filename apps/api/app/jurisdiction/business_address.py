"""The structured business address before a jurisdiction-dependent action (W21 ADDR).

The operator's instruction of 2026-11-12 ("Authorize a separate structured-business-address
package"): a location's business address is stored as street, town or locality, state or
province, postcode and country (``GeoCoverageRule.business_address_*``), and "missing or
inconsistent information must return UNRESOLVED with an actionable clarification message before
jurisdiction-dependent brief or draft actions". :func:`resolve_for_action` is that check. It is
pure, like :mod:`app.jurisdiction.resolver`, and it is for jurisdiction-dependent actions only:
Scout searches, Locations CRUD and every other path never call it.

**The steps, in a fixed order** (design v3 §4.1). Each step is applied to every rule, in the
caller's order, before the next: the first failing step decides, and within it the first failing
rule.

1. **The location.** :func:`~app.jurisdiction.resolver.resolve_jurisdiction` with no rules: its
   country, and its state or province where the jurisdiction is one (item 33). Otherwise
   :data:`LOCATION_UNRESOLVED`.
2. **Structured contradiction.** A rule's country, read with
   :func:`~app.jurisdiction.resolver.country_code_of`, is another country than the location's;
   or its country reads as the location's, that country is in
   :data:`~app.jurisdiction.subdivisions.SUBDIVISION_JURISDICTIONS`, and its state or province,
   read with :func:`~app.jurisdiction.resolver.subdivision_code_of` in that country, is another
   subdivision. A region is never read without the rule's own readable country (a region with
   no country is incomplete, step 4). A refusal (:data:`BUSINESS_ADDRESS_CONFLICT`); never
   confirmable.
3. **Free-text refusal.** The merged free-text reader, run on each rule's text on its own
   (``resolve_jurisdiction(location, (rule,))``), refused a text as an explicit contradiction:
   :data:`BUSINESS_ADDRESS_TEXT_CONFLICT`. It runs whether or not the fields are complete; it
   can only refuse, never make the action resolve, so a known contradiction is never turned
   into a request to fill something in.
4. **Completeness.** No rule at all, or a field of :data:`MANDATORY_FIELDS` missing (the town
   or locality among them whenever the location has a city to compare against), or the country
   or a required state or province unreadable: :data:`BUSINESS_ADDRESS_INCOMPLETE`. A location
   with no city has no locality to compare: its business town stays optional, and the check
   compares the country and the state or province only. No town is ever derived or invented.
   Empty fields stop here: the text's agreement or silence is never consulted for them ("Empty
   new fields must not fall back to “town alone states nothing.”").
5. **Free-text clarification.** The first reading, in rule order, that is UNRESOLVED with a
   category: the reader's ambiguity category, unchanged (the structured
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
and the jurisdiction it was made for, and lapses when any of them changes. It is the
customer's own statement: confirmed by the customer.

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
            "Service area tab, enter the business country; for a business in the United States, "
            "Canada, Australia or Nigeria, the business state or province; and, when the location "
            "has a city, the business town or locality: the town, locality or named place used in "
            "the business's postal address. If the location's own city is wrong, correct it on "
            "its Details tab instead. Then try again."
        ),
        BUSINESS_ADDRESS_CONFLICT: (
            "The business country, or the business state or province, is not this location's "
            "own. Correct the business address on the location's Service area tab, or the "
            "location's country or state or province on its Details tab, so that both name the "
            "same place. This cannot be confirmed instead of corrected."
        ),
        BUSINESS_ADDRESS_TEXT_CONFLICT: (
            "We read the Business address text as naming a different country, or state or "
            "province, from this location's. Correct that text on the location's Service area "
            "tab, or leave it empty and use the business address fields. This cannot be "
            "confirmed instead of corrected."
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
#: location's country and whether the selected location has a city to compare against. The
#: country always, and the state or province where the jurisdiction is a subdivision ("the
#: existing jurisdiction requirements", item 33); the town or locality whenever the location's
#: city folds to something non-empty (operator instruction of 2026-11-12, item 3: "require
#: town/locality before jurisdiction-dependent actions when the selected location has a
#: town/city to compare against"). A location with no city has no locality to compare, so its
#: business town stays optional; nothing ever derives or copies a town. One table, so a
#: further ruling is a one-line change.
MANDATORY_FIELDS: Final[Mapping[tuple[str, bool], tuple[str, ...]]] = MappingProxyType(
    {
        ("subdivision", True): (
            "business_address_country",
            "business_address_region",
            "business_address_locality",
        ),
        ("subdivision", False): ("business_address_country", "business_address_region"),
        ("country", True): ("business_address_country", "business_address_locality"),
        ("country", False): ("business_address_country",),
    }
)

#: The evidence line for a location with no city: the town is not required and not compared.
NO_LOCALITY_TO_COMPARE: Final = "no locality to compare: the location has no city"


def country_class(country: str) -> str:
    """``"subdivision"`` for a country of ``SUBDIVISION_JURISDICTIONS``, else ``"country"``."""
    return "subdivision" if country in SUBDIVISION_JURISDICTIONS else "country"


def mandatory_fields(country: str, city: str | None) -> tuple[str, ...]:
    """The fields :data:`MANDATORY_FIELDS` requires for a location in ``country`` whose city is
    ``city`` (the town only when the city folds to something non-empty)."""
    return MANDATORY_FIELDS[(country_class(country), bool(fold(city)))]


class StructuredAddressFacts(Protocol):
    """What the check reads from a coverage rule (a ``GeoCoverageRule`` row satisfies it).
    Attributes missing from an object read as empty, the state of every row after the
    migration."""

    @property
    def business_address(self) -> str | None: ...


def _text(obj: object, name: str) -> str | None:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) and value.strip() else None


def _filled(rule: object, field: str) -> bool:
    """A field holds something: non-blank, and for the town something that folds non-empty, so
    a placeholder ("-", "...") never stands in for a locality."""
    text = _text(rule, field)
    if text is None:
        return False
    return bool(fold(text)) if field == "business_address_locality" else True


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


def structured_status(rule: object, code: str, city: str | None) -> str:
    """``"conflict"``, ``"incomplete"`` or ``"agree"``: one rule's structured fields against
    the location's ``code`` and ``city`` (steps 2 and 4). Only the rule's own fields are read:
    the town is required, never derived, when the location's ``city`` is non-empty."""
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
    if any(not _filled(rule, field) for field in mandatory_fields(country, city)):
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
        and structured_status(rule, code, city) == "agree"
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
            "Complete the business address and save it before confirming the town or locality: "
            "the business country, the business town or locality and, for a business in the "
            "United States, Canada, Australia or Nigeria, the business state or province."
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
    city = getattr(location, "city", None)
    status = structured_status(rule, code, city)
    if status != "agree":
        return status  # "conflict" or "incomplete"
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


#: The steps after the location's own (module docstring), in order. Each step is applied to every
#: rule, in the caller's order (``created_at``, ``id``), before the next: the first failing step
#: decides, and within it the first failing rule (design v3 §4.1, review d1 C-2).
STEP_CONFLICT: Final = 2
STEP_TEXT_REFUSAL: Final = 3
STEP_INCOMPLETE: Final = 4
STEP_TEXT_CLARIFY: Final = 5
STEP_TOWN: Final = 6


def resolve_for_action(
    location: LocationFacts | None, coverage_rules: Sequence[StructuredAddressFacts] = ()
) -> JurisdictionResolution:
    """The jurisdiction a jurisdiction-dependent action may use, or UNRESOLVED with a category
    of :data:`ACTION_CLARIFICATIONS` (the module docstring gives the steps). ``coverage_rules``
    are the location's own rules, selected by the caller within its tenant, in its order.

    Each rule's free text is read on its own (``resolve_jurisdiction(location, (rule,))``), so
    one rule's ambiguity can never hide another rule's explicit contradiction."""
    base = resolve_jurisdiction(location, ())
    if not base.is_resolved:
        return _unresolved(base.evidence[:-1], LOCATION_UNRESOLVED, "the location's own fields")
    code = base.code
    rules = list(coverage_rules)
    city = getattr(location, "city", None)

    failures: list[tuple[int, int, str, str]] = []
    if not rules:
        failures.append((STEP_INCOMPLETE, 0, BUSINESS_ADDRESS_INCOMPLETE, "no coverage rule"))
    texts = confirmed = 0
    for index, rule in enumerate(rules):
        status = structured_status(rule, code, city)
        if status == "conflict":
            failures.append(
                (STEP_CONFLICT, index, BUSINESS_ADDRESS_CONFLICT, "structured address conflicts")
            )
        elif status == "incomplete":
            failures.append(
                (STEP_INCOMPLETE, index, BUSINESS_ADDRESS_INCOMPLETE, "structured incomplete")
            )
        if _text(rule, "business_address"):
            texts += 1
            reading = resolve_jurisdiction(location, (rule,))
            if not reading.is_resolved and reading.reason is None:
                failures.append(
                    (STEP_TEXT_REFUSAL, index, BUSINESS_ADDRESS_TEXT_CONFLICT, "text contradicts")
                )
            elif not reading.is_resolved:
                failures.append((STEP_TEXT_CLARIFY, index, reading.reason, "text is ambiguous"))
        if locality_differs(_text(rule, "business_address_locality"), city):
            if confirmation_applies(rule, city, code):
                confirmed += 1
            else:
                failures.append(
                    (STEP_TOWN, index, BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED, "town differs")
                )
    if failures:
        step, index, category, why = min(failures, key=lambda f: (f[0], f[1]))
        return _unresolved(base.evidence, category, f"coverage rule {index}: {why}")

    evidence = [*base.evidence, "structured business address agrees"]
    if not fold(city):
        evidence.append(NO_LOCALITY_TO_COMPARE)
    if texts:
        evidence.append("business address text states nothing against it")
    if confirmed:
        evidence.append(
            "town or locality confirmed by the customer within the location's area"
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
    "NO_LOCALITY_TO_COMPARE",
    "REFUSALS",
    "TEXT_REMEDY",
    "confirmation_applies",
    "confirmation_refusal",
    "country_class",
    "location_code",
    "locality_differs",
    "mandatory_fields",
    "resolve_for_action",
    "structured_status",
]
