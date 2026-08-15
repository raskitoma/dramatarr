"""Group-level inference for folders of files that no per-file rule can recognize.

`app.services.scanner.parse` looks at one filename at a time, which is the right unit for
`Show - S01E03.mkv` and useless for `701151231.720p.narrowv3.mp4`. A single opaque name carries
no episode signal at all; fifty-eight of them side by side carry a great deal, because the
*differences* between the names are the signal. This module reads a folder as one group, works
out the intended order, and returns a plan the admin reviews and approves in one action.

Nothing here touches the filesystem, the database, or the network. It takes `UnmatchedFile`
values and returns an `ImportPlan`. That matters beyond testability: `LIBRARY_ROOT` is mounted
read-only and dramatarr never renames, moves, writes or deletes a media file, so "applying" a
plan can only ever mean creating episode rows that point at the files under the names they
already have. The catalogue learns what the files are; the disk is untouched.

Five strategies are tried, each reporting its own confidence, and the most confident applicable
one wins. `natural_name` applies to any non-empty folder, so a plan always has a strategy.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from statistics import median

from app.services.scanner.parse import file_extension, parse_episode, parse_series_folder

# --- Public data ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnmatchedFile:
    """One `media_files` row with no episode attached, reduced to what inference needs.

    `name` is the basename and is what every strategy reads; `path` is carried through only so
    a caller can build an export script without a second query.
    """

    id: str
    name: str
    path: str = ""
    size_bytes: int = 0
    mtime: float = 0.0
    duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ProposedEpisode:
    """One row of the plan: this file is that episode. Nothing on disk changes."""

    media_file_id: str
    current_name: str
    season_number: int
    episode_number: int
    confidence: float
    reason: str
    #: What the chosen strategy sorted this file on, as a plain string — the extracted
    #: integer, the parsed date, the modification timestamp. `None` when the order is the
    #: filename itself. `reason` is English prose about the same fact; this is the fact.
    order_key: str | None = None


@dataclass(slots=True)
class ImportPlan:
    """A whole folder's proposed mapping, plus everything the admin needs to reject it."""

    folder: str
    suggested_series_title: str
    suggested_year: int | None
    season_number: int
    strategy: str
    confidence: float
    explanation: str
    #: Stable identifier for `explanation`, from `EXPLANATION_CODES`. The sentence is this
    #: module's English; the code lets a UI say the same thing in the reader's language
    #: instead, and an unknown code degrades to the sentence.
    explanation_code: str = ""
    episodes: list[ProposedEpisode] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


STRATEGY_SHARED_PATTERN_NUMBER = "shared_pattern_number"
STRATEGY_EMBEDDED_EPISODE = "embedded_episode"
STRATEGY_DATE = "date"
STRATEGY_NATURAL_NAME = "natural_name"
STRATEGY_MTIME = "mtime"

#: Declared order, which is the order strategies are tried in and the tie-break when two of
#: them report the same confidence.
STRATEGY_NAMES: tuple[str, ...] = (
    STRATEGY_SHARED_PATTERN_NUMBER,
    STRATEGY_EMBEDDED_EPISODE,
    STRATEGY_DATE,
    STRATEGY_NATURAL_NAME,
    STRATEGY_MTIME,
)

#: Explanation codes. One per *shape* of sentence rather than one per strategy: a contiguous
#: numeric run and a gapped one are read differently by the admin and so are worded
#: differently, while a one-file folder overrides whichever strategy technically won.
CODE_NUMERIC_SEQUENCE = "NUMERIC_SEQUENCE"
CODE_NUMERIC_SEQUENCE_GAPS = "NUMERIC_SEQUENCE_GAPS"
CODE_EMBEDDED_NUMBER = "EMBEDDED_NUMBER"
CODE_DATE_ORDER = "DATE_ORDER"
CODE_FILENAME_ORDER = "FILENAME_ORDER"
CODE_MODIFIED_TIME = "MODIFIED_TIME"
CODE_SINGLE_FILE = "SINGLE_FILE"
CODE_EMPTY_FOLDER = "EMPTY_FOLDER"

