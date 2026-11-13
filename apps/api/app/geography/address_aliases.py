"""Bounded alias lists for the business-address reader (GEO-P3, geography register G-10).

Operator ruling of 2026-11-12 (G-10, option (a)): the conventional US, Canadian and Australian
abbreviations of a state's, province's or territory's name ("Ala.", "Calif.", "Ont.", "Qld"), and a
few country aliases ("Deutschland", "Federal Republic of Nigeria", "Commonwealth of Australia",
"Estados Unidos Mexicanos"). Plain data, no logic; :mod:`app.geography.markets` reads it.

Each entry records the alias as its source writes it, its canonical ISO 3166 code (ISO 3166-2 for a
subdivision, ISO 3166-1 alpha-2 for a country), its source and how the reader handles it:

* :data:`ALIAS_NAME`: an alias of one area whose letters the tables do not read ("Ala.", "Ont.",
  "Deutschland"). A part that is the alias reads as the area's name would ("Alabama", "Ontario",
  "Germany").
* :data:`ALIAS_READ`: an alias whose letters, without periods or spaces, the tables already read as
  a code or a name that includes its area ("Ga.": GA is Gabon or US-GA; "N. Y."; "Qld"; "P.E.I.").
  It is read as before; its initials are joined ("N. Y." as "N.Y."). Where the letters name two
  or more areas ("Ga.", "La.", "Pa.", "N.L.", "NT", "SA", "WA") the existing C-8 group stands
  unchanged.
* :data:`ALIAS_ASKS`: a name that its source gives for two or more areas ("Korea": South Korea or
  North Korea). It is never one of them: an address with it asks when one reading agrees with the
  location and another contradicts, and is refused when every reading contradicts. Neither the
  location nor place knowledge settles it; an explicit statement of one area elsewhere in the
  address does.

Only a whole part is an alias (between commas, semicolons, slashes or line breaks, before any
postcode): "Montgomery, Ala." and "Toronto, Ont. M5V 2T6", not "Montgomery Ala.". The period is part
of an alias that its source writes with one: "Ala", "Miss", "Man", "Ind", "Wash" and "Del" are words
or names and are not read. Only the business-address reader reads these lists; exclusions, coverage
rules and observed markets read text as before. There is no city table.

Sources: :data:`SOURCES`, one per block, each a public page fetched on 2026-11-12; a dated text
snapshot of each is kept with the SHA-256 recorded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: An alias of one area that the tables do not read: read as that area's name.
ALIAS_NAME: Final = "name"
#: An alias whose letters the tables already read (as a code or a name that includes its area).
ALIAS_READ: Final = "read"
#: A name of two or more areas: a group that asks.
ALIAS_ASKS: Final = "asks"


@dataclass(frozen=True)
class AliasSource:
    """Where a block of aliases comes from."""

    title: str
    url: str
    retrieved: str
    snapshot_sha256: str
    note: str = ""


@dataclass(frozen=True)
class AddressAlias:
    """One alias: ``written`` as its source writes it; ``codes``: its ISO 3166 code (two or more for
    :data:`ALIAS_ASKS`); ``name``: the area's name as the tables read it (:data:`ALIAS_NAME` only);
    ``source``: a key of :data:`SOURCES`; ``handling``: :data:`ALIAS_NAME`, :data:`ALIAS_READ` or
    :data:`ALIAS_ASKS`."""

    written: str
    codes: tuple[str, ...]
    name: str | None
    source: str
    handling: str


SOURCES: Final[dict[str, AliasSource]] = {
    "usps": AliasSource(
        title=(
            "United States Postal Service, Postal History: 'Postal abbreviations for states/"
            "territories' (columns 1874 and 1943: United States Official Postal Guide)"
        ),
        url="https://about.usps.com/who/profile/history/state-abbreviations.htm",
        retrieved="2026-11-12",
        snapshot_sha256="154ddbf1babae7e5c329a1b4d545b6c8e8a15c56a653f6fadbe89f0ac9db8124",
        note=(
            "The 1874 and 1943 columns' abbreviations with a period. The 1831 column is left out "
            "(superseded forms that name other states later: 'Ia.' for Indiana, 'Ms.' for "
            "Massachusetts), as is June 1963 (in use four months; capital words such as 'MASS', "
            "'WASH', 'IND'). The October 1963 two-letter codes are already read."
        ),
    ),
    "canada-translation-bureau": AliasSource(
        title=(
            "Government of Canada, Translation Bureau, Language Portal of Canada: 'Abbreviations: "
            "Canadian provinces and territories' (traditional abbreviations)"
        ),
        url=(
            "https://www.noslangues-ourlanguages.gc.ca/en/writing-tips-plus/"
            "abbreviations-canadian-provinces-and-territories"
        ),
        retrieved="2026-11-12",
        snapshot_sha256="a8da1d21160fd90bb33bca51895105c7e23823bec8c8e474b7f42f9fde37e220",
        note=(
            "Read through the Internet Archive's 2025 capture (https://web.archive.org/web/2025/"
            "<url>): the site refuses automated requests. Nunavut has no traditional abbreviation."
        ),
    ),
    "abs": AliasSource(
        title=(
            "Australian Bureau of Statistics, 2909.0 'An Australian Guide to Geographic "
            "Classifications', Abbreviations"
        ),
        url="https://www.abs.gov.au/AUSSTATS/abs@.nsf/Lookup/2909.0Abbreviations12006",
        retrieved="2026-11-12",
        snapshot_sha256="2bdb8624c6e18954516b202f098fd0f6a64aebd7b0ce36717ae7e956f675c885",
    ),
    "eu-style-guide-en": AliasSource(
        title=(
            "Publications Office of the European Union, Interinstitutional Style Guide, Annex A5 "
            "'List of countries, territories and currencies' (English)"
        ),
        url="https://publications.europa.eu/code/en/en-5000500.htm",
        retrieved="2026-11-12",
        snapshot_sha256="08373ce6bc23a8df42e0a4054701da92aa8d41be8ef74ff93b4c1476bad8dbd8",
        note=(
            "Read through the Internet Archive's 2019 capture (https://web.archive.org/web/2019/"
            "<url>). Rows: 'Australia | Commonwealth of Australia | AU', 'Nigeria | Federal "
            "Republic of Nigeria | NG', 'Korea: see North Korea, South Korea', 'North Korea | "
            "Democratic People's Republic of Korea | KP', 'South Korea | Republic of Korea | KR'."
        ),
    ),
    "eu-style-guide-de": AliasSource(
        title="Interinstitutional Style Guide, Annex A5 (German edition)",
        url="https://publications.europa.eu/code/de/de-5000500.htm",
        retrieved="2026-11-12",
        snapshot_sha256="29cc2156f44c736255506b4dfd2a2eb88c90200d62f3553a5fadfd5ed7af7517",
        note=(
            "Internet Archive 2019 capture. Row: 'Deutschland | die Bundesrepublik Deutschland "
            "| DE'."
        ),
    ),
    "eu-style-guide-es": AliasSource(
        title="Interinstitutional Style Guide, Annex A5 (Spanish edition)",
        url="https://publications.europa.eu/code/es/es-5000500.htm",
        retrieved="2026-11-12",
        snapshot_sha256="cb17fa80576db472e400ae0878f35d1ffce954804d571e686d3ac338e039620c",
        note="Internet Archive 2019 capture. Row: 'México | Estados Unidos Mexicanos | MX'.",
    ),
}


def _us(written: str, code: str, name: str | None, handling: str = ALIAS_NAME) -> AddressAlias:
    return AddressAlias(written, (code,), name, "usps", handling)


def _ca(written: str, code: str, name: str | None, handling: str = ALIAS_NAME) -> AddressAlias:
    return AddressAlias(written, (code,), name, "canada-translation-bureau", handling)


def _au(written: str, code: str) -> AddressAlias:
    return AddressAlias(written, (code,), None, "abs", ALIAS_READ)


#: US state and territory abbreviations: USPS, 1874 and 1943 columns (both, unless one is named).
US_ABBREVIATIONS: Final[tuple[AddressAlias, ...]] = (
    _us("Ala.", "US-AL", "Alabama"),
    _us("Ariz.", "US-AZ", "Arizona"),
    _us("Ark.", "US-AR", "Arkansas"),
    _us("Cal.", "US-CA", "California"),  # 1874
    _us("Calif.", "US-CA", "California"),  # 1943
    _us("Colo.", "US-CO", "Colorado"),
    _us("Conn.", "US-CT", "Connecticut"),
    _us("Del.", "US-DE", "Delaware"),
    _us("D. C.", "US-DC", None, ALIAS_READ),  # "DC"
    _us("Fla.", "US-FL", "Florida"),
    _us("Ga.", "US-GA", None, ALIAS_READ),  # "GA": Gabon or US-GA (C-8)
    _us("Ill.", "US-IL", "Illinois"),
    _us("Ind.", "US-IN", "Indiana"),
    _us("Kans.", "US-KS", "Kansas"),
    _us("Ky.", "US-KY", None, ALIAS_READ),  # "KY": Cayman Islands or US-KY (C-8)
    _us("La.", "US-LA", None, ALIAS_READ),  # "LA": Laos, US-LA, and Lagos State (N-1)
    _us("Me.", "US-ME", None, ALIAS_READ),  # 1874; "ME": Montenegro or US-ME (C-8)
    _us("Md.", "US-MD", None, ALIAS_READ),  # "MD": Moldova or US-MD (C-8)
    _us("Mass.", "US-MA", "Massachusetts"),
    _us("Mich.", "US-MI", "Michigan"),
    _us("Minn.", "US-MN", "Minnesota"),
    _us("Miss.", "US-MS", "Mississippi"),
    _us("Mo.", "US-MO", None, ALIAS_READ),  # "MO": Macao or US-MO (C-8)
    _us("Mont.", "US-MT", "Montana"),
    _us("Nebr.", "US-NE", "Nebraska"),
    _us("Nev.", "US-NV", "Nevada"),
    _us("N. H.", "US-NH", None, ALIAS_READ),  # "NH"
    _us("N. J.", "US-NJ", None, ALIAS_READ),  # "NJ"
    _us("N. Mex.", "US-NM", "New Mexico"),
    _us("N. Y.", "US-NY", None, ALIAS_READ),  # "NY"
    _us("N. C.", "US-NC", None, ALIAS_READ),  # "NC": New Caledonia or US-NC (C-8)
    _us("N. Dak.", "US-ND", "North Dakota"),  # 1943
    _us("Okla.", "US-OK", "Oklahoma"),  # 1943
    _us("Oreg.", "US-OR", "Oregon"),
    _us("Pa.", "US-PA", None, ALIAS_READ),  # "PA": Panama or US-PA (C-8)
    # 1943. ISO 3166-1 PR (also ISO 3166-2 US-PR); the tables read Puerto Rico as the country PR.
    AddressAlias("P. R.", ("PR",), None, "usps", ALIAS_READ),
    _us("R. I.", "US-RI", None, ALIAS_READ),  # "RI": US-RI, and Rivers State (I-6)
    _us("S. C.", "US-SC", None, ALIAS_READ),  # "SC": Seychelles or US-SC (C-8)
    _us("S. Dak.", "US-SD", "South Dakota"),  # 1943
    _us("Tenn.", "US-TN", "Tennessee"),
    _us("Tex.", "US-TX", "Texas"),
    _us("Vt.", "US-VT", None, ALIAS_READ),  # "VT"
    _us("Va.", "US-VA", None, ALIAS_READ),  # "VA": Vatican City or US-VA (C-8)
    _us("Wash.", "US-WA", "Washington"),
    _us("W. Va.", "US-WV", "West Virginia"),
    _us("Wis.", "US-WI", "Wisconsin"),
    _us("Wyo.", "US-WY", "Wyoming"),
)

#: Canadian provinces' and territories' traditional abbreviations: Translation Bureau.
CA_ABBREVIATIONS: Final[tuple[AddressAlias, ...]] = (
    _ca("Alta.", "CA-AB", "Alberta"),
    _ca("B.C.", "CA-BC", None, ALIAS_READ),  # "BC"
    _ca("Man.", "CA-MB", "Manitoba"),
    _ca("N.B.", "CA-NB", None, ALIAS_READ),  # "NB"
    _ca("N.L.", "CA-NL", None, ALIAS_READ),  # "NL": Netherlands or CA-NL (C-8)
    _ca("N.W.T.", "CA-NT", "Northwest Territories"),
    _ca("N.S.", "CA-NS", None, ALIAS_READ),  # "NS"
    _ca("Ont.", "CA-ON", "Ontario"),
    _ca("P.E.I.", "CA-PE", None, ALIAS_READ),  # "PEI", already a name of CA-PE
    _ca("Que.", "CA-QC", "Quebec"),
    _ca("Sask.", "CA-SK", "Saskatchewan"),
    _ca("Yk.", "CA-YT", "Yukon"),
)

#: Australian states' and territories' abbreviations: ABS 2909.0. Each is already a code the tables
#: read ("NT": CA-NT or AU-NT, "SA": Saudi Arabia or AU-SA, "WA": US-WA or AU-WA, as C-8 groups).
AU_ABBREVIATIONS: Final[tuple[AddressAlias, ...]] = (
    _au("ACT", "AU-ACT"),
    _au("NSW", "AU-NSW"),
    _au("NT", "AU-NT"),
    _au("Qld", "AU-QLD"),
    _au("SA", "AU-SA"),
    _au("Tas.", "AU-TAS"),
    _au("Vic.", "AU-VIC"),
    _au("WA", "AU-WA"),
)

#: Country aliases: the Interinstitutional Style Guide, Annex A5.
COUNTRY_ALIASES: Final[tuple[AddressAlias, ...]] = (
    AddressAlias("Deutschland", ("DE",), "Germany", "eu-style-guide-de", ALIAS_NAME),
    AddressAlias(
        "Federal Republic of Nigeria", ("NG",), "Nigeria", "eu-style-guide-en", ALIAS_NAME
    ),
    AddressAlias(
        "Commonwealth of Australia", ("AU",), "Australia", "eu-style-guide-en", ALIAS_NAME
    ),
    AddressAlias("Estados Unidos Mexicanos", ("MX",), "Mexico", "eu-style-guide-es", ALIAS_NAME),
    # "Korea: see 'North Korea', 'South Korea'": never one of them by itself.
    AddressAlias("Korea", ("KR", "KP"), None, "eu-style-guide-en", ALIAS_ASKS),
)

#: Every alias the business-address reader reads.
ADDRESS_ALIASES: Final[tuple[AddressAlias, ...]] = (
    US_ABBREVIATIONS + CA_ABBREVIATIONS + AU_ABBREVIATIONS + COUNTRY_ALIASES
)
