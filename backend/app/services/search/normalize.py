"""Text normalization and script-aware romanization.

Every optional romanization backend is imported defensively: a missing wheel must degrade
that one script to `unidecode` rather than take the whole app down at import time.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger(__name__)

# --- Optional backends ---------------------------------------------------------------------

# Declared up front as `Any` so the "absent wheel" branch can null them out without mypy
# treating the fallback as a type error; every use site tests for None first.
_lazy_pinyin: Any = None
_PinyinStyle: Any = None
_Romanizer: Any = None
_kakasi: Any = None
_unidecode: Any = None

try:  # Chinese
    from pypinyin import Style as _pinyin_style
    from pypinyin import lazy_pinyin as _pinyin_fn

    _lazy_pinyin = _pinyin_fn
    _PinyinStyle = _pinyin_style
except Exception:  # pragma: no cover - exercised only when the wheel is absent
    log.warning("pypinyin unavailable; Han titles fall back to unidecode")

try:  # Korean
    from korean_romanizer.romanizer import Romanizer as _romanizer_cls

    _Romanizer = _romanizer_cls
except Exception:  # pragma: no cover
    log.warning("korean-romanizer unavailable; Hangul titles fall back to unidecode")

try:  # Japanese
    import pykakasi as _pykakasi

    _kakasi = _pykakasi.kakasi()
except Exception:  # pragma: no cover
    log.warning("pykakasi unavailable; Japanese titles fall back to unidecode")

try:
    from unidecode import unidecode as _unidecode_fn

    _unidecode = _unidecode_fn
except Exception:  # pragma: no cover
    log.warning("Unidecode unavailable; non-Latin titles are indexed verbatim only")


# --- Normalization -------------------------------------------------------------------------

# Only genuine diacritic blocks are stripped. A blanket `unicodedata.combining()` test would
# also eat U+3099/U+309A, turning が into か and silently corrupting every Japanese title.
_DIACRITIC_RANGES: tuple[tuple[int, int], ...] = (
    (0x0300, 0x036F),  # Combining Diacritical Marks
    (0x1AB0, 0x1AFF),  # ... Extended
    (0x1DC0, 0x1DFF),  # ... Supplement
    (0x20D0, 0x20FF),  # ... for Symbols
    (0xFE20, 0xFE2F),  # Combining Half Marks
)

# Dropped outright rather than turned into a space, so "don't" indexes as "dont".
# Apostrophe, right single quote, modifier apostrophe, grave, acute, prime, turned comma.
_ELIDED = frozenset("'`" + "\u2019\u02bc\u00b4\u02b9\u02bb")


def _is_diacritic(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _DIACRITIC_RANGES)


def normalize(text: str | None) -> str:
    """NFKC → casefold → strip diacritics → collapse punctuation and whitespace."""
    if not text:
        return ""

    folded = unicodedata.normalize("NFKC", text).casefold()
    decomposed = unicodedata.normalize("NFD", folded)
    stripped = "".join(ch for ch in decomposed if not _is_diacritic(ch))
    # Recompose so Hangul syllables torn into jamo by NFD come back whole.
    recomposed = unicodedata.normalize("NFC", stripped)

    out: list[str] = []
    for char in recomposed:
        if char in _ELIDED:
            continue
        category = unicodedata.category(char)
        if char.isspace() or category[0] in {"P", "S", "Z", "C"}:
            out.append(" ")
        else:
            out.append(char)
    return " ".join("".join(out).split())


# --- Script detection ----------------------------------------------------------------------

SCRIPT_HAN = "han"
SCRIPT_HANGUL = "hangul"
SCRIPT_KANA = "kana"
SCRIPT_CYRILLIC = "cyrillic"
SCRIPT_GREEK = "greek"
SCRIPT_ARABIC = "arabic"
SCRIPT_HEBREW = "hebrew"
SCRIPT_THAI = "thai"
SCRIPT_DEVANAGARI = "devanagari"
SCRIPT_LATIN = "latin"
SCRIPT_OTHER = "other"

_HAN_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2EBEF),
    (0x2F800, 0x2FA1F),
)
_KANA_RANGES: tuple[tuple[int, int], ...] = (
    (0x3041, 0x309F),  # Hiragana (incl. the voicing marks)
    (0x30A0, 0x30FF),  # Katakana
    (0x31F0, 0x31FF),
    (0xFF66, 0xFF9D),  # Halfwidth katakana
)
_HANGUL_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF),
    (0x3130, 0x318F),
    (0xA960, 0xA97F),
    (0xAC00, 0xD7FF),
)


def _in_ranges(code: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(low <= code <= high for low, high in ranges)


def _is_han(char: str) -> bool:
    return _in_ranges(ord(char), _HAN_RANGES)


def _is_kana(char: str) -> bool:
    return _in_ranges(ord(char), _KANA_RANGES)


def _is_hangul(char: str) -> bool:
    return _in_ranges(ord(char), _HANGUL_RANGES)


def detect_script(text: str | None) -> str:
    """Presence-ordered, not majority-ordered.

    A title like "迷情 Season 1" is mostly Latin by character count but the interesting part
    is the Han run, so the script that needs romanizing wins over the one that does not.
    Kana outranks Han because kana anywhere means the Han in that string is Japanese kanji.
    """
    if not text:
        return SCRIPT_OTHER

    has = {
        SCRIPT_KANA: False,
        SCRIPT_HANGUL: False,
        SCRIPT_HAN: False,
        SCRIPT_CYRILLIC: False,
        SCRIPT_GREEK: False,
        SCRIPT_ARABIC: False,
        SCRIPT_HEBREW: False,
        SCRIPT_THAI: False,
        SCRIPT_DEVANAGARI: False,
        SCRIPT_LATIN: False,
    }
    for char in text:
        code = ord(char)
        if _is_kana(char):
            has[SCRIPT_KANA] = True
        elif _is_hangul(char):
            has[SCRIPT_HANGUL] = True
        elif _is_han(char):
            has[SCRIPT_HAN] = True
        elif 0x0400 <= code <= 0x052F or 0x2DE0 <= code <= 0x2DFF:
            has[SCRIPT_CYRILLIC] = True
        elif 0x0370 <= code <= 0x03FF or 0x1F00 <= code <= 0x1FFF:
            has[SCRIPT_GREEK] = True
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F:
            has[SCRIPT_ARABIC] = True
        elif 0x0590 <= code <= 0x05FF:
            has[SCRIPT_HEBREW] = True
        elif 0x0E00 <= code <= 0x0E7F:
            has[SCRIPT_THAI] = True
        elif 0x0900 <= code <= 0x097F:
            has[SCRIPT_DEVANAGARI] = True
        elif char.isalpha() and code < 0x0250:
            has[SCRIPT_LATIN] = True

    for script in (
        SCRIPT_KANA,
        SCRIPT_HANGUL,
        SCRIPT_HAN,
        SCRIPT_CYRILLIC,
        SCRIPT_GREEK,
        SCRIPT_ARABIC,
        SCRIPT_HEBREW,
        SCRIPT_THAI,
        SCRIPT_DEVANAGARI,
        SCRIPT_LATIN,
    ):
        if has[script]:
            return script
    return SCRIPT_OTHER


# --- Romanization --------------------------------------------------------------------------


def _runs(text: str, predicate: Callable[[str], bool]) -> list[tuple[bool, str]]:
    """Split into alternating (matches_predicate, chunk) runs.

    Romanizing run by run is what keeps "迷情 Season 1" from collapsing into
    "miqingseason1" when the joined variant is built.
    """
    runs: list[tuple[bool, str]] = []
    for char in text:
        flag = predicate(char)
        if runs and runs[-1][0] == flag:
            runs[-1] = (flag, runs[-1][1] + char)
        else:
            runs.append((flag, char))
    return runs


def _fallback(text: str) -> list[str]:
    if _unidecode is None:
        return []
    return [_unidecode(text)]


def _han_variants(text: str) -> list[str]:
    if _lazy_pinyin is None or _PinyinStyle is None:
        return _fallback(text)
    spaced: list[str] = []
    joined: list[str] = []
    initials: list[str] = []
    for is_han, chunk in _runs(text, _is_han):
        if not is_han:
            spaced.append(chunk)
            joined.append(chunk)
            initials.append(chunk)
            continue
        syllables = _lazy_pinyin(chunk, style=_PinyinStyle.NORMAL, errors="ignore")
        heads = _lazy_pinyin(chunk, style=_PinyinStyle.FIRST_LETTER, errors="ignore")
        spaced.append(" ".join(syllables))
        joined.append("".join(syllables))
        initials.append("".join(heads))
    return [" ".join(spaced), " ".join(joined), " ".join(initials), *_fallback(text)]


def _hangul_variants(text: str) -> list[str]:
    if _Romanizer is None:
        return _fallback(text)
    try:
        romanized = _Romanizer(text).romanize()
    except Exception:  # the library trips over stray punctuation on some inputs
        return _fallback(text)
    return [romanized, romanized.replace(" ", ""), *_fallback(text)]


def _kana_variants(text: str) -> list[str]:
    if _kakasi is None:
        return _fallback(text)
    try:
        parts = [item.get("hepburn", "") for item in _kakasi.convert(text)]
    except Exception:  # pragma: no cover
        return _fallback(text)
    spaced = " ".join(part for part in parts if part)
    joined = "".join(parts)
    return [spaced, joined, *_fallback(text)]


def romanize(text: str | None) -> list[str]:
    """All useful Latin renderings of `text`, normalized and deduped.

    Han yields spaced, joined and initial-acronym forms (迷情 → "mi qing", "miqing", "mq")
    because users type all three.
    """
    if not text or not text.strip():
        return []

    script = detect_script(text)
    if script == SCRIPT_HAN:
        raw = _han_variants(text)
    elif script == SCRIPT_HANGUL:
        raw = _hangul_variants(text)
    elif script == SCRIPT_KANA:
        raw = _kana_variants(text)
    elif script == SCRIPT_LATIN:
        # Already Latin; normalize() has done the diacritic work. Still emit the unidecode
        # form so "Ångström" also indexes as "angstrom" when NFD could not decompose it.
        raw = _fallback(text)
    else:
        raw = _fallback(text)

    return _dedupe(normalize(variant) for variant in raw)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def search_keys(title: str | None) -> list[str]:
    """The full set of strings a user might type to find `title`."""
    return _dedupe([normalize(title), *romanize(title)])


__all__ = [
    "detect_script",
    "normalize",
    "romanize",
    "search_keys",
]
