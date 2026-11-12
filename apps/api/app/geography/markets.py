"""Structured market identity (P6-DATA-6).

A market is compared as ``(city, region, country)`` after normalisation, never as a
substring of another market's text. ``country`` is an ISO 3166-1 alpha-2 code;
``region`` is ``"<country>-<code>"`` for a subdivision in ``places_data.SUBDIVISIONS``
("US-TX", "CA-ON", "GB-ENG") and ``"<country>:<folded name>"`` for any other region;
``city`` is the folded city name.

Matching fails closed:

* :func:`market_within` is true only when the candidate is known to lie inside the
  scope: the scope must name a country, and every component the scope names must be
  present and equal in the candidate (a city's region only where addresses
  conventionally carry one, see ``REGION_QUALIFIES_CITY``). "London, UK" is not
  within "London, ON", and a bare "London" is within neither.
* :func:`may_overlap` (used for exclusions) is true unless a component known on both
  sides differs, so an exclusion also removes a market it cannot rule out.

An observed market's ambiguous qualifier is never resolved toward the scope's
country. "Atlanta, GA" (Georgia or Gabon), "Perth, WA" (Washington or Western
Australia) and "Tbilisi, Georgia" (US state or country) are read only where place
knowledge places the city in exactly one reading: the short list of principal cities in
``places_data.KNOWN_CITIES``, plus, when an observed market is compared with a scope,
the scope's own city ("Chicago, IL" against a Chicago, Illinois scope). Anything still
ambiguous stays unqualified and therefore lies within no scope. Text a customer
configured for their own location (:func:`parse_market`) may additionally be read in
that location's country when place knowledge does not decide, and a bare name there
is qualified by it ("Ikeja" for a Nigerian location); an observed market
(:func:`parse_observed`) never is, because a bare "London" in evidence says nothing
about which London.

**Jurisdiction forms** (Project Phase 5D; W21 lane 5DW2FIX, defects D-5DW4-01/02). Both
parsers also read a part written as a jurisdiction code or as a Nigerian subdivision, the
same way on both sides of a comparison:

* a code: ISO 3166-1 alpha-2, a hyphen and an ISO 3166-2 subdivision part ("US-TX",
  "us - tx", "NG-LA", "GB-ENG"), or the two words without the hyphen when they name a listed
  subdivision ("US TX"), alone or followed by its country ("US-TX, USA"). It is the
  subdivision, never a city. A code of a country whose subdivisions are listed (here or in
  ``app.jurisdiction.subdivisions``) is always a code; an upper-case code of any other
  country is read as one too, unless the text names another country ("Al-Ain, UAE" stays
  a place name). In a rule's text a code the tables cannot place ("US-ZZ", "DE-BY") keeps
  its own key: it rules nothing out within its country (:func:`may_overlap`) and contains
  nothing (:func:`market_within`). In an observed market (evidence, a request's market, a
  business address) only a placeable code is read: a road number or hyphenated name ("US-9",
  "GA-400", "AL-AIN", "Ca-Mau") is read as words, as before. A listed code contradicted by
  the rest of the text ("US-TX, Canada") is read as its own country with the region
  "US-TX?", which no location has and no scope contains.
* a Nigerian state or the FCT named with its suffix ("Lagos State", "Ogun State, Nigeria",
  "Federal Capital Territory"), read from ``app.jurisdiction.subdivisions`` (Nigeria is
  deliberately not in ``places_data.SUBDIVISIONS``, so a bare "Lagos" stays the city).
  It is the region ``"NG:<name>"``, the key a Nigerian region already had; a Nigerian
  region named by an alias or code ("FCT", "Abuja", "LA") takes its subdivision's first
  name, so every spelling of one state is one key.
* an alpha-2 code followed by the same country ("KE, Kenya") is that country.

**Exclusion checks** (operator rulings X-1 to X-5, 2026-11-08) compare every reading of an
exclusion's text (:func:`exclusion_readings`) with every plausible reading of the observed
market (:func:`observed_readings`). An explicit, valid identifier is read as itself only
("NG-LA" is Lagos State and never overlaps a market identified as Kenya); a name with several
places ("Niger", "CA", "New York") is each of them, narrowed by a known country; a missing
country or subdivision is still matched conservatively (item 35). Inclusions and connector
clearance read their text and the observed market once (:func:`inclusion_readings`,
:func:`parse_observed`), so ambiguity admits nothing new.

``jurisdiction_forms=False`` reads text exactly as before those forms were recognised (the
"word reading"). Display and source-text helpers keep it; coverage keeps it only beside an
upper-case code of a country whose subdivisions are not listed ("AL-AIN"), which may be a
place name.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Final

from app.geography.places_data import (
    CITY_FIRST_NAMES,
    COUNTRIES,
    COUNTRY_ALIASES,
    COUNTRY_DISPLAY_NAMES,
    KNOWN_CITIES,
    REGION_QUALIFIES_CITY,
    SUBDIVISIONS,
)
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS

_TOKEN_SYNONYMS: Final = {"saint": "st", "sainte": "ste", "mount": "mt", "fort": "ft"}
#: Placeholders an f-string over a missing field leaves behind ("London, None").
_EMPTY_PARTS: Final = frozenset({"none", "null", "undefined", "n a"})
#: Suffixes dropped from a free-text region name ("Lagos State" == "Lagos").
_REGION_SUFFIXES: Final = (" state", " province", " region")
#: Suffixes that mark a name as a subdivision ("Lagos State", "Federal Capital Territory"):
#: the jurisdiction resolver's list (``app.jurisdiction.resolver``).
_SUBDIVISION_SUFFIXES: Final = (" state", " province", " territory", " region")
_HAS_DIGIT = re.compile(r"\d")
#: A part written as a jurisdiction code: ISO 3166-1 alpha-2, "-", ISO 3166-2 part.
_CODE_FORM: Final = re.compile(r"([A-Za-z]{2})\s*-\s*([A-Za-z0-9]{1,3})")


def fold(text: str | None) -> str:
    """Case-, accent- and punctuation-insensitive form of a place name."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.casefold().replace("&", " and ")
    lowered = re.sub(r"[.'’`´]", "", lowered)
    lowered = re.sub(r"[\W_]+", " ", lowered)
    return " ".join(_TOKEN_SYNONYMS.get(tok, tok) for tok in lowered.split())


def _strip_postcode(folded: str) -> str:
    """Drop trailing digit-bearing words ("tx 75201" -> "tx"), keeping one word."""
    words = folded.split()
    while len(words) > 1 and _HAS_DIGIT.search(words[-1]):
        words.pop()
    return " ".join(words)


def address_part_key(text: str | None) -> str:
    """One part of an address as the business-address reader compares it: folded, without a
    trailing postcode ("Washington DC 20001" -> "washington dc")."""
    return _strip_postcode(fold(text))


# --- indexes built once from the reference tables ---------------------------------

_COUNTRY_BY_NAME: dict[str, str] = {}
for _code, _name in COUNTRIES.items():
    _COUNTRY_BY_NAME.setdefault(fold(_name), _code)
for _code, _name in COUNTRY_DISPLAY_NAMES.items():
    _COUNTRY_BY_NAME[fold(_name)] = _code
for _alias, _code in COUNTRY_ALIASES.items():
    _COUNTRY_BY_NAME[_alias] = _code

#: folded subdivision name or abbreviation -> [(country, region key)]
_REGION_INDEX: dict[str, list[tuple[str, str]]] = {}
for _country, _divisions in SUBDIVISIONS.items():
    for _code, _names in _divisions.items():
        _key = f"{_country}-{_code}"
        for _token in (_code.lower(), *_names):
            entries = _REGION_INDEX.setdefault(_token, [])
            if (_country, _key) not in entries:
                entries.append((_country, _key))


#: region key or country code -> folded principal cities (KNOWN_CITIES)
_KNOWN_CITIES: dict[str, frozenset[str]] = {
    _key: frozenset(fold(_name) for _name in _names) for _key, _names in KNOWN_CITIES.items()
}

#: Region keys of the subdivisions in ``SUBDIVISIONS`` ("US-TX"). Any other key with a
#: hyphen is a code the tables cannot place ("US-ZZ"); a key with ":" is a region by name.
_CODED_REGIONS: Final = frozenset(
    f"{_country}-{_code}" for _country, _divisions in SUBDIVISIONS.items() for _code in _divisions
)
#: Countries whose first-level subdivision codes are listed, here or in the jurisdiction
#: module: a code of one of these is checked against the list ("strict").
_CODED_COUNTRIES: Final = frozenset(SUBDIVISIONS) | frozenset(SUBDIVISION_JURISDICTIONS)


def _subdivision_tokens(*, codes: bool) -> dict[str, dict[str, str]]:
    """country -> {folded name, alias (and code) -> the subdivision's first name}, for the
    jurisdiction module's countries that ``SUBDIVISIONS`` deliberately leaves out (Nigeria).
    A token naming two subdivisions of one country is left out (it names neither)."""
    index: dict[str, dict[str, str]] = {}
    for country, divisions in SUBDIVISION_JURISDICTIONS.items():
        if country in SUBDIVISIONS:
            continue
        seen: dict[str, set[str]] = {}
        for code, names in divisions.items():
            for token in ((code.lower(),) if codes else ()) + tuple(names):
                seen.setdefault(fold(token), set()).add(fold(names[0]))
        index[country] = {token: next(iter(n)) for token, n in seen.items() if len(n) == 1}
    return index


#: Nigeria's subdivisions by name, alias or code ("lagos", "fct", "la") -> first name.
_JURISDICTION_REGION_TOKENS: Final = _subdivision_tokens(codes=True)
#: The same by name or alias only: what may precede a suffix ("Lagos State").
_JURISDICTION_REGION_NAMES: Final = _subdivision_tokens(codes=False)


def country_name(code: str | None) -> str | None:
    """English display name for an ISO alpha-2 code."""
    if not code:
        return None
    code = code.upper()
    return COUNTRY_DISPLAY_NAMES.get(code) or COUNTRIES.get(code)


def _country_code(folded: str) -> str | None:
    if len(folded) == 2 and folded.upper() in COUNTRIES:
        return folded.upper()
    return _COUNTRY_BY_NAME.get(folded)


def normalize_country(text: str | None) -> str | None:
    """ISO alpha-2 code for a country name, alias or code; ``None`` when unknown.

    Reads ``text`` as a country only (a value from a "country" field): "CA" is
    Canada and "Georgia" is the country here.
    """
    return _country_code(_strip_postcode(fold(text)))


def _region_entries(folded: str) -> list[tuple[str, str]]:
    entries = _REGION_INDEX.get(folded)
    if entries:
        return entries
    for suffix in _REGION_SUFFIXES:
        if folded.endswith(suffix) and folded[: -len(suffix)] in _REGION_INDEX:
            return _REGION_INDEX[folded[: -len(suffix)]]
    return []


def _free_region(folded: str, country: str, *, forms: bool = True) -> str:
    for suffix in _REGION_SUFFIXES:
        if folded.endswith(suffix) and len(folded) > len(suffix):
            folded = folded[: -len(suffix)]
            break
    if forms:
        # One key per Nigerian subdivision, whatever its spelling ("FCT", "Abuja" and
        # "Federal Capital Territory"; "LA" and "Lagos"). A function of the name above, so
        # names that shared a key before still share one.
        tokens = _JURISDICTION_REGION_TOKENS.get(country, {})
        name = tokens.get(folded)
        if name is None and folded.endswith(" territory"):
            name = tokens.get(folded[: -len(" territory")])
        folded = name or folded
    return f"{country}:{folded}"


def normalize_region(
    text: str | None, country: str | None, *, jurisdiction_forms: bool = True
) -> tuple[str | None, str | None]:
    """``(region key, country)`` for a region named within ``country``.

    A known subdivision of ``country`` (by name or abbreviation) gives its code;
    with no country, a subdivision name that exists in exactly one country gives that
    country too. Any other region of a known country is kept by its folded name, a
    Nigerian subdivision by its first name (``jurisdiction_forms=False``: as written).
    """
    folded = _strip_postcode(fold(text))
    if not folded or folded in _EMPTY_PARTS:
        return None, country
    entries = _region_entries(folded)
    if country:
        for entry_country, key in entries:
            if entry_country == country:
                return key, country
        return _free_region(folded, country, forms=jurisdiction_forms), country
    if len(entries) == 1:
        return entries[0][1], entries[0][0]
    return None, None


@dataclass(frozen=True)
class MarketIdentity:
    """A normalised market. Equality ignores ``label`` (the text it came from)."""

    city: str | None = None
    region: str | None = None
    country: str | None = None
    label: str = field(default="", compare=False)

    @property
    def is_qualified(self) -> bool:
        """True when the market names its country; only then can it be a scope."""
        return self.country is not None


#: An area reading of a token: ``(country, region key or None)``.
_Area = tuple[str, str | None]

#: W21-REV-GEO r1 C-2: each two-letter code of a country whose subdivisions only the jurisdiction
#: module lists (Nigeria) -> that subdivision's area ("la" -> Lagos State, "og" -> Ogun State). A
#: part that is only such a code ("ACME, LA, Ikeja", "Abeokuta, OG, Nigeria") has this reading
#: beside any other ("LA" is also Laos and Louisiana), so it is never read as naming nothing.
_JURISDICTION_CODE_AREAS: Final[dict[str, _Area]] = {
    code.lower(): (country, f"{country}:{fold(names[0])}")
    for country, divisions in SUBDIVISION_JURISDICTIONS.items()
    if country not in SUBDIVISIONS
    for code, names in divisions.items()
    if len(code) == 2
}


def _area_candidates(folded: str) -> list[_Area]:
    candidates: list[_Area] = []
    country = _country_code(folded)
    if country:
        candidates.append((country, None))
    for entry_country, key in _region_entries(folded):
        if (entry_country, key) not in candidates:
            candidates.append((entry_country, key))
    return candidates


