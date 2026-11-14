"""D-N4 item 39: the vendored Unicode data and the primitives built on it
(:mod:`app.claims.unicode_guard`).

* **The data is what it says it is.** Each of the four files is byte-identical to the
  Unicode 15.0.0 file its URL names (its SHA-256 is pinned; the reviewer compares it with the
  published file), its own header names that version, and the licence notice ships beside
  it. A changed, truncated or missing file raises :class:`UnicodeDataError` on load (fail
  closed: the review never matches with less data than it claims).
* **One Unicode version.** The data's version is not older than the runtime's own
  ``unicodedata`` (the NFD and NFKC the skeleton and the matching forms use): a newer Python
  fails here until the data is pinned again.
* **The primitives** read the data as UTS #39 and UAX #24 define it: skeleton prototypes,
  Script and Script_Extensions, Highly Restrictive words, word continuation.
"""

from __future__ import annotations

import hashlib
import shutil
import unicodedata
from pathlib import Path

import pytest

from app.claims import unicode_guard as guard
from app.claims.unicode_guard import (
    DATA_FILES,
    LICENCE_FILE,
    UNICODE_VERSION,
    UnicodeDataError,
    is_highly_restrictive,
    mixed_script_word,
    script_extensions,
    script_of,
    skeleton,
    small_capitals_as_letters,
    word_continues,
    words,
)

_CACHED = (
    guard._confusables,
    guard._script_codes,
    guard._script_ranges,
    guard._script_extensions,
    guard.script_of,
    guard.script_extensions,
)


def _clear_caches() -> None:
    for fn in _CACHED:
        fn.cache_clear()


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


