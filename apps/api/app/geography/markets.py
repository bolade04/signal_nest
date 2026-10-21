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

Ambiguous qualifiers are never guessed. "Perth, WA" (Washington or Western Australia),
"Atlanta, GA" (Georgia or Gabon) and "Tbilisi, Georgia" have no country until the
scoped location's country picks the reading that lies in it; without one they stay
unqualified and therefore match nothing. Text a customer configured for a location
(:func:`parse_market`) is also qualified by that location's country ("Ikeja" for a
Nigerian location); an observed market (:func:`parse_observed`) never is, because a
bare "London" in evidence says nothing about which London.
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


def _pick(candidates: list[_Area], default_country: str | None) -> _Area | None:
    """The single reading of an area token, or ``None`` when absent or ambiguous."""
    if default_country:
        local = [c for c in candidates if c[0] == default_country]
        if len(local) == 1:
            return local[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _split_trailing_code(folded: str) -> tuple[str, str] | None:
    """Split "dallas tx" / "london uk" into (place, code) when no comma separates
    them. Only a final 2-3 letter word that reads as a country or region code is
    split off; full names are not, so "Port of Spain" stays one place."""
    words = folded.split()
    if len(words) >= 2 and 2 <= len(words[-1]) <= 3 and _area_candidates(words[-1]):
        return " ".join(words[:-1]), words[-1]
    return None


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
    chooses between the readings of an ambiguous qualifier and qualifies a bare
    place name ("Ikeja" for a Nigerian location is Ikeja, NG). An explicit,
    unambiguous qualifier always wins over it.
    """
    return _parse(text, default_country, qualify=True)


def parse_observed(text: str | None, *, hint_country: str | None = None) -> MarketIdentity | None:
    """Parse a market observed in evidence or carried by a request.

    ``hint_country`` only chooses between the readings of an ambiguous qualifier
    ("Perth, WA" in Australia); it never supplies a country the text does not
    state, so a bare "London" stays unqualified and lies within no scope.
    """
    return _parse(text, hint_country, qualify=False)


def _parse(text: str | None, country: str | None, *, qualify: bool) -> MarketIdentity | None:
    if text is None:
        return None
    label = text.strip()
    parts = _clean_parts(text)
    if not parts:
        return None
    if len(parts) == 1:
        token = parts[0]
        split = None
        if token not in CITY_FIRST_NAMES and not _area_candidates(token):
            split = _split_trailing_code(token)
        if split is None:
            return _parse_single(token, country, label, qualify=qualify)
        parts = list(split)

    last = parts[-1]
    candidates = _area_candidates(last)
    area = _pick(candidates, country)
    if area is None:
        if not candidates and qualify and country:
            # An unlisted region of the location's own country ("Ikeja, Lagos").
            return _identity(parts[-2], _free_region(last, country), country, label)
        # Unknown or ambiguous qualifier: the market stays unqualified.
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
    area = _pick(candidates, country)
    if qualify and country:
        local = [c for c in candidates if c[0] == country]
        if not local and _country_code(token) is None:
            # A region of another country, or no area at all: a place in this one
            # ("Victoria" for a Canadian location, "Ikeja" for a Nigerian one).
            return _identity(token, None, country, label)
    if area is None:
        return _identity(token, None, None, label)
    found_country, region = area
    return _identity(None, region, found_country, label)


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
    """Is the observed market ``text`` within ``scope``? (:func:`parse_observed`
    with the scope's country as the hint, then :func:`market_within`)."""
    if scope is None:
        return False
    return market_within(parse_observed(text, hint_country=scope.country), scope)
