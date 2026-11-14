"""Unicode protections for protected claims (Project Phase 5C; operator ruling D-N4 item 39).

**The ruling** (2026-11-12, verbatim): "implement word-boundary checks, the blank-character
protection and reviewed confusable detection before real-provider activation. Cover
mixed-script and whole-script substitutions of protected claims. Do not defer that protection
solely because multilingual output is deferred. Preserve original text and test legitimate
names and false positives."

This module holds the Unicode primitives. The matching rules built on them are in
:mod:`app.claims.engine` (:func:`~app.claims.engine.contains_claim` for a blocked claim,
:func:`~app.claims.engine.carries_phrase` for a required disclaimer), and the withholding rules
in :func:`app.llm.screening.screen_creative`. **Nothing here changes a stored text**: every
function returns a verdict or a new string that is used for matching only.

* **Blank characters** (:data:`WITHHELD_BLANKS`, :func:`withheld_blank`). A code point that
  renders as a blank or as nothing, is not whitespace, and is removed neither from the 5C
  persisted form (``app.llm.screening._display``) nor from the matching form
  (``app.llm.untrusted.normalise``), so it reaches the stored draft and its reader: U+2800
  BRAILLE PATTERN BLANK, U+13441 and U+13442 EGYPTIAN HIEROGLYPH FULL and HALF BLANK, U+1D159
  MUSICAL SYMBOL NULL NOTEHEAD, U+16FE4 KHITAN SMALL SCRIPT FILLER. "free" + U+2800 +
  "parking" reads as "free parking" but is not that text, so a draft holding any of them is
  withheld. The Hangul fillers (U+3164, U+FFA0, U+115F, U+1160) also render as blanks, but both
  forms already remove them (they are ``Default_Ignorable_Code_Point``), so they never reach a
  stored draft; the
  zero-width characters (U+200B-U+200D, U+2060, U+FEFF, U+00AD, U+034F, U+180E) render as
  nothing and are removed for matching, which is what a reader sees, and each has ordinary uses
  (ZWJ emoji sequences, ZWNJ in Persian and Indic words, soft hyphens). Text that keeps a blank
  (a library entry, or a draft stored before this rule) is also read with every blank of
  :data:`BLANKS_READ_AS_SPACE` as a space (:func:`blanks_as_spaces`), as it reads.
* **Words** (:func:`words`, :func:`word_continues`). A word character is one of General_Category
  L (letter), M (mark), N (number) or Pc (connector punctuation, ``_``). For the word-boundary
  check, two adjacent characters continue one word when both are word characters of a script
  written with spaces between words, or both are Katakana: the default word boundaries of
  UAX #29 for these cases. Han, Hiragana and the scripts UAX #29 does not segment without a
  dictionary (Line_Break=SA: Thai, Lao, Myanmar, Khmer, the Tai scripts, Ahom; and the
  ideographic Tangut, Nushu and Khitan) never continue a word, so next to them every position
  is a boundary.
* **Mixed script within a word** (:func:`mixed_script_word`, :func:`is_highly_restrictive`). A
  word passes when it meets the script condition of UTS #39 §5.2 *Highly Restrictive*: its
  resolved script set (§5.1: each character's Script_Extensions, Common and Inherited counting
  as every script, Han augmented with Hanb/Jpan/Kore, Hiragana and Katakana with Jpan, Hangul
  with Kore, Bopomofo with Hanb) is not empty, or it is covered by Latin + Han + Hiragana +
  Katakana, Latin + Han + Bopomofo or Latin + Han + Hangul. So "東京タワー", "iPhone充電器",
  "삼성Galaxy", "Москва2024" and "Web3" pass, and "parking" spelt with a Cyrillic
  "a" (U+0430) does not. The
  identifier-profile half of the level is for identifiers and does not apply to free text.
* **Confusable skeleton** (:func:`skeleton`): UTS #39 §4, NFD, every character replaced by its
  prototype in ``confusables.txt``, NFD again. Applied only to text from which every
  ``Default_Ignorable_Code_Point`` has already been removed.
* **Latin small capitals** (:func:`small_capitals_as_letters`), for detection only: each code
  point named "LATIN LETTER SMALL CAPITAL" and one letter (25 in Unicode 15.0, from the
  runtime's own character names) reads as that letter. ``confusables.txt`` maps only the ten
  shaped like a lower-case letter (U+1D04 C, U+1D0F O, U+1D20 V, ...) and NFKC none, yet a word
  written wholly in small capitals is a single-script word that reads as the plain word, as
  NFKC already makes a mathematical, full-width or circled letter read.

**Data.** Unmodified copies of four Unicode 15.0.0 data files are in ``unicode_data/``
(:data:`DATA_FILES`: name, source URL and SHA-256 of each). Every load checks the SHA-256 and
raises :class:`UnicodeDataError` on a mismatch, so a changed or truncated file fails closed
instead of matching less. They are © 2022 Unicode, Inc., used under the Unicode License v3,
whose text is ``unicode_data/LICENSE-UNICODE.txt`` (:data:`LICENCE_FILE`). Version 15.0.0 is the
Unicode version of the runtime's own ``unicodedata`` (Python 3.12, in CI and in the image), so
NFD, NFKC, General_Category, the scripts and the confusables all come from one version, as
UTS #39 defines the skeleton. A runtime with a newer Unicode database fails
``test_dn4_39_unicode_data`` until the data is pinned again.
"""