def _city_known_in(city: str | None, area: _Area, known: MarketIdentity | None) -> bool:
    """Is ``city`` known to exist in the reading ``area``? From ``KNOWN_CITIES``, or
    from ``known`` (a place the caller knows exists, e.g. the scope being matched)."""
    if not city:
        return False
    country, region = area
    if known is not None and known.city == city and known.country == country:
        if region is None or known.region == region:
            return True
    if region is not None:
        return city in _KNOWN_CITIES.get(region, ())
    return city in _KNOWN_CITIES.get(country, ()) or any(
        city in cities for key, cities in _KNOWN_CITIES.items() if key.startswith(f"{country}-")
    )


def _resolve(
    candidates: list[_Area],
    city_for: dict[_Area, str | None],
    *,
    country: str | None,
    qualify: bool,
    known: MarketIdentity | None,
) -> _Area | None:
    """The single reading of a qualifier, or ``None`` (absent or still ambiguous).

    One reading: that one. Several: the reading in which place knowledge knows the
    city, when exactly one does. Otherwise, only for text configured for a location
    (``qualify``), the reading in that location's ``country``. Never the scope's
    country for an observed market: an observation that remains ambiguous matches
    nothing.
    """
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    knowing = [c for c in candidates if _city_known_in(city_for.get(c), c, known)]
    if len(knowing) == 1:
        return knowing[0]
    if not knowing and qualify and country:
        local = [c for c in candidates if c[0] == country]
        if len(local) == 1:
            return local[0]
    return None


def _split_trailing_code(folded: str) -> tuple[str, str] | None:
    """Split "dallas tx" / "london uk" into (place, code) when no comma separates
    them. Only a final 2-3 letter word that reads as a country or region code is
    split off; full names are not, so "Port of Spain" stays one place."""
    words = folded.split()
    if len(words) >= 2 and 2 <= len(words[-1]) <= 3 and _area_candidates(words[-1]):
        return " ".join(words[:-1]), words[-1]
    return None


#: Street-type abbreviations that can end a street line ("123 Main St", "3 Kings Ct"): the
#: common USPS Publication 28 suffixes and the usual Canadian, Australian, UK, Nigerian and
#: Kenyan forms. St, Pl, Ct, Cl and Gr are also codes (Sao Tome, Poland, Connecticut, Chile,
#: Greece), which a street line's last word must not be read as (defect D1).
STREET_SUFFIXES: Final = frozenset(
    "st ave av rd dr ln blvd bvd ct crt pl cl gr cres cir ter terr tce pde hwy pkwy sq trl esp"
    " cct gdns".split()
)
#: Street types, abbreviated or in full. One among a street line's street words, after the
#: first, means the street ended earlier: "123 Main St Hartford CT" names a town and a state.
_STREET_TYPES: Final = STREET_SUFFIXES | frozenset(
    "street avenue road drive lane boulevard court place close grove crescent circle terrace"
    " parade highway parkway square trail way esplanade circuit gardens".split()
)
#: A post-directional may follow the suffix ("1200 Pennsylvania Ave SE").
_POST_DIRECTIONALS: Final = frozenset("n s e w ne nw se sw".split())
#: Words that may stand before the house number ("No 3 Ajose Adeogun St", "Plot 1 Adeola Odeku St",
#: "Flat 2 10 Downing St", "Level 3 1 Martin Pl"). Any other first word followed by a number is a
#: street or a box ("Marszalkowska 1 Warszawa PL", "Box 12 Hartford CT"), not a street line.
_UNIT_LABELS: Final = frozenset(
    "no nr num number plot flat unit suite ste apt apartment shop house block blk lot bldg"
    " building office room rm level lvl floor fl".split()
)
#: A subdivision code as folded text ("US-CT" -> "us ct"): an explicit code, never a street's last
#: two words ("1500 Broadway US-CT").
_FOLDED_SUBDIVISION_CODES: Final = frozenset(
    f"{country} {code}".lower() for country, codes in SUBDIVISIONS.items() for code in codes
)


#: An ordinal street name ("4th", "1st", "22nd", "3rd"): a street-name word, not a number.
_ORDINAL: Final = re.compile(r"\d+(?:st|nd|rd|th)", re.IGNORECASE)


def _is_name(word: str) -> bool:
    """A street-name word: one without a digit, or an ordinal ("4th")."""
    return not _HAS_DIGIT.search(word) or _ORDINAL.fullmatch(word) is not None


def _is_street_line(folded: str) -> bool:
    """Is ``folded`` (one comma-free part, folded, trailing postcode dropped) a street line?

    A street line is a house number as the first word, or as the second after a unit label
    (:data:`_UNIT_LABELS`: "No", "Plot", "Flat", "Unit", "Suite", "Level", ...), then street
    words, then a :data:`STREET_SUFFIXES` abbreviation, and optionally a post-directional:
    "123 main st", "221b baker st", "no 3 ajose adeogun st", "1200 pennsylvania ave se". The
    street words must include a name; no street type may follow the first street word without a
    digit ("1 st paul st" is a street; "123 main st hartford ct" is not); and the last two words
    must not be a subdivision code ("1500 broadway us ct" names Connecticut). A number after
    any other first word is not a house number ("marszalkowska 1 warszawa pl", "box 12 hartford
    ct").
    """
    words = folded.split()
    if words and words[-1] in _POST_DIRECTIONALS:
        words = words[:-1]
    if len(words) < 3 or words[-1] not in STREET_SUFFIXES:
        return False
    number = next((i for i, word in enumerate(words[:2]) if _HAS_DIGIT.search(word)), None)
    if number is None or (number == 1 and words[0] not in _UNIT_LABELS):
        return False
    if " ".join(words[-2:]) in _FOLDED_SUBDIVISION_CODES:
        return False
    street = words[number + 1 : -1]
    named = [i for i, word in enumerate(street) if _is_name(word)]
    if not named:
        return False
    return not any(word in _STREET_TYPES for word in street[named[0] + 1 :])


#: How a business address that is a street line reads (D1, operator ruling of 2026-11-09 on
#: W21-REV-D1STREET r1 C-3): clearly a street, so its last word is not a code; or ambiguous,
#: because its last words may also be a town and a state or country code.
STREET_LINE_CLEAR: Final = "clear"
STREET_LINE_AMBIGUOUS: Final = "ambiguous"


def _written_words(text: str) -> list[str]:
    """``text``'s words as written (case kept), split as :func:`fold` splits them, without the
    trailing digit-bearing words :func:`_strip_postcode` drops."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = re.sub(r"[.'’`´]", "", stripped.replace("&", " and "))
    words = re.sub(r"[\W_]+", " ", stripped).split()
    while len(words) > 1 and _HAS_DIGIT.search(words[-1]):
        words.pop()
    return words


#: What separates the parts of a business address (W21-REV-D1STREET r2 C-10): a comma, a semicolon,
#: a slash or a line break ("123 MAIN ST / DALLAS"). A unit written "5/12 Main St" reads as the
#: parts "5" and "12 Main St", which state what the whole line states.
_PART_SEPARATORS: Final = re.compile(r"[,;/\r\n]")


def without_town_parts(text: str | None, towns: frozenset[str]) -> str | None:
    """GEO-P3 (register G-2): ``text`` without its parts that are the location's town (``towns``:
    folded keys of the location's city field) but that the reader would take for an area
    ("Washington" is also Washington State, "Minna" a region of the country Niger), joined by
    commas; ``None`` when no such part is left out or nothing that names anything is left. A listed
    code or a suffixed or prefixed subdivision is never left out (a city field "US-AL" must not
    hide the code "US-AL"), and a part that names no area is kept. The location's city is
    structured input the location already holds; this is not a city table."""
    if not text or not towns:
        return None
    parts = _PART_SEPARATORS.split(_other_separators("\n".join(text.splitlines())))

    def explicit(raw: str) -> bool:
        folded = _strip_postcode(fold(raw))
        return (
            bool(_listed_codes(raw, folded))
            or _named_subdivision(folded) is not None
            or bool(_AREA_PREFIX.match(folded))
        )

    def names_an_area(raw: str) -> bool:
        folded = _strip_postcode(fold(raw))
        return bool(_area_candidates(folded) or _homonym_areas(folded))

    kept = [
        raw
        for raw in parts
        if address_part_key(raw) not in towns or explicit(raw) or not names_an_area(raw)
    ]
    if len(kept) == len(parts) or not any(address_part_key(raw) for raw in kept):
        return None
    return ", ".join(raw.strip() for raw in kept if raw.strip())


def nigerian_readings_of_stated_code(
    stated: MarketIdentity, text: str | None
) -> list[MarketIdentity]:
    """GEO-P3 (review I-6): when ``stated`` is D1's explicit reading of the code that ends a later
    part, or a one-part address ("1 Main St, Springfield AB": Alberta), the Nigerian state that code
    also names (Abia State), for each such part; ``[]`` otherwise. A code form or road ("US-LA",
    "LA-1") keeps its reading."""
    if not text or stated.country is None:
        return []
    parts = _written_parts(_other_separators("\n".join(text.splitlines())))
    found = []
    for part in parts[1:] if len(parts) > 1 else parts:
        words = _strip_postcode(fold(part)).split()
        if len(words) < 2 or _code_form_part(part):
            continue
        coded = _JURISDICTION_CODE_AREAS.get(words[-1])
        if coded is not None and (stated.country, stated.region) in _area_candidates(words[-1]):
            found.append(MarketIdentity(region=coded[1], country=coded[0], label=part.strip()))
    return found


def parts_naming_an_area(text: str | None) -> int:
    """GEO-P3 (register G-3): how many parts of a business address name an area: a listed code, a
    suffixed or prefixed subdivision ("Adamawa State", "State of Texas"), or a name or code that
    reads as an area or has a listed homonym."""
    if not text:
        return 0
    count = 0
    for raw in _written_parts(_other_separators("\n".join(text.splitlines()))):
        folded = _strip_postcode(fold(raw))
        prefixed = _AREA_PREFIX.sub("", folded) if _AREA_PREFIX.match(folded) else None
        if (
            _listed_codes(raw, folded)
            or _named_subdivision(folded) is not None
            or (prefixed and _area_candidates(prefixed))
            or _area_candidates(folded)
            or _homonym_areas(folded)
        ):
            count += 1
    return count


def last_written_part(text: str | None) -> str | None:
    """The last part of a business address that names something, as the reader splits it."""
    if not text:
        return None
    parts = _written_parts(_other_separators("\n".join(text.splitlines())))
    return parts[-1] if parts else None


def own_reading_of_homonym(stated: MarketIdentity, last: str | None) -> list[_Area]:
    """The listed homonyms of the last part's name ("Niger": Niger State, X-5) when ``stated`` is
    that name's own reading (its country a reading of the name; its region, if any, naming no
    listed area); ``[]`` otherwise."""
    if last is None or stated.country is None:
        return []
    name = address_part_key(last)
    if stated.country not in {country for country, _ in _area_candidates(name)}:
        return []
    if stated.region is not None:
        region = stated.region.split(":", 1)[-1]
        if _area_candidates(region) or region in _JURISDICTION_CODE_AREAS:
            return []
    return _homonym_areas(name)


def _written_parts(text: str) -> list[str]:
    """The parts of ``text`` as written that name something (the parts :func:`_clean_parts`
    keeps), separated by :data:`_PART_SEPARATORS` ("123 MAIN ST\nDALLAS" reads as "123 MAIN ST,
    DALLAS")."""
    return [
        raw
        for raw in _PART_SEPARATORS.split(text)
        if (part := _strip_postcode(fold(raw))) and part not in _EMPTY_PARTS
    ]


def _street_kind(part: str) -> str | None:
    """:func:`street_line_kind` for one comma-free part as written."""
    words = _strip_postcode(fold(part)).split()
    if not words or not _is_street_line(" ".join(words)):
        return None
    written = _written_words(part)
    if [fold(word) for word in written] != words:
        return STREET_LINE_AMBIGUOUS
    if words[-1] in _POST_DIRECTIONALS:
        words, written = words[:-1], written[:-1]
    number = next(i for i, word in enumerate(words[:2]) if _HAS_DIGIT.search(word))
    names = [word for word in words[number + 1 : -1] if _is_name(word)]
    if len(names) > 1 and names[0] in _POST_DIRECTIONALS:
        names = names[1:]
    letters = "".join(word for word in written if not _HAS_DIGIT.search(word))
    as_street_type = (
        not written[-1].isupper()
        and any(ch.isupper() for ch in letters)
        and any(ch.islower() for ch in letters)
    )
    return STREET_LINE_CLEAR if len(names) == 1 and as_street_type else STREET_LINE_AMBIGUOUS


def street_line_kind(text: str | None) -> str | None:
    """How a business address that is a street line (:func:`_is_street_line`) reads; ``None``
    when it is not one (or has a comma).

    :data:`STREET_LINE_CLEAR` when only the street can be meant: exactly one street-name word (a
    leading pre-directional such as "N" is not counted), and the suffix written as a street type
    ("St", "Ct", not "ST" or "CT") in a line that mixes upper and lower case: "123 Main St",
    "3 Kings Ct", "100 N Main St", "1200 Pennsylvania Ave SE". Otherwise
    :data:`STREET_LINE_AMBIGUOUS`: the last words may be a town and a state or country code
    ("1500 Broadway Hartford CT", "No 3 Ajose Adeogun St", "1500 Broadway CT", "123 MAIN ST").
    """
    if text is None:
        return None
    parts = _written_parts(text)
    return _street_kind(parts[0]) if len(parts) == 1 else None


#: How a part of a business address that names a place by itself is read, wherever it stands
#: (GEO-P1; geography register G-1, G-4, G-5 and G-9). See :class:`PartPlace`.
PLACE_CODE: Final = "code"
PLACE_NAME: Final = "name"
PLACE_UNCLEAR: Final = "unclear"
PLACE_DIRECTION: Final = "direction"
PLACE_FIRST_PART: Final = "first_part"


@dataclass(frozen=True)
class PartPlace:
    """A part of a business address that names a place by itself, in any position (GEO-P1).

    ``kind`` says how explicit it is:

    * :data:`PLACE_CODE`: a listed subdivision code, anywhere in the part ("US-AL", "us al",
      "Hartford US-CT", "US-CT Hartford", "Lekki NG-LA"), or a subdivision named with its suffix or
      prefix ("Ogun State", "Federal Capital Territory", "State of Alabama"; a prefixed Nigerian
      code has its Nigerian reading too, "State of PL" is Poland or Plateau State). No town or
      business is written so, so it is explicit wherever it stands, the first part included.
    * :data:`PLACE_NAME`: a country's or a state's or province's name ("Nigeria", "Texas",
      "Kenya", "Georgia"), or a code of two or three letters written in capitals in an address
      that mixes capitals and small letters ("ACME, NG, Lagos"). It is explicit, as the reader
      takes such a name at the end of an address, but a town or a business may bear it
      ("Lebanon", "Jordan").
    * :data:`PLACE_UNCLEAR`: a word as likely a town's name as a place: a code of two or three
      letters in an address written in one case ("ACME, NG, LAGOS"), or written otherwise than in
      capitals in an address that mixes cases ("Dallas, Tx, 1 Main St"; "Bo, Southern Province",
      W21-REV-GEO r1 C-3), a Nigerian state's bare name, which is also a town's ("Adamawa",
      "Kano"; X-5), or a city-first name ("Washington"; X-4).
    * :data:`PLACE_DIRECTION`: a part that is only a direction ("ACME, NE, Omaha"), which may be a
      street's direction or a code, as "St NE" may (C-14, C-15).
    * :data:`PLACE_FIRST_PART` (W21-REV-GEO r1 C-1): the first part of an address of two or more
      parts read as a town and a code ("Dallas TX, 1 Main St"; "Springfield AN; 1 Main St"), as
      D1 reads a later part. It may as well be a business's name and its legal form ("ACME SA",
      "ACME AB", "Jane Doe MD"), so it is an ambiguous reading, never an explicit one.

    ``text``: the part, folded, without a trailing postcode. ``readings``: every area it may name,
    two or more for a name of two areas ("Georgia": the country or US-GA; "Niger"; a Nigerian
    code: "LA" is Laos, Louisiana or Lagos State).
    """

    kind: str
    text: str
    readings: tuple[MarketIdentity, ...]


@dataclass(frozen=True)
class BusinessAddressReading:
    """What a business address states (see :func:`read_business_address`).

    ``stated``: the whole address read as :func:`parse_observed` reads it, except that a
    comma-free street line states nothing and a postcode alone in a later part is left out.
    ``parts``: what later parts ending in a code state on their own ("..., Dallas TX").
    ``ambiguous``: the code readings of ambiguous street lines and parts ("1500 Broadway
    Hartford CT", "..., Hartford CT"), which a contradicting location may not ignore.
    ``places``: every part that names a place by itself, wherever it stands (GEO-P1,
    :func:`_part_places`); the resolver compares each with the location.
    """

    stated: MarketIdentity | None
    parts: tuple[MarketIdentity, ...] = ()
    ambiguous: tuple[MarketIdentity, ...] = ()
    #: C-8 (F6): the readings of a qualifier that names two or more areas and that place knowledge
    #: leaves undecided ("Dallas, GA": Georgia or Gabon), one group per qualifier; explicit text
    #: (``alternatives``) or an ambiguous street line or part (``ambiguous_alternatives``).
    alternatives: tuple[tuple[MarketIdentity, ...], ...] = ()
    ambiguous_alternatives: tuple[tuple[MarketIdentity, ...], ...] = ()
    places: tuple[PartPlace, ...] = ()
    #: GEO-P2 (register G-8, FU-H): the code readings of a route designator written as the whole
    #: address ("12000 FM 1960", "1 SR 520"), which may be a road and not a place, as C-16 reads one
    #: in a later part; ambiguous, with their own clarification. (C-16's later-part readings stay
    #: in ``ambiguous``, with D1's reason.)
    routes: tuple[MarketIdentity, ...] = ()
    route_groups: tuple[tuple[MarketIdentity, ...], ...] = ()
    #: GEO-P3 (register G-10 N-1; review I-6): the Nigerian readings the reader adds to a group of a
    #: code that is also a Nigerian state's ("LA": Laos, Louisiana or Lagos State). Such a reading
    #: never makes an address agree by itself: when it is the group's only agreeing reading and
    #: nothing settles it, the address asks (RULING-D1-C3).
    homonyms: tuple[MarketIdentity, ...] = ()


def _alternatives(text: str) -> tuple[MarketIdentity, ...]:
    """Each area reading of ``text``'s qualifier when :func:`parse_observed` places none because
    it names two or more areas that place knowledge does not decide; empty otherwise.

    A listed subdivision code keeps only the reading its country prefix names: "US-CA" is
    California, never Canada, and "US-AL" Alabama, never Albania. An unlisted code form has no
    readings ("NG-LA", "Lekki NG-LA" and a bare "NG LA" are not Laos or Louisiana; C-9).
    """
    observed = parse_observed(text)
    if observed is None or observed.country is not None or _unlisted_code_form(text):
        return ()
    spaced = _spaced_code_form(text)
    if spaced is not None:
        # "St NE" alone may be a street type and a direction: its part's group is ambiguous.
        return () if _street_and_direction(spaced) else _spaced_group(spaced, text)
    parts = [folded for folded, _ in _clean_parts(text)]  # 816fc941: (folded, as written)
    last = whole = parts[-1]
    if len(parts) == 1 and last not in CITY_FIRST_NAMES and not _area_candidates(last):
        split = _split_trailing_code(last)
        if split is None:
            return ()
        last = split[1]
    if last in CITY_FIRST_NAMES:
        return ()
    areas = _area_candidates(last)
    listed = whole.split()[-2:]
    if " ".join(listed) in _FOLDED_SUBDIVISION_CODES:
        areas = [(c, r) for c, r in areas if c == listed[0].upper()]
    elif len(areas) < 2:
        return ()
    return tuple(MarketIdentity(region=r, country=c, label=text) for c, r in areas)


def _groups(*texts: str) -> tuple[tuple[MarketIdentity, ...], ...]:
    return tuple(g for g in (_alternatives(t) for t in texts) if g)


#: GEO-P3 (register G-10, review W21-REV-GEO r1 I-2): separators a business address may use that the
#: reader did not split on: a pipe, a tab, a bullet, a middle dot, the full-width and ideographic
#: commas, the Arabic semicolon, and a dash with a space on each side. Each is a line break.
_OTHER_SEPARATORS: Final = re.compile(
    r"\s*[|\t\u2022\u00b7\uff0c\u3001\u061b]\s*|\s+[-\u2013\u2014]\s+"
)
#: A country's two-letter code, a dash with spaces and one to three letters ("NG - LA"): a code
#: form written with spaces, which stays one ("NG-LA") and is not split at its dash.
_SPACED_DASH_CODE: Final = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{2})\s+[-\u2013\u2014]\s+([A-Za-z]{1,3})(?![A-Za-z0-9])"
)