EXPLANATION_CODES: tuple[str, ...] = (
    CODE_NUMERIC_SEQUENCE,
    CODE_NUMERIC_SEQUENCE_GAPS,
    CODE_EMBEDDED_NUMBER,
    CODE_DATE_ORDER,
    CODE_FILENAME_ORDER,
    CODE_MODIFIED_TIME,
    CODE_SINGLE_FILE,
    CODE_EMPTY_FOLDER,
)

#: A folder of one file is not a sequence, whatever the filename says.
SINGLE_FILE_CONFIDENCE_CAP = 0.4
#: Below this many seconds a file is a trailer, a sample or a broken rip — not an episode.
SHORT_DURATION_SECONDS = 60.0
#: A file this many times longer than the median is a batch/omnibus, not one episode.
DURATION_OUTLIER_FACTOR = 3.0

_DEFAULT_START_EPISODE = 1
_CONTIGUOUS_CONFIDENCE = 0.95
_GAPPED_CONFIDENCE = 0.75
_EMBEDDED_CONFIDENCE = 0.98
#: `parse_episode` reports which pattern fired. Everything except the bare trailing number is
#: an explicit marker (`S01E03`, `第3集`, `EP12`, `[08]`); `trailing` is the fallback that will
#: read any digits at the end of a name as an episode, which is a guess, not a reading.
_WEAK_MATCHERS = frozenset({"trailing"})
#: What a folder of marker-free numbers earns: ahead of a blind name sort, behind both a shared
#: filename form and a run of real dates.
_EMBEDDED_WEAK_CONFIDENCE = 0.6
_EMBEDDED_UNPARSED_CONFIDENCE = 0.25
_DATE_CONFIDENCE = 0.8
_NATURAL_CONFIDENCE = 0.5
_MTIME_CONFIDENCE = 0.3


# --- Internal candidate --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Entry:
    """One file's place in a strategy's ordering. `number` is set only when the strategy read
    a real episode number off the file; otherwise the number comes from the rank."""

    file: UnmatchedFile
    reason: str
    confidence: float
    number: int | None = None
    #: The value the strategy actually sorted on — the extracted integer, the parsed date,
    #: the modification timestamp — as a plain string. `reason` is an English sentence and a
    #: caller that wants to *render* the ordering (a review column, a localized explanation)
    #: needs the value, not the prose about it. `None` when the order is the name itself.
    order_key: str | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    strategy: str
    confidence: float
    #: Stable identifier for the *shape* of the explanation, so a client can render the
    #: sentence in its own language instead of printing this module's English. One strategy
    #: can produce more than one code — a contiguous run and a gapped one need different
    #: wording, not the same wording with a different number in it.
    code: str
    explanation: str
    entries: tuple[_Entry, ...]
    warnings: tuple[str, ...] = ()
    #: Seasons the filenames stated outright, so `infer_plan` can flag a mismatch with the
    #: season the admin picked.
    detected_seasons: tuple[int, ...] = ()


# --- Small helpers -------------------------------------------------------------------------

_DIGIT_RUN = re.compile(r"(\d+)")
_ASCII_DIGITS = re.compile(r"^[0-9]+$")
_DIGIT_CHARS = "0123456789"


def _stem(name: str) -> str:
    """`Show.720p.mp4` -> `Show.720p`, using the scanner's own idea of an extension."""
    extension = file_extension(name)
    return name[: -(len(extension) + 1)] if extension else name


