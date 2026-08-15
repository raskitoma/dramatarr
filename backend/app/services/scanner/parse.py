"""Filename parsing for the NAS scanner.

Pure and dependency-free — no I/O, no app imports — so the whole recognition table in the
architecture doc is unit-testable without a database or a mounted library.

Extension sets are stored **without** a leading dot; use `is_video_file` / `is_subtitle_file`
rather than comparing `Path.suffix` directly.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {"mkv", "mp4", "m4v", "avi", "mov", "webm", "ts", "m2ts", "wmv", "flv"}
)
SUBTITLE_EXTENSIONS: frozenset[str] = frozenset({"srt", "ass", "ssa", "vtt", "sub"})


@dataclass(frozen=True, slots=True)
class ParsedEpisode:
    """`title` is the *episode* title (the text after the episode marker), not the series
    title — the series comes from the folder. `series_hint` carries the leading text for the
    rare file that sits directly in the library root."""

    season: int
    episode: int
    title: str | None = None
    year: int | None = None
    # Diagnostics / fallbacks; excluded from equality so tests can compare the four
    # contract fields positionally.
    season_explicit: bool = field(default=False, compare=False)
    series_hint: str | None = field(default=None, compare=False)
    matched_by: str = field(default="", compare=False)


# --- Release noise -------------------------------------------------------------------------

#: Tokens that carry a dot and must be removed before dots are turned into spaces.
_DOTTED_NOISE = re.compile(
    r"(?<![A-Za-z0-9])(?:[hx]\.?26[45]|dd\+?\s?[1-7]\.[01]|ddp?[1-7]\.[01]|[1-7]\.[01])"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

#: Everything else, matched token-wise. Deliberately conservative: bare words that could
#: plausibly appear in a real title (`web`, `dvd`, `hd`, `raw`, `max`, `extended`) are absent,
#: because eating a title word is worse than leaving a tag behind.
_NOISE_TOKENS: tuple[str, ...] = (
    # resolution
    "240p", "360p", "480p", "540p", "576p", "720p", "1080p", "1440p", "2160p", "4320p",
    "480i", "576i", "1080i", "4k", "uhd",
    # source
    "bluray", "blu-ray", "bdrip", "bd-rip", "brrip", "bdremux", "remux", "web-dl", "webdl",
    "web-rip", "webrip", "hdtv", "pdtv", "dvdrip", "dvdscr", "hdrip", "tvrip", "vhsrip",
    "amzn", "nf", "dsnp", "hulu", "atvp", "hmax", "itunes", "crunchyroll", "funimation",
    # video codec
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "av1", "vp9",
    "10bit", "8bit", "10-bit", "8-bit", "hi10p", "hdr", "hdr10", "dovi", "sdr",
    # audio
    "aac", "aac2", "ac3", "eac3", "e-ac3", "ddp", "truehd", "atmos", "dts", "dts-hd",
    "dtshd", "flac", "mp3", "opus", "vorbis", "2ch", "6ch", "8ch",
    # release flags
    "repack", "proper", "internal", "limited", "multi", "dual", "dubbed", "subbed",
    "hardsub", "hardsubs", "esub", "esubs", "msub", "msubs", "sample", "batch",
)
_NOISE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(re.escape(token) for token in sorted(_NOISE_TOKENS, key=len, reverse=True))
    + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_CRC_RE = re.compile(r"^[0-9a-f]{8}$", re.IGNORECASE)
_YEAR_BRACKETED = re.compile(r"[(\[\{]\s*(19\d{2}|20\d{2})\s*[)\]\}]")
_YEAR_BARE = re.compile(r"(?<![\d])(19\d{2}|20\d{2})(?![\d])")
_BRACKET_GROUP = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
_ABBREV_DOT = re.compile(r"(?<=\b[A-Za-z])\.(?=[A-Za-z]\b)")
_TRAILING_GROUP = re.compile(r"(?<=[A-Za-z0-9])(-\s?[A-Za-z][A-Za-z0-9]{1,14})\s*$")
_TRAILING_COMPOUND = re.compile(r"([A-Za-z0-9]+-\s?[A-Za-z][A-Za-z0-9]{1,14})\s*$")
_MULTI_DASH = re.compile(r"(?:\s*-\s*){2,}")
_WS = re.compile(r"\s+")


# --- Episode patterns, in the priority order fixed by the contract -------------------------

_SEASON_WORD = r"(?:season|temporada|saison|staffel|stagione|temporada)"
_EPISODE_WORD = r"(?:episodes?|episodios?|epis[oó]dio|cap[ií]tulos?|folge|ep|cap)"

_P_SXXEYY = re.compile(
    r"(?<![A-Za-z0-9])s\s?(\d{1,3})[\s._-]{0,3}e(?:p)?\s?(\d{1,4})(?!\d)"
    r"(?:[\s._-]{0,3}e\s?\d{1,4}(?!\d))*",  # multi-episode files keep the first number
    re.IGNORECASE,
)
_P_NXMM = re.compile(r"(?<![A-Za-z0-9.])(\d{1,2})x(\d{1,4})(?![\dx])", re.I)
_P_SEASON_EPISODE = re.compile(
    rf"{_SEASON_WORD}\s*(\d{{1,3}})[\s._·:-]{{0,4}}{_EPISODE_WORD}\s*\.?\s*(\d{{1,4}})(?!\d)",
    re.IGNORECASE,
)
# Both ideographic zeros and both forms of "two" are accepted.
_HAN_NUM = r"[0-9一二三四五六七八九十百零〇兩两]"  # noqa: RUF001
_P_CJK_EPISODE = re.compile(rf"第\s*({_HAN_NUM}{{1,6}})\s*[集话話回期]")
_P_CJK_SEASON = re.compile(rf"第\s*({_HAN_NUM}{{1,4}})\s*[季部]")
_P_ZHI = re.compile(r"之\s*(\d{1,4})(?!\d)")
_P_DASH = re.compile(r"(?:^|\s)-\s*(\d{1,4})\s*-(?=\s|$)")
_P_E = re.compile(r"(?<![A-Za-z0-9])e\s?(\d{1,4})(?!\d)", re.IGNORECASE)
_P_EP = re.compile(r"(?<![A-Za-z0-9])ep\.?\s?(\d{1,4})(?!\d)", re.IGNORECASE)
_P_BRACKET = re.compile(r"\[\s*(\d{1,4})\s*\](?!\d)")
_P_TRAILING = re.compile(r"(?:^|[\s._-])(\d{1,4})\s*$")

_P_SEASON_ONLY = re.compile(r"(?<![A-Za-z0-9])s\s?(\d{1,3})(?![\dp])", re.IGNORECASE)
_P_SEASON_WORD_ONLY = re.compile(rf"{_SEASON_WORD}\s*(\d{{1,3}})(?!\d)", re.IGNORECASE)

_SEASON_FOLDER = re.compile(
    rf"^(?:{_SEASON_WORD}|s|temp)\s*[._-]?\s*(\d{{1,3}})$", re.IGNORECASE
)
_SEASON_FOLDER_CJK = re.compile(rf"^第\s*({_HAN_NUM}{{1,4}})\s*[季部]$")
_SPECIALS_FOLDER = frozenset({"specials", "special", "especiales", "especial", "extras", "sp"})
_TRAILING_SEASON = re.compile(
    rf"[\s._-]+(?:{_SEASON_WORD}\s*\d{{1,3}}|s\d{{1,3}}|第{_HAN_NUM}{{1,4}}[季部])$",
    re.IGNORECASE,
)

_HAN_DIGITS = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5,  # noqa: RUF001
    "六": 6, "七": 7, "八": 8, "九": 9,
}


# --- Small helpers -------------------------------------------------------------------------


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("/\\"))[-1]


def _split_ext(name: str) -> tuple[str, str]:
    """Split a trailing extension only when it looks like one (1-5 alphanumerics)."""
    head, sep, tail = name.rpartition(".")
    if sep and head and 1 <= len(tail) <= 5 and tail.isalnum():
        return head, tail.lower()
    return name, ""


def file_extension(name: str) -> str:
    return _split_ext(_basename(name))[1]


def is_video_file(name: str) -> bool:
    return file_extension(name) in VIDEO_EXTENSIONS


def is_subtitle_file(name: str) -> bool:
    return file_extension(name) in SUBTITLE_EXTENSIONS


def han_to_int(text: str) -> int | None:
    """Chinese numerals up to 999 (第三十集 → 30). Plain digits pass straight through."""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    section = 0
    number: int | None = None
    for char in text:
        if char in _HAN_DIGITS:
            number = _HAN_DIGITS[char]
        elif char == "十":
            section += (number if number is not None else 1) * 10
            number = None
        elif char == "百":
            section += (number if number is not None else 1) * 100
            number = None
        else:
            return None
    total = section + (number or 0)
    return total or None


def _extract_year(text: str) -> tuple[int | None, str]:
    """Bracketed years win outright; a bare year is only trusted when something precedes it,
    so a series literally called `1917` keeps its name."""
    match = _YEAR_BRACKETED.search(text)
    if match:
        return int(match.group(1)), text[: match.start()] + " " + text[match.end() :]
    for match in _YEAR_BARE.finditer(text):
        if match.start() == 0 or not text[: match.start()].strip(" .-_[]()"):
            continue
        return int(match.group(1)), text[: match.start()] + " " + text[match.end() :]
    return None, text


def _strip_bracket_noise(text: str) -> str:
    """Drop group tags, CRCs and quality brackets, but keep a purely numeric group — `[03]`
    is an episode marker the contract requires us to recognize."""

    def replace(match: re.Match[str]) -> str:
        content = match.group(1).strip()
        if content.isdigit() and 1 <= len(content) <= 4:
            return f"[{content}]"
        if _CRC_RE.match(content):
            return " "
        return " "

    return _BRACKET_GROUP.sub(replace, text)


def _normalize_separators(text: str) -> str:
    guarded = _ABBREV_DOT.sub("\x00", text)
    guarded = guarded.replace(".", " ").replace("_", " ")
    return guarded.replace("\x00", ".")


def _strip_noise(text: str) -> tuple[str, bool]:
    """Remove quality/source/codec tags, and the trailing `-GROUP` of a scene release.

    The group is located in the *original* text — once the tags around it are blanked out the
    dash is no longer glued to a token and cannot be told apart from the dash in
    `Show - 07 - Title`. A hyphenated tag that is itself noise (`WEB-DL`) is left to the
    token pass instead of being mistaken for a group.
    """
    scene = bool(_NOISE_RE.search(text) or _DOTTED_NOISE.search(text))
    cleaned = _NOISE_RE.sub(" ", _DOTTED_NOISE.sub(" ", text))
    if scene:
        group = _TRAILING_GROUP.search(text)
        compound = _TRAILING_COMPOUND.search(text)
        is_noise_tag = compound is not None and _NOISE_RE.fullmatch(
            compound.group(1).replace(" ", "")
        )
        if group is not None and not is_noise_tag:
            cleaned = re.sub(re.escape(group.group(1)) + r"\s*$", "", cleaned)
    return cleaned, scene


def _tidy(text: str) -> str:
    cleaned = _WS.sub(" ", text.replace("\x00", "."))
    cleaned = _MULTI_DASH.sub(" - ", cleaned).strip()
    return cleaned.strip(" -_.·:[]()").strip()


def _clean_fragment(text: str, *, strip_season: bool = False) -> str | None:
    fragment = _tidy(text)
    if strip_season:
        fragment = _tidy(_TRAILING_SEASON.sub("", fragment))
    return fragment or None


# --- Public API ----------------------------------------------------------------------------


def parse_episode(filename: str) -> ParsedEpisode | None:
    """Recognize an episode in a filename, or return None so the caller files it as unmatched.

    Patterns are tried in the order fixed by the architecture doc: `SxxEyy`, `NxMM`,
    `Season x Episode y`, the CJK markers, `- yy -`, `Eyy`, `EPyy`, `[yy]`, bare trailing
    number. Season defaults to 1.
    """
    stem, _ext = _split_ext(_basename(filename))
    if not stem.strip():
        return None

    text = unicodedata.normalize("NFKC", stem)
    year, text = _extract_year(text)
    text = _strip_bracket_noise(text)
    text = _normalize_separators(text)
    text, _scene = _strip_noise(text)
    text = _WS.sub(" ", text).strip()

    for name, pattern in (
        ("sxxeyy", _P_SXXEYY),
        ("nxmm", _P_NXMM),
        ("season_episode", _P_SEASON_EPISODE),
    ):
        match = pattern.search(text)
        if match:
            return _build(
                text, match, int(match.group(1)), int(match.group(2)),
                year=year, season_explicit=True, matched_by=name,
            )

    match = _P_CJK_EPISODE.search(text)
    if match:
        episode = han_to_int(match.group(1))
        if episode is not None:
            season, explicit = _find_season(text)
            return _build(
                text, match, season, episode, year=year, season_explicit=explicit,
                matched_by="cjk",
            )

    for name, pattern in (
        ("zhi", _P_ZHI),
        ("dash", _P_DASH),
        ("e", _P_E),
        ("ep", _P_EP),
        ("bracket", _P_BRACKET),
    ):
        match = pattern.search(text)
        if match:
            season, explicit = _find_season(text)
            return _build(
                text, match, season, int(match.group(1)), year=year,
                season_explicit=explicit, matched_by=name,
            )

    match = _P_TRAILING.search(text)
    if match:
        value = int(match.group(1))
        # A trailing four-digit number in the year range is almost always a year that our
        # bracket/bare rules did not claim, not episode 1998.
        if not (len(match.group(1)) == 4 and 1900 <= value <= 2099):
            season, explicit = _find_season(text)
            return _build(
                text, match, season, value, year=year, season_explicit=explicit,
                matched_by="trailing",
            )

    return None


def _build(
    text: str,
    match: re.Match[str],
    season: int,
    episode: int,
    *,
    year: int | None,
    season_explicit: bool,
    matched_by: str,
) -> ParsedEpisode:
    return ParsedEpisode(
        season=season,
        episode=episode,
        title=_clean_fragment(text[match.end() :]),
        year=year,
        season_explicit=season_explicit,
        series_hint=_clean_fragment(text[: match.start()], strip_season=True),
        matched_by=matched_by,
    )


def _find_season(text: str) -> tuple[int, bool]:
    match = _P_SEASON_WORD_ONLY.search(text)
    if match:
        return int(match.group(1)), True
    match = _P_CJK_SEASON.search(text)
    if match:
        value = han_to_int(match.group(1))
        if value is not None:
            return value, True
    match = _P_SEASON_ONLY.search(text)
    if match:
        return int(match.group(1)), True
    return 1, False


def parse_series_folder(name: str) -> tuple[str, int | None]:
    """`Name (2024)` / `Name [2024]` / `Name.2024.1080p.WEB-DL` → (`Name`, 2024)."""
    base = _basename(name)
    text = unicodedata.normalize("NFKC", base)
    year, text = _extract_year(text)
    text = _strip_bracket_noise(text)
    text = _normalize_separators(text)
    text, _scene = _strip_noise(text)
    text = _WS.sub(" ", text).strip()
    title = _clean_fragment(text, strip_season=True)
    return (title or _tidy(base) or base.strip()), year


def parse_season_folder(name: str) -> int | None:
    """Season number for `Season 01` / `S01` / `Temporada 2` / `第二季`, 0 for specials.

    Returns None when the folder is not a season container, which is what lets the scanner
    walk up from a file to the real series directory.
    """
    text = _WS.sub(" ", unicodedata.normalize("NFKC", _basename(name))).strip()
    if not text:
        return None
    if text.casefold() in _SPECIALS_FOLDER:
        return 0
    match = _SEASON_FOLDER.match(text)
    if match:
        return int(match.group(1))
    match = _SEASON_FOLDER_CJK.match(text)
    if match:
        return han_to_int(match.group(1))
    return None


# --- Subtitle language ---------------------------------------------------------------------

_ISO639_1: frozenset[str] = frozenset(
    {
        "ar", "bg", "bn", "bs", "ca", "cs", "cy", "da", "de", "el", "en", "eo", "es", "et",
        "eu", "fa", "fi", "fr", "ga", "gl", "he", "hi", "hr", "hu", "hy", "id", "is", "it",
        "ja", "ka", "kk", "km", "ko", "lt", "lv", "mk", "ml", "mn", "ms", "mt", "my", "nb",
        "ne", "nl", "nn", "no", "pl", "pt", "ro", "ru", "sk", "sl", "sq", "sr", "sv", "sw",
        "ta", "te", "th", "tl", "tr", "uk", "ur", "vi", "zh",
    }
)

_LANG_ALIASES: dict[str, str] = {
    # ISO 639-2/B and /T, plus the spellings that actually show up in filenames.
    "alb": "sq", "ara": "ar", "arabic": "ar", "baq": "eu", "bul": "bg", "cat": "ca",
    "chi": "zh", "chinese": "zh", "chs": "zh-Hans", "cht": "zh-Hant", "cze": "cs",
    "ces": "cs", "dan": "da", "danish": "da", "deu": "de", "dut": "nl", "dutch": "nl",
    "ell": "el", "eng": "en", "english": "en", "esp": "es", "est": "et", "eus": "eu",
    "fas": "fa", "fin": "fi", "fra": "fr", "fre": "fr", "french": "fr", "ger": "de",
    "german": "de", "gle": "ga", "glg": "gl", "gre": "el", "heb": "he", "hin": "hi",
    "hrv": "hr", "hun": "hu", "ice": "is", "ind": "id", "isl": "is", "ita": "it",
    "italian": "it", "jpn": "ja", "japanese": "ja", "kat": "ka", "kaz": "kk", "khm": "km",
    "kor": "ko", "korean": "ko", "lat": "es-419", "lav": "lv", "lit": "lt", "mac": "mk",
    "may": "ms", "mkd": "mk", "msa": "ms", "nld": "nl", "nor": "no", "per": "fa",
    "pol": "pl", "polish": "pl", "por": "pt", "portuguese": "pt", "ron": "ro", "rum": "ro",
    "rus": "ru", "russian": "ru", "slk": "sk", "slo": "sk", "slv": "sl", "spa": "es",
    "spanish": "es", "castellano": "es", "srp": "sr", "swe": "sv", "swedish": "sv",
    "tam": "ta", "tel": "te", "tha": "th", "tur": "tr", "ukr": "uk", "urd": "ur",
    "vie": "vi", "vietnamese": "vi", "zho": "zh", "cmn": "zh", "und": "", "unknown": "",
    "latino": "es-419",
}

#: Trailing markers that qualify a track rather than name its language.
_SUBTITLE_FLAGS = frozenset(
    {"forced", "sdh", "cc", "hearing", "full", "default", "foreign", "signs", "songs"}
)


def normalize_language(token: str | None) -> str | None:
    """`spa` → `es`, `pt_BR` → `pt-BR`, `und` → None. Unknown tokens return None."""
    if not token:
        return None
    raw = token.strip().strip(".").replace("_", "-")
    if not raw:
        return None
    base, _, region = raw.partition("-")
    base = base.casefold()

    mapped = _LANG_ALIASES.get(base)
    if mapped is None:
        mapped = base if base in _ISO639_1 else None
    if mapped is None or mapped == "":
        return None

    if "-" in mapped:  # alias already carries a script/region, e.g. zh-Hans
        return mapped
    if not region:
        return mapped
    if region.isdigit() and len(region) == 3:
        return f"{mapped}-{region}"
    if region.isalpha() and len(region) == 2:
        return f"{mapped}-{region.upper()}"
    if region.isalpha() and len(region) == 4:
        return f"{mapped}-{region.capitalize()}"
    return mapped


def guess_subtitle_language(filename: str) -> str | None:
    """Language from a sidecar's trailing suffix: `.es.srt`, `.spa.srt`, `.pt-BR.srt`."""
    stem, _ext = _split_ext(_basename(filename))
    parts = re.split(r"[.\s]+", unicodedata.normalize("NFKC", stem))
    for token in reversed(parts[1:]):
        candidate = token.strip("[]()-_ ")
        if not candidate or candidate.casefold() in _SUBTITLE_FLAGS:
            continue
        language = normalize_language(candidate)
        # The first non-flag token that is not a language ends the search: anything further
        # left belongs to the title, not to a language suffix.
        return language
    return None


def is_forced_subtitle(filename: str) -> bool:
    return "forced" in _basename(filename).casefold()


__all__ = [
    "SUBTITLE_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "ParsedEpisode",
    "file_extension",
    "guess_subtitle_language",
    "han_to_int",
    "is_forced_subtitle",
    "is_subtitle_file",
    "is_video_file",
    "normalize_language",
    "parse_episode",
    "parse_season_folder",
    "parse_series_folder",
]
