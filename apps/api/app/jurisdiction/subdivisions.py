"""Which countries resolve to a subdivision, and their subdivisions (Project Phase 5D, 5D-W2).

Plain data, no logic. A jurisdiction is the level at which the rules that govern a
location's marketing are made. For most countries that is the country (``GB``, ``KE``).
For the federations listed here it is the first-level subdivision, whose own advertising,
signage and consumer-protection law sits under the national law (``US-TX``, ``NG-LA``).
A location in one of these countries resolves only when its subdivision is known; a
country alone is not enough, so it is ``UNRESOLVED`` (fail closed).

Codes are ISO 3166-2 subdivision codes without the country prefix. The United States,
Canada and Australia reuse the market engine's table (``places_data.SUBDIVISIONS``),
whose codes are the ISO 3166-2 codes. Nigeria has its own table here. It is deliberately
**not** added to ``places_data.SUBDIVISIONS``: there, "Lagos" would start to read as the
state instead of the city, and every Lagos market match would change.

Names are folded (see ``app.geography.markets.fold``).
"""

from __future__ import annotations

from typing import Final

from app.geography.places_data import SUBDIVISIONS

#: ISO 3166-2:NG: the 36 states and the Federal Capital Territory.
_NIGERIA: Final[dict[str, tuple[str, ...]]] = {
    "AB": ("abia",),
    "AD": ("adamawa",),
    "AK": ("akwa ibom",),
    "AN": ("anambra",),
    "BA": ("bauchi",),
    "BE": ("benue",),
    "BO": ("borno",),
    "BY": ("bayelsa",),
    "CR": ("cross river",),
    "DE": ("delta",),
    "EB": ("ebonyi",),
    "ED": ("edo",),
    "EK": ("ekiti",),
    "EN": ("enugu",),
    "FC": ("federal capital territory", "fct", "abuja", "abuja fct"),
    "GO": ("gombe",),
    "IM": ("imo",),
    "JI": ("jigawa",),
    "KD": ("kaduna",),
    "KE": ("kebbi",),
    "KN": ("kano",),
    "KO": ("kogi",),
    "KT": ("katsina",),
    "KW": ("kwara",),
    "LA": ("lagos",),
    "NA": ("nasarawa", "nassarawa"),
    "NI": ("niger",),
    "OG": ("ogun",),
    "ON": ("ondo",),
    "OS": ("osun",),
    "OY": ("oyo",),
    "PL": ("plateau",),
    "RI": ("rivers",),
    "SO": ("sokoto",),
    "TA": ("taraba",),
    "YO": ("yobe",),
    "ZA": ("zamfara",),
}

#: Countries whose jurisdiction is a first-level subdivision: country -> {code: names}.
#: Every other country resolves to the country itself.
SUBDIVISION_JURISDICTIONS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "US": SUBDIVISIONS["US"],
    "CA": SUBDIVISIONS["CA"],
    "AU": SUBDIVISIONS["AU"],
    "NG": _NIGERIA,
}

__all__ = ["SUBDIVISION_JURISDICTIONS"]