def _natural_key(name: str) -> tuple[tuple[int, int, str], ...]:
    """Digit-aware sort key, so `ep2` sorts before `ep10` instead of after it.

    Chunks alternate text/number; each becomes a comparable triple so tuples of mixed chunk
    kinds never compare an `int` against a `str`.
    """
    chunks = _DIGIT_RUN.split(name)
    key: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(chunks):
        if index % 2:
            key.append((0, int(chunk), ""))
        elif chunk:
            key.append((1, 0, chunk.casefold()))
    key.append((2, 0, name))  # exact name last, so equal-looking names still order stably
    return tuple(key)


def _file_sort_key(file: UnmatchedFile) -> tuple[tuple[int, int, str], ...]:
    return _natural_key(file.name)


def _count(quantity: int, noun: str) -> str:
    """`1 file` / `7 files`. Every sentence here is read by a human before they approve a
    plan, and "1 files ordered by natural filename sort" reads like a bug even when it is
    not."""
    return f"{quantity} {noun}" if quantity == 1 else f"{quantity} {noun}s"


def _common_prefix(values: Sequence[str]) -> str:
    if not values:
        return ""
    first = values[0]
    shortest = min(len(value) for value in values)
    index = 0
    while index < shortest and all(value[index] == first[index] for value in values):
        index += 1
    return first[:index]


def _common_suffix(values: Sequence[str]) -> str:
    return _common_prefix([value[::-1] for value in values])[::-1]


def _name_list(files: Sequence[UnmatchedFile], limit: int = 3) -> str:
    names = ", ".join(file.name for file in files[:limit])
    extra = len(files) - limit
    return f"{names} and {extra} more" if extra > 0 else names


def _format_mtime(value: float) -> str:
    try:
        return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):  # pragma: no cover - absurd timestamps only
        return str(value)


# --- Date recognition ----------------------------------------------------------------------

_SEP = r"[-._\s]"
_YEAR = r"(?:19\d{2}|20\d{2})"
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], tuple[int, int, int]], ...] = (
    # YYYY-MM-DD — a four-digit year first is unambiguous, so it is tried first.
    (re.compile(rf"(?<!\d)({_YEAR}){_SEP}(\d{{1,2}}){_SEP}(\d{{1,2}})(?!\d)"), (1, 2, 3)),
    # DD-MM-YYYY — day first, per the contract; never month first.
    (re.compile(rf"(?<!\d)(\d{{1,2}}){_SEP}(\d{{1,2}}){_SEP}({_YEAR})(?!\d)"), (3, 2, 1)),
    # YYYYMMDD — a bare eight-digit run, guarded so it cannot bite into a longer number.
    (re.compile(rf"(?<!\d)({_YEAR})(\d{{2}})(\d{{2}})(?!\d)"), (1, 2, 3)),
)


def parse_filename_date(name: str) -> date | None:
    """First valid `YYYY-MM-DD`, `DD-MM-YYYY` or `YYYYMMDD` in a filename, else None.

    A match that is not a real calendar date (`2024-13-05`, `20240230`) is skipped rather than
    accepted, and the scan continues — a bad hit early in the name must not mask a good one
    later.
    """
    for pattern, (year_group, month_group, day_group) in _DATE_PATTERNS:
        for match in pattern.finditer(name):
            try:
                return date(
                    int(match.group(year_group)),
                    int(match.group(month_group)),
                    int(match.group(day_group)),
                )
            except ValueError:
                continue
    return None


# --- Strategy 1: shared_pattern_number -----------------------------------------------------


def _detect_numeric_pattern(names: Sequence[str]) -> tuple[str, str, tuple[int, ...]] | None:
    """Strip the longest common prefix and suffix; succeed only if pure integers remain.

    The two `strip`/`lstrip` calls are the whole trick. A raw common prefix eats part of the
    number it is supposed to delimit — `701151231…701151288` share `7011512`, which would leave
    `31…88` and report the wrong range, and `1001…1004` would leave `1…4` and lose the digits
    that identify the file. Pushing the prefix back off any trailing digits (and the suffix
    forward off any leading digits) hands the entire digit run to the variable part, which is
    the only place it can safely live.
    """
    if len(names) < 2:
        return None

    prefix = _common_prefix(names).rstrip(_DIGIT_CHARS)
    rests = [name[len(prefix) :] for name in names]
    suffix = _common_suffix(rests).lstrip(_DIGIT_CHARS)
    middles = [rest[: len(rest) - len(suffix)] if suffix else rest for rest in rests]

    if not all(_ASCII_DIGITS.match(middle) for middle in middles):
        return None
    return prefix, suffix, tuple(int(middle) for middle in middles)