from __future__ import annotations

import hashlib
import re
import sys
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

#: The Unicode version of every data file below.
UNICODE_VERSION = "15.0.0"
#: Where the data files are, beside this module.
DATA_DIR = Path(__file__).with_name("unicode_data")


@dataclass(frozen=True)
class DataFile:
    """One vendored Unicode data file: its name in :data:`DATA_DIR`, where it was fetched from
    and the SHA-256 of its bytes (checked on every load)."""

    name: str
    url: str
    sha256: str


#: The vendored data files, unmodified, fetched once from unicode.org on 2026-11-14.
DATA_FILES: dict[str, DataFile] = {
    f.name: f
    for f in (
        DataFile(
            "confusables.txt",
            "https://www.unicode.org/Public/security/15.0.0/confusables.txt",
            "2b10130885c3370b101c52d7baedc452ab7f0e257b86c1e52ee657ecfc29ce64",
        ),
        DataFile(
            "Scripts.txt",
            "https://www.unicode.org/Public/15.0.0/ucd/Scripts.txt",
            "cca85d830f46aece2e7c1459ef1249993dca8f2e46d51e869255be140d7ea4b0",
        ),
        DataFile(
            "ScriptExtensions.txt",
            "https://www.unicode.org/Public/15.0.0/ucd/ScriptExtensions.txt",
            "7e07313d9d0bee42220c476b64485995130ae30917bbcf7780b602d677d7e33f",
        ),
        DataFile(
            "PropertyValueAliases.txt",
            "https://www.unicode.org/Public/15.0.0/ucd/PropertyValueAliases.txt",
            "13a7666843abea5c6b7eb8c057c57ab9bb2ba96cfc936e204224dd67d71cafad",
        ),
    )
}
#: The licence the data files are used under (Unicode License v3), as published.
LICENCE_FILE = DataFile(
    "LICENSE-UNICODE.txt",
    "https://www.unicode.org/license.txt",
    "e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96",
)

#: Blanks that reach a stored draft (module docstring): a draft holding one is withheld.
WITHHELD_BLANKS: frozenset[str] = frozenset(
    {"\u2800", "\U00013441", "\U00013442", "\U0001d159", "\U00016fe4"}
)
#: Every blank read as a space by :func:`blanks_as_spaces`: the withheld blanks and the Hangul
#: fillers (which the persisted and matching forms remove).
BLANKS_READ_AS_SPACE: frozenset[str] = WITHHELD_BLANKS | {"\u3164", "\uffa0", "\u115f", "\u1160"}
_AS_SPACE = str.maketrans({ch: " " for ch in BLANKS_READ_AS_SPACE})

