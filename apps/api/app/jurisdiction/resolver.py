"""Normalised, fail-closed jurisdiction resolution (Project Phase 5D §2; lane 5D-W2).

Pure and framework-free, like ``app.geography.engine``: no database, no network, no
geocoder. It reads what a location record already holds (its country and state or
province, which the location form fills from the geocoder) and answers with a
:class:`JurisdictionResolution`: the jurisdiction code, the codes whose rules apply there,
a confidence and the evidence for it.

**What a jurisdiction is.** A jurisdiction code (:mod:`app.jurisdiction.codes`) at the level
where the location's marketing rules are made: the country (``GB``, ``KE``), or, in the
federations listed in :mod:`app.jurisdiction.subdivisions`, the first-level subdivision
(``US-TX``, ``NG-LA``). The four-market matrix resolves to ``US-TX`` (Dallas), ``GB``
(London), ``NG-LA`` (Lagos) and ``KE`` (Nairobi).

**Fail closed.** The resolver never guesses. It answers :data:`UNRESOLVED` when:

* the location names no country, or a country it cannot read (``"Narnia"``);
* the country resolves at subdivision level and the location names no subdivision of it,
  or one it cannot read (a United States location without a state);
* the facts disagree: a coverage rule's ``business_address`` states another country or
  another subdivision than the location's own fields, or the location's captured
  ``jurisdiction_code`` is not what its fields resolve to now;
* a business address is an ambiguous street line, or has an ambiguous part ("1500 Broadway,
  Hartford CT", "123 MAIN ST, DALLAS"), whose last word may be a street type or a code that
  contradicts the location ("1500 Broadway Hartford CT" for a Texas location), and no explicit
  geography in the address settles that reading or rules it out. The answer then carries the
  reason :data:`ADDRESS_SUFFIX_AMBIGUOUS`, whose fixed clarification is in
  :data:`CLARIFICATIONS` (D1, operator ruling of 2026-11-09). No page or route uses it yet;
* a business address names a place by a code of two or more areas that place knowledge leaves
  undecided ("Dallas, GA": Georgia or Gabon), and every reading contradicts the location: a
  refusal for explicit text, the same clarification for an ambiguous street line or part
  ("1 SPRING HILL ST NE": Nebraska or Niger). A reading in the location's own area agrees;
* a part of a business address names a place by itself, wherever it stands, and contradicts the
  location (GEO-P1): a listed code or a suffixed or prefixed subdivision ("US-AL; Montgomery;
  1 Main St; Suite 5" for Anchorage) is refused; a country's or a state's name, or a code written
  in capitals, is refused unless another part names the location's own area, when it asks with the
  reason :data:`ADDRESS_NAMES_ANOTHER_PLACE`; a word that may be a town's name asks with
  :data:`ADDRESS_PLACE_UNCLEAR`, and a part that is only a direction with
  :data:`ADDRESS_DIRECTION_AMBIGUOUS` unless explicit geography settles it. A first part read as a
  town and a code ("Dallas TX, 1 Main St"), which may be a business's name ("ACME SA"), asks with
  :data:`ADDRESS_PLACE_UNCLEAR` unless explicit geography settles it (W21-REV-GEO r1 C-1);
* a business address is only a route designator ("1 SR 520", "12000 FM 1960"), whose code reading
  may be a road and contradicts the location: :data:`ADDRESS_ROUTE_AMBIGUOUS` (GEO-P2). A floor
  ("Fl 3") is never read as Florida, and a street with no house number ("Main St") is an ambiguous
  street line.

A city alone never resolves, known or unknown: a bare city says nothing about which
country it is in (``app.geography.markets`` reads a bare "London" the same way). Every
consumer treats :data:`UNRESOLVED` as **deny**; :func:`jurisdiction_permits` is the gate
they share.

**Matching is by code, never by substring.** Codes compare by equality and by the one
containment the grammar defines (a subdivision lies inside its country): ``US-NY`` is not
inside ``US-N``, and ``GB`` is not inside ``GB-ENG``. Free-text market matching stays in
``app.geography`` and is not used here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol

from app.geography.markets import (
    PLACE_CODE,
    PLACE_DIRECTION,
    PLACE_FIRST_PART,
    PLACE_NAME,
    PLACE_UNCLEAR,
    BusinessAddressReading,
    MarketIdentity,
    PartPlace,
    address_part_key,
    fold,
    last_written_part,
    normalize_country,
    normalize_region,
    own_reading_of_homonym,
    read_business_address,
    without_town_parts,
)
from app.jurisdiction.codes import UNRESOLVED, is_jurisdiction_code
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS


class LocationFacts(Protocol):
    """What the resolver reads from a location. A ``BusinessLocation`` row satisfies it; an
    optional ``jurisdiction_code`` attribute (the captured code) is read when present."""

    @property
    def city(self) -> str | None: ...

    @property
    def state_province(self) -> str | None: ...

    @property
    def country(self) -> str | None: ...


class CoverageFacts(Protocol):
    """What the resolver reads from a coverage rule (a ``GeoCoverageRule`` row satisfies it)."""

    @property
    def business_address(self) -> str | None: ...


#: Confidence of a resolution read from structured fields by exact table lookup. The
#: resolver does not guess, so it reports no intermediate value; gating never thresholds on
#: confidence, it gates on :attr:`JurisdictionResolution.is_resolved`.
RESOLVED_CONFIDENCE: Final = 1.0

#: Suffixes dropped from a subdivision name ("Lagos State" is Lagos).
_SUFFIXES: Final = (" state", " province", " territory", " region")


def _token_index() -> dict[str, dict[str, str]]:
    """country -> {folded name, alias or code: subdivision code}. A token that would name
    two subdivisions of one country is left out (ambiguous, so it resolves nothing)."""
    index: dict[str, dict[str, str]] = {}
    for country, divisions in SUBDIVISION_JURISDICTIONS.items():
        seen: dict[str, set[str]] = {}
        for code, names in divisions.items():
            for token in (code.lower(), *names):
                seen.setdefault(token, set()).add(code)
        index[country] = {token: next(iter(c)) for token, c in seen.items() if len(c) == 1}
    return index


_TOKENS: Final = _token_index()


@dataclass(frozen=True)
class JurisdictionResolution:
    """The resolver's answer.

    ``code`` is a jurisdiction code or :data:`UNRESOLVED`. ``codes`` are the jurisdictions
    whose rules apply at the location, most specific first (``("US-TX", "US")``,
    ``("GB",)``); empty when unresolved. ``evidence`` says how the answer was reached, or
    why there is none. ``reason`` is a reason category for an UNRESOLVED answer the customer
    can act on (:data:`ADDRESS_SUFFIX_AMBIGUOUS`; its message is in :data:`CLARIFICATIONS`),
    and ``None`` otherwise.
    """

    code: str
    codes: tuple[str, ...]
    confidence: float
    evidence: tuple[str, ...]
    reason: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.code != UNRESOLVED and is_jurisdiction_code(self.code)


#: Reason category: a business address ends in a word that may be a street type or a state or
#: country code, and as a code it contradicts the location (D1, operator ruling 2026-11-09).
ADDRESS_SUFFIX_AMBIGUOUS: Final = "address_suffix_ambiguous"
#: Reason category (GEO-P1): one part of a business address names a state, province or country
#: that contradicts the location, while another part names the location's own. The part may be a
#: town's or a business's name ("Lebanon", "Jordan"), so it is not refused.
ADDRESS_NAMES_ANOTHER_PLACE: Final = "address_names_another_place"
#: Reason category (GEO-P1): a word of a business address may be a state's or a country's name or
#: code, or a town's name (a two-letter code in an address written in one case, or not in capitals,
#: a Nigerian state's bare name, "Washington"), or the first part a town and a code or a business's
#: name ("Dallas TX", "ACME SA"; W21-REV-GEO r1 C-1), and as a place it contradicts the location.
ADDRESS_PLACE_UNCLEAR: Final = "address_place_unclear"
#: Reason category (GEO-P1): a part of a business address is only a direction ("SE", "NE"), which
#: may be a street's direction or a state or country code, and as a code it contradicts the
#: location.
ADDRESS_DIRECTION_AMBIGUOUS: Final = "address_direction_ambiguous"
#: Reason category (GEO-P2): a business address that is only a route designator ("1 SR 520",
#: "12000 FM 1960") may be a road, or a state or country code that contradicts the location. (A
#: designator in a later part keeps D1's reason, C-16.)
ADDRESS_ROUTE_AMBIGUOUS: Final = "address_route_ambiguous"

#: The fixed, customer-safe clarification for each reason category. It never contains the
#: address or other customer text, nor the resolver's evidence.
CLARIFICATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        ADDRESS_SUFFIX_AMBIGUOUS: (
            "We couldn't tell whether the end of the business address is a street type or a "
            "state or country. Write the street type or direction in full (for example "
            "'Street', 'Court' or 'Southeast'), or add the city and state or country after a "
            "comma (for example '1500 Broadway, Hartford, CT')."
        ),
        ADDRESS_NAMES_ANOTHER_PLACE: (
            "The business address names a state, province or country that is not this "
            "location's, as well as the location's own. If that part of the address is the "
            "town, enter the same town as the location's city. If it is the name of the "
            "business or a building, leave it out of the address. Otherwise correct or remove it."
        ),
        ADDRESS_PLACE_UNCLEAR: (
            "We couldn't tell whether a word in the business address is a state, province or "
            "country, or the name of a town. If it is the town, enter the same town as the "
            "location's city. If it is a state, province or country, write its full name (for "
            "example 'Lagos State' or 'Nigeria') after the town, separated by a comma. If it is "
            "the name of the business or a building, leave it out of the address. Otherwise "
            "correct or remove it."
        ),
        ADDRESS_ROUTE_AMBIGUOUS: (
            "We couldn't tell whether a route or unit number in the business address (such as "
            "'SR 520', 'FM 1960' or 'Unit 5 NY 10001') is part of the street or a state or "
            "country. After it, write the town and the state or province in full, separated by "
            "commas (for example '1 SR 520, Redmond, Washington'), or write the road's name in "
            "full (for example '1 State Route 520')."
        ),
        ADDRESS_DIRECTION_AMBIGUOUS: (
            "We couldn't tell whether a part of the business address that is only a direction "
            "(such as 'NE' or 'SE') is the street's direction or a state or country. Write the "
            "direction in full after the street name (for example '1200 Pennsylvania Avenue "
            "Southeast'), or add the city and state or country after a comma."
        ),
    }
)


def _unresolved(evidence: list[str], why: str, reason: str | None = None) -> JurisdictionResolution:
    return JurisdictionResolution(
        code=UNRESOLVED,
        codes=(),
        confidence=0.0,
        evidence=(*evidence, f"unresolved: {why}"),
        reason=reason,
    )


def country_code_of(text: str | None) -> str | None:
    """The ISO country a location's country field names, read as ``market_from_parts``
    reads it (a region typed there, "England", gives its country); ``None`` when unknown."""
    if text is None or not text.strip():
        return None
    code = normalize_country(text)
    if code is None:
        _, code = normalize_region(text, None)
    return code


def subdivision_code_of(country: str, text: str | None) -> str | None:
    """The subdivision of ``country`` that ``text`` names (``"Texas"``, ``"TX"``,
    ``"US-TX"``, ``"Lagos State"``), as a full code (``"US-TX"``); ``None`` otherwise."""
    tokens = _TOKENS.get(country)
    if tokens is None or text is None or not text.strip():
        return None
    raw = text.strip().upper()
    if "-" in raw and is_jurisdiction_code(raw):
        prefix, part = raw.split("-", 1)
        return raw if prefix == country and part in SUBDIVISION_JURISDICTIONS[country] else None
    folded = fold(text)
    found = tokens.get(folded)
    if found is None:
        for suffix in _SUFFIXES:
            if folded.endswith(suffix) and folded[: -len(suffix)] in tokens:
                found = tokens[folded[: -len(suffix)]]
                break
    return f"{country}-{found}" if found else None


def code_within(code: str | None, scope: str | None) -> bool:
    """True only when ``code`` lies inside ``scope``: the same code, or a subdivision of the
    country ``scope`` names. Never by substring; anything that is not a code is inside
    nothing and contains nothing."""
    if not (isinstance(code, str) and isinstance(scope, str)):
        return False
    if not (is_jurisdiction_code(code) and is_jurisdiction_code(scope)):
        return False
    return code == scope or ("-" not in scope and code.startswith(f"{scope}-"))


def codes_may_overlap(code: str | None, other: str | None) -> bool:
    """False only when ``code`` and ``other`` are known to be disjoint. Something that is not
    a code cannot be ruled out, so it overlaps everything (as exclusions need)."""
    if not (is_jurisdiction_code(code) and is_jurisdiction_code(other)):
        return True
    return code_within(code, other) or code_within(other, code)


def jurisdiction_permits(
    subject: JurisdictionResolution | str | None,
    *,
    allowed: Iterable[str] = (),
    excluded: Iterable[str] = (),
) -> bool:
    """The gating decision every jurisdiction consumer makes (fail closed).

    ``subject`` is a resolution, or a stored code (a snapshot column; ``None`` means none
    was ever captured). The answer is False for :data:`UNRESOLVED`, ``None`` or anything
    that is not a jurisdiction code, whatever ``allowed`` and ``excluded`` say. Otherwise it
    is False when the code may overlap an ``excluded`` entry (an excluded entry that is not
    a code cannot be ruled out), and, when ``allowed`` is non-empty, False unless the code
    lies inside one of its entries (an entry that is not a code clears nothing).
    """
    if isinstance(subject, JurisdictionResolution):
        code: str | None = subject.code if subject.is_resolved else None
    else:
        code = subject
    if code is None or code == UNRESOLVED or not is_jurisdiction_code(code):
        return False
    if any(codes_may_overlap(code, entry) for entry in excluded):
        return False
    allowed_codes = tuple(allowed)
    return not allowed_codes or any(code_within(code, entry) for entry in allowed_codes)


def _stated_subdivision(stated: MarketIdentity, country: str) -> str | None:
    """The subdivision code of ``country`` that ``stated``'s region names, if any. A known
    subdivision is keyed "US-TX"; any other region "NG:lagos" (markets)."""
    region = stated.region
    if region is None:
        return None
    if region.startswith(f"{country}-"):
        return region
    return subdivision_code_of(country, region.split(":", 1)[-1])


def _conflict(stated: MarketIdentity | None, country: str, code: str) -> str | None:
    """``"country XX"`` or ``"subdivision XX-YY"`` when ``stated`` names another country, or
    another subdivision of a country that resolves at subdivision level; ``None`` otherwise. A
    listed code that the rest of the address leaves unplaceable ("US-TX?") is always a conflict."""
    if stated is None or stated.country is None:
        return None
    if stated.country != country:
        return f"country {stated.country}"
    if "-" in code:
        stated_code = _stated_subdivision(stated, country)
        if stated_code is not None and stated_code.endswith("?"):
            # A listed code the reader could not place with the rest of the address ("US-TX,
            # Canada"; markets keys it "US-TX?"): say so in words (W21-REV-5DW2FIX r3 I-1).
            listed = stated_code[:-1]
            return f"subdivision {listed}, but the rest of the address cannot be read with it"
        if stated_code is not None and stated_code != code:
            return f"subdivision {stated_code}"
    return None


def _address_check(
    address: str | None, country: str, code: str, city: str | None = None
) -> tuple[bool, str | None, str | None]:
    """``(states a country, conflict, reason)`` for a coverage rule's ``business_address``.

    Only what the address states is compared (``read_business_address``: no country is
    supplied that the text does not name, and a street line's "St", "Pl" or "Ct" is not read as
    a code). An address, or a later part of it ("..., Dallas TX"), naming another country or
    another subdivision of a country that resolves at subdivision level contradicts the
    location. An ambiguous line or part whose last word, read as a code, would contradict it
    ("1500 Broadway Hartford CT" or "1500 Broadway, Hartford CT" for Dallas) is a conflict too,
    with the reason :data:`ADDRESS_SUFFIX_AMBIGUOUS`: it is never read as stating nothing.
    Explicit geography settles what it states: an explicit subdivision leaves nothing ambiguous,
    and an explicit country rules out readings in another country ("123 MAIN ST, Dallas, TX"
    and "123 MAIN ST, USA" agree with Dallas).

    A group of readings of a code that names two or more areas (W21-REV-D1STREET r2 C-8)
    contradicts the location when every reading does: explicit text is refused ("Dallas, GA" for
    London "states country GA or subdivision US-GA"), and a stated country cannot excuse it; an
    ambiguous line or part gets :data:`ADDRESS_SUFFIX_AMBIGUOUS` after a stated country has ruled
    out its readings elsewhere, since its street reading remains. A reading in the location's own
    area agrees.

    GEO-P1: a part that names a place by itself is compared wherever it stands
    (``reading.places``; :class:`app.geography.markets.PartPlace`), and only to find a
    contradiction: it never settles an ambiguous reading and never makes an address agree. A part
    whose every reading contradicts the location is:

    * a listed code or a suffixed or prefixed subdivision ("US-AL; Montgomery; 1 Main St; 1 Main
      St" for Anchorage, "Ogun State, Abeokuta" for Ikeja): refused, "states subdivision US-AL";
    * a country's or a state's name, or a code written in capitals ("ACME, NG, Lagos" for Dallas):
      refused, unless another part names the location's own area at the same level (its country,
      or its subdivision where the contradiction is a subdivision of its own country), when it may
      be a town's or a business's name ("Oklahoma, US-TX, Texas, USA" for Dallas): then
      :data:`ADDRESS_NAMES_ANOTHER_PLACE`;
    * a word that may be a town's name (:data:`app.geography.markets.PLACE_UNCLEAR`):
      :data:`ADDRESS_PLACE_UNCLEAR`;
    * a part that is only a direction ("SE", "NE"): read as an ambiguous street reading is (C-14),
      so explicit geography settles it; otherwise :data:`ADDRESS_DIRECTION_AMBIGUOUS`;
    * the first part read as a town and a code (:data:`app.geography.markets.PLACE_FIRST_PART`:
      "Dallas TX, 1 Main St", "Springfield AN; 1 Main St"; W21-REV-GEO r1 C-1 and ruling R-2): read
      as D1 reads the first part's ambiguous reading, since it may be a business's name and its
      legal form ("ACME SA, Calle Mayor 1, 28013 Madrid"). An explicit subdivision of the location's
      country settles it and an explicit country rules out its readings in other countries, so
      "ACME SA, ..., Spain" resolves for Madrid and "Dallas TX, 1 Main St, Anchorage, AK" for
      Anchorage, as on main; otherwise :data:`ADDRESS_PLACE_UNCLEAR`. It is never refused and never
      agrees.

    A name or word that is the location's own city ("Washington, US-DC" for a Washington
    location; "Lebanon, PA" for a Lebanon location) is the town and is not compared: the
    location's city is structured input, not a city table. A code is compared whatever the city.

    GEO-P3 (register G-2): the location's city also settles a part that the qualifier rules read
    as an area. When the address is refused, it is read again without its parts that are the
    location's town but name an area (:func:`app.geography.markets.without_town_parts`: "US-DC,
    Washington" for Washington, DC, where "Washington" is also Washington State); the second
    reading stands only when it states the location's own area and nothing contradicts it. A
    listed code or a suffixed or prefixed subdivision is never left out.

    GEO-P3 (register G-2; ruling P3-R-6): a last part whose name has a listed homonym ("Niger": the
    country, or Niger State, X-5) is read as the country, as before, and the homonym never makes the
    address agree by itself. When the homonym is the only reading that agrees ("Minna, Niger" and
    "1 Allen Ave, Minna, Niger" for a Niger State location) and nothing settles it, the address
    asks (:data:`ADDRESS_PLACE_UNCLEAR`: "is ambiguous: country NE or subdivision NG-NI"), after an
    explicit code elsewhere that contradicts is refused. "Minna, Niger State" and "Minna, Niger,
    Nigeria" resolve; for any other location the country reading still refuses.
    """
    result = _reading_check(read_business_address(address), address, country, code, city)
    if result[1] is not None and result[2] is None:
        trimmed = without_town_parts(address, _town_keys(city))
        if trimmed is not None:
            retry = _reading_check(read_business_address(trimmed), trimmed, country, code, city)
            if retry[0] and retry[1] is None:
                return retry
    return result


def _reading_check(
    reading: BusinessAddressReading,
    address: str | None,
    country: str,
    code: str,
    city: str | None,
) -> tuple[bool, str | None, str | None]:
    """:func:`_address_check` for one reading of the address."""
    explicit = [s for s in (reading.stated, *reading.parts) if s is not None and s.country]
    homonym_ask = None
    last = last_written_part(address)
    for stated in list(explicit):
        conflict = _conflict(stated, country, code)
        if conflict is not None:
            if stated is reading.stated:
                agreeing = [
                    MarketIdentity(region=r, country=c, label=stated.label)
                    for c, r in own_reading_of_homonym(stated, last)
                ]
                agreeing = [a for a in agreeing if _conflict(a, country, code) is None]
                if agreeing:
                    # "Niger" is the country or Niger State: one reading agrees, one contradicts.
                    explicit.remove(stated)
                    either = " or ".join(
                        f"subdivision {_stated_subdivision(a, country) or a.region}"
                        for a in agreeing
                    )
                    homonym_ask = f"is ambiguous: {conflict} or {either}"
                    continue
            return True, f"states {conflict}", None
    for group in reading.alternatives:
        # Every reading is a place: a stated country cannot excuse them ("Dallas GA, Canada").
        found = _group_conflicts(group, (), country, code)
        if found and all(found):
            return True, "states " + " or ".join(found), None
        unsettled = _unsettled_homonym(group, found, reading, explicit, country, code)
        if unsettled is not None:
            return True, f"is ambiguous: {unsettled}", ADDRESS_PLACE_UNCLEAR
    if homonym_ask is not None and not any(
        p.kind == PLACE_CODE and _part_conflicts(p, country, code) for p in reading.places
    ):
        return True, homonym_ask, ADDRESS_PLACE_UNCLEAR
    towns = _town_keys(city)
    # W21-REV-GEO r1 C-1: the first part's "town code" is compared whatever the city, as a code is.
    first_parts = [p for p in reading.places if p.kind == PLACE_FIRST_PART]
    places = [
        p for p in reading.places if p.kind in (PLACE_CODE, PLACE_DIRECTION) or p.text not in towns
    ]
    directions = [p for p in places if p.kind == PLACE_DIRECTION]
    places = [p for p in places if p.kind not in (PLACE_DIRECTION, PLACE_FIRST_PART)]
    contradicting = [(p, found) for p in places if (found := _part_conflicts(p, country, code))]
    agreeing = [
        r
        for r in (*explicit, *(r for group in reading.alternatives for r in group))
        if _conflict(r, country, code) is None
    ] + [
        r
        for p in places
        if p.kind != PLACE_UNCLEAR and not _part_conflicts(p, country, code)
        for r in p.readings
        if _conflict(r, country, code) is None
    ]
    for place, found in contradicting:
        if place.kind == PLACE_CODE or (
            place.kind == PLACE_NAME and not _own_area_named(place, agreeing, country, code)
        ):
            return True, "states " + " or ".join(found), None
    if not any(_stated_subdivision(stated, country) is not None for stated in explicit):
        countries = {stated.country for stated in explicit}
        for possible in reading.ambiguous:
            if countries and possible.country not in countries:
                continue
            conflict = _conflict(possible, country, code)
            if conflict is not None:
                return True, f"is ambiguous: a street type or {conflict}", ADDRESS_SUFFIX_AMBIGUOUS
        for group in reading.ambiguous_alternatives:
            found = _group_conflicts(group, countries, country, code)
            if found and all(found):
                either = " or ".join(found)
                return True, f"is ambiguous: a street type or {either}", ADDRESS_SUFFIX_AMBIGUOUS
        for place in first_parts:
            # W21-REV-GEO r1 C-1: read as D1 reads the first part's ambiguous reading, since it may
            # be a business's name and its legal form ("ACME SA"): settled above, never refused.
            found = _group_conflicts(place.readings, countries, country, code)
            if found and all(found):
                either = " or ".join(found)
                return True, f"is ambiguous: a name or {either}", ADDRESS_PLACE_UNCLEAR
        for possible in reading.routes:
            # GEO-P2 (G-8): a designator's code, settled or ruled out as an ambiguous reading is.
            if countries and possible.country not in countries:
                continue
            conflict = _conflict(possible, country, code)
            if conflict is not None:
                return True, f"is ambiguous: a route or {conflict}", ADDRESS_ROUTE_AMBIGUOUS
        for group in reading.route_groups:
            found = _group_conflicts(group, countries, country, code)
            if found and all(found):
                either = " or ".join(found)
                return True, f"is ambiguous: a route or {either}", ADDRESS_ROUTE_AMBIGUOUS
        for place in directions:
            # GEO-P1: read as C-14 reads "St NE": explicit geography settles it (above).
            found = _group_conflicts(place.readings, countries, country, code)
            if found and all(found):
                either = " or ".join(found)
                return True, f"is ambiguous: a direction or {either}", ADDRESS_DIRECTION_AMBIGUOUS
    for place, found in contradicting:
        either = " or ".join(found)
        if place.kind == PLACE_NAME:
            return True, f"also names {either}", ADDRESS_NAMES_ANOTHER_PLACE
        return True, f"is ambiguous: a town's name or {either}", ADDRESS_PLACE_UNCLEAR
    return bool(explicit), None, None


def _unsettled_homonym(
    group: Sequence[MarketIdentity],
    found: list[str | None],
    reading: BusinessAddressReading,
    explicit: Sequence[MarketIdentity],
    country: str,
    code: str,
) -> str | None:
    """GEO-P3 (register G-10 N-1; review I-6): the readings of a C-8 group when its only agreeing
    reading is a Nigerian one the reader added (``homonyms``: "…, Ikeja, LA" for a Lagos location,
    whose "LA" is also Laos and Louisiana), every other reading contradicts, and nothing settles it.
    An address that states Nigeria explicitly settles it; an explicit code elsewhere that
    contradicts the location is refused first (GEO-P1's order). Such a reading never makes the
    address agree by itself: it asks (RULING-D1-C3). ``None`` otherwise."""
    added = [i for i, r in enumerate(group) if r in reading.homonyms]
    others = [i for i in range(len(group)) if i not in added]
    if not added or not others or len(found) != len(group):
        return None
    if any(found[i] is None for i in others) or all(found[i] is not None for i in added):
        return None
    if any(s.country == "NG" for s in explicit):
        return None
    if any(p.kind == PLACE_CODE and _part_conflicts(p, country, code) for p in reading.places):
        return None
    agreeing = [
        f"subdivision {_stated_subdivision(group[i], country) or group[i].region}"
        for i in added
        if found[i] is None
    ]
    return " or ".join([found[i] for i in others if found[i]] + agreeing)


def _town_keys(city: str | None) -> frozenset[str]:
    """The location's city as a part of an address is compared (folded, without a postcode): the
    whole field and its first comma part ("Washington, D.C." is "washington dc" or "washington")."""
    if city is None:
        return frozenset()
    keys = {address_part_key(city), address_part_key(city.split(",")[0])}
    return frozenset(key for key in keys if key)


def _part_conflicts(place: PartPlace, country: str, code: str) -> list[str]:
    """GEO-P1: each reading's conflict when every reading of ``place`` contradicts the location;
    empty when any reading agrees ("Georgia" for Atlanta; "CA" for Toronto)."""
    found = [_conflict(reading, country, code) for reading in place.readings]
    return [f for f in found if f is not None] if found and all(found) else []


def _own_area_named(
    place: PartPlace, agreeing: Sequence[MarketIdentity], country: str, code: str
) -> bool:
    """GEO-P1: does another part name the location's own area at the level ``place`` contradicts
    it: its country, when ``place`` names only other countries ("Kenya" beside "US-AL"); its
    subdivision, when ``place`` names another subdivision of its country ("Oklahoma" beside
    "US-TX")? A country alone ("United States") does not name a subdivision."""
    if "-" in code and any(reading.country == country for reading in place.readings):
        return any(_stated_subdivision(r, country) == code for r in agreeing)
    return any(r.country == country for r in agreeing)


def _group_conflicts(
    group: Sequence[MarketIdentity], countries: Iterable[str | None], country: str, code: str
) -> list[str | None]:
    """C-8: each reading's conflict (or ``None``), after ruling out readings in another country
    than an explicitly stated one (for an ambiguous line or part, whose street reading remains)."""
    stated = set(countries)
    return [
        _conflict(possible, country, code)
        for possible in group
        if not stated or possible.country in stated
    ]


def resolve_jurisdiction(
    location: LocationFacts | None, coverage_rules: Sequence[CoverageFacts] = ()
) -> JurisdictionResolution:
    """Resolve a location's jurisdiction (fail closed; see the module docstring)."""
    evidence: list[str] = []
    if location is None:
        return _unresolved(evidence, "no location")
    country_text = location.country
    country = country_code_of(country_text)
    if country is None:
        reason = (
            "the location names no country"
            if not (country_text or "").strip()
            else f"country {country_text!r} is not a known country"
        )
        return _unresolved(evidence, reason)
    evidence.append(f"country {country_text!r} -> {country}")

    if country in SUBDIVISION_JURISDICTIONS:
        state_text = location.state_province
        code = subdivision_code_of(country, state_text)
        if code is None:
            reason = (
                f"{country} resolves by state or province and the location names none"
                if not (state_text or "").strip()
                else f"{state_text!r} is not a state or province of {country}"
            )
            return _unresolved(evidence, reason)
        evidence.append(f"state or province {state_text!r} -> {code}")
    else:
        code = country
        evidence.append(f"{country} resolves by country")

    city = getattr(location, "city", None)
    for index, rule in enumerate(coverage_rules):
        address = getattr(rule, "business_address", None)
        states, conflict, category = _address_check(address, country, code, city)
        if conflict is not None:
            return _unresolved(
                evidence,
                f"coverage rule {index} business address {address!r} {conflict}",
                reason=category,
            )
        if states:
            evidence.append(f"coverage rule {index} business address {address!r} agrees")

    captured = getattr(location, "jurisdiction_code", None)
    if captured is not None:
        if captured != code:
            return _unresolved(
                evidence, f"captured jurisdiction {captured!r} is not the resolved {code}"
            )
        evidence.append(f"captured jurisdiction {captured} agrees")

    codes = (code, country) if code != country else (code,)
    return JurisdictionResolution(
        code=code, codes=codes, confidence=RESOLVED_CONFIDENCE, evidence=tuple(evidence)
    )


__all__ = [
    "ADDRESS_DIRECTION_AMBIGUOUS",
    "ADDRESS_ROUTE_AMBIGUOUS",
    "ADDRESS_NAMES_ANOTHER_PLACE",
    "ADDRESS_PLACE_UNCLEAR",
    "ADDRESS_SUFFIX_AMBIGUOUS",
    "CLARIFICATIONS",
    "RESOLVED_CONFIDENCE",
    "CoverageFacts",
    "JurisdictionResolution",
    "LocationFacts",
    "code_within",
    "codes_may_overlap",
    "country_code_of",
    "jurisdiction_permits",
    "resolve_jurisdiction",
    "subdivision_code_of",
]