def _shared_pattern_candidate(files: Sequence[UnmatchedFile]) -> _Candidate | None:
    if len(files) < 2:
        return None

    wildcard_extension = False
    detected = _detect_numeric_pattern([file.name for file in files])
    if detected is None:
        # Mixed extensions break the common suffix; the naming pattern is still there.
        detected = _detect_numeric_pattern([_stem(file.name) for file in files])
        wildcard_extension = True
    if detected is None:
        return None

    prefix, suffix, numbers = detected
    distinct = sorted(set(numbers))
    low, high = distinct[0], distinct[-1]
    missing = (high - low + 1) - len(distinct)
    repeats = len(numbers) - len(distinct)
    contiguous = missing == 0 and repeats == 0

    order = sorted(
        zip(files, numbers, strict=True),
        key=lambda pair: (pair[1], _file_sort_key(pair[0])),
    )
    total = len(order)
    confidence = _CONTIGUOUS_CONFIDENCE if contiguous else _GAPPED_CONFIDENCE
    entries = tuple(
        _Entry(
            file=file,
            reason=f"pattern number {number}, position {position} of {total}",
            confidence=confidence,
            order_key=str(number),
        )
        for position, (file, number) in enumerate(order, start=1)
    )

    if contiguous:
        shape = "contiguous"
    elif missing:
        shape = f"{missing} missing"
    else:
        shape = "with repeats"
    form = f"{prefix}N{suffix}" + (".*" if wildcard_extension else "")
    explanation = (
        f'{total} files share the form "{form}" with numbers {low}–{high}, {shape}'  # noqa: RUF001
    )

    warnings: list[str] = []
    if missing:
        warnings.append(
            f"The numeric run is not contiguous: {missing} number(s) missing between "
            f"{low} and {high}. Files may be absent from the folder."
        )
    return _Candidate(
        strategy=STRATEGY_SHARED_PATTERN_NUMBER,
        confidence=confidence,
        code=CODE_NUMERIC_SEQUENCE if contiguous else CODE_NUMERIC_SEQUENCE_GAPS,
        explanation=explanation,
        entries=entries,
        warnings=tuple(warnings),
    )


# --- Strategy 2: embedded_episode ----------------------------------------------------------