def _other_separators(text: str) -> str:
    """GEO-P3 (G-10, I-2): ``text`` with :data:`_OTHER_SEPARATORS` read as line breaks, except the
    dash of a code form written with spaces ("NG - LA" stays "NG-LA"; "Montgomery AL - 1 Main St"
    is two parts). Before this, "Alabama | Montgomery | 1 Main St" was one part that named nothing,
    and a line merged across the separators that ended in "St" was refused as Sao Tome."""
    text = _SPACED_DASH_CODE.sub(
        lambda m: f"{m[1]}-{m[2]}" if m[1].upper() in COUNTRIES else m[0], text
    )
    return _OTHER_SEPARATORS.sub("\n", text)


def read_business_address(text: str | None) -> BusinessAddressReading:
    """Read a business's own address (a coverage rule's ``business_address``).

    Parts are separated by commas, semicolons, slashes or line breaks (C-10;
    :data:`_PART_SEPARATORS`); every line break that :meth:`str.splitlines` knows counts as one
    (W21-REV-GEO r1 C-4: also VT, FF, NEL, U+2028 and U+2029). An address of one part that
    is an ambiguous street line has an ambiguous reading. In an address of several parts, the
    whole text is read as before (a postcode alone in a later part is left out, so "...,
    Hartford, CT, 06103, USA" reads Connecticut), and each part after the first is also read on
    its own when it ends in a code:

    * a street line: a clear one states nothing, an ambiguous one is ambiguous
      ("Suite 5, 1500 Broadway CT");
    * a listed subdivision code, alone or at its end ("US-TX", "Hartford US-CT"), or a town and a
      code that is not a street-type abbreviation ("Dallas TX", "Sydney NSW"): states that place;
    * an unlisted code form ("DE-BY", "NG-PL"), alone or after a town and before any postcode
      ("Munich DE-BY", "Andheri IN-MH 400053"): nothing, as on main (C-9);
    * a town and a code that is also a street-type abbreviation ("Hartford CT", "Warszawa PL",
      "Kings Ct"): ambiguous, since it may be a street.

    The first part (W21-REV-D1STREET C-6 and FU-R1): its ambiguous reading counts too, whatever the
    later parts state ("1500 Broadway Hartford CT, USA", "123 MAIN ST, DALLAS", "KHALIFA ST,
    AL-AIN"); only its ambiguous reading, never an explicit one. When every later part is only a
    postcode or a unit ("Suite 200", "06103"; a label that is also a code, "FL-12", is not a unit),
    the address is read as its first part alone, as a comma-free address ("1500 Broadway CT, Suite
    200").

    A qualifier that names two or more areas that place knowledge leaves undecided ("Dallas, GA":
    Georgia or Gabon; "1 SPRING HILL ST NE": Nebraska or Niger) keeps its readings as a group,
    never as nothing (C-8): ``alternatives`` for explicit text, ``ambiguous_alternatives`` for an
    ambiguous street line or part. A listed subdivision code keeps only the reading its country
    prefix names ("US-CA" is California). A street type before a post-directional with no house
    number ("St NE" alone, C-14; "5th St NE", R17) is an ambiguous part, and an ordinal ("4th") is a
    street name (C-13).

    Every part that names a place by itself is also read wherever it stands, first, middle or last,
    at any distance from the end and whatever the separator (GEO-P1, :func:`_part_places`):
    ``places``. The first part's explicit reading ("Dallas TX, 1 Main St") is one of them, as an
    ambiguous reading (W21-REV-GEO r1 C-1: :data:`PLACE_FIRST_PART`).

    GEO-P2 (register G-6 and G-8): a floor after the street ("123 Main St Fl 3", "..., Fl 3") is a
    floor and never Florida (:func:`_without_floors`, :data:`_FLOOR`); an address that is only a
    route designator ("1 SR 520") has its code reading in ``routes``, and one that is only a street
    with no house number written as a street ("Main St", "NE 45th St") in ``ambiguous``
    (:func:`_numberless_street`); a code inside a route's name ("4400 US Hwy 75") is a part place;
    a last part that is only a direction after a street line ("1200 Pennsylvania Ave, SE") is a part
    place that is only a direction, never the qualifier's code (review W21-REV-GEO r1 I-3,
    :func:`_trailing_direction`).

    GEO-P3 (register G-10, review I-2): a pipe, a tab, a bullet, a middle dot, the full-width and
    ideographic commas, the Arabic semicolon and a spaced dash separate parts too
    (:func:`_other_separators`).
    """
    if text is None:
        return BusinessAddressReading(None)
    text = "\n".join(text.splitlines())  # W21-REV-GEO r1 C-4: every line break is one
    text = _other_separators(text)  # GEO-P3 (register G-10, review I-2)
    text = _without_floors(text)
    reading = _read_business_address(text)
    return BusinessAddressReading(
        reading.stated,
        reading.parts,
        reading.ambiguous,
        reading.alternatives,
        reading.ambiguous_alternatives,
        _part_places(text),
        reading.routes,
        reading.route_groups,
        reading.homonyms,
    )


#: GEO-P2 (register G-6, FU-B): a floor ("Fl 3", "FL 12", "Flr 2", "Floor 3"). A road written with a
#: hyphen ("FL-12") and a state with its ZIP code ("FL 32801", five digits) are not floors.
_FLOOR: Final = re.compile(r"(?:fl|flr|floor)\.?\s*#?\s*\d{1,3}[A-Za-z]?", re.IGNORECASE)
#: A floor at the end of a street line ("123 Main St Fl 3"), which leaves the street line.
_FLOOR_AFTER_STREET: Final = re.compile(
    r"(\b(?:" + "|".join(sorted(STREET_SUFFIXES)) + r")\.?)[ \t]+" + _FLOOR.pattern + r"\s*$",
    re.IGNORECASE,
)


def _without_floors(text: str) -> str:
    """GEO-P2 (G-6): each part with a floor after its street type left out ("123 Main St Fl 3" reads
    as "123 Main St"); separators are kept as written. A floor as its own part is a unit
    (:func:`_unit_or_postcode`)."""
    pieces = re.split(r"([,;/\r\n])", text)
    return "".join(
        _FLOOR_AFTER_STREET.sub(r"\1", piece) if index % 2 == 0 else piece
        for index, piece in enumerate(pieces)
    )


def _numberless_street(text: str) -> bool:
    """GEO-P2 (G-8, FU-H): a street name and a street type with no house number, written as a
    street: the type as a street type ("St", not "ST") in an address that mixes cases ("Main St",
    "High St", "NE 45th St"), or any such line in an address written in one case ("MAIN ST",
    "HARTFORD CT"), where case cannot tell a street type from a code, as D1 reads "123 MAIN ST". Its
    type may also be a code ("ST" is Sao Tome), so the reading is ambiguous. A town and a code
    written as a code in a mixed-case address ("Hartford CT") stays explicit, as on main."""
    written = _written_words(text)
    words = [fold(word) for word in written]
    if len(words) < 2 or words[-1] not in STREET_SUFFIXES:
        return False
    if any(_HAS_DIGIT.search(word) and not _ORDINAL.fullmatch(word) for word in words):
        return False  # a house number: a street line (D1)
    if " ".join(words[-2:]) in _FOLDED_SUBDIVISION_CODES:
        return False  # "US-CT", "US CT": a listed code, as D1's street lines exclude it
    letters = "".join(word for word in written if not _HAS_DIGIT.search(word))
    mixed = any(ch.isupper() for ch in letters) and any(ch.islower() for ch in letters)
    # In one case nothing tells "MAIN ST" from "HARTFORD CT", as D1 reads "123 MAIN ST": ambiguous.
    return not mixed or not written[-1].isupper()


