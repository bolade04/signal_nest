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

_TOKEN_SYNONYMS: Final = {"saint": "st", "sainte": "ste", "mount": "mt", "fort": "ft"}
#: Placeholders an f-string over a missing field leaves behind ("London, None").
_EMPTY_PARTS: Final = frozenset({"none", "null", "undefined", "n a"})
#: Suffixes dropped from a free-text region name ("Lagos State" == "Lagos").
_REGION_SUFFIXES: Final = (" state", " province", " region")
_HAS_DIGIT = re.compile(r"\d")


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


def _free_region(folded: str, country: str) -> str:
    for suffix in _REGION_SUFFIXES:
        if folded.endswith(suffix) and len(folded) > len(suffix):
            folded = folded[: -len(suffix)]
            break
    return f"{country}:{folded}"


def normalize_region(text: str | None, country: str | None) -> tuple[str | None, str | None]:
    """``(region key, country)`` for a region named within ``country``.

    A known subdivision of ``country`` (by name or abbreviation) gives its code;
    with no country, a subdivision name that exists in exactly one country gives that
    country too. Any other region of a known country is kept by its folded name.
    """
    folded = _strip_postcode(fold(text))
    if not folded or folded in _EMPTY_PARTS:
        return None, country
    entries = _region_entries(folded)
    if country:
        for entry_country, key in entries:
            if entry_country == country:
                return key, country
        return _free_region(folded, country), country
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
    named = [i for i, word in enumerate(street) if not _HAS_DIGIT.search(word)]
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
    names = [word for word in words[number + 1 : -1] if not _HAS_DIGIT.search(word)]
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


@dataclass(frozen=True)
class BusinessAddressReading:
    """What a business address states (see :func:`read_business_address`).

    ``stated``: the whole address read as :func:`parse_observed` reads it, except that a
    comma-free street line states nothing and a postcode alone in a later part is left out.
    ``parts``: what later parts ending in a code state on their own ("..., Dallas TX").
    ``ambiguous``: the code readings of ambiguous street lines and parts ("1500 Broadway
    Hartford CT", "..., Hartford CT"), which a contradicting location may not ignore.
    """

    stated: MarketIdentity | None
    parts: tuple[MarketIdentity, ...] = ()
    ambiguous: tuple[MarketIdentity, ...] = ()
    #: C-8 (F6): the readings of a qualifier that names two or more areas and that place knowledge
    #: leaves undecided ("Dallas, GA": Georgia or Gabon), one group per qualifier; explicit text
    #: (``alternatives``) or an ambiguous street line or part (``ambiguous_alternatives``).
    alternatives: tuple[tuple[MarketIdentity, ...], ...] = ()
    ambiguous_alternatives: tuple[tuple[MarketIdentity, ...], ...] = ()


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
        return _spaced_group(spaced, text)
    parts = _clean_parts(text)
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


def read_business_address(text: str | None) -> BusinessAddressReading:
    """Read a business's own address (a coverage rule's ``business_address``).

    Parts are separated by commas, semicolons, slashes or line breaks (C-10;
    :data:`_PART_SEPARATORS`). An address of one part that
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
    prefix names ("US-CA" is California).
    """
    if text is None:
        return BusinessAddressReading(None)
    raw_parts = _written_parts(text)
    if len(raw_parts) >= 2 and all(_unit_or_postcode(part) for part in raw_parts[1:]):
        # C-6(b): "1500 Broadway CT, Suite 200" and "1 Asylum Hartford CT, 06103" read as their
        # first part, so an ambiguous street line stays ambiguous.
        text, raw_parts = raw_parts[0], raw_parts[:1]
    if len(raw_parts) < 2:
        stated = _parse(text, None, qualify=False, known=None, street_address=True)
        kind = street_line_kind(text)
        ambiguous = parse_observed(text) if kind == STREET_LINE_AMBIGUOUS else None
        return BusinessAddressReading(
            stated,
            (),
            tuple(_placed(ambiguous)),
            _groups(text) if kind is None else (),
            _groups(text) if kind == STREET_LINE_AMBIGUOUS else (),
        )
    kept, parts, ambiguous = [raw_parts[0]], [], []
    groups, ambiguous_groups = [], []
    for part in raw_parts[1:]:
        folded = _strip_postcode(fold(part))
        if all(_HAS_DIGIT.search(word) for word in folded.split()):
            continue
        kept.append(part)
        stated_here, maybe = _part_reading(part)
        parts += stated_here
        ambiguous += maybe
        explicit_here, ambiguous_here = _part_groups(part)
        groups += explicit_here
        ambiguous_groups += ambiguous_here
    stated = _parse(", ".join(kept), None, qualify=False, known=None, street_address=True)
    # C-6 and FU-R1 (ambiguous readings only): a first part whose last word may be a street type or
    # a code ("1500 Broadway Hartford CT, USA", "123 MAIN ST, DALLAS") is ambiguous, never nothing,
    # whatever the later parts state. Explicit geography in the address settles that reading or
    # rules it out (``_address_check``); a town alone ("..., DALLAS") does neither.
    ambiguous = _part_reading(raw_parts[0])[1] + ambiguous
    groups = list(_groups(", ".join(kept))) + groups
    ambiguous_groups = _part_groups(raw_parts[0])[1] + ambiguous_groups
    return BusinessAddressReading(
        stated, tuple(parts), tuple(ambiguous), tuple(groups), tuple(ambiguous_groups)
    )


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
        group = _spaced_group(spaced, part)
        return ([group] if group else []), []
    if (len(town) == 2 and _country_code(town) and not explicit_code) or (
        not explicit_code and _unlisted_code_form(part)
    ):
        return [], []
    if code in STREET_SUFFIXES:
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