def _embedded_candidate(files: Sequence[UnmatchedFile]) -> _Candidate | None:
    """Believe the filenames that carry a real episode number, and say so about the rest.

    Confidence falls off with coverage: a folder where every name parses is as good as
    recognition gets, while one where a handful parse is a hint, not an answer.
    """
    if not files:
        return None

    parsed: list[tuple[UnmatchedFile, int, int, str]] = []
    unparsed: list[UnmatchedFile] = []
    seasons: set[int] = set()
    for file in files:
        episode = parse_episode(file.name)
        if episode is None:
            unparsed.append(file)
            continue
        parsed.append((file, episode.episode, episode.season, episode.matched_by))
        if episode.season_explicit:
            seasons.add(episode.season)
    if not parsed:
        return None

    # A number with no episode marker around it is `parse_episode`'s last resort, and reading a
    # whole folder of them as episode numbers is how `IMG_0012/0013/0014.MP4` becomes "episodes
    # 12, 13 and 14" instead of a camera's three clips. When every match is that weak the group
    # evidence a shared filename form provides is worth more, so this scores below it.
    marker_free = all(matched_by in _WEAK_MATCHERS for _f, _n, _s, matched_by in parsed)
    base = _EMBEDDED_WEAK_CONFIDENCE if marker_free else _EMBEDDED_CONFIDENCE

    coverage = len(parsed) / len(files)
    confidence = base if not unparsed else round(base * (0.6 + 0.4 * coverage), 2)

    parsed.sort(key=lambda item: (item[1], _file_sort_key(item[0])))
    entries = [
        _Entry(
            file=file,
            reason=f"filename gives episode {number} ({matched_by} pattern)",
            confidence=confidence,
            number=number,
            order_key=str(number),
        )
        for file, number, _season, matched_by in parsed
    ]
    unparsed.sort(key=_file_sort_key)
    entries.extend(
        _Entry(
            file=file,
            reason="no episode number in the filename; placed after the parsed files by name",
            confidence=_EMBEDDED_UNPARSED_CONFIDENCE,
        )
        for file in unparsed
    )

    numbers = [number for _file, number, _season, _matched in parsed]
    source = "a bare trailing number" if marker_free else "an episode marker"
    explanation = (
        f"{len(parsed)} of {len(files)} filenames carry an episode number from {source} "
        f"({min(numbers)}–{max(numbers)}); those numbers are used as they stand"  # noqa: RUF001
    )
    warnings: list[str] = []
    if unparsed:
        warnings.append(
            f"{len(unparsed)} of {len(files)} filenames carry no episode number and were "
            f"given the next free numbers in name order: {_name_list(unparsed)}."
        )
    return _Candidate(
        strategy=STRATEGY_EMBEDDED_EPISODE,
        confidence=confidence,
        code=CODE_EMBEDDED_NUMBER,
        explanation=explanation,
        entries=tuple(entries),
        warnings=tuple(warnings),
        detected_seasons=tuple(sorted(seasons)),
    )


# --- Strategy 3: date ----------------------------------------------------------------------


def _date_candidate(files: Sequence[UnmatchedFile]) -> _Candidate | None:
    """Every file must carry a date and at least two must differ, or the ordering is really
    just the name ordering wearing a more confident explanation."""
    if len(files) < 2:
        return None

    dated: list[tuple[UnmatchedFile, date]] = []
    for file in files:
        stamp = parse_filename_date(file.name)
        if stamp is None:
            return None
        dated.append((file, stamp))
    if len({stamp for _file, stamp in dated}) < 2:
        return None

    dated.sort(key=lambda pair: (pair[1], _file_sort_key(pair[0])))
    total = len(dated)
    entries = tuple(
        _Entry(
            file=file,
            reason=f"dated {stamp.isoformat()}, position {position} of {total}",
            confidence=_DATE_CONFIDENCE,
            order_key=stamp.isoformat(),
        )
        for position, (file, stamp) in enumerate(dated, start=1)
    )
    explanation = (
        f"{total} files carry a date in the filename, from {dated[0][1].isoformat()} to "
        f"{dated[-1][1].isoformat()}; order follows the dates"
    )
    return _Candidate(
        strategy=STRATEGY_DATE,
        confidence=_DATE_CONFIDENCE,
        code=CODE_DATE_ORDER,
        explanation=explanation,
        entries=entries,
    )


# --- Strategy 4: natural_name --------------------------------------------------------------


def _natural_candidate(files: Sequence[UnmatchedFile]) -> _Candidate | None:
    """The floor. Applies to any non-empty folder, which is what guarantees a plan exists."""
    if not files:
        return None

    order = sorted(files, key=_file_sort_key)
    total = len(order)
    entries = tuple(
        _Entry(
            file=file,
            reason=f"natural filename order, position {position} of {total}",
            confidence=_NATURAL_CONFIDENCE,
        )
        for position, file in enumerate(order, start=1)
    )
    explanation = (
        f'1 file ordered by natural filename sort (digit-aware): "{order[0].name}"'
        if total == 1
        else (
            f'{total} files ordered by natural filename sort (digit-aware), "{order[0].name}" '
            f'first and "{order[-1].name}" last'
        )
    )
    return _Candidate(
        strategy=STRATEGY_NATURAL_NAME,
        confidence=_NATURAL_CONFIDENCE,
        code=CODE_FILENAME_ORDER,
        explanation=explanation,
        entries=entries,
    )