def _trailing_direction(raw_parts: list[str], index: int) -> bool:
    """GEO-P2 (review W21-REV-GEO r1 I-3, register G-8): is part ``index`` only a direction ("SE")
    after a street line, with only units or postcodes between them and no named part after it
    ("1200 Pennsylvania Ave, SE"; "1200 Pennsylvania Ave, Suite 5, SE, 20003")? It may be the
    street's direction, as "Ave SE" is (C-15), or a code, so the qualifier rules do not read it as
    Sweden: it is a part place that is only a direction (:data:`PLACE_DIRECTION`), which explicit
    geography settles and which otherwise asks. After a town it stays the qualifier's code
    ("Washington, SE")."""
    if index == 0 or _strip_postcode(fold(raw_parts[index])) not in _DIRECTION_PARTS:
        return False
    before = [part for part in raw_parts[:index] if not _unit_or_postcode(part)]
    return (
        bool(before)
        and _street_kind(before[-1]) is not None
        and not any(_is_named(address_part_key(part)) for part in raw_parts[index + 1 :])
    )


def _read_business_address(text: str) -> BusinessAddressReading:
    """:func:`read_business_address` as D1 and the exclusion fix read it, without ``places``."""
    raw_parts = _written_parts(text)
    if len(raw_parts) >= 2 and all(_unit_or_postcode(part) for part in raw_parts[1:]):
        # C-6(b): "1500 Broadway CT, Suite 200" and "1 Asylum Hartford CT, 06103" read as their
        # first part, so an ambiguous street line stays ambiguous.
        text, raw_parts = raw_parts[0], raw_parts[:1]
    if len(raw_parts) < 2:
        stated = _parse(text, None, qualify=False, known=None, forms=True, street_address=True)
        stated = _with_unlisted_form_country(stated, text)  # GEO-P3 (G-7)
        kind = street_line_kind(text)
        ambiguous = parse_observed(text) if kind == STREET_LINE_AMBIGUOUS else None
        if stated is not None and stated.country is not None and _street_name_and_direction(text):
            # C-15: "5th St SE" may be "5th Street Southeast": its code reading is ambiguous.
            ambiguous, stated = stated, MarketIdentity(city=stated.city, label=stated.label)
        groups = _groups(text) if kind is None and not _street_name_and_direction(text) else ()
        ambiguous_groups = (
            _groups(text) if kind == STREET_LINE_AMBIGUOUS else _street_direction_group(text)
        )
        routes: tuple[MarketIdentity, ...] = ()
        route_groups: tuple[tuple[MarketIdentity, ...], ...] = ()
        if kind is None and _designator(text) and not _us_route(text):
            # GEO-P2 (G-8, FU-H): a route designator as the whole address ("1 SR 520", "12000 FM
            # 1960") may be a road, as C-16 reads it in a later part: ambiguous, never refused.
            routes, route_groups, groups = tuple(_placed(stated)), groups, ()
            stated = MarketIdentity(city=stated.city, label=stated.label) if stated else None
        elif kind is None and _numberless_street(text):
            # GEO-P2 (G-8, FU-H): "Main St", "NE 45th St": the street type may be a code.
            ambiguous = stated if stated is not None and stated.country is not None else ambiguous
            ambiguous_groups, groups = ambiguous_groups + groups, ()
            stated = MarketIdentity(city=stated.city, label=stated.label) if stated else None
        groups, homonyms = _with_nigerian_homonym(tuple(groups), text)
        groups, homonyms = _with_town_and_nigerian_code(groups, homonyms, [text])
        return BusinessAddressReading(
            stated,
            (),
            tuple(_placed(ambiguous)),
            groups,
            ambiguous_groups,
            routes=routes,
            route_groups=route_groups,
            homonyms=homonyms,
        )
    kept, parts, ambiguous = [raw_parts[0]], [], []
    groups, ambiguous_groups = [], []
    for index, part in enumerate(raw_parts[1:], 1):
        folded = _strip_postcode(fold(part))
        if all(_HAS_DIGIT.search(word) for word in folded.split()) or _FLOOR.fullmatch(
            part.strip()
        ):
            # A postcode alone; GEO-P2 (G-6, FU-B): a floor ("Fl 3") is no qualifier, never Florida.
            continue
        if _trailing_direction(raw_parts, index):
            continue  # GEO-P2 (I-3): "1200 Pennsylvania Ave, SE" is a direction part, not Sweden
        kept.append(part)
        stated_here, maybe = _part_reading(part)
        explicit_here, ambiguous_here = _part_groups(part)
        if _designator(part):
            # C-16(b): "12000 FM 1960", "Unit 5 NY 10001": a route or a state and its ZIP.
            stated_here, maybe = [], stated_here + maybe
            explicit_here, ambiguous_here = [], explicit_here + ambiguous_here
        parts += stated_here
        ambiguous += maybe
        groups += explicit_here
        ambiguous_groups += ambiguous_here
    stated = _parse(
        ", ".join(kept), None, qualify=False, known=None, forms=True, street_address=True
    )
    # C-6 and FU-R1 (ambiguous readings only): a first part whose last word may be a street type or
    # a code ("1500 Broadway Hartford CT, USA", "123 MAIN ST, DALLAS") is ambiguous, never nothing,
    # whatever the later parts state. Explicit geography in the address settles that reading or
    # rules it out (``_address_check``); a town alone ("..., DALLAS") does neither.
    ambiguous = _part_reading(raw_parts[0])[1] + ambiguous
    groups = list(_groups(", ".join(kept))) + groups
    ambiguous_groups = _part_groups(raw_parts[0])[1] + ambiguous_groups
    # GEO-P3 (G-7): an unlisted ISO 3166-2 form or a country-prefixed postcode names its country,
    # wherever it is written ("DE-BY, Bavaria"; "ACME, Flat 3, NO-0150 Oslo").
    stated = _with_unlisted_form_country(stated, None)
    parts += [
        MarketIdentity(country=found, label=raw.strip())
        for raw in raw_parts
        if (found := _unlisted_form_country(raw)) is not None
    ]
    shadowed = _unlisted_form_shadows(text)
    if stated is not None and stated.country in shadowed:
        stated = MarketIdentity(city=stated.city, label=stated.label)
    with_homonym, homonyms = _with_nigerian_homonym(tuple(groups), raw_parts[-1])
    with_homonym, homonyms = _with_town_and_nigerian_code(with_homonym, homonyms, raw_parts[1:])
    return BusinessAddressReading(
        stated,
        tuple(parts),
        tuple(ambiguous),
        with_homonym,
        tuple(ambiguous_groups),
        homonyms=homonyms,
    )


#: GEO-P3 (register G-7): an ISO 3166-2 form of a country whose subdivisions are not listed, two
#: letters, a hyphen and two letters, in capitals ("DE-BY", "IN-MH", "ZA-GT"). Three letters
#: ("FR-IDF") and digits ("KE-30") are not read: they cannot be told from a road ("GA-400",
#: "SR-12") or a hyphenated name ("AL-AIN") without each country's code list.
_UNLISTED_FORM: Final = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2})-([A-Z]{2})(?![A-Za-z0-9])")
#: GEO-P3 (register G-7): a country-prefixed postcode followed by a town ("NO-0150 Oslo"), not after
#: a house number.
_PREFIXED_POSTCODE: Final = re.compile(r"(?<![A-Za-z0-9])(?<!\d )([A-Z]{2})-(\d{4,6}) +[A-Z][a-z]")


def _unlisted_form_country(part: str) -> str | None:
    """GEO-P3 (register G-7): the country that an unlisted ISO 3166-2 form or a country-prefixed
    postcode in ``part`` names ("Munich DE-BY": DE; "NO-0150 Oslo": NO), when its country's
    subdivisions are not listed (a listed country's form is read as a listed code, GEO-P1)."""
    for pattern in (_UNLISTED_FORM, _PREFIXED_POSTCODE):
        for match in pattern.finditer(part):
            country = match[1]
            if country in COUNTRIES and country not in _CODED_COUNTRIES:
                return country
    return None


def _unlisted_form_shadows(text: str) -> frozenset[str]:
    """GEO-P3 (G-7): the countries that the letters after the hyphen of an unlisted form would name
    if read alone ("DE-BY": BY, Belarus), which the form's own reading replaces."""
    found = set()
    for match in _UNLISTED_FORM.finditer(text):
        if match[1] in COUNTRIES and match[1] not in _CODED_COUNTRIES:
            second = _country_code(match[2].lower())
            if second is not None and second != match[1]:
                found.add(second)
    return frozenset(found)


def _with_unlisted_form_country(
    stated: MarketIdentity | None, text: str | None
) -> MarketIdentity | None:
    """GEO-P3 (G-7): a one-part address with an unlisted form ("DE-BY", "Andheri IN-MH 400053")
    states the form's country, never the country its second letters would name ("BY" is Belarus).
    With ``text`` ``None`` (an address of several parts) ``stated`` is returned unchanged."""
    if text is None:
        return stated
    country = _unlisted_form_country(text)
    if country is None:
        return stated
    city = stated.city if stated is not None else None
    return MarketIdentity(city=city, country=country, label=text.strip())


def town_and_nigerian_code(part: str) -> MarketIdentity | None:
    """GEO-P3 (review I-6): the Nigerian reading of a part that is a town and a two-letter Nigerian
    code ("Ikeja LA", "Springfield OG", "Springfield AB 100001"), as GEO-P1's C-2 reads the code as
    a part place and C-1b in the first part; ``None`` for a street line, a city-first name, a part
    that names an area as a whole, a route designator, or a code form or road ("US-LA", "LA-1")."""
    folded = _strip_postcode(fold(part))
    if (
        len(folded.split()) < 2
        or _street_kind(part) is not None
        or folded in CITY_FIRST_NAMES
        or _area_candidates(folded)
        or _designator(part)
    ):
        return None
    return _nigerian_homonym(part)


def _with_town_and_nigerian_code(
    groups: tuple[tuple[MarketIdentity, ...], ...],
    homonyms: tuple[MarketIdentity, ...],
    targets: list[str],
) -> tuple[tuple[tuple[MarketIdentity, ...], ...], tuple[MarketIdentity, ...]]:
    """GEO-P3 (review I-6; register G-1, G-9 class of GEO-P3): D1's later-part and one-part readings
    of a town and a code knew no Nigerian code. A shared code's group gains its Nigerian reading
    ("1 Main St, Ikeja LA": Laos, Louisiana or Lagos State), and a Nigerian-only code becomes a
    group of its own ("1 Main St, Springfield OG": Ogun State; "Munich DE OG": Germany or Ogun
    State, C-12's pair). Each added reading is listed in ``homonyms``: it never makes an address
    agree by itself where another reading of the code contradicts (RULING-D1-C3)."""
    parts = [part.strip() for part in targets]
    added = list(homonyms)
    out, done = [], set()
    for group in groups:
        label = (group[0].label or "").strip() if group else ""
        reading = town_and_nigerian_code(label) if label in parts else None
        # A group that already reads Nigeria ("Ikeja NG LA", C-12's spaced form) keeps its readings.
        if reading is not None and all(r.country != reading.country for r in group):
            group = (*group, reading)
            added.append(reading)
            done.add(label)
        out.append(group)
    for part in parts:
        reading = town_and_nigerian_code(part)
        if reading is None or part in done:
            continue
        if any(r.region == reading.region for group in out for r in group):
            continue
        if _area_candidates(_strip_postcode(fold(part)).split()[-1]):
            continue  # D1 reads the code as something else here: its reading stands
        spaced = _spaced_code_form(part)
        if spaced is not None:
            out.append((MarketIdentity(country=spaced[0], label=part), reading))
        else:
            out.append((reading,))
        added.append(reading)
    return tuple(out), tuple(added)


def _code_form_part(part: str) -> bool:
    """GEO-P3: does ``part`` end in a listed code form written with a space ("US LA") or hold a
    hyphenated code form or road ("US-LA", "1500 Broadway US-LA", "LA-1")? Its code keeps the
    reading it has; it is not a town and a code, nor a code alone."""
    words = _strip_postcode(fold(part)).split()
    return " ".join(words[-2:]) in _FOLDED_SUBDIVISION_CODES or bool(_HYPHEN_CODE.search(part))


#: A two-letter code joined by a dash to letters or digits ("US-LA", "LA-1", "NG–LA").
_HYPHEN_CODE: Final = re.compile(r"[A-Za-z]{2}\s*[-\u2010-\u2015\u2212]\s*[A-Za-z0-9]")


def _nigerian_homonym(part: str) -> MarketIdentity | None:
    """GEO-P3: the Nigerian state that the code ending ``part`` also names (C-2's reading: "LA" is
    Lagos State, "DE" Delta State), when that code ends the part as a word and is no code form."""
    words = _strip_postcode(fold(part)).split()
    if not words or _code_form_part(part):
        return None
    coded = _JURISDICTION_CODE_AREAS.get(words[-1])
    if coded is None:
        return None
    return MarketIdentity(region=coded[1], country=coded[0], label=part.strip())