# --------------------------------------------------------------------------- #
# The data
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(DATA_FILES))
def test_each_data_file_is_the_pinned_unicode_15_file(name):
    data = DATA_FILES[name]
    raw = (guard.DATA_DIR / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == data.sha256
    assert data.url.startswith("https://www.unicode.org/Public/")
    assert f"/{UNICODE_VERSION}/" in data.url
    head = raw.decode("utf-8").splitlines()[:8]
    # confusables.txt states "# Version: 15.0.0"; the UCD files name it in their file name.
    assert any(
        line in (f"# Version: {UNICODE_VERSION}", f"# {name[:-4]}-{UNICODE_VERSION}.txt")
        for line in head
    ), head


def test_the_pins_are_the_published_sha256s():
    """The four pins, written out: the SHA-256s of the files fetched from unicode.org once on
    2026-11-14 (capture P6-W21-DN439-01/raw/001 and /002)."""
    assert {name: d.sha256 for name, d in DATA_FILES.items()} == {
        "confusables.txt": "2b10130885c3370b101c52d7baedc452ab7f0e257b86c1e52ee657ecfc29ce64",
        "Scripts.txt": "cca85d830f46aece2e7c1459ef1249993dca8f2e46d51e869255be140d7ea4b0",
        "ScriptExtensions.txt": "7e07313d9d0bee42220c476b64485995130ae30917bbcf7780b602d677d7e33f",
        "PropertyValueAliases.txt": (
            "13a7666843abea5c6b7eb8c057c57ab9bb2ba96cfc936e204224dd67d71cafad"
        ),
    }


def test_the_licence_notice_ships_with_the_data():
    raw = (guard.DATA_DIR / LICENCE_FILE.name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == LICENCE_FILE.sha256
    assert raw.decode("utf-8").startswith("UNICODE LICENSE V3")
    assert sorted(p.name for p in guard.DATA_DIR.iterdir()) == sorted(
        [*DATA_FILES, LICENCE_FILE.name]
    )


def test_the_runtime_unicode_database_is_not_newer_than_the_data():
    """NFD/NFKC come from the runtime; the confusables and scripts from the data. A runtime
    newer than the data would know characters the data cannot map: re-pin the data first."""
    assert _version(unicodedata.unidata_version) <= _version(UNICODE_VERSION), (
        f"runtime Unicode {unicodedata.unidata_version} is newer than the vendored "
        f"{UNICODE_VERSION} data: pin the matching confusables.txt and UCD files"
    )


@pytest.fixture
def data_copy(tmp_path, monkeypatch) -> Path:
    """A private copy of the data directory, read instead of the real one."""
    copy = tmp_path / "unicode_data"
    shutil.copytree(guard.DATA_DIR, copy)
    monkeypatch.setattr(guard, "DATA_DIR", copy)
    _clear_caches()
    yield copy
    _clear_caches()


@pytest.mark.parametrize("name", sorted(DATA_FILES))
def test_a_changed_data_file_fails_closed(data_copy, name):
    path = data_copy / name
    raw = bytearray(path.read_bytes())
    raw[-2] ^= 0x01
    path.write_bytes(bytes(raw))
    with pytest.raises(UnicodeDataError, match="pinned SHA-256"):
        guard.load()


def test_a_truncated_or_missing_data_file_fails_closed(data_copy):
    path = data_copy / "confusables.txt"
    path.write_bytes(path.read_bytes()[:1000])
    with pytest.raises(UnicodeDataError):
        skeleton("free parking")
    path.unlink()
    with pytest.raises(UnicodeDataError, match="cannot be read"):
        skeleton("free parking")


def test_every_script_name_and_every_confusable_parses():
    guard.load()
    assert len(guard._confusables()) == 6311  # the 15.0.0 file's data lines
    starts, ends, codes = guard._script_ranges()
    assert len(starts) == len(ends) == len(codes) > 2000
    assert all(len(code) == 4 for code in codes)
    assert all(s <= e for s, e in zip(starts, ends, strict=True))
    assert all(e < s for e, s in zip(ends, starts[1:], strict=False))  # no overlap


# --------------------------------------------------------------------------- #
# The primitives
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, expected",
    [
        ("\u0435\u0430\u0455\u0443 \u0441\u0430\u0455\u04bb", "easy cash"),  # all Cyrillic
        ("\u039d\u039f \u03a4\u0391\u03a7", "NO TAX"),  # Greek capitals
        ("money", "rnoney"),  # m -> rn
        ("C00L", "COOL"),  # 0 -> O
        ("Il1|", "llll"),  # I, l, 1, | -> l
        ("free parking forever", "free parking forever"),
    ],
)
def test_the_skeleton_maps_through_confusables(text, expected):
    assert skeleton(text) == unicodedata.normalize("NFD", expected)


def test_latin_small_capitals_read_as_their_letters():
    """The 25 code points named "LATIN LETTER SMALL CAPITAL" and one letter (Unicode 15.0); the
    skeleton alone leaves most of them (e.g. U+A730 small capital F) as they are."""
    table = guard._small_capitals()
    assert len(table) == 25
    assert {chr(cp) for cp in table} >= {"\u1d00", "\u0280", "\ua730", "\u0262", "\u1d18"}
    assert small_capitals_as_letters("\ua730\u0280\u1d07\u1d07") == "free"
    assert skeleton("\ua730\u0280\u1d07\u1d07") != "free"
    assert small_capitals_as_letters("FREE free") == "FREE free"


def test_a_name_with_diacritics_keeps_them_in_its_skeleton():
    assert skeleton("Jos\u00e9") != skeleton("Jose")
    assert skeleton("Nguy\u1ec5n") == unicodedata.normalize("NFD", "Nguy\u1ec5n")


@pytest.mark.parametrize(
    "ch, script, extensions",
    [
        ("a", "Latn", {"Latn"}),
        ("\u0430", "Cyrl", {"Cyrl"}),
        ("\u03b1", "Grek", {"Grek"}),
        ("1", "Zyyy", {"Zyyy"}),
        ("\u0301", "Zinh", {"Zinh"}),
        ("\u30fc", "Zyyy", {"Hira", "Kana"}),  # prolonged sound mark: Script_Extensions
        ("\u6771", "Hani", {"Hani"}),
        ("\ud55c", "Hang", {"Hang"}),
        ("\u2800", "Brai", {"Brai"}),
        ("\u0378", "Zzzz", {"Zzzz"}),  # unassigned
    ],
)
def test_script_and_script_extensions_come_from_the_ucd(ch, script, extensions):
    assert script_of(ch) == script
    assert script_extensions(ch) == frozenset(extensions)


@pytest.mark.parametrize(
    "word",
    [
        "parking", "Zo\u00eb", "Nguy\u1ec5n", "\u0391\u03b8\u03ae\u03bd\u03b1",
        "\u041c\u043e\u0441\u043a\u0432\u0430", "\u041c\u043e\u0441\u043a\u0432\u04302024", "Web3",
        "\u6771\u4eac\u30bf\u30ef\u30fc", "iPhone\u5145\u96fb\u5668", "\uc0bc\uc131Galaxy",
        "Pok\u00e9mon\u30ab\u30fc\u30c9", "\u6771\u4eac\uc11c\uc6b8", "\u6ce8\u97f3\u3105\u3106",
        "R2D2", "x_y",
    ],
)
def test_highly_restrictive_words(word):
    assert is_highly_restrictive(word), word


@pytest.mark.parametrize(
    "word",
    [
        "p\u0430rking", "fr\u03b5e", "VK\u043e\u043d\u0442\u0430\u043a\u0442\u0435", "\u0570ello",
        "\u13aapple", "\u6771\u4eac\u041c\u043e\u0441\u043a\u0432\u0430",
        "\u3072\u3089\u304c\u306a\u3105",
    ],
)
def test_words_that_are_not_highly_restrictive(word):
    assert not is_highly_restrictive(word), word
    assert mixed_script_word(f"Visit {word} today") == word


def test_words_are_runs_of_letters_marks_digits_and_connectors():
    assert words("Zo\u00eb's caf\u00e9\u2014open 24/7, free_parking!") == [
        "Zo\u00eb", "s", "caf\u00e9", "open", "24", "7", "free_parking"
    ]


@pytest.mark.parametrize(
    "before, after, continues",
    [
        ("a", "v", True), ("2", "v", True), ("_", "v", True), ("e", "\u0301", True),
        ("\u30ab", "\u30ab", True),  # Katakana continues Katakana (UAX #29 WB13)
        (" ", "v", False), ("-", "v", False), ("'", "v", False), (".", "v", False),
        ("\u6771", "v", False), ("\u3042", "v", False), ("\u0e01", "v", False),
        ("\u30ab", "v", False), ("\u6771", "\u6771", False), ("\u0e01", "\u0e02", False),
    ],
)
def test_word_continuation(before, after, continues):
    assert word_continues(before, after) is continues