#: Script values that place no constraint on a word (UTS #39 §5.1: Common, Inherited).
_ANY_SCRIPT = frozenset({"Zyyy", "Zinh"})
#: UTS #39 §5.1 augmentation of a character's script set.
_AUGMENT: dict[str, tuple[str, ...]] = {
    "Hani": ("Hanb", "Jpan", "Kore"),
    "Hira": ("Jpan",),
    "Kana": ("Jpan",),
    "Hang": ("Kore",),
    "Bopo": ("Hanb",),
}
#: UTS #39 §5.2 Highly Restrictive: the script sets that may cover one word.
HIGHLY_RESTRICTIVE_COVERS: tuple[frozenset[str], ...] = (
    frozenset({"Latn", "Hani", "Hira", "Kana"}),
    frozenset({"Latn", "Hani", "Bopo"}),
    frozenset({"Latn", "Hani", "Hang"}),
)
#: Scripts whose letters never continue a word for the word-boundary check (module docstring).
NO_SPACE_SCRIPTS: frozenset[str] = frozenset(
    {"Hani", "Hira", "Tang", "Nshu", "Kits", "Thai", "Laoo", "Mymr", "Khmr", "Tale", "Talu",
     "Lana", "Tavt", "Ahom"}
)


class UnicodeDataError(RuntimeError):
    """A vendored Unicode data file is missing or does not match its pinned SHA-256."""


def _text(data: DataFile) -> str:
    try:
        raw = (DATA_DIR / data.name).read_bytes()
    except OSError as exc:
        raise UnicodeDataError(f"{data.name} cannot be read") from exc
    if hashlib.sha256(raw).hexdigest() != data.sha256:
        raise UnicodeDataError(f"{data.name} does not match its pinned SHA-256")
    return raw.decode("utf-8")


def _rows(name: str) -> list[list[str]]:
    """The data lines of ``name``: comments removed, fields split on ``;`` and stripped."""
    rows = []
    for line in _text(DATA_FILES[name]).splitlines():
        content = line.split("#", 1)[0].strip()
        if content:
            rows.append([field.strip() for field in content.split(";")])
    return rows


def _code_points(field: str) -> range:
    first, _, last = field.partition("..")
    return range(int(first, 16), int(last or first, 16) + 1)


@cache
def _confusables() -> dict[str, str]:
    """``confusables.txt``: each source character → its prototype (one or more characters)."""
    table: dict[str, str] = {}
    for source, target, *_ in _rows("confusables.txt"):
        table[chr(int(source, 16))] = "".join(chr(int(cp, 16)) for cp in target.split())
    return table


@cache
def _script_codes() -> dict[str, str]:
    """``PropertyValueAliases.txt``: each Script value's long name → its four-letter code."""
    return {row[2]: row[1] for row in _rows("PropertyValueAliases.txt") if row[0] == "sc"}


@cache
def _script_ranges() -> tuple[list[int], list[int], list[str]]:
    """``Scripts.txt`` as sorted ranges: (starts, ends, four-letter codes)."""
    codes = _script_codes()
    ranges = []
    for cp, long_name, *_ in _rows("Scripts.txt"):
        span = _code_points(cp)
        ranges.append((span.start, span.stop - 1, codes[long_name]))
    ranges.sort()
    return [r[0] for r in ranges], [r[1] for r in ranges], [r[2] for r in ranges]


@cache
def _script_extensions() -> dict[int, frozenset[str]]:
    """``ScriptExtensions.txt``: code point → its Script_Extensions (four-letter codes)."""
    table: dict[int, frozenset[str]] = {}
    for cp, values, *_ in _rows("ScriptExtensions.txt"):
        scripts = frozenset(values.split())
        for point in _code_points(cp):
            table[point] = scripts
    return table


def load() -> None:
    """Read and check every data file now (each is otherwise read on first use)."""
    _confusables()
    _script_ranges()
    _script_extensions()


@lru_cache(maxsize=4096)
def script_of(ch: str) -> str:
    """The Script property of ``ch`` as a four-letter code; ``Zzzz`` (Unknown) when unlisted."""
    starts, ends, codes = _script_ranges()
    cp = ord(ch)
    i = bisect_right(starts, cp) - 1
    return codes[i] if i >= 0 and cp <= ends[i] else "Zzzz"


@lru_cache(maxsize=4096)
def script_extensions(ch: str) -> frozenset[str]:
    """The Script_Extensions of ``ch``: its listed set, else its Script alone."""
    return _script_extensions().get(ord(ch)) or frozenset({script_of(ch)})