def _with_nigerian_homonym(
    groups: tuple[tuple[MarketIdentity, ...], ...], last: str
) -> tuple[tuple[tuple[MarketIdentity, ...], ...], tuple[MarketIdentity, ...]]:
    """GEO-P3 (register G-10 N-1): the qualifier's C-8 groups of the last part's code, each with
    the code's Nigerian reading added when the code is also a Nigerian state's ("…, Ikeja, LA":
    Laos, Louisiana or Lagos State; "…, Springfield, DE": Germany, Delaware or Delta State).
    Before, "1 Main St, Ikeja, LA" was refused for an Ikeja location. The added readings are
    returned too: they never make an address agree by themselves (``homonyms``)."""
    homonym = _nigerian_homonym(last)
    if homonym is None:
        return groups, ()
    code = _strip_postcode(fold(last)).split()[-1]
    readings = set(_area_candidates(code))
    added = False
    out = []
    for group in groups:
        areas = {(r.country, r.region) for r in group}
        if group and areas <= readings and all(r.region != homonym.region for r in group):
            group = (*group, homonym)
            added = True
        out.append(group)
    return tuple(out), ((homonym,) if added else ())


#: A code written with a hyphen or a dash inside a part ("US-CT Hartford", "Hartford US-CT",
#: "Lekki NG–LA"); :func:`_code_form` decides whether it is a listed code.
_CODE_TOKEN: Final = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{2}\s*[-\u2010-\u2015\u2212]\s*[A-Za-z0-9]{1,3}(?![A-Za-z0-9])"
)
#: A part that is only a direction may be a street's direction ("1200 Pennsylvania Ave, SE") or a
#: code.
_DIRECTION_PARTS: Final = frozenset({"ne", "nw", "se", "sw"})


def _listed_codes(raw: str, folded: str) -> list[_Form]:
    """Every listed subdivision code one part (``raw`` as written) writes: the whole part
    ("US-AL", "us al", "NG LA 100001"), a code with a hyphen or a dash anywhere in it ("US-CT
    Hartford", "Hartford US-CT", "Lekki NG–LA"), or a code with a space at its end ("1500 Broadway
    US CT", "1500 Broadway NG AB"). Only a code the tables place is one: a road number or a
    hyphenated name ("US-9", "GA-400", "AL-AIN", "Ca-Mau") is not, as everywhere a business address
    is read (W21-REV-5DW2FIX r1 C-1). A listed code written with a space stays explicit (D1's C-12);
    since the exclusion fix (#312) that includes Nigeria's, so "NG AB" after a word is Abia State,
    not only a group of Nigeria and Alberta."""
    whole = _code_form(folded, raw, observed=True)
    if whole is not None:
        return [whole] if whole.strict else []
    found: list[_Form] = []
    for token in _CODE_TOKEN.findall(raw):
        form = _code_form(fold(token), token, observed=True)
        if form is not None and form.strict and form not in found:
            found.append(form)
    words = folded.split()
    if not found and len(words) > 2:
        country, part = words[-2].upper(), words[-1].upper()
        key = _subdivision_key(country, part) if country in _CODED_COUNTRIES else None
        if key is not None:
            found.append(_Form(country, key, True, f"{country}-{part}"))
    return found


#: Words that make the code before them a road's country or state ("US Hwy 75", "US Route 1").
_ROUTE_WORDS: Final = frozenset({"hwy", "highway", "route", "rte", "rt"})


#: A US ZIP code, five digits or ZIP+4 ("33101", "33101-1234").
_ZIP_CODE: Final = re.compile(r"\d{5}(?:-\d{4})?")


#: A two-letter code and a ZIP code, the whole part ("NE 68102").
_STATE_AND_ZIP: Final = re.compile(r"[A-Za-z]{2}\s+\d{5}(?:-\d{4})?")


def _floor_after_town(raw_parts: list[str], index: int) -> bool:
    """GEO-P2 (review W21-REV-GEO P2 r1 C-1): is the floor-shaped part at ``index`` right after a
    bare town and before no street line ("1 Main St, Miami, FL 3")? There it may be a floor or the
    state with a short postcode. After a street line, a unit, a state or a country ("123 Main St, Fl
    3", "Dallas, TX 75201, Fl 3"), or before a street line ("Fl 3, 123 Main St"), it is a floor."""
    before = [part for part in raw_parts[:index] if not _unit_or_postcode(part)]
    after = [part for part in raw_parts[index + 1 :] if not _unit_or_postcode(part)]
    if not before or _street_kind(before[-1]) is not None:
        return False
    if after and _street_kind(after[0]) is not None:
        return False
    town = before[-1]
    return not _area_candidates(_strip_postcode(fold(town))) and not _part_reading(town)[0]


def _route_code(part: str) -> str | None:
    """GEO-P2 (G-8): the two-letter code before a highway word in ``part`` as written ("4400 US Hwy
    75" -> "US", "U.S. Route 1" -> "US"), when it names an area and is not a street type ("St Rt 9"
    is a State Route); ``None`` otherwise."""
    written = _written_words(part)
    for word, after in zip(written, written[1:], strict=False):
        folded = word.lower()
        if (
            after.lower() in _ROUTE_WORDS
            and len(word) == 2
            and word.isalpha()
            and folded not in STREET_SUFFIXES
            # Review W21-REV-GEO P2 r1 C-2: a street's direction ("SE Hwy 212", C-14) or a county
            # road ("CO Hwy 5") is no route code.
            and folded not in _DIRECTION_PARTS
            and folded != "co"
            and _area_candidates(folded)
        ):
            return word
    return None


def _is_named(part: str) -> bool:
    """Does a folded part name anything but a number (a unit or a postcode alone)?"""
    return (
        bool(part)
        and part not in _EMPTY_PARTS
        and not all(_HAS_DIGIT.search(word) for word in part.split())
    )


def _part_places(text: str) -> tuple[PartPlace, ...]:
    """GEO-P1: every part of ``text`` (separated as :data:`_PART_SEPARATORS` separates them) that
    names a place by itself, wherever it stands.

    A code ("US-AL; Montgomery; 1 Main St; Suite 5"), a state or country before the town ("Ogun
    State, Abeokuta", "KE, Nairobi") or alone in a middle part ("ACME, NG, Lagos") is read where the
    qualifier rules (:func:`_form_reading`, :func:`_parse`) do not look. See :class:`PartPlace` for
    what each kind means.

    Left to other rules, as on main: a floor or unit label that is also a code, before its number
    ("FL 3", "NO 5"; the unit rules); a road number or a hyphenated name ("US-9", "GA-400",
    "AL-AIN"; W21-REV-5DW2FIX r1 C-1), whose number would otherwise be dropped as a postcode;
    route designators ("12000 FM 1960"; C-16) and unlisted code forms ("DE-BY"; C-9, FU-C), which
    name no listed code; and a code of two or three letters right before the name of a country
    whose subdivisions are not listed ("Mumbai, MH, India"), the region slot of "town, region,
    country", which the tables cannot check (the geography register's G-7). In that slot before a
    country whose subdivisions are listed, the code is read as that country's subdivision first, as
    the qualifier rules read it ("Ikeja, LA, Nigeria" is Lagos State, "Dallas, TX, USA" Texas,
    "Abeokuta, OG, Nigeria" Ogun State).

    W21-REV-GEO r1: a Nigerian two-letter code has its Nigerian reading beside any other (C-2,
    :data:`_JURISDICTION_CODE_AREAS`); a short code that case does not settle is unclear, never a
    word (C-3); and the first part of an address of two or more parts is read as D1 reads a later
    part, "Dallas TX" or "Springfield AN" (C-1 and C-1b, :func:`_first_part_readings`), except a
    route designator ("1 SR 520, Redmond"), whose first-part reading D1 keeps (C-16)."""
    letters = [ch for ch in text if ch.isalpha()]
    mixed = any(ch.isupper() for ch in letters) and any(ch.islower() for ch in letters)
    places: list[PartPlace] = []

    def identities(areas: list[_Area]) -> tuple[MarketIdentity, ...]:
        return tuple(MarketIdentity(region=r, country=c, label=text) for c, r in areas)

    raw_parts = _written_parts(text)
    for index, raw in enumerate(raw_parts):
        folded = _strip_postcode(fold(raw))
        codes = _listed_codes(raw, folded)
        if codes:
            areas = [(form.country, form.region) for form in codes]
            places += [PartPlace(PLACE_CODE, folded, identities([area])) for area in areas]
            continue
        if _CODE_TOKEN.search(raw):
            continue  # "US-9", "GA-400", "AL-AIN", "DE-BY": no listed code (C-1, C-9)
        named = _named_subdivision(folded)
        prefixed = _AREA_PREFIX.sub("", folded) if _AREA_PREFIX.match(folded) else None
        if named is not None:
            area = (named.country, named.region)
            places.append(PartPlace(PLACE_CODE, folded, identities([area])))
            continue
        if prefixed:
            areas = _area_candidates(prefixed) + _homonym_areas(prefixed)
            coded = _JURISDICTION_CODE_AREAS.get(prefixed)
            if coded is not None and coded not in areas:
                # W21-REV-GEO r2 C-9: "State of PL" is Poland or Plateau State, as "PL" is (C-2).
                areas.append(coded)
            if areas:
                places.append(PartPlace(PLACE_CODE, folded, identities(areas)))
                continue
        if index == 0 and len(raw_parts) > 1 and not _designator(raw):
            # W21-REV-GEO r1 C-1: "Dallas TX, 1 Main St". A designator keeps D1's first-part
            # reading (C-16: "1 SR 520, Redmond"). W21-REV-GEO r2 C-9: it comes after the suffixed
            # and prefixed readings above, which are explicit in the first part as elsewhere
            # ("State of WA, 1 Main St, Dallas, TX" is refused for Dallas).
            found = _first_part_readings(raw, folded)
            if found:
                places += [PartPlace(PLACE_FIRST_PART, folded, readings) for readings in found]
                continue
        route = _route_code(raw)
        if route is not None:
            # GEO-P2 (G-8, FU-H): "4400 US Hwy 75" is a road of the US; read by case, as a code is.
            areas = _area_candidates(route.lower())
            if route.isupper() and mixed:
                places.append(PartPlace(PLACE_NAME, folded, identities(areas)))
            elif not mixed:
                places.append(PartPlace(PLACE_UNCLEAR, folded, identities(areas)))
            continue
        areas = _area_candidates(folded)
        coded = _JURISDICTION_CODE_AREAS.get(folded)
        if coded is not None and coded not in areas:
            areas = [*areas, coded]  # W21-REV-GEO r1 C-2: "LA" is Lagos State too, "OG" Ogun State
        homonyms = [area for area in _homonym_areas(folded) if area not in areas]
        if not areas and not homonyms:
            continue
        if folded in _DIRECTION_PARTS and not (
            index == 0 and _STATE_AND_ZIP.fullmatch(raw.strip())
        ):
            # Review W21-REV-GEO P2 r1 I-9: a first part "NE 68102" is a state and its ZIP code, as
            # "FL 33101" is, never a direction.
            places.append(PartPlace(PLACE_DIRECTION, folded, identities(areas)))
            continue
        # A code of two or three letters ("NG", "TX", "NSW"); a name that short ("Edo", "Imo",
        # "Oyo", a Nigerian state's bare name) is not one.
        short = " " not in folded and len(folded) <= 3 and bool(areas)
        numbers = fold(raw).split()[1:]
        zip_code = any(_ZIP_CODE.fullmatch(number) for number in numbers)
        if short and folded in _UNIT_LABELS and numbers and not (folded == "fl" and zip_code):
            # "FL 3", "NO 5", "No 1234": a floor or a unit before its number. GEO-P2: "FL" before a
            # ZIP code is Florida ("FL 33101"); any other label stays a unit (review P2 r1 C-3).
            if folded == "fl" and _floor_after_town(raw_parts, index):
                # Review W21-REV-GEO P2 r1 C-1: "1 Main St, Miami, FL 3" is a floor, or Florida
                # with a short postcode: an unclear word, as GEO-P1 reads one.
                places.append(PartPlace(PLACE_UNCLEAR, folded, identities(areas)))
            continue
        following = [p for p in map(address_part_key, raw_parts[index + 1 :]) if _is_named(p)]
        country_after = _country_code(following[0]) if following else None
        if short and country_after is not None:
            if country_after not in _CODED_COUNTRIES:
                # GEO-P3 (review I-1): a name whose country is unlisted but which has a listed
                # reading ("Georgia" is the country GE or US-GA; "Niger" the country NE or Niger
                # State) reads the code in that listed reading's country: "AL" beside "Georgia" is
                # US-AL. Before, "Springfield, AL, Georgia" read nothing in the region slot.
                listed = [
                    c
                    for c, r in _area_candidates(following[0]) + _homonym_areas(following[0])
                    if c in _CODED_COUNTRIES and r
                ]
                if not listed:
                    continue  # "Mumbai, MH, India": an unlisted country's region code (G-7)
                country_after = listed[0]
            region, _ = normalize_region(folded, country_after)
            tokens = _JURISDICTION_REGION_TOKENS.get(country_after, {})
            if region in _CODED_REGIONS or folded in tokens:
                areas, homonyms = [(country_after, region)], []  # "Ikeja, LA, Nigeria"
        written = _written_words(raw)
        if not areas or folded in CITY_FIRST_NAMES:
            kind = PLACE_UNCLEAR  # "Adamawa", "Kano" (a town's name too, X-5); "Washington" (X-4)
        elif not short:
            kind = PLACE_NAME
        elif not mixed:
            kind = PLACE_UNCLEAR  # "ACME, NG, LAGOS": case does not tell a code from a word
        elif len(written) == 1 and written[0].isupper():
            kind = PLACE_NAME  # "ACME, NG, Lagos": a code written as one
        else:
            # W21-REV-GEO r1 C-3: "Dallas, Tx, 1 Main St", "Bo, Southern Province": case does not
            # settle it, so it is never read as naming nothing. "Bo" for a location in Bo is the
            # town (the resolver's city exemption; r3b C-4).
            kind = PLACE_UNCLEAR
        places.append(PartPlace(kind, folded, identities(areas + homonyms)))
    return tuple(places)


