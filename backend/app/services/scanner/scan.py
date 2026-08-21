"""Incremental library scan.

Walks the read-only mount, diffs it against `media_files`, probes what changed and attaches
series and episodes. The mount is never written to and every file is accounted for: one that
cannot be parsed still gets a row with `episode_id = NULL` so it surfaces under "unmatched".

The whole walk runs in a worker thread — a NAS directory tree is slow enough that doing it on
the event loop would stall the API for the duration of the scan.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError, Forbidden
from app.models import (
    AudioTrack,
    Episode,
    JobType,
    MediaFile,
    MediaStatus,
    ScanDirectory,
    Series,
    SeriesTitle,
    SubtitleTrack,
    TitleKind,
    TitleSource,
)
from app.services.scanner.parse import (
    SUBTITLE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    ParsedEpisode,
    file_extension,
    guess_subtitle_language,
    is_forced_subtitle,
    parse_episode,
    parse_season_folder,
    parse_series_folder,
)
from app.services.scanner.probe import ProbeResult, compute_direct_play, probe
from app.utils.ids import new_id, slugify
from app.utils.time import utcnow

log = logging.getLogger(__name__)

#: Commit every so often so a long scan survives a restart and the UI sees partial results.
COMMIT_EVERY = 25

#: NAS housekeeping directories that must never be treated as library content.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "@eadir",
        "@__thumb",
        "#recycle",
        "#snapshot",
        "lost+found",
        "$recycle.bin",
        "system volume information",
        "extrafanart",
        "extrathumbs",
    }
)


class ScanContext(Protocol):
    """Structural subset of `JobContext`; `scan_library` also runs fine with `ctx=None`."""

    async def progress(self, percent: float, message: str) -> None: ...
    async def log(self, level: str, message: str) -> None: ...
    def cancelled(self) -> bool: ...


@dataclass(slots=True)
class ScanReport:
    files_seen: int = 0
    files_added: int = 0
    files_updated: int = 0
    files_missing: int = 0
    series_created: int = 0
    episodes_created: int = 0
    unmatched: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "files_seen": self.files_seen,
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_missing": self.files_missing,
            "series_created": self.series_created,
            "episodes_created": self.episodes_created,
            "unmatched": self.unmatched,
            "errors": self.errors,
        }

    def summary(self) -> str:
        return (
            f"{self.files_seen} files · +{self.files_added} new · ~{self.files_updated} updated · "
            f"{self.files_missing} missing · {self.series_created} series · "
            f"{self.episodes_created} episodes · {self.unmatched} unmatched · {self.errors} errors"
        )


# --- Path safety ---------------------------------------------------------------------------


def library_root() -> Path:
    return get_settings().library_root


def safe_media_path(relative: str | os.PathLike[str]) -> Path:
    """Resolve `relative` under `LIBRARY_ROOT`, refusing anything that escapes it.

    Symlinks are resolved before the check, so a link inside the library pointing at `/etc`
    is refused too. Used by the scanner and by every streaming route that opens a file.
    """
    root = library_root().resolve(strict=False)
    candidate = Path(str(os.fspath(relative)).replace("\\", "/"))
    resolved = (
        candidate.resolve(strict=False)
        if candidate.is_absolute()
        else (root / candidate).resolve(strict=False)
    )
    if resolved != root and not resolved.is_relative_to(root):
        raise Forbidden("PATH_TRAVERSAL", "Path resolves outside the library root")
    return resolved


def relative_media_path(path: str | os.PathLike[str]) -> str:
    """Inverse of `safe_media_path`: an absolute path back to its stored library-relative form."""
    root = library_root().resolve(strict=False)
    resolved = Path(os.fspath(path)).resolve(strict=False)
    if resolved != root and not resolved.is_relative_to(root):
        raise Forbidden("PATH_TRAVERSAL", "Path resolves outside the library root")
    return _posix_relative(root, resolved)


# --- Filesystem walk (thread) --------------------------------------------------------------


@dataclass(slots=True)
class _FileEntry:
    rel_path: str
    dir_rel: str
    name: str
    size: int
    mtime: float


@dataclass(slots=True)
class _WalkResult:
    videos: list[_FileEntry] = field(default_factory=list)
    subtitles: dict[str, list[str]] = field(default_factory=dict)
    #: Every directory the walk reached, with the mtime it had. Written back after a successful
    #: scan so the next one can compare.
    dir_mtimes: dict[str, float] = field(default_factory=dict)
    #: The subset whose files were actually listed and stat'd. **The missing-file sweep is
    #: scoped to these**: an incremental scan never sees the files in a skipped directory, and
    #: without this it would mark the whole library missing on its first run.
    scanned_dirs: set[str] = field(default_factory=set)
    #: True when at least one directory was skipped, so the caller can say so honestly rather
    #: than reporting "1141 files" after looking at nineteen.
    skipped_dirs: int = 0


def _posix_relative(root: Path, target: str | os.PathLike[str]) -> str:
    rel = os.path.relpath(os.fspath(target), root)
    rel = rel.replace(os.sep, "/")
    return "" if rel == "." else rel


def _join(dir_rel: str, name: str) -> str:
    return f"{dir_rel}/{name}" if dir_rel else name


def _stem(name: str) -> str:
    extension = file_extension(name)
    return name[: -(len(extension) + 1)] if extension else name


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name.casefold() in _SKIP_DIRS


def _walk_library(
    root: Path, start: Path, unchanged: dict[str, float] | None = None
) -> _WalkResult:
    """Blocking directory walk. Always invoke through `asyncio.to_thread`.

    `unchanged` turns this incremental: a directory whose mtime matches what the last scan
    recorded has its files skipped entirely — no `os.stat` per file, no per-file database work.
    A directory's mtime moves when a file is added, removed or renamed inside it, which is the
    whole set of events this scan exists to notice.

    Subdirectories are still descended into. Only the *files* of an unchanged directory are
    skipped, because a folder's mtime says nothing about what happened inside its children.
    """
    if not start.is_dir():
        raise FileNotFoundError(str(start))

    result = _WalkResult()
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not _skip_dir(name))
        dir_rel = _posix_relative(root, dirpath)

        try:
            dir_mtime = os.stat(dirpath).st_mtime
        except OSError:  # vanished mid-walk; treat it as changed so its files are re-checked
            dir_mtime = 0.0
        result.dir_mtimes[dir_rel] = dir_mtime

        if unchanged is not None and unchanged.get(dir_rel) == dir_mtime:
            result.skipped_dirs += 1
            continue

        result.scanned_dirs.add(dir_rel)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            extension = file_extension(name)
            if extension in SUBTITLE_EXTENSIONS:
                result.subtitles.setdefault(dir_rel, []).append(name)
                continue
            if extension not in VIDEO_EXTENSIONS:
                continue
            try:
                stat = os.stat(os.path.join(dirpath, name))
            except OSError:  # vanished or unreadable mid-walk; the sweep will mark it missing
                continue
            result.videos.append(
                _FileEntry(
                    rel_path=_join(dir_rel, name),
                    dir_rel=dir_rel,
                    name=name,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
    result.videos.sort(key=lambda entry: entry.rel_path)
    return result


# --- Scan state ----------------------------------------------------------------------------


@dataclass(slots=True)
class _State:
    session: AsyncSession
    root: Path
    report: ScanReport
    full: bool
    known: dict[str, MediaFile]
    series_cache: dict[str, Series] = field(default_factory=dict)
    episode_cache: dict[tuple[str, int, int], Episode] = field(default_factory=dict)
    #: Series created since the last commit. They only graduate to `created_series` once the
    #: rows are durable — enqueueing a job for a rolled-back series violates its foreign key.
    pending_series: list[str] = field(default_factory=list)
    created_series: list[str] = field(default_factory=list)
    #: Series that gained an episode title during this scan, so their index slice is stale.
    #: A spurious entry only costs one idempotent re-index, so this is not savepoint-tracked.
    reindex_ids: set[str] = field(default_factory=set)
    _mark: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0)

    def checkpoint(self) -> None:
        """Called right after a commit: everything counted so far is now durable."""
        self.created_series.extend(self.pending_series)
        self.pending_series.clear()

    def mark(self) -> None:
        """Snapshot the counters before a file is processed, so its savepoint can be undone."""
        self._mark = (
            self.report.files_added,
            self.report.files_updated,
            self.report.series_created,
            self.report.episodes_created,
            self.report.errors,
            len(self.pending_series),
        )

    def rewind(self) -> None:
        """Called after a savepoint rollback: undo the counters for that one file."""
        (
            self.report.files_added,
            self.report.files_updated,
            self.report.series_created,
            self.report.episodes_created,
            self.report.errors,
            pending,
        ) = self._mark
        del self.pending_series[pending:]


async def _progress(ctx: ScanContext | None, percent: float, message: str) -> None:
    if ctx is not None:
        await ctx.progress(percent, message[:500])


async def _log(ctx: ScanContext | None, level: str, message: str) -> None:
    if ctx is not None:
        await ctx.log(level, message)


# --- Entry point ---------------------------------------------------------------------------


async def scan_library(
    session: AsyncSession,
    *,
    ctx: ScanContext | None = None,
    path: str | None = None,
    full: bool = False,
    incremental: bool = False,
) -> ScanReport:
    """Reconcile the library mount with the database.

    `path` restricts the scan to a subtree (relative to `LIBRARY_ROOT`); `full` re-probes files
    whose size and mtime are unchanged. Cancellation is cooperative: `ctx.cancelled()` is polled
    once per file and a cancelled scan returns the partial report with everything done so far
    committed.

    `incremental` skips the files of directories whose mtime has not moved since the last scan
    — one stat per folder instead of one per file, which on this library is nineteen instead of
    eleven hundred, on a network-backed mount. It is what the *scheduled* scan uses. A manual
    scan stays complete on purpose: the one thing a directory mtime cannot see is a file
    rewritten in place under the same name, and the button somebody presses when they think
    something is wrong should not share that blind spot.
    """
    root = library_root().resolve(strict=False)
    start = safe_media_path(path) if path else root
    prefix = _posix_relative(root, start)
    report = ScanReport()

    await _progress(ctx, 0.0, "Reading the library…")
    unchanged = await _known_dir_mtimes(session) if (incremental and not full) else None
    try:
        walk = await asyncio.to_thread(_walk_library, root, start, unchanged)
    except FileNotFoundError as exc:
        raise AppError(
            "LIBRARY_UNAVAILABLE",
            f"Library path is not readable: {start}",
            503,
        ) from exc

    # Create the FTS table now, while the session is still clean: `ensure_index` commits its
    # DDL in that case, and doing it later would land inside a per-file savepoint.
    await _ensure_search_index(session)

    known = await _load_known(session, prefix)
    state = _State(session=session, root=root, report=report, full=full, known=known)

    total = len(walk.videos)
    present_before = sum(1 for row in known.values() if row.status == MediaStatus.PRESENT)
    await _log(ctx, "info", f"{total} video files under {prefix or '/'}")

    step = max(1, total // 100)
    seen: set[str] = set()
    cancelled = False

    for index, entry in enumerate(walk.videos, start=1):
        if ctx is not None and ctx.cancelled():
            cancelled = True
            await _log(ctx, "warning", f"Cancelled after {index - 1}/{total} files")
            break

        seen.add(entry.rel_path)
        report.files_seen += 1
        state.mark()
        try:
            # A SAVEPOINT per file. Rolling the whole transaction back instead would discard
            # the up-to-`COMMIT_EVERY` files already processed since the last commit — and
            # since the walk is ordered, a file that fails deterministically would starve the
            # same predecessors on every scan, so they would never be recorded at all.
            async with session.begin_nested():
                await _process_file(state, entry, walk.subtitles.get(entry.dir_rel, ()))
        except Exception as exc:  # one unreadable file must not abort the whole scan
            state.rewind()
            report.errors += 1
            log.warning("Scan failed on %s: %s", entry.rel_path, exc, exc_info=True)
            await _log(ctx, "error", f"{entry.rel_path}: {exc}")
            # Objects created inside the savepoint were expunged with it, so anything the
            # caches held for this file is stale. `known` is left alone: the only entry that
            # could be affected is this file's, and the missing sweep skips it as seen.
            state.series_cache.clear()
            state.episode_cache.clear()

        if index % step == 0 or index == total:
            await _progress(ctx, index * 100.0 / total, f"{index}/{total} · {entry.name}")
        if index % COMMIT_EVERY == 0:
            await session.commit()
            state.checkpoint()

    await session.commit()
    state.checkpoint()

    # Done before the cancel check: those series are committed, and nothing else would ever
    # queue their metadata — a later scan finds them already present and creates nothing.
    await _enqueue_metadata(state)
    await session.commit()

    await _reindex_episode_titles(state)
    await session.commit()

    if cancelled:
        await _progress(ctx, 100.0, report.summary())
        return report

    # A walk that finds nothing while the database is full of present files means the mount
    # went away, not that the library was emptied. Marking everything missing would be worse
    # than doing nothing, so the sweep is skipped and the admin gets a warning.
    if total == 0 and present_before:
        await _log(
            ctx,
            "warning",
            f"No media found but {present_before} files are on record — mount unavailable?",
        )
    else:
        _sweep_missing(
            state,
            seen,
            prefix,
            scanned_dirs=walk.scanned_dirs if unchanged is not None else None,
            walked_dirs=set(walk.dir_mtimes) if unchanged is not None else None,
        )
        await session.commit()

    # Written only after the sweep committed, so a scan that died half way leaves the old
    # mtimes in place and the next one re-examines rather than trusting a partial pass.
    await _remember_dir_mtimes(session, walk.dir_mtimes, replace_all=unchanged is None)
    await session.commit()

    if walk.skipped_dirs:
        await _log(ctx, "info", f"{walk.skipped_dirs} unchanged folders skipped")

    await _progress(ctx, 100.0, report.summary())
    await _log(ctx, "info", report.summary())
    return report


async def _known_dir_mtimes(session: AsyncSession) -> dict[str, float]:
    rows = (await session.execute(select(ScanDirectory))).scalars().all()
    return {row.rel_path: row.mtime for row in rows}


async def _remember_dir_mtimes(
    session: AsyncSession, mtimes: dict[str, float], *, replace_all: bool
) -> None:
    """Record what each directory looked like, for the next scan to compare against.

    `replace_all` on a complete scan: that pass looked at everything under `start`, so rows for
    directories it did not see are stale and would otherwise keep a deleted folder alive in the
    table for ever. An incremental pass only merges, because it deliberately did not look
    everywhere.
    """
    now = utcnow()
    existing = {
        row.rel_path: row for row in (await session.execute(select(ScanDirectory))).scalars()
    }

    for rel_path, mtime in mtimes.items():
        row = existing.get(rel_path)
        if row is None:
            session.add(ScanDirectory(rel_path=rel_path, mtime=mtime, scanned_at=now))
        else:
            row.mtime = mtime
            row.scanned_at = now

    if replace_all:
        for rel_path, row in existing.items():
            if rel_path not in mtimes:
                await session.delete(row)


async def _load_known(session: AsyncSession, prefix: str) -> dict[str, MediaFile]:
    stmt = select(MediaFile)
    if prefix:
        stmt = stmt.where(
            or_(MediaFile.path == prefix, MediaFile.path.startswith(f"{prefix}/", autoescape=True))
        )
    rows = (await session.execute(stmt)).scalars().all()
    return {row.path: row for row in rows}


# --- Per-file work -------------------------------------------------------------------------


async def _process_file(state: _State, entry: _FileEntry, sidecars: Iterable[str]) -> None:
    media = state.known.get(entry.rel_path)
    stat_changed = media is None or (
        media.size_bytes != entry.size or abs(media.mtime - entry.mtime) > 1e-3
    )
    # A file that previously failed to probe is retried on every scan: the cause is often a
    # transient NAS hiccup, and a permanently broken file costs one cheap ffprobe.
    changed = (
        stat_changed or state.full or (media is not None and media.status != MediaStatus.PRESENT)
    )

    if media is None:
        media = MediaFile(path=entry.rel_path, size_bytes=entry.size, mtime=entry.mtime)
        state.session.add(media)
        state.known[entry.rel_path] = media
        state.report.files_added += 1
    elif stat_changed:
        state.report.files_updated += 1

    if changed:
        media.size_bytes = entry.size
        media.mtime = entry.mtime
        await _probe_into(state, media, entry)

    media.scanned_at = utcnow()
    _sync_sidecars(media, entry, sidecars)

    if media.episode_id is not None:
        return  # an existing match — including a manual assignment — is never overwritten

    parsed = parse_episode(entry.name)
    if parsed is None:
        state.report.unmatched += 1
        return

    parsed = _apply_folder_season(parsed, entry)
    series = await _resolve_series(state, entry, parsed)
    episode = await _get_or_create_episode(state, series, parsed)
    media.episode_id = episode.id


async def _probe_into(state: _State, media: MediaFile, entry: _FileEntry) -> None:
    fallback_container = file_extension(entry.name) or None
    try:
        target = safe_media_path(entry.rel_path)
    except Forbidden:
        # A symlink in the library pointing outside it. Recorded rather than raised, so the
        # admin sees the file in the unmatched/error view instead of it silently vanishing.
        error: str | None = "Path resolves outside the library root"
        outcome = None
    else:
        outcome = await probe(target)
        error = outcome.error

    if outcome is None or outcome.result is None:
        media.container = media.container or fallback_container
        media.direct_play_ok = compute_direct_play(media.container, None, None)
        media.status = MediaStatus.ERROR
        media.scan_error = (error or "probe failed")[:1000]
        state.report.errors += 1
        return

    result = outcome.result
    media.container = result.container or fallback_container
    media.video_codec = result.video_codec
    media.audio_codec = result.audio_codec
    media.width = result.width
    media.height = result.height
    media.duration_seconds = result.duration_seconds
    media.bitrate = result.bitrate
    media.direct_play_ok = result.direct_play_ok
    media.status = MediaStatus.PRESENT
    media.scan_error = None
    _replace_streams(media, result)


def _replace_streams(media: MediaFile, result: ProbeResult) -> None:
    """Embedded tracks mirror the file, so they are rebuilt wholesale. Sidecar rows are left
    alone here — they are reconciled separately and carry a cached `extracted_path`."""
    media.audio_tracks.clear()
    for stream in result.audio:
        media.audio_tracks.append(
            AudioTrack(
                stream_index=stream.index,
                language=stream.language,
                title=stream.title,
                codec=stream.codec,
                channels=stream.channels,
                is_default=stream.is_default,
            )
        )

    media.subtitle_tracks[:] = [
        track for track in media.subtitle_tracks if track.stream_index is None
    ]
    for subtitle in result.subtitles:
        media.subtitle_tracks.append(
            SubtitleTrack(
                stream_index=subtitle.index,
                language=subtitle.language,
                title=subtitle.title,
                codec=subtitle.codec,
                is_default=subtitle.is_default,
                is_forced=subtitle.is_forced,
            )
        )


def _sync_sidecars(media: MediaFile, entry: _FileEntry, filenames: Iterable[str]) -> None:
    """Attach `.srt`/`.ass`/… files whose stem prefixes the video's, and drop rows for
    sidecars that are gone. Runs even for unchanged videos, so adding a subtitle file to the
    NAS is picked up without touching the video."""
    stem = _stem(entry.name)
    wanted: dict[str, str] = {}
    for name in filenames:
        remainder = _stem(name)[len(stem) :] if _stem(name).startswith(stem) else None
        if remainder is None or (remainder and remainder[0] not in ".-_ "):
            continue
        wanted[_join(entry.dir_rel, name)] = name

    current = {track.sidecar_path for track in media.subtitle_tracks if track.sidecar_path}
    for rel_path, name in wanted.items():
        if rel_path in current:
            continue
        media.subtitle_tracks.append(
            SubtitleTrack(
                stream_index=None,
                language=guess_subtitle_language(name),
                codec=file_extension(name),
                is_forced=is_forced_subtitle(name),
                sidecar_path=rel_path,
            )
        )

    stale = [
        track
        for track in media.subtitle_tracks
        if track.sidecar_path and track.sidecar_path not in wanted
    ]
    if stale:
        media.subtitle_tracks[:] = [track for track in media.subtitle_tracks if track not in stale]


def _apply_folder_season(parsed: ParsedEpisode, entry: _FileEntry) -> ParsedEpisode:
    """`Series/Season 03/Series - 05.mkv` carries its season in the folder, not the name."""
    if parsed.season_explicit or not entry.dir_rel:
        return parsed
    folder_season = parse_season_folder(entry.dir_rel.rsplit("/", 1)[-1])
    if folder_season is None:
        return parsed
    return replace(parsed, season=folder_season, season_explicit=True)


# --- Series and episodes -------------------------------------------------------------------


def _series_dir(dir_rel: str) -> str:
    """Climb out of `Season 01` / `第二季` folders to the directory that names the series."""
    parts = [part for part in dir_rel.split("/") if part]
    while parts and parse_season_folder(parts[-1]) is not None:
        parts.pop()
    return "/".join(parts)


async def _resolve_series(state: _State, entry: _FileEntry, parsed: ParsedEpisode) -> Series:
    folder_rel = _series_dir(entry.dir_rel)
    if folder_rel:
        title, year = parse_series_folder(folder_rel.rsplit("/", 1)[-1])
        cache_key = folder_rel
    else:
        # A file dropped directly into the library root has no folder to name it.
        title = parsed.series_hint or _stem(entry.name)
        year = parsed.year
        cache_key = f"\x00title:{title.casefold()}"

    cached = state.series_cache.get(cache_key)
    if cached is not None:
        return cached

    if folder_rel:
        stmt = select(Series).where(Series.library_path == folder_rel)
    else:
        stmt = select(Series).where(Series.title == title).limit(1)
    series = (await state.session.execute(stmt)).scalars().first()

    if series is None:
        series = await _create_series(
            state,
            library_path=folder_rel or entry.rel_path,
            title=title,
            year=year,
        )
    state.series_cache[cache_key] = series
    return series


async def _create_series(
    state: _State, *, library_path: str, title: str, year: int | None
) -> Series:
    series = Series(
        slug=await _unique_slug(state.session, title, year),
        title=title,
        sort_title=_sort_title(title),
        year=year,
        library_path=library_path,
    )
    state.session.add(series)
    await state.session.flush()

    # Without this row a series that never reaches TMDB would be invisible to search.
    state.session.add(
        SeriesTitle(
            series_id=series.id,
            language="",
            title=title,
            kind=TitleKind.OFFICIAL,
            source=TitleSource.FILENAME,
            normalized=_normalize_title(title),
        )
    )
    await state.session.flush()

    # §7 wants the index rebuilt whenever a title row changes, and the row above is one. Doing
    # it here rather than leaving it to `metadata_refresh` is what makes a library scanned
    # before TMDB is configured searchable at all: that job returns early when there is no API
    # key, so its own reindex never runs and the series would only ever match a title *prefix*.
    await _reindex(state.session, series.id)

    state.report.series_created += 1
    state.pending_series.append(series.id)
    return series


async def _unique_slug(session: AsyncSession, title: str, year: int | None) -> str:
    base = slugify(title)
    candidates = [base]
    if year:
        candidates.append(f"{base}-{year}")
    candidates.extend(f"{base}-{n}" for n in range(2, 50))
    for candidate in candidates:
        taken = (
            await session.execute(select(Series.id).where(Series.slug == candidate).limit(1))
        ).first()
        if taken is None:
            return candidate
    return f"{base}-{new_id()[:8]}"


async def _get_or_create_episode(state: _State, series: Series, parsed: ParsedEpisode) -> Episode:
    key = (series.id, parsed.season, parsed.episode)
    cached = state.episode_cache.get(key)
    if cached is not None:
        return cached

    episode = (
        (
            await state.session.execute(
                select(Episode).where(
                    Episode.series_id == series.id,
                    Episode.season_number == parsed.season,
                    Episode.episode_number == parsed.episode,
                )
            )
        )
        .scalars()
        .first()
    )

    if episode is None:
        episode = Episode(
            series_id=series.id,
            season_number=parsed.season,
            episode_number=parsed.episode,
            title=parsed.title,
        )
        state.session.add(episode)
        await state.session.flush()
        state.report.episodes_created += 1
        if parsed.title:
            state.reindex_ids.add(series.id)
    elif episode.title is None and parsed.title and not episode.metadata_locked:
        episode.title = parsed.title
        state.reindex_ids.add(series.id)

    state.episode_cache[key] = episode
    return episode


def _normalize_title(title: str) -> str:
    """Search owns normalization; fall back to a plain casefold if it is not importable yet."""
    try:
        from app.services.search.normalize import normalize
    except ImportError:
        return " ".join(title.casefold().split())
    return normalize(title)


async def _ensure_search_index(session: AsyncSession) -> None:
    try:
        from app.services.search.index import ensure_index
    except ImportError:  # pragma: no cover - optional during bring-up
        return
    await ensure_index(session)


async def _reindex_episode_titles(state: _State) -> None:
    """Second indexing pass, once the episodes exist.

    `_create_series` indexes a series the moment it is created — necessarily *before* any of
    its episodes, which are built from the files that follow. Nothing reindexed it afterwards,
    so the index held series titles and nothing else: episode search returned an empty list
    for every query against a library TMDB has never touched, which is exactly the library
    dramatarr is usable with out of the box.
    """
    if not state.reindex_ids:
        return
    for series_id in sorted(state.reindex_ids):
        await _reindex(state.session, series_id)
    log.info("re-indexed episode titles for %d series", len(state.reindex_ids))
    state.reindex_ids.clear()


async def _reindex(session: AsyncSession, series_id: str) -> None:
    """Add a series to the search index, tolerating a search module that cannot be imported.

    Transaction control stays here: the call sits inside the current file's savepoint, so a
    file that fails afterwards takes its index rows back out with it.
    """
    try:
        from app.services.search.index import reindex_series
    except ImportError:  # pragma: no cover - optional during bring-up
        log.warning("Search index unavailable; %s will not be searchable yet", series_id)
        return
    await reindex_series(session, series_id)


def _sort_title(title: str) -> str:
    return _normalize_title(title) or title.casefold()


# --- Finishing up --------------------------------------------------------------------------


def _sweep_missing(
    state: _State,
    seen: set[str],
    prefix: str,
    *,
    scanned_dirs: set[str] | None = None,
    walked_dirs: set[str] | None = None,
) -> None:
    """Mark as missing what the scan should have seen and did not.

    **The scoping is the whole safety of an incremental scan.** This walks `known` — every file
    on record — and a scan that deliberately skipped directories saw none of their files. Left
    unscoped it would mark the entire library missing on the first incremental run, silently,
    and the result would look exactly like the mount having gone away.

    So a row is only touched when either:
      * its directory was actually examined this pass, and the file was not there; or
      * its directory is gone from the walk altogether, which is a deleted folder and is the
        case a naive scoping would miss.

    `scanned_dirs is None` means a complete scan, where every directory was examined and the
    old behaviour is exactly right.
    """
    for path, row in state.known.items():
        if path in seen or row.status == MediaStatus.MISSING:
            continue
        if prefix and path != prefix and not path.startswith(f"{prefix}/"):
            continue
        if scanned_dirs is not None:
            directory = path.rsplit("/", 1)[0] if "/" in path else ""
            examined = directory in scanned_dirs
            vanished = walked_dirs is not None and directory not in walked_dirs
            if not examined and not vanished:
                continue
        row.status = MediaStatus.MISSING
        row.scanned_at = utcnow()
        state.report.files_missing += 1


async def _enqueue_metadata(state: _State) -> None:
    if not state.created_series:
        return
    # Imported here because the queue imports the scanner to run this job in the first place.
    from app.workers.queue import enqueue

    for series_id in state.created_series:
        await enqueue(
            state.session,
            JobType.METADATA_REFRESH,
            {"seriesId": series_id},
            series_id=series_id,
        )


__all__ = [
    "COMMIT_EVERY",
    "ScanContext",
    "ScanReport",
    "library_root",
    "relative_media_path",
    "safe_media_path",
    "scan_library",
]