# --- Strategy 5: mtime ---------------------------------------------------------------------


def _mtime_candidate(files: Sequence[UnmatchedFile]) -> _Candidate | None:
    if not files:
        return None

    order = sorted(files, key=lambda file: (file.mtime, _file_sort_key(file)))
    total = len(order)
    entries = tuple(
        _Entry(
            file=file,
            reason=f"modified {_format_mtime(file.mtime)}, position {position} of {total}",
            confidence=_MTIME_CONFIDENCE,
            order_key=_format_mtime(file.mtime),
        )
        for position, file in enumerate(order, start=1)
    )
    explanation = (
        f"{_count(total, 'file')} ordered by modification time, "
        f"{_format_mtime(order[0].mtime)} to {_format_mtime(order[-1].mtime)}"
    )
    return _Candidate(
        strategy=STRATEGY_MTIME,
        confidence=_MTIME_CONFIDENCE,
        code=CODE_MODIFIED_TIME,
        explanation=explanation,
        entries=entries,
        warnings=(
            "Order comes only from file modification times, which record when the files were "
            "copied rather than what is in them. Treat this as a guess and check it.",
        ),
    )


# --- Assembly ------------------------------------------------------------------------------

_BUILDERS = (
    (STRATEGY_SHARED_PATTERN_NUMBER, _shared_pattern_candidate),
    (STRATEGY_EMBEDDED_EPISODE, _embedded_candidate),
    (STRATEGY_DATE, _date_candidate),
    (STRATEGY_NATURAL_NAME, _natural_candidate),
    (STRATEGY_MTIME, _mtime_candidate),
)


def _build_candidates(files: Sequence[UnmatchedFile]) -> list[_Candidate]:
    """Every applicable strategy, most confident first; ties keep the declared order."""
    candidates: list[_Candidate] = []
    for _name, builder in _BUILDERS:
        candidate = builder(files)
        if candidate is not None:
            candidates.append(candidate)
    # Stable, so equal confidences fall back to the declared order above.
    candidates.sort(key=lambda item: -item.confidence)
    return candidates


def available_strategies(files: Sequence[UnmatchedFile]) -> list[tuple[str, float]]:
    """Strategies that could order this folder, best first, with each one's own confidence.

    The single-file cap `infer_plan` applies is deliberately *not* applied here: this answers
    "what could work", which is what a strategy picker in the UI needs.
    """
    return [(candidate.strategy, candidate.confidence) for candidate in _build_candidates(files)]


def _finalize_numbers(candidate: _Candidate, start_episode: int) -> list[int]:
    """Turn a strategy's ordering into episode numbers.

    Rank-based strategies simply count up from `start_episode`. `embedded_episode` supplies
    real numbers and they are authoritative, so they are rebased only when the admin asked for
    a non-default start — otherwise a folder of `E05…E10` would be silently renumbered to 1.

    Files with no number of their own continue *above* the parsed run rather than restarting at
    `start_episode`. Restarting is both a worse guess and a worse review experience: a folder of
    `E04` plus two anonymous files would list them as 4, 1, 2, and an admin scanning a jumbled
    table for mistakes is an admin who approves one.
    """
    explicit = [entry.number for entry in candidate.entries if entry.number is not None]
    rebase = bool(explicit) and start_episode != _DEFAULT_START_EPISODE
    shift = start_episode - min(explicit) if rebase else 0

    used = {number + shift for number in explicit}
    numbers: list[int] = []
    cursor = max(used) + 1 if used else start_episode
    cursor = max(cursor, start_episode)
    for entry in candidate.entries:
        if entry.number is not None:
            numbers.append(entry.number + shift)
            continue
        while cursor in used:
            cursor += 1
        used.add(cursor)
        numbers.append(cursor)
        cursor += 1
    return numbers