def _first_part_readings(raw: str, folded: str) -> list[tuple[MarketIdentity, ...]]:
    """W21-REV-GEO r1 C-1: the readings of an address's first part as a town and a code, one group
    each: D1's explicit reading of a later part (:func:`_part_reading`: "Dallas TX") and its
    explicit C-8 and C-12 groups (:func:`_part_groups`: "Dallas GA", "Munich DE BY"), each with the
    Nigerian reading of a two-letter code (C-2: "Springfield LA" is Louisiana, Laos or Lagos
    State). C-1b (coordinator ruling R-2): a word and a Nigerian code that names nothing else
    ("Springfield AN", where D1 reads "Springfield TX"), with that code's reading alone, or beside
    the country of a spaced code form, as C-12 reads one ("Munich DE OG"). Empty for anything
    else."""
    explicit, _ = _part_reading(raw)
    groups, _ = _part_groups(raw)
    found = [(reading,) for reading in explicit] + [tuple(group) for group in groups]
    words = folded.split()
    coded = _JURISDICTION_CODE_AREAS.get(words[-1]) if words else None
    if coded is None:
        return found
    nigerian = MarketIdentity(region=coded[1], country=coded[0], label=raw)
    if found:
        return [(*readings, nigerian) for readings in found]
    if len(words) < 2 or _area_candidates(words[-1]):
        return []  # "OG" alone is a short part (C-2); "12 Allen Pl" is D1's ("PL" is Poland too)
    spaced = _spaced_code_form(raw)
    if spaced is not None:
        # C-12: "Munich DE OG" is Germany's code form or the code OG after a word.
        return [(MarketIdentity(country=spaced[0], label=raw), nigerian)]
    return [(nigerian,)]


def _part_groups(
    part: str,
) -> tuple[list[tuple[MarketIdentity, ...]], list[tuple[MarketIdentity, ...]]]:
    """(explicit, ambiguous) C-8 groups of one part, as :func:`_part_reading` branches."""
    folded = _strip_postcode(fold(part))
    kind = _street_kind(part)
    if kind is not None:
        return [], (list(_groups(part)) if kind == STREET_LINE_AMBIGUOUS else [])
    if folded in CITY_FIRST_NAMES or _area_candidates(folded):
        return [], []
    split = _split_trailing_code(folded)
    if split is None:
        return [], []
    town, code = split
    explicit_code = " ".join(folded.split()[-2:]) in _FOLDED_SUBDIVISION_CODES
    spaced = None if explicit_code else _spaced_code_form(part)
    if spaced is not None:
        group = [_spaced_group(spaced, part)] if _spaced_group(spaced, part) else []
        return ([], group) if _street_and_direction(spaced) else (group, [])
    if (len(town) == 2 and _country_code(town) and not explicit_code) or (
        not explicit_code and (_unlisted_code_form(part) or _designator(part, unit=True))
    ):
        return [], []
    if code in STREET_SUFFIXES or _street_name_and_direction(part):
        return [], list(_groups(part))
    return list(_groups(part)), []


#: An ISO 3166-2 form at the end of a part as written: "DE-BY", "Munich DE-BY", "IN-MH", also with
#: a typographic dash ("NG\u2013LA").
_TRAILING_CODE_FORM: Final = re.compile(
    r"(?:^|[^A-Za-z])([A-Za-z]{2})\s*[-\u2010-\u2015\u2212]\s*[A-Za-z]{1,3}\s*$"
)
#: Trailing words with a digit (a postcode, "Munich DE-BY 80331"), as :func:`_strip_postcode` drops.
_TRAILING_POSTCODE: Final = re.compile(r"(?:\s+\S*\d\S*)+\s*$")


def _unlisted_code_form(part: str) -> bool:
    """Does ``part`` (or the last part of ``part``) end, before any postcode, in an ISO 3166-2 form
    of a country whose subdivision is not listed ("Munich DE-BY", "Andheri IN-MH 400053")? Its
    letters after the hyphen are not a code on their own; the form states nothing."""
    last = _PART_SEPARATORS.split(part.rstrip(",;/\r\n \t"))[-1]
    match = _TRAILING_CODE_FORM.search(_TRAILING_POSTCODE.sub("", last.rstrip()))
    if match is None or _country_code(match[1].lower()) is None:
        return False
    return " ".join(_strip_postcode(fold(last)).split()[-2:]) not in _FOLDED_SUBDIVISION_CODES


#: An ISO 3166-2 form written with a space at the end of a part ("Munich DE BY", "Ikeja NG LA").
_SPACED_CODE_FORM: Final = re.compile(r"(?:^|\s)([A-Za-z]{2})\s+([A-Za-z]{1,3})\s*$")


def _spaced_code_form(part: str) -> tuple[str, str] | None:
    """``(country, letters)`` when ``part`` (or its last part) ends, before a postcode, in "XX YY":
    XX a country code and "XX YY" not a listed subdivision code ("Munich DE BY", "Ikeja NG LA",
    "NG LA", "Harris Co TX"). It is a code form of XX or the code YY after a word (W21-REV-D1STREET
    r2 re-bind C-12). XX may not be a street-type abbreviation after other words ("123 MAIN ST
    NE" is C-8's NE case); alone with YY it is a code form too ("PL MA"). Dots and apostrophes are
    dropped first, as :func:`fold` drops them ("N.G. LA")."""
    last = _PART_SEPARATORS.split(part.rstrip(",;/\r\n \t"))[-1]
    written = _TRAILING_POSTCODE.sub("", re.sub(r"[.'’`´]", "", last).rstrip())
    match = _SPACED_CODE_FORM.search(written)
    if match is None:
        return None
    country, letters = match[1].lower(), match[2].lower()
    if _country_code(country) is None or f"{country} {letters}" in _FOLDED_SUBDIVISION_CODES:
        return None
    if country in STREET_SUFFIXES and len(written.split()) > 2:
        return None
    return country.upper(), letters


def _street_and_direction(spaced: tuple[str, str]) -> bool:
    """C-14: "St NE" may be a street type and a post-directional, so its code readings are
    ambiguous, as in "123 MAIN ST NE"."""
    return spaced[0].lower() in STREET_SUFFIXES and spaced[1] in _POST_DIRECTIONALS


def _street_name_and_direction(part: str) -> bool:
    """R17: a street name, a street type and a post-directional with no house number ("5th St NE",
    "Massachusetts Ave NE"): the direction may be a code, so its readings are ambiguous, as for
    "St NE" alone (C-14) and "123 MAIN ST NE"."""
    words = _strip_postcode(fold(part)).split()
    return (
        len(words) > 2
        and words[-2] in STREET_SUFFIXES
        and words[-1] in _POST_DIRECTIONALS
        and _street_kind(part) is None
    )


def _street_direction_group(text: str) -> tuple[tuple[MarketIdentity, ...], ...]:
    """C-14: the ambiguous group of a one-part address that is only "St NE"; R17: or a street name
    before it ("5th St NE")."""
    spaced = _spaced_code_form(text)
    if spaced is None:
        return _groups(text) if _street_name_and_direction(text) else ()
    if not _street_and_direction(spaced):
        return ()
    group = _spaced_group(spaced, text)
    return (group,) if group else ()


def _spaced_group(spaced: tuple[str, str], label: str) -> tuple[MarketIdentity, ...]:
    """C-12: both readings of a spaced code form, the country XX and each reading of YY; empty when
    YY names no area ("AL ULA")."""
    country, letters = spaced
    areas = _area_candidates(letters)
    if not areas:
        return ()
    readings = [(country, None)] + [area for area in areas if area != (country, None)]
    return tuple(MarketIdentity(region=r, country=c, label=label) for c, r in readings)


#: Words that number a unit ("Flat No 3", "Plot Nr 12").
_NUMBER_LABELS: Final = frozenset({"no", "nr", "num", "number"})


def _us_route(text: str) -> bool:
    """GEO-P2 (G-8, FU-H): is ``text`` a route designator whose code is "US" ("1000 US-75", "1000
    us 75")? Its road reading is a United States route and its code reading the United States, so
    both name the same country: it names the United States, as "4400 US Hwy 75" does, and is not an
    ambiguous route ("1 SR 520": a road, or Suriname)."""
    return _designator(text) and _strip_postcode(fold(text)).split()[-1] == "us"


def _designator(part: str, unit: bool = False) -> bool:
    """C-16: is the last word of ``part`` a code only once a trailing number is dropped, and after
    a number or a unit label ("12000 FM 1960", "1 SR 520", "Unit 5 NY 10001"), or a unit label
    after a street type ("123 Main St No 5"; a floor, "Fl 3", is left out first, GEO-P2)? In a
    later part such a code is ambiguous. With
    ``unit``: is it a number label after a unit label ("Flat No 3", "Plot No 12"), which states
    nothing?"""
    words, kept = fold(part).split(), _strip_postcode(fold(part)).split()
    if len(words) == len(kept) or len(kept) < 2:
        return False
    if unit:
        return kept[-1] in _NUMBER_LABELS and kept[-2] in _UNIT_LABELS
    return (
        bool(_HAS_DIGIT.search(kept[-2]))
        or kept[-2] in _UNIT_LABELS
        or (kept[-1] in _UNIT_LABELS and kept[-2] in STREET_SUFFIXES)
    )


def _part_reading(part: str) -> tuple[list[MarketIdentity], list[MarketIdentity]]:
    """(explicit, ambiguous) readings of one comma part that ends in a code."""
    folded = _strip_postcode(fold(part))
    kind = _street_kind(part)
    if kind is not None:
        return [], (_placed(parse_observed(part)) if kind == STREET_LINE_AMBIGUOUS else [])
    if folded in CITY_FIRST_NAMES or _area_candidates(folded):
        return [], []
    split = _split_trailing_code(folded)
    if split is None:
        return [], []
    town, code = split
    if (
        folded in _FOLDED_SUBDIVISION_CODES
        or " ".join(folded.split()[-2:]) in _FOLDED_SUBDIVISION_CODES
    ):
        return _placed(parse_observed(part)), []
    if (
        (len(town) == 2 and _country_code(town))
        or _unlisted_code_form(part)
        or _designator(part, unit=True)
    ):
        return [], []
    if _spaced_code_form(part) is not None:
        return [], []  # C-12: read as a group of both readings (_part_groups)
    if code in STREET_SUFFIXES or _street_name_and_direction(part):
        return [], _placed(parse_observed(part))
    return _placed(parse_observed(part)), []


def _unit_or_postcode(part: str) -> bool:
    """A comma part that is only a postcode ("06103") or a unit ("Suite 200", "Apt 4B")."""
    words = fold(part).split()
    if not words:
        return True
    if all(_HAS_DIGIT.search(word) for word in words):
        return True
    if _FLOOR.fullmatch(part.strip()):
        return True  # GEO-P2 (G-6): "Fl 3" is a floor, never Florida
    # A label that is also a code ("FL-12" Florida road, "NO 5") is not a unit.
    return (
        len(words) >= 2
        and words[0] in _UNIT_LABELS
        and not _area_candidates(words[0])
        and all(_HAS_DIGIT.search(word) for word in words[1:])
    )


def _placed(identity: MarketIdentity | None) -> list[MarketIdentity]:
    return [identity] if identity is not None and identity.country is not None else []


def _clean_parts(text: str) -> list[tuple[str, str]]:
    """``(folded, as written)`` for each comma-separated part that names something."""
    parts = []
    for raw in text.split(","):
        folded = _strip_postcode(fold(raw))
        if folded and folded not in _EMPTY_PARTS:
            parts.append((folded, raw))
    return parts


def _identity(
    city: str | None, region: str | None, country: str | None, label: str
) -> MarketIdentity:
    return MarketIdentity(city=city or None, region=region, country=country, label=label)


# --- jurisdiction forms (Project Phase 5D; W21 5DW2FIX) -----------------------------


@dataclass(frozen=True)
class _Form:
    """A part that names its own jurisdiction: a code, or a suffixed Nigerian subdivision."""

    country: str
    #: Region key, or ``None`` for a country code ("KE, Kenya").
    region: str | None
    #: A code of a country whose subdivisions are listed: never a place name, so text that
    #: contradicts it is unplaceable rather than read as words.
    strict: bool
    #: The code as written, normalised ("US-TX"); ``None`` for a form that is not a code.
    code: str | None = None


def _subdivision_key(country: str, part: str) -> str | None:
    """The region key of a listed subdivision code ("US", "TX" -> "US-TX"; "NG", "LA" ->
    "NG:lagos"); ``None`` when ``country`` lists no such code."""
    if part in SUBDIVISIONS.get(country, {}):
        return f"{country}-{part}"
    names = SUBDIVISION_JURISDICTIONS.get(country, {}).get(part)
    return f"{country}:{fold(names[0])}" if names else None


def _code_form(folded: str, raw: str, *, observed: bool = False) -> _Form | None:
    """A part written as a jurisdiction code: "US-TX", "us - tx", "NG-LA", "US TX".

    In an ``observed`` market (evidence, a request's market, a business address) only a code
    the tables place is one: a road number ("US-9", "GA-400", "IN-37") or a hyphenated name
    ("AL-AIN", "Ca-Mau") there is read as words, as before (W21-REV-5DW2FIX r1, C-1). A
    rule's text keeps the fail-closed reading of an unplaceable code."""
    match = _CODE_FORM.fullmatch(raw.strip())
    if match:
        country, part = match[1].upper(), match[2].upper()
        if country not in COUNTRIES:
            return None
        strict = country in _CODED_COUNTRIES
        # Another country's subdivisions are not listed, so its code cannot be checked and
        # a hyphenated place name looks the same ("Al-Ain"): only upper case reads as a code.
        if not strict and match[0] != match[0].upper():
            return None
    else:
        words = folded.split()
        if len(words) != 2:
            return None
        country, part = words[0].upper(), words[1].upper()
        if _subdivision_key(country, part) is None:
            return None
        strict = True
    code = f"{country}-{part}"
    key = _subdivision_key(country, part)
    if observed and key is None:
        return None
    return _Form(country, key or code, strict, code)


def _named_subdivision(folded: str) -> _Form | None:
    """A Nigerian subdivision named with its suffix ("lagos state", "federal capital
    territory"), from ``app.jurisdiction.subdivisions``. A bare name is not one."""
    found: set[tuple[str, str]] = set()
    for suffix in _SUBDIVISION_SUFFIXES:
        if folded.endswith(suffix) and len(folded) > len(suffix):
            for country, names in _JURISDICTION_REGION_NAMES.items():
                name = names.get(folded) or names.get(folded[: -len(suffix)])
                if name:
                    found.add((country, f"{country}:{name}"))
    if len(found) != 1:
        return None
    country, key = found.pop()
    return _Form(country, key, strict=False)


