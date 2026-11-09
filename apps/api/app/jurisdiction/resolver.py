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
  ``jurisdiction_code`` is not what its fields resolve to now.

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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from app.geography.markets import (
    fold,
    normalize_country,
    normalize_region,
    parse_business_address,
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
    why there is none.
    """

    code: str
    codes: tuple[str, ...]
    confidence: float
    evidence: tuple[str, ...]

    @property
    def is_resolved(self) -> bool:
        return self.code != UNRESOLVED and is_jurisdiction_code(self.code)


def _unresolved(evidence: list[str], reason: str) -> JurisdictionResolution:
    return JurisdictionResolution(
        code=UNRESOLVED, codes=(), confidence=0.0, evidence=(*evidence, f"unresolved: {reason}")
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


def _address_check(address: str | None, country: str, code: str) -> tuple[bool, str | None]:
    """``(states a country, conflict)`` for a coverage rule's ``business_address``.

    Only what the address states is compared (``parse_business_address``: no country is
    supplied that the text does not name, and a street line's "St", "Pl" or "Ct" is part of
    the street, not a code). An address naming another country, or another subdivision of a
    country that resolves at subdivision level, contradicts the location.
    """
    stated = parse_business_address(address)
    if stated is None or stated.country is None:
        return False, None
    if stated.country != country:
        return True, f"states country {stated.country}"
    if "-" in code and stated.region is not None:
        # A known subdivision is keyed "US-TX"; any other region "NG:lagos" (markets).
        region = stated.region
        stated_code = (
            region
            if region.startswith(f"{country}-")
            else subdivision_code_of(country, region.split(":", 1)[-1])
        )
        if stated_code is not None and stated_code != code:
            return True, f"states subdivision {stated_code}"
    return True, None


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

    for index, rule in enumerate(coverage_rules):
        address = getattr(rule, "business_address", None)
        states, conflict = _address_check(address, country, code)
        if conflict is not None:
            return _unresolved(
                evidence, f"coverage rule {index} business address {address!r} {conflict}"
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