def _duration_warnings(files: Sequence[UnmatchedFile]) -> list[str]:
    """Three independent readings of the runtimes, ordered from most specific to least.

    The relative checks are the ones that earn their keep. A 3-minute recap sitting among
    45-minute episodes is nowhere near the absolute floor and is obviously not an episode;
    conversely a folder where *every* file is short is short-form content, and naming three
    of them plus "and 55 more" tells the admin nothing they cannot see. So: files far above
    the median, files far below it, and only then the absolute floor — applied to the files
    the relative check did not already account for, and worded differently when it covers
    the whole folder.
    """
    warnings: list[str] = []
    known = [file for file in files if file.duration_seconds is not None]
    if not known:
        return warnings

    midpoint = median([file.duration_seconds or 0.0 for file in known])
    out_of_family: list[UnmatchedFile] = []
    if midpoint > 0:
        long_ones = [
            file
            for file in known
            if (file.duration_seconds or 0.0) > DURATION_OUTLIER_FACTOR * midpoint
        ]
        if long_ones:
            warnings.append(
                f"{len(long_ones)} file(s) run more than {DURATION_OUTLIER_FACTOR:g}x the "
                f"median duration of {midpoint:g}s: {_name_list(long_ones)}. "
                "They may not all be episodes."
            )
        floor = midpoint / DURATION_OUTLIER_FACTOR
        out_of_family = [file for file in known if (file.duration_seconds or 0.0) < floor]
        if out_of_family:
            warnings.append(
                f"{len(out_of_family)} file(s) run under 1/{DURATION_OUTLIER_FACTOR:g} of the "
                f"median duration of {midpoint:g}s and are probably trailers or samples: "
                f"{_name_list(out_of_family)}."
            )

    accounted = {file.id for file in out_of_family}
    short = [
        file
        for file in known
        if (file.duration_seconds or 0.0) < SHORT_DURATION_SECONDS and file.id not in accounted
    ]
    if not short:
        return warnings
    if len(short) == len(files):
        subject = "This file is" if len(files) == 1 else f"All {len(files)} files are"
        warnings.append(
            f"{subject} shorter than {SHORT_DURATION_SECONDS:g}s. This folder may hold clips "
            "or samples rather than episodes."
        )
    else:
        warnings.append(
            f"{len(short)} of {len(files)} file(s) are shorter than "
            f"{SHORT_DURATION_SECONDS:g}s and are probably trailers or samples: "
            f"{_name_list(short)}."
        )
    return warnings


def _folder_identity(folder: str) -> tuple[str, int | None]:
    if not folder.strip(" /\\"):
        return "", None
    return parse_series_folder(folder)