def _spaced_group(spaced: tuple[str, str], label: str) -> tuple[MarketIdentity, ...]:
    """C-12: both readings of a spaced code form, the country XX and each reading of YY; empty when
    YY names no area ("AL ULA")."""
    country, letters = spaced
    areas = _area_candidates(letters)
    if not areas:
        return ()
    readings = [(country, None)] + [area for area in areas if area != (country, None)]
    return tuple(MarketIdentity(region=r, country=c, label=label) for c, r in readings)


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
    if (len(town) == 2 and _country_code(town)) or _unlisted_code_form(part):
        return [], []
    if _spaced_code_form(part) is not None:
        return [], []  # C-12: read as a group of both readings (_part_groups)
    if code in STREET_SUFFIXES:
        return [], _placed(parse_observed(part))
    return _placed(parse_observed(part)), []


def _unit_or_postcode(part: str) -> bool:
    """A comma part that is only a postcode ("06103") or a unit ("Suite 200", "Apt 4B")."""
    words = fold(part).split()
    if not words:
        return True
    if all(_HAS_DIGIT.search(word) for word in words):
        return True
    # A label that is also a code ("FL-12" Florida road, "NO 5") is not a unit.
    return (
        len(words) >= 2
        and words[0] in _UNIT_LABELS
        and not _area_candidates(words[0])
        and all(_HAS_DIGIT.search(word) for word in words[1:])
    )


def _placed(identity: MarketIdentity | None) -> list[MarketIdentity]:
    return [identity] if identity is not None and identity.country is not None else []


def _clean_parts(text: str) -> list[str]:
    parts = []
    for raw in text.split(","):
        folded = _strip_postcode(fold(raw))
        if folded and folded not in _EMPTY_PARTS:
            parts.append(folded)
    return parts


def _identity(
    city: str | None, region: str | None, country: str | None, label: str
) -> MarketIdentity:
    return MarketIdentity(city=city or None, region=region, country=country, label=label)


def parse_market(text: str | None, *, default_country: str | None = None) -> MarketIdentity | None:
    """Parse a market a customer configured ("Dallas, TX", "London, Ontario, Canada",
    "Texas", "Ikeja") into an identity; ``None`` when nothing usable remains.

    ``default_country`` is the country of the location the text belongs to. It
    qualifies a bare place name ("Ikeja" for a Nigerian location is Ikeja, NG) and
    chooses between the readings of an ambiguous qualifier that place knowledge
    does not decide. An explicit, unambiguous qualifier always wins over it.
    """
    return _parse(text, default_country, qualify=True, known=None)


def parse_observed(
    text: str | None, *, known: MarketIdentity | None = None
) -> MarketIdentity | None:
    """Parse a market observed in evidence or carried by a request.

    An ambiguous qualifier is resolved only by place knowledge (``KNOWN_CITIES``,
    plus ``known``, a place the caller knows exists - typically the scope it is
    about to compare with). No country is ever supplied that the text does not
    state: a bare "London" stays unqualified and lies within no scope.
    """
    return _parse(text, None, qualify=False, known=known)


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
    street_address: bool = False,
) -> MarketIdentity | None:
    if text is None:
        return None
    label = text.strip()
    parts = _clean_parts(text)
    if not parts:
        return None
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
            return _identity(parts[-2], _free_region(last, country), country, label)
        # Unknown or still-ambiguous qualifier: the market stays unqualified.
        return _identity(parts[-2], None, None, label)

    found_country, region = area
    if region is not None:
        return _identity(parts[-2], region, found_country, label)
    if len(parts) >= 3:
        region, _ = normalize_region(parts[-2], found_country)
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
    *, city: str | None, region: str | None, country: str | None
) -> MarketIdentity | None:
    """Identity from separate fields (a location record, a geocoder result)."""
    country_code = normalize_country(country)
    if country_code is None and country:
        # A region typed into the country field ("England", "Ontario").
        _, country_code = normalize_region(country, None)
    region_key, country_code = normalize_region(region, country_code)
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
    """
    if not text or not text.strip():
        return text
    code = normalize_country(country)
    if code is None:
        return text
    observed = parse_observed(text)
    if observed is not None and observed.country is not None:
        return text
    located = parse_market(text, default_country=code)
    if located is None or located.country != code:
        return text
    return f"{text.strip()}, {code}"


def market_within(candidate: MarketIdentity | None, scope: MarketIdentity | None) -> bool:
    """True only when ``candidate`` is known to lie inside ``scope`` (fail closed).

    The scope must name a country, which the candidate must share. A scope's city
    must be the candidate's. A scope's region must be the candidate's region, except
    that a city outside ``REGION_QUALIFIES_CITY`` countries is identified by city and
    country, so there the region only has to agree when the candidate states one.
    """
    if candidate is None or scope is None or not scope.is_qualified:
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


def may_overlap(candidate: MarketIdentity | None, scope: MarketIdentity | None) -> bool:
    """False only when a component known on both sides differs (for exclusions)."""
    if candidate is None or scope is None:
        return True
    for mine, theirs in (
        (candidate.country, scope.country),
        (candidate.region, scope.region),
        (candidate.city, scope.city),
    ):
        if mine is not None and theirs is not None and mine != theirs:
            return False
    return True


def text_within(text: str | None, scope: MarketIdentity | None) -> bool:
    """Is the observed market ``text`` within ``scope``? The scope's own city counts
    as place knowledge when reading an ambiguous qualifier, its country does not."""
    if scope is None:
        return False
    return market_within(parse_observed(text, known=scope), scope)