def _form(folded: str, raw: str, *, head_of_two: bool, observed: bool) -> _Form | None:
    form = _code_form(folded, raw, observed=observed) or _named_subdivision(folded)
    if form is None and head_of_two and len(folded) == 2 and folded.upper() in COUNTRIES:
        # "KE, Kenya": a country code before its country. Soft: "Bo, Sierra Leone" is a city.
        form = _Form(folded.upper(), None, strict=False)
    return form


def _agree(first: _Form, second: _Form) -> _Form | None:
    if first.country != second.country:
        return None
    if first.region is not None and second.region is not None and first.region != second.region:
        return None
    return _Form(
        first.country,
        first.region or second.region,
        first.strict or second.strict,
        first.code or second.code,
    )


def _unplaceable(label: str, *forms: _Form, city: str | None = None) -> MarketIdentity | None:
    """A listed code that the rest of the text contradicts ("US-TX, Canada") is read as its
    own country with a region that cannot be placed ("US-TX?"): it may cover any market of
    that country, contains none, and is no location's subdivision. Any other contradicted
    form is read as words (``None``)."""
    strict = next((f for f in forms if f.strict), None)
    if strict is None:
        return None
    return _identity(city, f"{strict.code}?", strict.country, label)


def _form_reading(
    parts: list[tuple[str, str]], label: str, *, observed: bool, address: bool = False
) -> MarketIdentity | None:
    """The reading of a text with a jurisdiction form where a qualifier goes, or ``None``
    to read the text as words (no form there, or one that place names could explain).

    ``address``: the text is a business address, where a listed code may come before the town
    ("US-CT, Hartford", "1 Main St, US-CT, Hartford, USA"; FU-E, W21-REV-5DW2FIX r3) or before
    its own state's name and the country ("US-TX, Texas, USA"; r1 I-2, r3b)."""
    words = [folded for folded, _ in parts]
    n = len(parts)
    forms = [
        _form(folded, raw, head_of_two=n == 2 and index == 0, observed=observed)
        for index, (folded, raw) in enumerate(parts)
    ]
    last = forms[-1]
    if last is not None:
        # The qualifier names the jurisdiction: "US-TX", "Ikeja, Lagos State", "Dallas, US-TX".
        before = forms[-2] if n > 1 else None
        if before is not None:
            both = _agree(before, last)
            if both is not None:
                return _identity(None, both.region, both.country, label)
            if before.strict:
                return _unplaceable(label, before)
            # A soft form that disagrees is the city's name ("Bo, US-TX").
        return _identity(words[-2] if n > 1 else None, last.region, last.country, label)
    form = forms[-2] if n >= 2 else None
    if form is None:
        head = forms[-3] if n >= 3 else None
        if address and head is not None and head.strict and _a_town(words[-2], head):
            # FU-E: a business address "US-CT, Hartford, USA" is Hartford in US-CT when the
            # country after the town agrees ("US-CT, Hartford, Canada" stays unplaceable).
            if any(_agree(head, _Form(c, r, strict=False)) for c, r in _area_candidates(words[-1])):
                return _identity(words[-2], head.region, head.country, label)
        if address and head is not None and head.strict and _names_area(words[-2], head):
            # I-2: "US-TX, Texas, USA" is Texas when the state after the code is the code's own
            # and the country after it agrees ("US-TX, Ohio, USA" stays unplaceable).
            if any(_agree(head, _Form(c, r, strict=False)) for c, r in _area_candidates(words[-1])):
                return _identity(None, head.region, head.country, label)
        return _unplaceable(label, head) if head is not None else None
    # The head of "X, country" or the region of "city, X, country": the qualifier after it
    # must name the same country ("US-TX, USA", "Lagos State, Nigeria", "KE, Kenya").
    if n >= 3 and forms[-3] is not None:
        both = _agree(forms[-3], form)
        if address and both is not None and forms[-3].strict:
            # C-5 (r3c): "NG-LA, Lagos State, Nigeria" is Lagos State when the code and the
            # suffixed name agree and so does the country after them.
            if any(_agree(both, _Form(c, r, strict=False)) for c, r in _area_candidates(words[-1])):
                return _identity(None, both.region, both.country, label)
        return _unplaceable(label, form, forms[-3])
    for country, region in _area_candidates(words[-1]):
        both = _agree(form, _Form(country, region, strict=False))
        if both is not None:
            return _identity(words[-3] if n >= 3 else None, both.region, both.country, label)
    if address and form.strict and _a_town(words[-1], form):
        # FU-E: a business address "US-CT, Hartford" is Hartford in US-CT; a town contradicts
        # no code. A country or state after the code ("US-TX, Canada") still does.
        return _identity(words[-1], form.region, form.country, label)
    return _unplaceable(label, form, city=words[-3] if n >= 3 else None)


def _a_town(folded: str, form: _Form) -> bool:
    """Is ``folded`` (a part that is no form) a town for ``form``? It names no country or
    region, and no Nigerian state but the code's own ("Kano" after NG-KN)."""
    homonyms = _homonym_areas(folded)
    return not _area_candidates(folded) and all(area[1] == form.region for area in homonyms)


def _names_area(folded: str, form: _Form) -> bool:
    """Does ``folded`` name ``form``'s own subdivision ("texas" for US-TX; r1 I-2, r3b)?"""
    if (form.country, form.region) in _area_candidates(folded):
        return True
    # C-5 (r3c): the code's own Nigerian state named bare, even when also a country
    # ("NG-NI, Niger, Nigeria").
    return any(region == form.region for _, region in _homonym_areas(folded))


#: "State of Texas", "Province of Ontario": the area named after the prefix (P-CN).
_AREA_PREFIX: Final = re.compile(r"^(?:state|province) of ")


def _address_agrees(parts: list[tuple[str, str]], label: str) -> MarketIdentity | None:
    """P-CN: a business address whose listed code the qualifier rules left unplaceable is that
    code's subdivision when every other part agrees with it and at least one names its area: the
    code's own state ("US-AL, Alabama, Montgomery", "US-AL, Montgomery, State of Alabama",
    "Abia State, NG-AB, Nigeria"), an agreeing country, or another form of the same code. A part
    that names no area is a town or a street. Any part naming another area keeps it unplaceable.
    When no part names an area, the code agrees if at most one other part is a word without a
    digit: a town ("US-AL, 1 Main St, Montgomery"; C-6, W21-REV-5DW2FIX r3d). A unit or a postcode
    alone is not a word ("US-AL, Montgomery, Suite 5"; C-8, r3e)."""
    forms = [_form(folded, raw, head_of_two=False, observed=True) for folded, raw in parts]
    code = next((form for form in forms if form is not None and form.strict), None)
    if code is None:
        return None
    confirmed = False
    for (folded, _), form in zip(parts, forms, strict=True):
        if form is code:
            continue
        if form is not None:
            areas = [(form.country, form.region)]
        else:
            name = _AREA_PREFIX.sub("", folded)
            areas = _area_candidates(name) + _homonym_areas(name)
            if not areas:
                continue
        if not any(_agree(code, _Form(c, r, strict=False)) for c, r in areas):
            return None
        confirmed = True
    if not confirmed:
        # C-6 (r3d): no part names an area. As FU-E reads "US-AL, Montgomery", one town and any
        # streets or units agree ("US-AL, 1 Main St, Montgomery"); two words may hide an area.
        # C-8 (r3e): ``folded`` has lost a unit's number ("Suite 5" -> "suite"), so D1's
        # ``_unit_or_postcode`` reads each part as written and leaves units out of the count.
        words = [
            folded
            for (folded, raw), form in zip(parts, forms, strict=True)
            if form is not code and not _unit_or_postcode(raw)
        ]
        confirmed = sum(1 for word in words if not _HAS_DIGIT.search(word)) <= 1
    return _identity(None, code.region, code.country, label) if confirmed else None


# --- every reading, for exclusion checks (operator rulings X-1 to X-5, 2026-11-08) -------

#: "<name> City" where a city-first name's city lies in the subdivision of the same name. The
#: qualifier narrows "New York" to the city (X-4); both spellings of the city are read.
_CITY_QUALIFIED: Final[dict[str, tuple[str, str, tuple[str, ...]]]] = {
    "new york city": ("US", "US-NY", ("new york", "new york city")),
    "nyc": ("US", "US-NY", ("new york", "new york city")),
    "quebec city": ("CA", "CA-QC", ("quebec", "quebec city")),
}


def _homonym_areas(word: str) -> list[_Area]:
    """The Nigerian subdivision a bare name also is ("lagos", "niger", "abuja"): names and
    aliases from ``app.jurisdiction.subdivisions``, never a two-letter state code ("ke" is
    Kenya, never Kebbi)."""
    return [
        (country, f"{country}:{name}")
        for country, names in _JURISDICTION_REGION_NAMES.items()
        if (name := names.get(word))
    ]


def _narrow(areas: list[_Area], city: str | None, country: str | None) -> list[_Area]:
    """X-2: "a known country must constrain the possible readings". Place knowledge first (a
    city that ``KNOWN_CITIES`` lists under exactly one reading: "Niamey, Niger" is the
    country), then the location's country ("Niger" for a Nigerian location is Niger State).
    With neither, every reading stays."""
    if len(areas) < 2:
        return areas
    knowing = [area for area in areas if _city_known_in(city, area, None)]
    if len(knowing) == 1:
        return knowing
    local = [area for area in areas if area[0] == country]
    return local or areas


def _may_be_a_place_name(region: str | None, country: str | None) -> bool:
    """An upper-case code of a country whose subdivisions are not listed, with a letters-only
    part ("AL-AIN", "DE-BY"): it cannot be checked, and a hyphenated place name looks the
    same, so its word reading ("Al Ain") is kept beside it."""
    if not _unplaced(region) or (region or "").endswith("?") or country in _CODED_COUNTRIES:
        return False
    return (region or "").partition("-")[2].isalpha()


def _single_name_readings(
    word: str,
    primary: MarketIdentity | None,
    country: str | None,
    label: str,
    *,
    observed: bool,
) -> list[MarketIdentity | None]:
    if word in _CITY_QUALIFIED and not observed:
        # X-4: an excluded "New York City" is the city only. An observed one names no country
        # and stays a place anywhere (X-1: missing geography is matched conservatively).
        code, region, names = _CITY_QUALIFIED[word]
        return [_identity(name, region, code, label) for name in names]
    areas = list(_area_candidates(word))
    if areas or not observed:
        # X-5: an observed market that is only a Nigerian state's name ("Lagos") names no
        # state; no state is invented for it. A rule's text may mean the state.
        areas += [area for area in _homonym_areas(word) if area not in areas]
    if len(areas) > 1:
        # X-1, X-2: a name with several areas ("CA", "NE", "Georgia", "Niger") is each of them,
        # never an unqualified city; the location's country narrows them, but only to readings
        # the text already had for that location (W21-REV-5DW2FIX r2 C-2: "Niger" for a Nigerian
        # location stays the country Niger too, and is Niger State as well).
        narrowed = [
            _identity(None, region, code, label) for code, region in _narrow(areas, None, country)
        ]
        if not observed and primary is not None and primary.country and primary.city is None:
            narrowed.append(primary)
        return narrowed
    if areas:
        # X-4 and X-1: one area and a place: "New York" is the city and the state, "Lagos" the
        # city and the state, and a subdivision the location's country re-read as a local place
        # ("Texas" for a Nigerian location) is still the subdivision too.
        code, region = areas[0]
        return [primary, _identity(None, region, code, label)]
    return [primary]


def _qualified_readings(
    text: str,
    words: list[str],
    primary: MarketIdentity | None,
    country: str | None,
    label: str,
    *,
    observed: bool,
) -> list[MarketIdentity | None]:
    n = len(words)
    readings: list[MarketIdentity | None] = [primary]
    candidates = _area_candidates(words[-1])
    if observed and len(candidates) > 1 and primary is not None and primary.country is None:
        # X-3: an observation whose qualifier place knowledge does not decide ("York, PA",
        # "Smallville, GA") is compared in each reading its qualifier allows, not as a place
        # anywhere.
        readings = []
        for code, region in candidates:
            city = words[-3] if region is None and n >= 3 else words[-2]
            known = MarketIdentity(city=city, region=region, country=code)
            readings.append(_parse(text, None, qualify=False, known=known, forms=True))
    if n == 2 and not observed:
        # "Lagos, NG" in a rule's text: the city, and Lagos State. An observed "Lagos, NG"
        # names no state (X-5).
        for code, region in _homonym_areas(words[0]):
            if (code, None) in candidates:
                readings.append(_identity(None, region, code, label))
    states = _homonym_areas(words[-1])
    if states:
        city = words[-2]
        others = [area for area in candidates if area not in states]
        if others:
            # "Minna, Niger": Niger the country or Niger State, narrowed by a known country.
            chosen = _narrow(others + states, city, country)
            readings = [r for r in readings if r is None or (r.country, None) in chosen]
            readings += [_identity(city, region, code, label) for code, region in chosen if region]
        else:  # "Ikeja, Lagos": also Lagos State, whatever the location's country.
            readings += [_identity(city, region, code, label) for code, region in states]
    return readings