def infer_plan(
    folder: str,
    files: Sequence[UnmatchedFile],
    *,
    season: int = 1,
    start_episode: int = _DEFAULT_START_EPISODE,
    strategy: str | None = None,
) -> ImportPlan:
    """Propose a complete episode mapping for one folder of unmatched files.

    `season` and `start_episode` shift the whole run, for a folder that is really season 2 or
    the back half of a split release. `strategy` forces one of `STRATEGY_NAMES`; if the forced
    strategy cannot read these files the best applicable one is used instead and a warning
    says so, because returning nothing would be worse than returning something reviewable.

    Raises `ValueError` for a strategy name that does not exist — that is a caller bug, not a
    property of the files.
    """
    if strategy is not None and strategy not in STRATEGY_NAMES:
        raise ValueError(f"unknown import strategy: {strategy!r}")

    ordered = tuple(files)
    title, year = _folder_identity(folder)
    candidates = _build_candidates(ordered)
    warnings: list[str] = []

    if not candidates:
        return ImportPlan(
            folder=folder,
            suggested_series_title=title,
            suggested_year=year,
            season_number=season,
            strategy=STRATEGY_NATURAL_NAME,
            confidence=0.0,
            explanation="The folder holds no unmatched files, so there is nothing to order.",
            explanation_code=CODE_EMPTY_FOLDER,
            episodes=[],
            warnings=["This folder has fewer than 2 files, so no sequence can be inferred."],
        )

    chosen = candidates[0]
    if strategy is not None:
        forced = next((item for item in candidates if item.strategy == strategy), None)
        if forced is None:
            warnings.append(
                f'The "{strategy}" strategy cannot read these filenames; '
                f'used "{chosen.strategy}" instead.'
            )
        else:
            chosen = forced

    # A folder of one file is not a sequence, and the cap has to reach the rows as well as the
    # plan: the review table renders the per-row confidence, so leaving it at the strategy's
    # own figure would show a 0.4 plan made of 0.5 rows and invite the admin to trust them.
    row_cap = SINGLE_FILE_CONFIDENCE_CAP if len(ordered) < 2 else 1.0
    numbers = _finalize_numbers(chosen, start_episode)
    episodes = [
        ProposedEpisode(
            media_file_id=entry.file.id,
            current_name=entry.file.name,
            season_number=season,
            episode_number=number,
            confidence=min(entry.confidence, row_cap),
            reason=entry.reason,
            order_key=entry.order_key,
        )
        for entry, number in zip(chosen.entries, numbers, strict=True)
    ]

    confidence = chosen.confidence
    if len(ordered) < 2:
        confidence = min(confidence, SINGLE_FILE_CONFIDENCE_CAP)
        warnings.append(
            "A single file is not a sequence: check the episode number rather than trusting it."
        )

    warnings.extend(chosen.warnings)

    if chosen.detected_seasons and tuple(chosen.detected_seasons) != (season,):
        stated = ", ".join(str(value) for value in chosen.detected_seasons)
        warnings.append(
            f"The filenames state season {stated}, but this plan files them under season "
            f"{season}."
        )

    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    if duplicates:
        warnings.append(
            "Duplicate episode numbers proposed: "
            + ", ".join(str(number) for number in duplicates)
            + ". Two files cannot be the same episode."
        )

    warnings.extend(_duration_warnings(ordered))

    return ImportPlan(
        folder=folder,
        suggested_series_title=title,
        suggested_year=year,
        season_number=season,
        strategy=chosen.strategy,
        confidence=confidence,
        explanation=chosen.explanation,
        # A one-file folder is described as a one-file folder whatever strategy technically
        # won: "sorted alphabetically, first and last" is not a true sentence about one file.
        explanation_code=CODE_SINGLE_FILE if len(ordered) < 2 else chosen.code,
        episodes=episodes,
        warnings=warnings,
    )


__all__ = [
    "CODE_DATE_ORDER",
    "CODE_EMBEDDED_NUMBER",
    "CODE_EMPTY_FOLDER",
    "CODE_FILENAME_ORDER",
    "CODE_MODIFIED_TIME",
    "CODE_NUMERIC_SEQUENCE",
    "CODE_NUMERIC_SEQUENCE_GAPS",
    "CODE_SINGLE_FILE",
    "DURATION_OUTLIER_FACTOR",
    "EXPLANATION_CODES",
    "SHORT_DURATION_SECONDS",
    "SINGLE_FILE_CONFIDENCE_CAP",
    "STRATEGY_DATE",
    "STRATEGY_EMBEDDED_EPISODE",
    "STRATEGY_MTIME",
    "STRATEGY_NAMES",
    "STRATEGY_NATURAL_NAME",
    "STRATEGY_SHARED_PATTERN_NUMBER",
    "ImportPlan",
    "ProposedEpisode",
    "UnmatchedFile",
    "available_strategies",
    "infer_plan",
    "parse_filename_date",
]