def skeleton(text: str) -> str:
    """The UTS #39 skeleton of ``text``: NFD, each character replaced by its
    ``confusables.txt`` prototype, NFD again. For matching only."""
    table = _confusables()
    mapped = "".join(table.get(ch, ch) for ch in unicodedata.normalize("NFD", text))
    return unicodedata.normalize("NFD", mapped)


_SMALL_CAPITAL = re.compile(r"LATIN LETTER SMALL CAPITAL ([A-Z])")


@cache
def _small_capitals() -> dict[int, str]:
    """Every Latin small capital (by its Unicode name) → its lower-case letter."""
    table: dict[int, str] = {}
    for cp in range(sys.maxunicode + 1):
        found = _SMALL_CAPITAL.fullmatch(unicodedata.name(chr(cp), ""))
        if found:
            table[cp] = found.group(1).lower()
    return table


def small_capitals_as_letters(text: str) -> str:
    """``text`` with every Latin small capital read as its letter. For matching only."""
    return text.translate(_small_capitals())


def withheld_blank(text: str) -> str | None:
    """The first character of ``text`` in :data:`WITHHELD_BLANKS`, or None."""
    return next((ch for ch in text if ch in WITHHELD_BLANKS), None)


def blanks_as_spaces(text: str) -> str:
    """``text`` with every blank of :data:`BLANKS_READ_AS_SPACE` read as a space."""
    return text.translate(_AS_SPACE)


def is_word_character(ch: str) -> bool:
    """General_Category L, M, N or Pc."""
    category = unicodedata.category(ch)
    return category[0] in "LMN" or category == "Pc"


def _word_class(ch: str) -> str | None:
    """``"word"`` or ``"kana"`` for a character that can continue a word, else None."""
    if not is_word_character(ch):
        return None
    scripts = script_extensions(ch)
    if "Kana" in scripts:
        return "kana"
    if scripts & NO_SPACE_SCRIPTS:
        return None
    return "word"


def word_continues(before: str, after: str) -> bool:
    """Whether ``before`` and ``after``, adjacent, are inside one word: no word boundary
    between them (module docstring)."""
    kind = _word_class(before)
    return kind is not None and kind == _word_class(after)


def words(text: str) -> list[str]:
    """The maximal runs of word characters in ``text``."""
    found: list[str] = []
    current: list[str] = []
    for ch in text:
        if is_word_character(ch):
            current.append(ch)
        elif current:
            found.append("".join(current))
            current = []
    if current:
        found.append("".join(current))
    return found


def _augmented(ch: str) -> frozenset[str] | None:
    """The augmented script set of ``ch`` (UTS #39 §5.1); None for "every script"."""
    scripts = script_extensions(ch)
    if scripts & _ANY_SCRIPT:
        return None
    return scripts.union(*(_AUGMENT.get(s, ()) for s in scripts))


def is_highly_restrictive(word: str) -> bool:
    """Whether ``word`` meets the script condition of UTS #39 Highly Restrictive."""
    sets = [s for s in map(_augmented, word) if s is not None]
    if not sets or frozenset.intersection(*sets):
        return True
    return any(all(s & cover for s in sets) for cover in HIGHLY_RESTRICTIVE_COVERS)


def mixed_script_word(text: str) -> str | None:
    """The first word of ``text`` that is not Highly Restrictive, or None."""
    return next((w for w in words(text) if not is_highly_restrictive(w)), None)


__all__ = [
    "BLANKS_READ_AS_SPACE",
    "DATA_DIR",
    "DATA_FILES",
    "HIGHLY_RESTRICTIVE_COVERS",
    "LICENCE_FILE",
    "NO_SPACE_SCRIPTS",
    "UNICODE_VERSION",
    "WITHHELD_BLANKS",
    "DataFile",
    "UnicodeDataError",
    "blanks_as_spaces",
    "is_highly_restrictive",
    "is_word_character",
    "load",
    "mixed_script_word",
    "script_extensions",
    "script_of",
    "skeleton",
    "small_capitals_as_letters",
    "withheld_blank",
    "word_continues",
    "words",
]