def _readings(text: str | None, country: str | None, *, observed: bool) -> list[MarketIdentity]:
    """Every reading of ``text`` an exclusion check compares (see :func:`exclusion_readings`
    and :func:`observed_readings`)."""
    if text is None:
        return []
    cleaned = _clean_parts(text)
    if not cleaned:
        return []
    label = text.strip()
    qualify = not observed
    form = _form_reading(cleaned, label, observed=observed)
    if form is not None:
        # X-1: an explicit identifier is read as itself only ("NG-LA" is Lagos State, never
        # the city "ng" that the word reading made of it).
        readings: list[MarketIdentity | None] = [form]
        if (form.region or "").endswith("?"):  # "US-TX, Canada": also the country it names.
            stated = _parse(text, country, qualify=qualify, known=None, forms=False)
            if stated is not None and stated.country not in (None, form.country):
                readings.append(_identity(form.city, None, stated.country, label))
        elif _may_be_a_place_name(form.region, form.country):
            readings.append(_parse(text, country, qualify=qualify, known=None, forms=False))
        elif not observed and _unplaced(form.region):
            # Not an explicit valid identifier, so its geography stays ambiguous (X-1,
            # conservative): an unplaceable code ("GA-400", "AL-69" for a US location) keeps a
            # word reading that names an area (Georgia, Alabama), never a city misreading
            # ("US-ZZ" is no city "us zz"). W21-REV-5DW2FIX r2 C-1.
            word = _parse(text, country, qualify=True, known=None, forms=False)
            if word is not None and word.country is not None and word.city is None:
                readings.append(word)
        return list(dict.fromkeys(r for r in readings if r is not None))
    primary = _parse(text, country, qualify=qualify, known=None, forms=True)
    words = [folded for folded, _ in cleaned]
    if len(words) == 1:
        token = words[0]
        split = None
        if (
            token not in CITY_FIRST_NAMES
            and token not in _CITY_QUALIFIED
            and not _area_candidates(token)
            and not _homonym_areas(token)
        ):
            split = _split_trailing_code(token)
        if split is None:
            found = _single_name_readings(token, primary, country, label, observed=observed)
            return list(dict.fromkeys(r for r in found if r is not None))
        words = list(split)
    found = _qualified_readings(text, words, primary, country, label, observed=observed)
    return list(dict.fromkeys(r for r in found if r is not None))


def exclusion_readings(
    text: str | None, *, default_country: str | None = None
) -> list[MarketIdentity]:
    """Every place an exclusion's text may name. An exclusion removes an observed market that
    any of them may cover (item 35; operator rulings X-1 to X-5, 2026-11-08):

    * X-1: an explicit, valid identifier ("NG-LA", "Lagos State", "US-TX", "KE, Kenya") is read
      as itself only. A listed code its own text contradicts ("US-TX, Canada") is its country
      and the country the text names. An upper-case code of a country whose subdivisions are
      not listed ("AL-AIN") keeps its word reading too: it may be a place name.
    * X-1, X-2: a single name with several areas ("CA", "NE", "Georgia", "Niger") is each of
      them, never an unqualified city; place knowledge and the location's country narrow them,
      but never below the reading the text had for that location ("Niger" for a Nigerian
      location is the country Niger and Niger State; "CA" for a US location is California).
    * an unplaceable code ("US-ZZ", "GA-400") is its country, and also the area its words name
      for the location ("GA-400" for a US location is Georgia too).
    * X-2: "<city>, Niger" is the country or Niger State, narrowed the same way.
    * X-4: "New York", "Washington", "Quebec" are the city and the state or province; "New York
      City", "New York, NY", "New York State" and "NY" are the one they name.
    * a bare name that is also a Nigerian state ("Lagos", "Lagos, NG", "Ikeja, Lagos") is the
      place and the state.
    * any other text has the one reading :func:`parse_market` gives it (conservative where
      its geography is missing: a bare "London" is a London anywhere).
    """
    return _readings(text, default_country, observed=False)


def observed_readings(text: str | None) -> list[MarketIdentity]:
    """Every plausible reading of an observed market in an exclusion check (X-3: "consistently
    with its recorded country, recognised codes and qualifiers"): the readings
    :func:`exclusion_readings` gives a text, without a location's country, and each reading an
    undecided qualifier allows ("York, PA" is York in Pennsylvania or in Panama). Inclusions
    and connector clearance read an observed market once (:func:`parse_observed`)."""
    return _readings(text, None, observed=True)


def inclusion_readings(
    text: str | None, *, default_country: str | None = None
) -> list[MarketIdentity]:
    """The places an inclusion's text admits: :func:`parse_market`'s reading, and, for an
    upper-case code of a country whose subdivisions are not listed ("AL-AIN"), its word reading
    too (it may be a place name). One reading otherwise: ambiguity widens no inclusion."""
    reading = parse_market(text, default_country=default_country)
    if reading is None:
        return []
    readings = [reading]
    if _may_be_a_place_name(reading.region, reading.country):
        word = parse_market(text, default_country=default_country, jurisdiction_forms=False)
        if word is not None:
            readings.append(word)
    return list(dict.fromkeys(readings))


def parse_market(
    text: str | None,
    *,
    default_country: str | None = None,
    jurisdiction_forms: bool = True,
) -> MarketIdentity | None:
    """Parse a market a customer configured ("Dallas, TX", "London, Ontario, Canada",
    "Texas", "Ikeja", "US-TX", "Lagos State") into an identity; ``None`` when nothing
    usable remains.

    ``default_country`` is the country of the location the text belongs to. It
    qualifies a bare place name ("Ikeja" for a Nigerian location is Ikeja, NG) and
    chooses between the readings of an ambiguous qualifier that place knowledge
    does not decide. An explicit, unambiguous qualifier always wins over it.
    ``jurisdiction_forms=False`` gives the word reading (see the module docstring).
    """
    return _parse(text, default_country, qualify=True, known=None, forms=jurisdiction_forms)


def parse_observed(
    text: str | None,
    *,
    known: MarketIdentity | None = None,
    jurisdiction_forms: bool = True,
) -> MarketIdentity | None:
    """Parse a market observed in evidence or carried by a request.

    An ambiguous qualifier is resolved only by place knowledge (``KNOWN_CITIES``,
    plus ``known``, a place the caller knows exists - typically the scope it is
    about to compare with). No country is ever supplied that the text does not
    state: a bare "London" stays unqualified and lies within no scope. Jurisdiction
    forms read exactly as in :func:`parse_market`.
    """
    return _parse(text, None, qualify=False, known=known, forms=jurisdiction_forms)


def parse_business_address(text: str | None) -> MarketIdentity | None:
    """What a business's own address (a coverage rule's ``business_address``) states.

    The whole address, read as :func:`parse_observed` reads it, with two differences: a
    comma-free street line ("123 Main St", "3 Kings Ct", "1 Market Pl"; see
    :func:`_is_street_line`) states no place for certain, so its street-type abbreviation is not
    read as a code; and a postcode alone in a later part is left out. What later parts ending in
    a code state, and the ambiguous readings, are in :func:`read_business_address`. Explicit
    geography still counts: a comma-separated component ("123 Main St, Dallas, TX", "10 Rua X,
    ST"), a code ("US-CT"), a name, a bare "PL", and a comma-free text that is not a street
    line ("Hartford CT", "123 Main St Hartford CT").
    """
    return read_business_address(text).stated


def _parse(
    text: str | None,
    country: str | None,
    *,
    qualify: bool,
    known: MarketIdentity | None,
    forms: bool,
    street_address: bool = False,
) -> MarketIdentity | None:
    if text is None:
        return None
    label = text.strip()
    cleaned = _clean_parts(text)
    if not cleaned:
        return None
    if forms:
        reading = _form_reading(cleaned, label, observed=not qualify, address=street_address)
        if street_address and reading is not None and (reading.region or "").endswith("?"):
            reading = _address_agrees(cleaned, label) or reading
        if reading is not None:
            return reading
    parts = [folded for folded, _ in cleaned]
    if len(parts) == 1:
        token = parts[0]
        split = None
        if (
            token not in CITY_FIRST_NAMES
            and not _area_candidates(token)
            and not (street_address and _is_street_line(token))
        ):
            split = _split_trailing_code(token)
        if split is None:
            return _parse_single(token, country, label, qualify=qualify)
        parts = list(split)

    last = parts[-1]
    candidates = _area_candidates(last)
    # The city each reading would give: a country qualifier in "city, region,
    # country" sits one part further from the city than a region qualifier does.
    city_for = {
        c: (parts[-3] if c[1] is None and len(parts) >= 3 else parts[-2]) for c in candidates
    }
    area = _resolve(candidates, city_for, country=country, qualify=qualify, known=known)
    if area is None:
        if not candidates and qualify and country:
            # An unlisted region of the location's own country ("Ikeja, Lagos").
            return _identity(parts[-2], _free_region(last, country, forms=forms), country, label)
        # Unknown or still-ambiguous qualifier: the market stays unqualified.
        return _identity(parts[-2], None, None, label)

    found_country, region = area
    if region is not None:
        return _identity(parts[-2], region, found_country, label)
    if len(parts) >= 3:
        region, _ = normalize_region(parts[-2], found_country, jurisdiction_forms=forms)
        return _identity(parts[-3], region, found_country, label)
    head = parts[0]
    if head not in CITY_FIRST_NAMES:
        for entry_country, key in _region_entries(head):
            if entry_country == found_country:
                return _identity(None, key, found_country, label)
    return _identity(head, None, found_country, label)


def _parse_single(token: str, country: str | None, label: str, *, qualify: bool) -> MarketIdentity:
    if token in CITY_FIRST_NAMES:
        return _identity(token, None, country if qualify else None, label)
    candidates = _area_candidates(token)
    if qualify and country:
        local = [c for c in candidates if c[0] == country]
        if len(local) == 1:
            found_country, region = local[0]
            return _identity(None, region, found_country, label)
        if not local and _country_code(token) is None:
            # A region of another country, or no area at all: a place in this one
            # ("Victoria" for a Canadian location, "Ikeja" for a Nigerian one).
            return _identity(token, None, country, label)
    if len(candidates) == 1:
        found_country, region = candidates[0]
        return _identity(None, region, found_country, label)
    # No area, or an area name with several readings and no city to decide by.
    return _identity(token, None, None, label)


def market_from_parts(
    *,
    city: str | None,
    region: str | None,
    country: str | None,
    jurisdiction_forms: bool = True,
) -> MarketIdentity | None:
    """Identity from separate fields (a location record, a geocoder result)."""
    country_code = normalize_country(country)
    if country_code is None and country:
        # A region typed into the country field ("England", "Ontario").
        _, country_code = normalize_region(country, None)
    region_key, country_code = normalize_region(
        region, country_code, jurisdiction_forms=jurisdiction_forms
    )
    city_folded = _strip_postcode(fold(city))
    if city_folded in _EMPTY_PARTS:
        city_folded = ""
    if not (city_folded or region_key or country_code):
        return None
    label = ", ".join(p.strip() for p in (city, region, country) if p and p.strip())
    return _identity(city_folded, region_key, country_code, label)


def qualify_for_location(text: str | None, *, country: str | None) -> str | None:
    """The market text a Scout tied to a location shows its sources (W16, PROPOSED).

    The sources match the request's market as observed text (:func:`text_within`), which never
    supplies a country the text does not state. For a Scout with a location, the text is the
    customer's for that location, so it is read in the location's country (the rule
    :func:`app.geography.engine.coverage_scopes` already applies): when the text names no country
    and reading it in ``country`` qualifies it in that country, the ISO code is appended
    ("Lagos, Lagos" + NG -> "Lagos, Lagos, NG"). Any other text is returned unchanged: one that
    already names a country (an explicit market wins), one that cannot be read, and every text
    when ``country`` is unknown. Nothing is broadened: the appended code only qualifies what the
    text already names.

    The text is read as words (``jurisdiction_forms=False``): this is the source text, not a
    coverage decision, and it stays what it was before jurisdiction forms were read (5DW2FIX).
    """
    if not text or not text.strip():
        return text
    code = normalize_country(country)
    if code is None:
        return text
    observed = parse_observed(text, jurisdiction_forms=False)
    if observed is not None and observed.country is not None:
        return text
    located = parse_market(text, default_country=code, jurisdiction_forms=False)
    if located is None or located.country != code:
        return text
    return f"{text.strip()}, {code}"


def market_within(candidate: MarketIdentity | None, scope: MarketIdentity | None) -> bool:
    """True only when ``candidate`` is known to lie inside ``scope`` (fail closed).

    The scope must name a country, which the candidate must share. A scope's city
    must be the candidate's. A scope's region must be the candidate's region, except
    that a city outside ``REGION_QUALIFIES_CITY`` countries is identified by city and
    country, so there the region only has to agree when the candidate states one. A region
    the tables cannot place ("US-ZZ", a contradicted "US-TX?") contains nothing and lies in
    no scope.
    """
    if candidate is None or scope is None or not scope.is_qualified:
        return False
    if _unplaced(scope.region) or _unplaced(candidate.region):
        return False
    if candidate.country != scope.country:
        return False
    if scope.city is not None and candidate.city != scope.city:
        return False
    if scope.region is not None and candidate.region != scope.region:
        lenient = scope.city is not None and scope.country not in REGION_QUALIFIES_CITY
        if not (lenient and candidate.region is None):
            return False
    return True


def _unplaced(region: str | None) -> bool:
    """A region key written as a code the tables cannot place ("US-ZZ", "DE-BY")."""
    return region is not None and ":" not in region and region not in _CODED_REGIONS


def may_overlap(candidate: MarketIdentity | None, scope: MarketIdentity | None) -> bool:
    """False only when a component known on both sides differs (for exclusions). A region
    the tables cannot place is not known: it rules out no region of its country."""
    if candidate is None or scope is None:
        return True
    if candidate.country is not None and scope.country is not None:
        if candidate.country != scope.country:
            return False
    if candidate.region is not None and scope.region is not None:
        if candidate.region != scope.region and not (
            _unplaced(candidate.region) or _unplaced(scope.region)
        ):
            return False
    if candidate.city is not None and scope.city is not None:
        if candidate.city != scope.city:
            return False
    return True


def text_within(
    text: str | None, scope: MarketIdentity | None, *, jurisdiction_forms: bool = True
) -> bool:
    """Is the observed market ``text`` within ``scope``? The scope's own city counts
    as place knowledge when reading an ambiguous qualifier, its country does not."""
    if scope is None:
        return False
    return market_within(
        parse_observed(text, known=scope, jurisdiction_forms=jurisdiction_forms), scope
    )
