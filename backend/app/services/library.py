"""Read paths for the library, plus the two write paths that remove things from it.

Every helper here aggregates in SQL and bulk-loads relationships; the viewer app asks for a
page of series or a whole season at once, so a per-row query would be felt immediately. The
URL builders below hold to the same rule: they are pure functions of columns already loaded,
so serializing a page of series or a whole season adds no queries at all.

The deletion section at the bottom removes **catalogue rows only**. `LIBRARY_ROOT` is mounted
read-only and nothing in this module — or anywhere else in the application — creates, moves or
deletes a file under it. It reads the mount for exactly one purpose: to tell the admin whether
the files behind a series are still there, because if they are, the next scan puts the series
straight back and the delete was theatre.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from sqlalchemy import Select, String, and_, case, cast, func, or_, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.expression import Exists

from app.config import get_settings
from app.errors import AppError, Conflict, NotFound
from app.models import (
    AudioTrack,
    Episode,
    EpisodeTitle,
    MediaFile,
    MediaStatus,
    PlayEvent,
    Series,
    SeriesCast,
    SeriesLink,
    SeriesTitle,
    SeriesTranslation,
    SubtitleTrack,
    TitleKind,
    TitleSource,
    WatchProgress,
)
from app.models.library import OverviewSource
from app.schemas.common import Page
from app.schemas.library import (
    AudioTrackOut,
    CleanupResult,
    ContinueWatchingItem,
    EpisodeDetail,
    EpisodeSummary,
    GenreFacet,
    HistoryItem,
    ImportApplied,
    ImportApplyIn,
    ImportAssignmentOut,
    ImportCreated,
    ImportPlanOut,
    ImportStrategyOut,
    JobRef,
    MediaFileOut,
    SeasonGroup,
    SeriesDeleted,
    SeriesDeletionPreview,
    CastMemberOut,
    SeriesDetail,
    SeriesLinkOut,
    SeriesProgress,
    SeriesSummary,
    SeriesTitleOut,
    SubtitleTrackOut,
    UnmatchedFileOut,
    UnmatchedGroupOut,
    WatchProgressOut,
)
from app.services import audit
from app.services.images import (
    BACKDROP_SIZE,
    POSTER_SIZE,
    STILL_SIZE,
    is_cached_path,
    purge_series_images,
)
from app.services.importer import (
    ImportPlan,
    UnmatchedFile,
    available_strategies,
    infer_plan,
)
from app.services.scanner.parse import file_extension, parse_season_folder, parse_series_folder
from app.services.scanner.scan import safe_media_path
from app.services.search.index import reindex_series
from app.services.search.normalize import normalize
from app.services.vocab import canonical_content_rating, canonical_network, normalize_genres
from app.services.watch import get_progress, progress_for_episodes, resume_position
from app.utils.ids import new_id, slugify
from app.utils.time import as_aware, utcnow

#: TMDB's image CDN. TMDB itself is gone from this application, but `poster_path` rows
#: written while it was here still hold `/abc.jpg` source paths, and an image that stops
#: rendering because a *source* was removed is a regression the reader cannot explain. The
#: constant stays so those keep resolving; nothing new is ever written in this shape.
LEGACY_IMAGE_BASE = "https://image.tmdb.org/t/p"

log = logging.getLogger(__name__)

SortKey = Literal["title", "added", "recent", "progress"]

#: Continue-watching walks a bounded candidate set rather than the whole library.
_MAX_ACTIVITY_CANDIDATES = 200

#: The §5 route table. `app.main` mounts every router under `API_PREFIX`; importing it here
#: would be an import cycle, so the prefix is spelled out and has to stay in step with it.
_STREAM_ROUTE = "/api/stream"
_IMAGES_ROUTE = f"{_STREAM_ROUTE}/images"

#: `app/workers/handlers.py` writes the sprite and its cues into `DATA_DIR/thumbs/<id>/`, and
#: `app/api/stream.py` serves them from there under exactly this name.
_THUMBNAILS_NAME = "thumbnails.vtt"

#: The one host an `<img src>` we emit may point at.
_TMDB_IMAGE_HOST = (urlsplit(LEGACY_IMAGE_BASE).hostname or "").lower()


@dataclass(slots=True)
class SeriesCounts:
    episode_count: int = 0
    available_count: int = 0
    watched_count: int = 0
    last_played_at: datetime | None = None

    @property
    def percent(self) -> float:
        if self.available_count <= 0:
            return 0.0
        return min(100.0, self.watched_count / self.available_count * 100.0)


# --- SQL fragments --------------------------------------------------------------------------


def _present_file() -> Exists:
    """Correlated to the enclosing query's `episodes` row."""
    return (
        select(MediaFile.id)
        .where(MediaFile.episode_id == Episode.id, MediaFile.status == MediaStatus.PRESENT)
        .exists()
    )


def _aggregate_select(user_id: str) -> Select[Any]:
    """One grouped row per series: totals plus this user's watched count and last activity."""
    return (
        select(
            Episode.series_id.label("series_id"),
            func.count(Episode.id).label("episode_count"),
            func.coalesce(func.sum(case((_present_file(), 1), else_=0)), 0).label(
                "available_count"
            ),
            func.coalesce(func.sum(case((WatchProgress.completed.is_(True), 1), else_=0)), 0).label(
                "watched_count"
            ),
            func.max(WatchProgress.last_played_at).label("last_played_at"),
        )
        .select_from(Episode)
        .outerjoin(
            WatchProgress,
            and_(
                WatchProgress.episode_id == Episode.id,
                WatchProgress.user_id == user_id,
            ),
        )
        .group_by(Episode.series_id)
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- URLs -----------------------------------------------------------------------------------
#
# The client puts these values straight into `src`, so a wrong one is a broken image or a dead
# player — not a field it can repair. Every builder is pure: it reads columns the caller has
# already loaded and touches neither the database nor (except where noted) the filesystem.


def _cached_image_url(stored: str) -> str | None:
    """`images/<series_id>/poster.jpg` -> `/api/stream/images/<series_id>/poster.jpg`.

    The route's `{kind}` segment is not a category. `stream_image` joins `{kind}/{name}` under
    `DATA_DIR/images`, and `app/services/images.py` lays every file out as
    `DATA_DIR/images/<series_id>/<stem>.<ext>` — so `{kind}` *is* the series id and `{name}`
    the file. Composing `/api/stream/images/poster/<stored path>` instead, which the old
    `posterPath` docstring invited, addresses a file that does not exist.

    A stored value that is not exactly two segments below `images/` cannot be served by that
    route at all, so it yields None rather than a URL that would 404.
    """
    parts = PurePosixPath(stored).parts
    if len(parts) != 3 or parts[0] != "images":
        return None
    return f"{_IMAGES_ROUTE}/{quote(parts[1], safe='')}/{quote(parts[2], safe='')}"


def image_url(stored: str | None, size: str) -> str | None:
    """A URL the browser can fetch for a stored poster/backdrop/still, or None.

    Three shapes reach this function, because the columns hold whatever the last job wrote:

    * a DATA_DIR-relative path (`images/<series_id>/poster.jpg`) once `image_cache` has run —
      served locally;
    * a bare TMDB source path (`/abc.jpg`) written by a metadata refresh that the cache job
      has not caught up with — pointed at the TMDB CDN so the grid is not blank meanwhile;
    * an absolute URL. Only TMDB's image host is passed through: the value comes from the
      database, and an arbitrary absolute URL in an `<img src>` is a third-party beacon.

    Null in, null out — never the string "null" and never a URL to nothing.
    """
    if not stored:
        return None
    if is_cached_path(stored):
        return _cached_image_url(stored)
    if stored.startswith(("http://", "https://")):
        host = (urlsplit(stored).hostname or "").lower()
        return stored if host == _TMDB_IMAGE_HOST else None
    return f"{LEGACY_IMAGE_BASE}/{size}{stored}"


def stream_url(episode_id: str) -> str:
    """The canonical playback route. Always emitted; it 404s while no file is present."""
    return f"{_STREAM_ROUTE}/{quote(episode_id, safe='')}"


def subtitle_url(episode_id: str, track_id: str) -> str:
    """WebVTT rendition of one track — the extraction happens on first request."""
    return f"{_STREAM_ROUTE}/{quote(episode_id, safe='')}/subtitles/{quote(track_id, safe='')}.vtt"


def thumbnails_url(episode_id: str) -> str:
    return f"{_STREAM_ROUTE}/{quote(episode_id, safe='')}/{_THUMBNAILS_NAME}"


async def thumbnails_url_if_ready(episode_id: str) -> str | None:
    """None unless `thumbnail_sprite` has actually written the cues.

    The route 404s when the file is missing, and the player must not attach a `<track>` that
    will fail — so this is the one builder that looks at the filesystem. One stat, on the
    episode-detail path only, off the event loop.
    """
    if not episode_id or any(bad in episode_id for bad in ("/", "\\", "\x00")):
        return None
    path = get_settings().thumbs_dir / episode_id / _THUMBNAILS_NAME
    return thumbnails_url(episode_id) if await asyncio.to_thread(path.is_file) else None


# --- Serialization --------------------------------------------------------------------------


def _aware(value: datetime) -> datetime:
    """`as_aware` for a NOT NULL column: same conversion, non-optional return type."""
    return as_aware(value) or value


def progress_out(progress: WatchProgress) -> WatchProgressOut:
    return WatchProgressOut(
        episode_id=progress.episode_id,
        position_seconds=progress.position_seconds,
        duration_seconds=progress.duration_seconds,
        percent=progress.percent,
        completed=progress.completed,
        play_count=progress.play_count,
        resume_position_seconds=resume_position(progress),
        first_played_at=as_aware(progress.first_played_at),
        last_played_at=as_aware(progress.last_played_at),
    )


def optional_progress_out(progress: WatchProgress | None) -> WatchProgressOut | None:
    return None if progress is None else progress_out(progress)


def episode_summary(episode: Episode, progress: WatchProgress | None) -> EpisodeSummary:
    media = episode.primary_file
    runtime = episode.runtime_seconds
    if media is not None and media.duration_seconds:
        runtime = int(media.duration_seconds)
    return EpisodeSummary(
        id=episode.id,
        series_id=episode.series_id,
        season_number=episode.season_number,
        episode_number=episode.episode_number,
        absolute_number=episode.absolute_number,
        title=episode.title,
        air_date=episode.air_date,
        still_path=episode.still_path,
        still_url=image_url(episode.still_path, STILL_SIZE),
        runtime_seconds=runtime,
        available=media is not None,
        direct_play_ok=media.direct_play_ok if media is not None else True,
        watched=bool(progress is not None and progress.completed),
        in_progress=bool(progress is not None and not progress.completed and progress.percent > 0),
        percent=float(progress.percent) if progress is not None else 0.0,
        resume_position_seconds=resume_position(progress),
        play_count=progress.play_count if progress is not None else 0,
    )


def series_summary(series: Series, counts: SeriesCounts | None = None) -> SeriesSummary:
    counts = counts or SeriesCounts()
    return SeriesSummary(
        id=series.id,
        slug=series.slug,
        title=series.title,
        sort_title=series.sort_title or series.title,
        original_title=series.original_title,
        year=series.year,
        status=series.status,
        poster_path=series.poster_path,
        backdrop_path=series.backdrop_path,
        poster_url=image_url(series.poster_path, POSTER_SIZE),
        genres=list(series.genres or []),
        network=series.network,
        content_rating=series.content_rating,
        episode_count=counts.episode_count,
        available_episode_count=counts.available_count,
        declared_episode_count=series.declared_episode_count,
        watched_episode_count=counts.watched_count,
        progress_percent=counts.percent,
        added_at=_aware(series.added_at),
        last_played_at=as_aware(counts.last_played_at),
    )


def unmatched_file_out(media: MediaFile) -> UnmatchedFileOut:
    return UnmatchedFileOut(
        id=media.id,
        path=media.path,
        size_bytes=media.size_bytes,
        container=media.container,
        video_codec=media.video_codec,
        audio_codec=media.audio_codec,
        duration_seconds=media.duration_seconds,
        width=media.width,
        height=media.height,
        direct_play_ok=media.direct_play_ok,
        status=media.status,
        scan_error=media.scan_error,
        first_seen_at=_aware(media.first_seen_at),
        scanned_at=as_aware(media.scanned_at),
    )


# --- Lookups --------------------------------------------------------------------------------


async def get_series_or_404(session: AsyncSession, id_or_slug: str) -> Series:
    stmt = select(Series).where(or_(Series.id == id_or_slug, Series.slug == id_or_slug))
    series = (await session.execute(stmt)).scalars().first()
    if series is None:
        raise NotFound("Series", "SERIES_NOT_FOUND")
    return series


async def get_episode_or_404(session: AsyncSession, episode_id: str) -> Episode:
    # `episode_summary` / `episode_detail` read `media_files` (and, through it, the track
    # collections) from sync property code. A bare `session.get` returns an identity-map
    # instance untouched when the caller already loaded or created that episode in this
    # session, leaving the collection unloaded — the lazy load then raises MissingGreenlet.
    # Forcing the eager load here keeps every caller safe instead of each one remembering.
    episode = await session.get(
        Episode,
        episode_id,
        options=[selectinload(Episode.media_files)],
        populate_existing=True,
    )
    if episode is None:
        raise NotFound("Episode", "EPISODE_NOT_FOUND")
    return episode


async def series_counts(
    session: AsyncSession, series_ids: list[str], user_id: str
) -> dict[str, SeriesCounts]:
    if not series_ids:
        return {}
    stmt = _aggregate_select(user_id).where(Episode.series_id.in_(series_ids))
    return {
        row.series_id: SeriesCounts(
            episode_count=int(row.episode_count or 0),
            available_count=int(row.available_count or 0),
            watched_count=int(row.watched_count or 0),
            last_played_at=as_aware(row.last_played_at),
        )
        for row in (await session.execute(stmt)).all()
    }


async def episodes_with_progress(
    session: AsyncSession, series_id: str, user_id: str
) -> list[tuple[Episode, WatchProgress | None]]:
    """Ordered episodes joined to this user's state. `media_files` come along via selectin,
    so availability costs one extra query for the whole series rather than one per episode."""
    stmt = (
        select(Episode, WatchProgress)
        .outerjoin(
            WatchProgress,
            and_(
                WatchProgress.episode_id == Episode.id,
                WatchProgress.user_id == user_id,
            ),
        )
        .where(Episode.series_id == series_id)
        .order_by(Episode.season_number, Episode.episode_number)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


# --- Aggregates -----------------------------------------------------------------------------


def _progress_from_pairs(
    series: Series, pairs: list[tuple[Episode, WatchProgress | None]]
) -> SeriesProgress:
    episode_count = len(pairs)
    available = 0
    watched = 0
    next_episode_id: str | None = None
    resume_episode_id: str | None = None
    resume_at: datetime | None = None

    for episode, progress in pairs:
        is_available = episode.is_available
        available += 1 if is_available else 0
        if progress is not None and progress.completed:
            watched += 1
        if (
            is_available
            and next_episode_id is None
            and not (progress is not None and progress.completed)
        ):
            next_episode_id = episode.id
        if is_available and resume_position(progress) is not None and progress is not None:
            last = as_aware(progress.last_played_at)
            if resume_at is None or (last is not None and last > resume_at):
                resume_at = last
                resume_episode_id = episode.id

    percent = min(100.0, watched / available * 100.0) if available else 0.0
    return SeriesProgress(
        episode_count=episode_count,
        available_episode_count=available,
        declared_episode_count=series.declared_episode_count,
        watched_episode_count=watched,
        percent=percent,
        completed=available > 0 and watched >= available,
        resume_episode_id=resume_episode_id,
        next_episode_id=next_episode_id,
    )


async def series_progress(session: AsyncSession, series: Series, user_id: str) -> SeriesProgress:
    pairs = await episodes_with_progress(session, series.id, user_id)
    return _progress_from_pairs(series, pairs)


# --- Listing --------------------------------------------------------------------------------


async def list_series(
    session: AsyncSession,
    *,
    user_id: str,
    q: str | None = None,
    genre: str | None = None,
    status: str | None = None,
    language: str | None = None,
    sort: SortKey = "title",
    available_only: bool = False,
    page: int = 1,
    page_size: int = 40,
) -> Page[SeriesSummary]:
    agg = _aggregate_select(user_id).subquery()
    episode_count = func.coalesce(agg.c.episode_count, 0)
    available_count = func.coalesce(agg.c.available_count, 0)
    watched_count = func.coalesce(agg.c.watched_count, 0)

    conditions: list[ColumnElement[bool]] = []
    if q:
        # `normalize` strips punctuation, so a query made only of it (`%`, `_`, `***`) reduces
        # to "". Interpolating that would build the pattern `%%`, which matches every row and
        # silently turns the filter into a no-op — the opposite of what the caller asked for.
        # Each spelling therefore only contributes a clause when it has something left to match.
        term = q.strip()
        normalized_term = normalize(q)
        matches: list[ColumnElement[bool]] = []
        if term:
            raw = f"%{_escape_like(term)}%"
            matches.append(Series.title.ilike(raw, escape="\\"))
            matches.append(Series.original_title.ilike(raw, escape="\\"))
        if normalized_term:
            normalized = f"%{_escape_like(normalized_term)}%"
            matches.append(Series.sort_title.like(normalized, escape="\\"))
            matches.append(
                select(SeriesTitle.id)
                .where(
                    SeriesTitle.series_id == Series.id,
                    SeriesTitle.normalized.like(normalized, escape="\\"),
                )
                .exists()
            )
        if matches:
            conditions.append(or_(*matches))
    if genre:
        # `genres` is a JSON array of strings. Matching the quoted form against the serialized
        # column is exact-element (the quotes delimit) and needs no JSON1 extension.
        conditions.append(
            cast(Series.genres, String).like(f'%"{_escape_like(genre)}"%', escape="\\")
        )
    if status:
        conditions.append(Series.status == status)
    if language:
        conditions.append(Series.original_language == language)
    if available_only:
        conditions.append(available_count > 0)

    joined = (
        select(
            Series,
            episode_count.label("episode_count"),
            available_count.label("available_count"),
            watched_count.label("watched_count"),
            agg.c.last_played_at,
        )
        .outerjoin(agg, agg.c.series_id == Series.id)
        .where(*conditions)
    )

    total = (
        await session.execute(
            select(func.count()).select_from(
                select(Series.id)
                .outerjoin(agg, agg.c.series_id == Series.id)
                .where(*conditions)
                .subquery()
            )
        )
    ).scalar_one()

    if sort == "added":
        joined = joined.order_by(Series.added_at.desc(), Series.id)
    elif sort == "recent":
        joined = joined.order_by(agg.c.last_played_at.desc(), Series.added_at.desc())
    elif sort == "progress":
        ratio = case((available_count > 0, watched_count * 1.0 / available_count), else_=0.0)
        joined = joined.order_by(ratio.desc(), agg.c.last_played_at.desc(), Series.id)
    else:
        # One expression, shared with `series_neighbours`. If these two ever disagree the
        # arrows walk a different sequence than the page they came from, which is the kind of
        # difference nobody notices until two titles sort the same.
        joined = joined.order_by(*_catalogue_order())

    joined = joined.limit(page_size).offset(max(0, (page - 1) * page_size))
    rows = (await session.execute(joined)).all()

    items = [
        series_summary(
            row[0],
            SeriesCounts(
                episode_count=int(row.episode_count or 0),
                available_count=int(row.available_count or 0),
                watched_count=int(row.watched_count or 0),
                last_played_at=as_aware(row.last_played_at),
            ),
        )
        for row in rows
    ]
    return Page[SeriesSummary](items=items, total=int(total), page=page, page_size=page_size)


async def recently_added(
    session: AsyncSession, *, user_id: str, limit: int = 20
) -> list[SeriesSummary]:
    result = await list_series(
        session, user_id=user_id, sort="added", page=1, page_size=max(1, limit)
    )
    return result.items


async def genre_facets(session: AsyncSession) -> list[GenreFacet]:
    """One query; the genre lists are tiny JSON arrays, so the tally is cheaper in Python
    than a json_each join."""
    rows = (await session.execute(select(Series.genres))).scalars().all()
    tally: dict[str, int] = {}
    for genres in rows:
        for name in genres or []:
            if not name:
                continue
            tally[name] = tally.get(name, 0) + 1
    return [
        GenreFacet(genre=name, count=count)
        for name, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0].casefold()))
    ]


# --- Detail ---------------------------------------------------------------------------------


#: The catalogue's canonical order, as one expression per level.
#:
#: Level 1 and 2 are exactly what the `title` sort in `list_series` orders by, so the arrows walk
#: the sequence Explorar shows by default. Level 3 is the id, which that sort does not carry: a
#: list may leave two identically-titled rows in either order and nobody notices, but a *walk*
#: needs a total order or a pair of ties becomes a loop between two titles with no way past.
def _catalogue_order() -> tuple[ColumnElement[Any], ...]:
    # `lower()` on the first level because the two things it coalesces are cased differently:
    # `sort_title` is written by `normalize()` and is lower case, while the fallback is the
    # display title. SQLite collates binary, so without folding every row that fell back sorted
    # *before* every row that did not — "Zeta" ahead of "alfa" — which reads as a random title
    # pinned to the front of the catalogue. A no-op for well-formed rows, since theirs is
    # already lower case.
    return (
        func.lower(func.coalesce(func.nullif(Series.sort_title, ""), Series.title)),
        Series.title,
        Series.id,
    )


async def series_neighbours(
    session: AsyncSession, series: Series
) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """The titles either side of this one in the catalogue's order.

    Returns `(previous, next)`, each either `(slug, title)` or None at the ends.

    Written as a lexicographic comparison rather than by loading the ordered list and finding an
    index. At seventeen series either works; the difference is that this one keeps working, and
    the list form has a specific trap — `GET /library/series` caps `page_size` at 100, so a
    client walking the list would start **silently** skipping neighbours at the 101st series
    with nothing failing anywhere.

    Two queries rather than a window function, because SQLite's `LAG`/`LEAD` need a full scan of
    the ordered set anyway and this shape can use an index on the sort key.
    """
    first, second, third = _catalogue_order()
    # The anchor must be folded exactly the way `_catalogue_order` folds the column, or the
    # comparison is against a value that is in the ordering nowhere. That mistake does not
    # raise: it returns a neighbour on the wrong side, and the walk loops between two titles
    # forever. `test_the_walk_visits_exactly_what_the_list_shows_in_the_same_order` is what
    # catches it, by hanging rather than by asserting.
    anchor = (
        (series.sort_title or series.title).lower(),
        series.title,
        series.id,
    )

    # `(a, b, c) < (x, y, z)` spelled out. A row-value comparison would read better and is
    # supported by SQLite 3.15+, but this form is what every backend agrees on and the query
    # planner treats it the same.
    def before() -> ColumnElement[bool]:
        return or_(
            first < anchor[0],
            and_(first == anchor[0], second < anchor[1]),
            and_(first == anchor[0], second == anchor[1], third < anchor[2]),
        )

    def after() -> ColumnElement[bool]:
        return or_(
            first > anchor[0],
            and_(first == anchor[0], second > anchor[1]),
            and_(first == anchor[0], second == anchor[1], third > anchor[2]),
        )

    previous_row = (
        await session.execute(
            select(Series.slug, Series.title)
            .where(before())
            .order_by(first.desc(), second.desc(), third.desc())
            .limit(1)
        )
    ).first()
    next_row = (
        await session.execute(
            select(Series.slug, Series.title)
            .where(after())
            .order_by(first, second, third)
            .limit(1)
        )
    ).first()

    return (
        (previous_row[0], previous_row[1]) if previous_row else None,
        (next_row[0], next_row[1]) if next_row else None,
    )


async def series_detail(session: AsyncSession, series: Series, user_id: str) -> SeriesDetail:
    pairs = await episodes_with_progress(session, series.id, user_id)
    progress = _progress_from_pairs(series, pairs)
    previous_series, next_series = await series_neighbours(session, series)

    seasons: list[SeasonGroup] = []
    by_id: dict[str, EpisodeSummary] = {}
    for episode, row in pairs:
        summary = episode_summary(episode, row)
        by_id[summary.id] = summary
        if not seasons or seasons[-1].season_number != episode.season_number:
            seasons.append(SeasonGroup(season_number=episode.season_number))
        group = seasons[-1]
        group.episodes.append(summary)
        group.episode_count += 1
        group.available_episode_count += 1 if summary.available else 0
        group.watched_episode_count += 1 if summary.watched else 0

    # What Play/Resume starts. `_progress_from_pairs` already picked both candidates out of
    # the same rows, so this costs a dict lookup rather than another query.
    next_id = progress.resume_episode_id or progress.next_episode_id
    next_episode = by_id.get(next_id) if next_id else None

    counts = SeriesCounts(
        episode_count=progress.episode_count,
        available_count=progress.available_episode_count,
        watched_count=progress.watched_episode_count,
    )
    base = series_summary(series, counts)
    return SeriesDetail(
        **base.model_dump(),
        overview=series.overview,
        # Provenance travels with the text. For a Wikipedia extract the URL is the attribution
        # its licence requires, so anything that renders the overview has to be able to show it.
        overview_source=series.overview_source,
        overview_source_url=series.overview_source_url,
        overview_source_lang=series.overview_source_lang,
        original_language=series.original_language,
        backdrop_url=image_url(series.backdrop_path, BACKDROP_SIZE),
        tmdb_id=series.tmdb_id,
        imdb_id=series.imdb_id,
        dramadex_id=series.dramadex_id,
        cast=[
            CastMemberOut(
                dramadex_person_id=member.dramadex_person_id,
                name=member.name,
                role=member.role,
                billing_order=member.billing_order,
            )
            for member in series.cast
        ],
        links=[
            SeriesLinkOut(
                studio_key=link.studio_key, studio_name=link.studio_name, url=link.url
            )
            for link in series.links
        ],
        library_path=series.library_path,
        metadata_locked=series.metadata_locked,
        updated_at=as_aware(series.updated_at),
        titles=[
            SeriesTitleOut(
                id=title.id,
                language=title.language,
                title=title.title,
                kind=title.kind,
                source=title.source,
                normalized=title.normalized,
            )
            for title in sorted(series.titles, key=lambda t: (t.kind, t.language, t.title))
        ],
        seasons=seasons,
        progress=progress,
        next_episode=next_episode,
        previous_series_slug=previous_series[0] if previous_series else None,
        previous_series_title=previous_series[1] if previous_series else None,
        next_series_slug=next_series[0] if next_series else None,
        next_series_title=next_series[1] if next_series else None,
    )


async def series_episodes(
    session: AsyncSession, series_id: str, user_id: str
) -> list[EpisodeSummary]:
    pairs = await episodes_with_progress(session, series_id, user_id)
    return [episode_summary(episode, progress) for episode, progress in pairs]


async def _neighbour_ids(session: AsyncSession, episode: Episode) -> tuple[str | None, str | None]:
    """Previous/next *available* episode, which is what the player's next-up card needs."""
    rows = (
        await session.execute(
            select(Episode.id, _present_file().label("available"))
            .where(Episode.series_id == episode.series_id)
            .order_by(Episode.season_number, Episode.episode_number)
        )
    ).all()
    ordered = [(row[0], bool(row[1])) for row in rows]
    try:
        index = [eid for eid, _ in ordered].index(episode.id)
    except ValueError:
        return None, None
    previous = next((eid for eid, ok in reversed(ordered[:index]) if ok), None)
    following = next((eid for eid, ok in ordered[index + 1 :] if ok), None)
    return previous, following


async def episode_detail(session: AsyncSession, episode: Episode, user_id: str) -> EpisodeDetail:
    progress = await get_progress(session, user_id, episode.id)
    series = await session.get(Series, episode.series_id)
    media = episode.primary_file
    previous_id, next_id = await _neighbour_ids(session, episode)

    base = episode_summary(episode, progress)
    return EpisodeDetail(
        **base.model_dump(),
        overview=episode.overview,
        metadata_locked=episode.metadata_locked,
        tmdb_id=episode.tmdb_id,
        series_title=series.title if series is not None else "",
        series_slug=series.slug if series is not None else "",
        # The player reads these flat; `media_file` keeps the same facts for the admin view.
        duration_seconds=media.duration_seconds if media is not None else None,
        width=media.width if media is not None else None,
        height=media.height if media is not None else None,
        video_codec=media.video_codec if media is not None else None,
        audio_codec=media.audio_codec if media is not None else None,
        container=media.container if media is not None else None,
        stream_url=stream_url(episode.id),
        thumbnails_url=await thumbnails_url_if_ready(episode.id),
        media_file=(
            MediaFileOut(
                id=media.id,
                path=media.path,
                container=media.container,
                video_codec=media.video_codec,
                audio_codec=media.audio_codec,
                width=media.width,
                height=media.height,
                duration_seconds=media.duration_seconds,
                bitrate=media.bitrate,
                size_bytes=media.size_bytes,
                direct_play_ok=media.direct_play_ok,
                status=media.status,
            )
            if media is not None
            else None
        ),
        audio_tracks=[
            AudioTrackOut(
                id=track.id,
                stream_index=track.stream_index,
                language=track.language,
                title=track.title,
                codec=track.codec,
                channels=track.channels,
                is_default=track.is_default,
            )
            for track in (media.audio_tracks if media is not None else [])
        ],
        subtitle_tracks=[
            SubtitleTrackOut(
                id=track.id,
                stream_index=track.stream_index,
                language=track.language,
                title=track.title,
                codec=track.codec,
                is_default=track.is_default,
                is_forced=track.is_forced,
                is_sidecar=track.sidecar_path is not None,
                url=subtitle_url(episode.id, track.id),
            )
            for track in (media.subtitle_tracks if media is not None else [])
        ],
        progress=optional_progress_out(progress),
        previous_episode_id=previous_id,
        next_episode_id=next_id,
    )


# --- Continue watching / history --------------------------------------------------------------


async def _recent_series_activity(
    session: AsyncSession, user_id: str
) -> list[tuple[str, datetime]]:
    """Series ordered by the user's last activity. `play_events` is the contract's ordering
    source; watch_progress is merged in so force-marked series are not lost."""
    events = (
        await session.execute(
            select(PlayEvent.series_id, func.max(PlayEvent.at))
            .where(PlayEvent.user_id == user_id)
            .group_by(PlayEvent.series_id)
        )
    ).all()
    states = (
        await session.execute(
            select(Episode.series_id, func.max(WatchProgress.last_played_at))
            .join(WatchProgress, WatchProgress.episode_id == Episode.id)
            .where(WatchProgress.user_id == user_id)
            .group_by(Episode.series_id)
        )
    ).all()

    latest: dict[str, datetime] = {}
    for series_id, at in [*events, *states]:
        moment = as_aware(at)
        if series_id is None or moment is None:
            continue
        current = latest.get(series_id)
        if current is None or moment > current:
            latest[series_id] = moment
    ordered = sorted(latest.items(), key=lambda kv: kv[1], reverse=True)
    return ordered[:_MAX_ACTIVITY_CANDIDATES]


async def continue_watching(
    session: AsyncSession, user_id: str, *, limit: int = 30
) -> list[ContinueWatchingItem]:
    activity = await _recent_series_activity(session, user_id)
    if not activity:
        return []
    series_ids = [series_id for series_id, _ in activity]

    # The episode still in flight, most recent first within each series.
    ranked = func.row_number().over(
        partition_by=Episode.series_id,
        order_by=WatchProgress.last_played_at.desc(),
    )
    in_flight_sq = (
        select(
            Episode.series_id.label("series_id"),
            Episode.id.label("episode_id"),
            ranked.label("rank"),
        )
        .join(
            WatchProgress,
            and_(
                WatchProgress.episode_id == Episode.id,
                WatchProgress.user_id == user_id,
            ),
        )
        .where(
            Episode.series_id.in_(series_ids),
            WatchProgress.completed.is_(False),
            WatchProgress.position_seconds > 0,
            _present_file(),
        )
        .subquery()
    )
    chosen: dict[str, str] = {
        row.series_id: row.episode_id
        for row in (
            await session.execute(
                select(in_flight_sq.c.series_id, in_flight_sq.c.episode_id).where(
                    in_flight_sq.c.rank == 1
                )
            )
        ).all()
    }

    # Everything else falls back to the next episode never finished.
    remaining = [series_id for series_id in series_ids if series_id not in chosen]
    if remaining:
        watched_exists = (
            select(WatchProgress.id)
            .where(
                WatchProgress.episode_id == Episode.id,
                WatchProgress.user_id == user_id,
                WatchProgress.completed.is_(True),
            )
            .exists()
        )
        next_ranked = func.row_number().over(
            partition_by=Episode.series_id,
            order_by=(Episode.season_number, Episode.episode_number),
        )
        next_sq = (
            select(
                Episode.series_id.label("series_id"),
                Episode.id.label("episode_id"),
                next_ranked.label("rank"),
            )
            .where(
                Episode.series_id.in_(remaining),
                ~watched_exists,
                _present_file(),
            )
            .subquery()
        )
        for row in (
            await session.execute(
                select(next_sq.c.series_id, next_sq.c.episode_id).where(next_sq.c.rank == 1)
            )
        ).all():
            chosen[row.series_id] = row.episode_id

    if not chosen:
        return []

    episode_ids = list(chosen.values())
    episodes = {
        episode.id: episode
        for episode in (await session.execute(select(Episode).where(Episode.id.in_(episode_ids))))
        .scalars()
        .all()
    }
    series_rows = {
        series.id: series
        for series in (
            await session.execute(select(Series).where(Series.id.in_(list(chosen.keys()))))
        )
        .scalars()
        .all()
    }
    states = await progress_for_episodes(session, user_id, episode_ids)
    counts = await series_counts(session, list(chosen.keys()), user_id)

    items: list[ContinueWatchingItem] = []
    for series_id, last_played_at in activity:
        episode_id = chosen.get(series_id)
        series = series_rows.get(series_id)
        episode = episodes.get(episode_id or "")
        if episode is None or series is None:
            continue
        state = states.get(episode.id)
        items.append(
            ContinueWatchingItem(
                series=series_summary(series, counts.get(series_id)),
                episode=episode_summary(episode, state),
                progress=optional_progress_out(state),
                last_played_at=last_played_at,
            )
        )
        if len(items) >= limit:
            break
    return items


async def watch_history(
    session: AsyncSession, user_id: str, *, page: int = 1, page_size: int = 30
) -> Page[HistoryItem]:
    total = (
        await session.execute(
            select(func.count()).select_from(WatchProgress).where(WatchProgress.user_id == user_id)
        )
    ).scalar_one()

    rows = (
        await session.execute(
            select(WatchProgress, Episode, Series)
            .join(Episode, Episode.id == WatchProgress.episode_id)
            .join(Series, Series.id == Episode.series_id)
            .where(WatchProgress.user_id == user_id)
            .order_by(WatchProgress.last_played_at.desc())
            .limit(page_size)
            .offset(max(0, (page - 1) * page_size))
        )
    ).all()

    counts = await series_counts(session, [row[2].id for row in rows], user_id)
    items = [
        HistoryItem(
            series=series_summary(row[2], counts.get(row[2].id)),
            episode=episode_summary(row[1], row[0]),
            progress=progress_out(row[0]),
        )
        for row in rows
    ]
    return Page[HistoryItem](items=items, total=int(total), page=page, page_size=page_size)


# --- Unmatched files ------------------------------------------------------------------------


async def list_unmatched(
    session: AsyncSession, *, page: int = 1, page_size: int = 50
) -> Page[UnmatchedFileOut]:
    condition = MediaFile.episode_id.is_(None)
    total = (
        await session.execute(select(func.count()).select_from(MediaFile).where(condition))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(MediaFile)
                .where(condition)
                .order_by(MediaFile.path)
                .limit(page_size)
                .offset(max(0, (page - 1) * page_size))
            )
        )
        .scalars()
        .all()
    )
    return Page[UnmatchedFileOut](
        items=[unmatched_file_out(media) for media in rows],
        total=int(total),
        page=page,
        page_size=page_size,
    )


# --- Deletion and cleanup ---------------------------------------------------------------------
#
# Everything below removes rows from the catalogue and nothing else. The one thing here that
# touches `LIBRARY_ROOT` is `_resolve_existing`, and all it does is ask whether a file is there.


#: SQLite's default host-parameter limit is 999 on older builds, so id lists are fed in slices
#: rather than as one enormous IN. A library-wide cleanup can easily name thousands of rows.
_ID_CHUNK = 400


def _chunks(ids: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(ids), _ID_CHUNK):
        yield ids[start : start + _ID_CHUNK]


async def _count(session: AsyncSession, stmt: Select[Any]) -> int:
    return int((await session.execute(stmt)).scalar_one() or 0)


# --- Is the file actually there? ---------------------------------------------------------
#
# `media_files.status` is the scanner's opinion as of its last pass, and the admin is about to
# make an irreversible decision on it. These read the mount instead, always in a thread —
# a stat against a sleeping NAS is not something to do on the event loop.


def _resolve_existing(stored: str) -> bool:
    """True when a library-relative path is a real file under `LIBRARY_ROOT`. Never raises.

    `safe_media_path` resolves symlinks and refuses anything that escapes the mount, which is
    the same gate the streaming layer opens files through. A path it rejects is one no viewer
    could ever play, so it is not "still on disk" in any sense the admin cares about.
    """
    try:
        resolved = safe_media_path(stored)
    except AppError:
        return False
    try:
        return resolved.is_file()
    except OSError:
        return False


def _any_present_on_disk(paths: Sequence[str]) -> bool:
    return any(_resolve_existing(path) for path in paths)


def _episodes_with_a_file_on_disk(rows: Sequence[tuple[str, str]]) -> set[str]:
    """`(episode_id, path)` pairs in, the episode ids whose file is really there out."""
    found: set[str] = set()
    for episode_id, path in rows:
        if episode_id not in found and _resolve_existing(path):
            found.add(episode_id)
    return found


async def _files_on_disk(paths: Sequence[str]) -> bool:
    """One stat per path, off the event loop, stopping at the first file that exists."""
    if not paths:
        return False
    return await asyncio.to_thread(_any_present_on_disk, paths)


async def _require_library_mount() -> None:
    """Refuse to reason about absence while the mount itself is absent."""
    root = get_settings().library_root
    if not await asyncio.to_thread(root.is_dir):
        raise AppError(
            "LIBRARY_UNAVAILABLE",
            f"Refusing to touch the catalogue while {root} is unreadable",
            503,
        )


# --- What a delete would take ------------------------------------------------------------


@dataclass(slots=True)
class _SeriesFacts:
    episode_count: int = 0
    media_file_count: int = 0
    present_file_count: int = 0
    missing_file_count: int = 0
    users_with_progress: int = 0
    #: Paths of the `present` files, for the one question the database cannot answer.
    present_paths: list[str] = field(default_factory=list)


async def _series_facts(session: AsyncSession, series_id: str) -> _SeriesFacts:
    episode_ids = select(Episode.id).where(Episode.series_id == series_id).scalar_subquery()
    facts = _SeriesFacts(
        episode_count=await _count(
            session,
            select(func.count()).select_from(Episode).where(Episode.series_id == series_id),
        ),
        users_with_progress=await _count(
            session,
            select(func.count(func.distinct(WatchProgress.user_id))).where(
                WatchProgress.episode_id.in_(episode_ids)
            ),
        ),
    )
    rows = (
        await session.execute(
            select(MediaFile.status, MediaFile.path).where(MediaFile.episode_id.in_(episode_ids))
        )
    ).all()
    for status, path in rows:
        facts.media_file_count += 1
        if status == MediaStatus.PRESENT:
            facts.present_file_count += 1
            facts.present_paths.append(path)
        elif status == MediaStatus.MISSING:
            facts.missing_file_count += 1
    return facts


#: Fields whose change means "a human curated this row", so the nightly jobs must leave it be.
SERIES_METADATA_FIELDS = frozenset(
    {
        "title",
        "sort_title",
        "original_title",
        "original_language",
        "overview",
        "year",
        "status",
        "content_rating",
        "genres",
        "network",
        "declared_episode_count",
        "imdb_id",
    }
)


async def apply_series_patch(
    session: AsyncSession,
    series: Series,
    changes: dict[str, Any],
    *,
    metadata_locked: bool | None = None,
) -> bool:
    """Write an admin's edit onto one series. Returns whether the title index needs rebuilding.

    Extracted so that editing one series and editing forty go through the **same** code, which
    is the only way the two stay honest about the three things easy to get wrong in a bulk
    path: canonicalizing genres, network and rating on the way in (or `dramabox` and `DramaBox`
    become two networks in the filter list); setting `metadata_locked` so the nightly refresh
    does not undo the edit; and recomputing `sort_title` when the title moved.

    `changes` must already be the *set* fields only — a dict built from
    `model_dump(exclude_unset=True)`. A patch that carried every field with its default would
    blank the columns the admin never touched, and in bulk that is forty rows at once.
    """
    if "genres" in changes:
        changes["genres"] = normalize_genres(changes["genres"] or [])
    if "network" in changes:
        changes["network"] = canonical_network(changes["network"])
    if "content_rating" in changes:
        changes["content_rating"] = canonical_content_rating(changes["content_rating"])

    titles_changed = False
    metadata_changed = False
    for field_name, value in changes.items():
        if field_name == "metadata_locked":
            continue
        if getattr(series, field_name) == value:
            continue
        setattr(series, field_name, value)
        metadata_changed = metadata_changed or field_name in SERIES_METADATA_FIELDS
        titles_changed = titles_changed or field_name in {
            "title",
            "original_title",
            "sort_title",
        }

    if "title" in changes and "sort_title" not in changes:
        series.sort_title = normalize(series.title) or series.title
        titles_changed = True

    if metadata_changed:
        series.metadata_locked = True
    # An explicit flag in the body wins, so an admin can hand the row back to the jobs.
    if metadata_locked is not None:
        series.metadata_locked = metadata_locked

    series.updated_at = utcnow()
    await session.flush()
    if titles_changed:
        await reindex_series(session, series.id)
    return titles_changed


async def deletion_preview(session: AsyncSession, series: Series) -> SeriesDeletionPreview:
    """Everything the admin needs before confirming, including whether it would stick."""
    facts = await _series_facts(session, series.id)
    return SeriesDeletionPreview(
        series_title=series.title,
        episode_count=facts.episode_count,
        media_file_count=facts.media_file_count,
        present_file_count=facts.present_file_count,
        missing_file_count=facts.missing_file_count,
        users_with_progress=facts.users_with_progress,
        files_still_on_disk=await _files_on_disk(facts.present_paths),
    )


# --- Removing rows -----------------------------------------------------------------------


async def _episode_child_leftovers(
    session: AsyncSession, episode_ids: Sequence[str]
) -> dict[str, int]:
    """Rows that should have gone with these episodes and did not."""
    leftovers: dict[str, int] = {}
    for chunk in _chunks(episode_ids):
        media_ids = select(MediaFile.id).where(MediaFile.episode_id.in_(chunk)).scalar_subquery()
        counted: dict[str, Select[Any]] = {
            "episodes": select(func.count()).select_from(Episode).where(Episode.id.in_(chunk)),
            "media_files": select(func.count())
            .select_from(MediaFile)
            .where(MediaFile.episode_id.in_(chunk)),
            "audio_tracks": select(func.count())
            .select_from(AudioTrack)
            .where(AudioTrack.media_file_id.in_(media_ids)),
            "subtitle_tracks": select(func.count())
            .select_from(SubtitleTrack)
            .where(SubtitleTrack.media_file_id.in_(media_ids)),
            "episode_titles": select(func.count())
            .select_from(EpisodeTitle)
            .where(EpisodeTitle.episode_id.in_(chunk)),
            "watch_progress": select(func.count())
            .select_from(WatchProgress)
            .where(WatchProgress.episode_id.in_(chunk)),
            "play_events": select(func.count())
            .select_from(PlayEvent)
            .where(PlayEvent.episode_id.in_(chunk)),
        }
        for name, stmt in counted.items():
            total = await _count(session, stmt)
            if total:
                leftovers[name] = leftovers.get(name, 0) + total
    return leftovers


async def _sweep_episode_children(session: AsyncSession, episode_ids: Sequence[str]) -> None:
    """Delete by hand, deepest first, what the cascade should have taken."""
    for chunk in _chunks(episode_ids):
        media_ids = select(MediaFile.id).where(MediaFile.episode_id.in_(chunk)).scalar_subquery()
        for stmt in (
            sql_delete(AudioTrack).where(AudioTrack.media_file_id.in_(media_ids)),
            sql_delete(SubtitleTrack).where(SubtitleTrack.media_file_id.in_(media_ids)),
            sql_delete(MediaFile).where(MediaFile.episode_id.in_(chunk)),
            sql_delete(EpisodeTitle).where(EpisodeTitle.episode_id.in_(chunk)),
            sql_delete(WatchProgress).where(WatchProgress.episode_id.in_(chunk)),
            sql_delete(PlayEvent).where(PlayEvent.episode_id.in_(chunk)),
            sql_delete(Episode).where(Episode.id.in_(chunk)),
        ):
            await session.execute(stmt, execution_options={"synchronize_session": False})
    await session.flush()


async def _confirm_children_gone(session: AsyncSession, episode_ids: Sequence[str]) -> None:
    """Check the cascade rather than trust it.

    Every child table declares ON DELETE CASCADE, but SQLite honours a foreign key only when
    `PRAGMA foreign_keys=ON` is set on the connection — `app/db.py` does that on connect. A
    connection that missed it would leave orphaned media files and, worse, orphaned watch
    progress behind *silently*, so the deletion verifies its own outcome and repairs it.
    """
    if not episode_ids:
        return
    leftovers = await _episode_child_leftovers(session, episode_ids)
    if not leftovers:
        return
    log.warning(
        "ON DELETE CASCADE left %s behind; this connection is missing PRAGMA foreign_keys=ON. "
        "Removing the rows explicitly.",
        leftovers,
    )
    await _sweep_episode_children(session, episode_ids)


async def _delete_episode_rows(session: AsyncSession, episode_ids: Sequence[str]) -> None:
    """Delete episodes; their media files, tracks, progress and play events follow."""
    if not episode_ids:
        return
    await session.flush()
    for chunk in _chunks(episode_ids):
        await session.execute(
            sql_delete(Episode).where(Episode.id.in_(chunk)),
            execution_options={"synchronize_session": False},
        )
    await session.flush()
    await _confirm_children_gone(session, episode_ids)


async def _delete_series_row(session: AsyncSession, series_id: str) -> None:
    """Delete one series row and let the cascade take the rest of the catalogue with it.

    Nothing under `LIBRARY_ROOT` is opened, moved or removed here — the files that produced
    this series stay exactly where they are.
    """
    episode_ids = list(
        (await session.execute(select(Episode.id).where(Episode.series_id == series_id))).scalars()
    )
    # Push anything pending before deleting behind the ORM's back.
    await session.flush()
    await session.execute(
        sql_delete(Series).where(Series.id == series_id),
        execution_options={"synchronize_session": False},
    )
    await session.flush()

    owned: dict[str, Select[Any]] = {
        "episodes": select(func.count()).select_from(Episode).where(Episode.series_id == series_id),
        "series_titles": select(func.count())
        .select_from(SeriesTitle)
        .where(SeriesTitle.series_id == series_id),
        "series_translations": select(func.count())
        .select_from(SeriesTranslation)
        .where(SeriesTranslation.series_id == series_id),
        "play_events": select(func.count())
        .select_from(PlayEvent)
        .where(PlayEvent.series_id == series_id),
    }
    leftovers = {
        name: total for name, stmt in owned.items() if (total := await _count(session, stmt))
    }
    if leftovers:
        log.warning(
            "ON DELETE CASCADE left %s behind for series %s; this connection is missing "
            "PRAGMA foreign_keys=ON. Removing the rows explicitly.",
            leftovers,
            series_id,
        )
        for stmt in (
            sql_delete(PlayEvent).where(PlayEvent.series_id == series_id),
            sql_delete(SeriesTitle).where(SeriesTitle.series_id == series_id),
            sql_delete(SeriesTranslation).where(SeriesTranslation.series_id == series_id),
        ):
            await session.execute(stmt, execution_options={"synchronize_session": False})
        await session.flush()
    await _confirm_children_gone(session, episode_ids)

    # Forget any live instance of the row. `Session.get` answers from the identity map without
    # touching the database, so a stale Series left in it would let `reindex_series` rebuild
    # this series' title rows from an object whose row no longer exists — an INSERT straight
    # into a foreign-key violation. Returns None (one cheap SELECT) when nothing is mapped.
    stale = await session.get(Series, series_id)
    if stale is not None:
        session.expunge(stale)


async def _forget_series(session: AsyncSession, series_id: str) -> None:
    """Drop what a deleted series leaves outside its own tables.

    With the row already gone, `reindex_series` degenerates to precisely what is wanted: it
    clears this series' slice of the FTS index and finds nothing to write back, so a deleted
    series stops answering searches. The cached artwork lives under DATA_DIR — never under
    LIBRARY_ROOT — and is ours to remove.
    """
    await reindex_series(session, series_id)
    # Deliberately not rolled back with the transaction: a poster deleted in error costs one
    # re-download from the `image_cache` job, while an orphaned directory would sit there for
    # the life of the install.
    await purge_series_images(series_id)


async def delete_series(
    session: AsyncSession, series: Series, *, actor_id: str | None = None
) -> SeriesDeleted:
    """Remove a series from the catalogue — not from the disk.

    The media files are left untouched, which is exactly why `files_still_on_disk` is measured
    again here and handed back: when it is true this delete bought the admin nothing, because
    the next scan re-creates the series from the same files.
    """
    series_id = series.id
    facts = await _series_facts(session, series_id)
    still_on_disk = await _files_on_disk(facts.present_paths)
    # Read for the audit trail while the row is still here to read.
    detail: dict[str, Any] = {
        "title": series.title,
        "slug": series.slug,
        "libraryPath": series.library_path,
        "episodes": facts.episode_count,
        "mediaFiles": facts.media_file_count,
        "usersWithProgress": facts.users_with_progress,
        "filesStillOnDisk": still_on_disk,
    }

    await _delete_series_row(session, series_id)
    await _forget_series(session, series_id)
    await audit.record(session, actor_id, "series.delete", "series", series_id, detail)

    return SeriesDeleted(
        deleted_episodes=facts.episode_count,
        deleted_media_files=facts.media_file_count,
        files_still_on_disk=still_on_disk,
    )


# --- Cleanup ------------------------------------------------------------------------------


@dataclass(slots=True)
class _CleanupPlan:
    """What a cleanup would remove. Computed identically whether or not it then runs."""

    #: episode id -> series id.
    episodes: dict[str, str] = field(default_factory=dict)
    media_file_count: int = 0
    #: `(series_id, title)` for the series this pass would leave with no episodes at all.
    series: list[tuple[str, str]] = field(default_factory=list)
    #: Ids of unmatched rows (`episode_id IS NULL`) whose file is no longer on the mount.
    #: They belong to no episode, so the episode-centric pass above cannot reach them, and
    #: without this they sit in the admin's "unmatched" view forever naming files that are
    #: gone — which is exactly what renaming a folder leaves behind.
    orphan_media_files: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.episodes and not self.series and not self.orphan_media_files


async def _plan_cleanup(session: AsyncSession) -> _CleanupPlan:
    """Which catalogue rows have lost their media. Reads only.

    Conservative in three separate ways, because every one of them is a way to delete
    something an admin wanted to keep:

    * an episode with no media file at all is never a candidate. That is how "declared by
      metadata but not available" is represented (§4) and it is a legitimate row — only an
      episode that once had files and has none left qualifies;
    * an episode survives while any of its files is `present` *or* `error`. An `error` file is
      one that is on the mount and would not probe, which is a different problem entirely;
    * every candidate is checked against the filesystem before it is condemned. `missing` is
      the scanner's opinion from its last pass; if the file is back, the episode is not gone.
    """
    plan = _CleanupPlan()

    # Unmatched rows first, and outside the episode logic entirely: they belong to no episode,
    # so nothing below would ever reach them. Same evidence standard as everything else — the
    # row must say `missing` AND the path must really be gone.
    orphans = [
        (str(row[0]), str(row[1]))
        for row in (
            await session.execute(
                select(MediaFile.id, MediaFile.path).where(
                    MediaFile.episode_id.is_(None), MediaFile.status == MediaStatus.MISSING
                )
            )
        ).all()
    ]
    if orphans:
        still_there = await asyncio.to_thread(_episodes_with_a_file_on_disk, orphans)
        plan.orphan_media_files = [
            media_id for media_id, _ in orphans if media_id not in still_there
        ]

    has_file = select(MediaFile.id).where(MediaFile.episode_id == Episode.id).exists()
    has_live_file = (
        select(MediaFile.id)
        .where(MediaFile.episode_id == Episode.id, MediaFile.status != MediaStatus.MISSING)
        .exists()
    )
    candidates: dict[str, str] = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(Episode.id, Episode.series_id).where(has_file, ~has_live_file)
            )
        ).all()
    }
    if not candidates:
        return plan

    rows: list[tuple[str, str]] = []
    for chunk in _chunks(list(candidates)):
        rows += [
            (str(row[0]), str(row[1]))
            for row in (
                await session.execute(
                    select(MediaFile.episode_id, MediaFile.path).where(
                        MediaFile.episode_id.in_(chunk)
                    )
                )
            ).all()
        ]
    resurfaced = await asyncio.to_thread(_episodes_with_a_file_on_disk, rows)
    plan.episodes = {
        episode_id: series_id
        for episode_id, series_id in candidates.items()
        if episode_id not in resurfaced
    }
    if not plan.episodes:
        return plan
    plan.media_file_count = sum(1 for episode_id, _ in rows if episode_id in plan.episodes)

    # A series goes only when this pass takes its last episode. A series that was already
    # empty is left alone: an empty row is what a freshly scanned folder of unmatched files
    # looks like, and nothing about it says its media is gone.
    doomed_per_series = Counter(plan.episodes.values())
    totals: dict[str, int] = {}
    for chunk in _chunks(list(doomed_per_series)):
        for row in (
            await session.execute(
                select(Episode.series_id, func.count(Episode.id))
                .where(Episode.series_id.in_(chunk))
                .group_by(Episode.series_id)
            )
        ).all():
            totals[row[0]] = int(row[1] or 0)
    emptied = [
        series_id
        for series_id, total in totals.items()
        if total and doomed_per_series[series_id] >= total
    ]
    for chunk in _chunks(emptied):
        plan.series += [
            (str(row[0]), str(row[1]))
            for row in (
                await session.execute(select(Series.id, Series.title).where(Series.id.in_(chunk)))
            ).all()
        ]
    plan.series.sort(key=lambda item: item[1].casefold())
    return plan


async def cleanup_catalogue(
    session: AsyncSession, *, dry_run: bool = False, actor_id: str | None = None
) -> CleanupResult:
    """Remove the catalogue entries whose media is entirely gone.

    Refuses to run while `LIBRARY_ROOT` is unreadable. With the mount down every file looks
    missing and this would delete the entire library — the precise outcome the scanner's
    never-delete rule exists to prevent.

    `dry_run` takes the same plan and stops short of applying it, so the numbers the admin
    confirms are the numbers they get.
    """
    await _require_library_mount()
    plan = await _plan_cleanup(session)
    result = CleanupResult(
        removed_series=[title for _, title in plan.series],
        removed_episodes=len(plan.episodes),
        removed_media_files=plan.media_file_count + len(plan.orphan_media_files),
        dry_run=dry_run,
    )
    if dry_run or plan.is_empty:
        return result

    # Orphans carry no episode, so they cannot ride out on a cascade.
    for chunk in _chunks(plan.orphan_media_files):
        await session.execute(sql_delete(MediaFile).where(MediaFile.id.in_(chunk)))

    doomed_series = {series_id for series_id, _ in plan.series}
    survivors = {
        series_id for series_id in plan.episodes.values() if series_id not in doomed_series
    }
    # Episodes whose series survives have to be deleted on their own; the rest leave with it.
    await _delete_episode_rows(
        session,
        [
            episode_id
            for episode_id, series_id in plan.episodes.items()
            if series_id not in doomed_series
        ],
    )
    for series_id in doomed_series:
        await _delete_series_row(session, series_id)
        await _forget_series(session, series_id)
    # A surviving series just lost episodes, so its slice of the index still names them.
    for series_id in survivors:
        await reindex_series(session, series_id)

    await audit.record(
        session,
        actor_id,
        "library.cleanup",
        "library",
        None,
        {
            "removedSeries": result.removed_series,
            "removedEpisodes": result.removed_episodes,
            "removedMediaFiles": result.removed_media_files,
        },
    )
    return result


# --- The import assistant -----------------------------------------------------------------
#
# A folder whose filenames carry no episode signal is hopeless one file at a time and obvious
# as a group: 58 files called `701151231.720p.narrowv3.mp4` … `701151288.720p.narrowv3.mp4`
# are episodes 1..58 the moment you look at them together. `app.services.importer` does that
# reading — it is pure, takes `UnmatchedFile` values and knows nothing about the database.
# Everything below is the half it deliberately leaves out: which rows to feed it, which series
# a folder already belongs to, and what its answer means for the catalogue.
#
# Applying a plan creates **episodes that point at the files under the names they already
# have**. Nothing here renames, moves, writes or deletes anything under `LIBRARY_ROOT` — the
# mount is read-only and this module reads it for exactly one purpose, the traversal check in
# `_normalize_folder`. That is not a limitation worked around, it is the design: matching is
# reversible and no bug in the planner can damage the library, whereas a rename is neither.
# `rename_script()` is the escape hatch for an operator who wants tidy names on disk, and all
# dramatarr ever does with it is hand over the text.
#
# This section sits below the deletion helpers because it reuses `_chunks`.


#: Characters no sane filesystem wants in a name. Only ever applied to the *proposed* names
#: in the rename script — never to a path dramatarr reads.
_UNSAFE_IN_FILENAME = re.compile(r'[\\/:*?"<>|]|[\x00-\x1f]')

#: Comments in a generated shell script must not be able to end their own line.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _candidates(files: Sequence[MediaFile]) -> list[UnmatchedFile]:
    """Reduce `media_files` rows to what group inference actually reads.

    Everything here is a column the scan already wrote, which is what keeps planning free of
    filesystem work: `GET /unmatched/groups` can plan a hundred folders without opening one
    file or shelling out to ffprobe once.
    """
    return [
        UnmatchedFile(
            id=media.id,
            name=PurePosixPath(media.path).name,
            path=media.path,
            size_bytes=media.size_bytes or 0,
            mtime=media.mtime or 0.0,
            duration_seconds=media.duration_seconds,
        )
        for media in files
    ]


def _infer(
    folder: str,
    candidates: Sequence[UnmatchedFile],
    *,
    strategy: str | None,
    season_number: int | None,
    start_episode: int | None,
) -> ImportPlan:
    try:
        return infer_plan(
            folder,
            candidates,
            season=season_number if season_number is not None else 1,
            start_episode=start_episode if start_episode is not None else 1,
            strategy=strategy,
        )
    except ValueError as exc:
        # `infer_plan` documents this as the one thing it refuses: a strategy name that does
        # not exist. A strategy that exists but cannot read these files comes back as a plan
        # with a warning, which is the admin's problem to look at, not an error.
        raise AppError(
            "UNKNOWN_IMPORT_STRATEGY", f"No import strategy named {strategy!r}", 400
        ) from exc


# --- Folder arithmetic ----------------------------------------------------------------------


def _folder_of(path: str) -> str:
    """Containing folder of a library-relative path; `""` for a file loose at the root."""
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _folder_identity(folder: str) -> tuple[str, int | None]:
    """Series title and year to suggest for a folder.

    A season container names the season, not the series, so `Show (2024)/Season 02` has to be
    read one level up — otherwise a folder of opaque names inside it would propose a series
    literally called "Season 02".
    """
    name = folder.rsplit("/", 1)[-1]
    parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
    if parent and parse_season_folder(name) is not None:
        return parse_series_folder(parent)
    return parse_series_folder(name)


def _folder_season(folder: str) -> int | None:
    """The season a `Season 02` / `Temporada 2` / `第二季` subfolder states, else None.

    Only ever read one level down. A *top-level* folder called "Season 02" is a series whose
    name happens to look like a season container, not season 2 of anything.
    """
    return parse_season_folder(folder) if "/" in folder else None


def _ancestry(folder: str) -> list[str]:
    """`A/B/C` → `[A/B/C, A/B, A]`. Used to find the series a season subfolder belongs to."""
    parts = [part for part in folder.split("/") if part]
    return ["/".join(parts[: index + 1]) for index in range(len(parts) - 1, -1, -1)]


def _normalize_folder(folder: str) -> str:
    cleaned = folder.replace("\\", "/").strip().strip("/")
    if not cleaned or cleaned == ".":
        # A file loose at the library root is not a folder of anything, and a series row
        # needs a `library_path` of its own — the single-file assign route covers those.
        raise NotFound("Unmatched folder", "IMPORT_FOLDER_NOT_FOUND")
    # Resolved and discarded: all we want is the refusal when it escapes the mount.
    safe_media_path(cleaned)
    return cleaned


def _like_escape(value: str) -> str:
    return value.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")


async def _folder_media_files(
    session: AsyncSession, folder: str, *, unmatched_only: bool
) -> list[MediaFile]:
    """Media files sitting directly in `folder`, ordered by path.

    The LIKE narrows it in SQL and the comprehension drops anything from a deeper subfolder,
    because `Series/Season 01` must not be swept up by a plan for `Series`.
    """
    stmt = select(MediaFile).where(
        MediaFile.path.like(f"{_like_escape(folder)}/%", escape="\\"),
        MediaFile.status == MediaStatus.PRESENT,
    )
    if unmatched_only:
        stmt = stmt.where(MediaFile.episode_id.is_(None))
    rows = (await session.execute(stmt.order_by(MediaFile.path))).scalars().all()
    return [media for media in rows if _folder_of(media.path) == folder]


async def _series_by_folder(session: AsyncSession, folders: Sequence[str]) -> dict[str, Series]:
    """Map each folder to the series indexed at it, or at its nearest indexed ancestor.

    The ancestor walk is what makes `Series (2024)/Season 03` full of opaque names attach to
    the series that already exists instead of offering to create a second copy of it.
    """
    chains = {folder: _ancestry(folder) for folder in folders}
    candidates = sorted({path for chain in chains.values() for path in chain})
    if not candidates:
        return {}

    indexed: dict[str, Series] = {}
    for chunk in _chunks(candidates):
        rows = (
            (await session.execute(select(Series).where(Series.library_path.in_(chunk))))
            .scalars()
            .all()
        )
        for series in rows:
            indexed[series.library_path] = series

    resolved: dict[str, Series] = {}
    for folder, chain in chains.items():
        for path in chain:
            nearest = indexed.get(path)
            if nearest is not None:
                resolved[folder] = nearest
                break
    return resolved


# --- Planning -------------------------------------------------------------------------------


def _proposed_filename(title: str, season: int, number: int, source: str) -> str:
    safe = _UNSAFE_IN_FILENAME.sub(" ", title).strip(" .") or "Series"
    extension = file_extension(source)
    suffix = f".{extension}" if extension else ""
    return f"{safe} - S{season:02d}E{number:02d}{suffix}"


async def _episodes_by_slot(
    session: AsyncSession, series_id: str
) -> dict[tuple[int, int], Episode]:
    rows = (
        (await session.execute(select(Episode).where(Episode.series_id == series_id)))
        .scalars()
        .all()
    )
    return {(episode.season_number, episode.episode_number): episode for episode in rows}


def _holds_another_file(episode: Episode, media_file_id: str) -> bool:
    """True when the slot is already taken by a *different* file that is actually present.

    An episode carrying only a `missing` row is not a collision — that is exactly the case
    where re-pointing it at a file that is on the mount is the right answer.
    """
    return any(
        media.status == MediaStatus.PRESENT and media.id != media_file_id
        for media in episode.media_files
    )


async def _plan_out(
    session: AsyncSession,
    *,
    folder: str,
    files: Sequence[MediaFile],
    existing: Series | None,
    strategy: str | None = None,
    season_number: int | None = None,
    start_episode: int | None = None,
) -> ImportPlanOut:
    candidates = _candidates(files)
    plan = _infer(
        folder,
        candidates,
        strategy=strategy,
        # An admin override wins; otherwise a folder that names its own season is believed.
        season_number=season_number if season_number is not None else _folder_season(folder),
        start_episode=start_episode,
    )
    by_id = {media.id: media for media in files}

    folder_title, folder_year = _folder_identity(folder)
    suggested_title = folder_title or folder.rsplit("/", 1)[-1]
    # The rename script names files after the series they will belong to, which is the one
    # already indexed here when there is one — not the guess made from the folder name.
    display_title = existing.title if existing is not None else suggested_title
    slots = await _episodes_by_slot(session, existing.id) if existing is not None else {}

    assignments: list[ImportAssignmentOut] = []
    for proposed in plan.episodes:
        media = by_id.get(proposed.media_file_id)
        if media is None:  # pragma: no cover - the planner only returns what it was given
            continue
        season, number = proposed.season_number, proposed.episode_number
        episode = slots.get((season, number))
        assignments.append(
            ImportAssignmentOut(
                media_file_id=media.id,
                path=media.path,
                filename=proposed.current_name,
                size_bytes=media.size_bytes or 0,
                season_number=season,
                episode_number=number,
                confidence=proposed.confidence,
                reason=proposed.reason,
                order_key=proposed.order_key,
                proposed_filename=_proposed_filename(display_title, season, number, media.path),
                existing_episode_id=episode.id if episode is not None else None,
                conflict=episode is not None and _holds_another_file(episode, media.id),
            )
        )

    warnings = list(plan.warnings)
    conflicts = sum(1 for item in assignments if item.conflict)
    if conflicts:
        # The planner cannot know this — it never sees the catalogue — so it is added here,
        # next to the per-row `conflict` flags the review table renders.
        warnings.append(
            f"{conflicts} of these episode numbers already hold a different file. "
            "Applying as-is will be refused."
        )

    return ImportPlanOut(
        folder=folder,
        strategy=plan.strategy,
        confidence=plan.confidence,
        explanation=plan.explanation,
        explanation_code=plan.explanation_code,
        season_number=plan.season_number,
        start_episode=min(
            (item.episode_number for item in assignments),
            default=start_episode if start_episode is not None else 1,
        ),
        file_count=len(files),
        suggested_series_title=suggested_title,
        suggested_year=folder_year,
        existing_series_id=existing.id if existing is not None else None,
        existing_series_title=existing.title if existing is not None else None,
        strategies=[
            ImportStrategyOut(name=name, confidence=confidence)
            for name, confidence in available_strategies(candidates)
        ],
        assignments=assignments,
        warnings=warnings,
        has_conflicts=conflicts > 0,
    )


async def unmatched_groups(session: AsyncSession, *, limit: int = 50) -> list[UnmatchedGroupOut]:
    """Every folder that still holds unmatched files, each with a complete proposal.

    Files loose at the library root are left out: they share no folder, so there is nothing
    to read as a group and no `library_path` a series could be created at. They stay visible
    under `GET /unmatched`, one at a time, which is the right tool for them.
    """
    rows = (
        (
            await session.execute(
                select(MediaFile)
                .where(MediaFile.episode_id.is_(None), MediaFile.status == MediaStatus.PRESENT)
                .order_by(MediaFile.path)
            )
        )
        .scalars()
        .all()
    )

    grouped: dict[str, list[MediaFile]] = {}
    for media in rows:
        folder = _folder_of(media.path)
        if folder:
            grouped.setdefault(folder, []).append(media)
    if not grouped:
        return []

    existing = await _series_by_folder(session, list(grouped))
    # Biggest first: a folder of 58 files is the one the admin came here for.
    ordered = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]

    groups: list[UnmatchedGroupOut] = []
    for folder, files in ordered:
        series = existing.get(folder)
        plan = await _plan_out(session, folder=folder, files=files, existing=series)
        groups.append(
            UnmatchedGroupOut(
                folder=folder,
                file_count=len(files),
                total_bytes=sum(media.size_bytes or 0 for media in files),
                suggested_series_title=plan.suggested_series_title,
                suggested_year=plan.suggested_year,
                existing_series_id=plan.existing_series_id,
                existing_series_title=plan.existing_series_title,
                strategies=plan.strategies,
                plan=plan,
            )
        )
    return groups


async def plan_folder_import(
    session: AsyncSession,
    *,
    folder: str,
    strategy: str | None = None,
    season_number: int | None = None,
    start_episode: int | None = None,
) -> ImportPlanOut:
    """Recompute one folder's plan with the admin's overrides applied."""
    resolved = _normalize_folder(folder)
    files = await _folder_media_files(session, resolved, unmatched_only=True)
    if not files:
        raise NotFound("Unmatched folder", "IMPORT_FOLDER_NOT_FOUND")
    existing = (await _series_by_folder(session, [resolved])).get(resolved)
    return await _plan_out(
        session,
        folder=resolved,
        files=files,
        existing=existing,
        strategy=strategy,
        season_number=season_number,
        start_episode=start_episode,
    )


# --- Applying -------------------------------------------------------------------------------


async def _unique_import_slug(session: AsyncSession, title: str, year: int | None) -> str:
    """Mirrors the scanner's slug rule so a folder imported by hand and the same folder
    picked up by a scan cannot end up with two different-looking slugs."""
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


async def _resolve_import_series(
    session: AsyncSession, *, folder: str, body: ImportApplyIn
) -> tuple[Series, bool]:
    if body.series_id:
        return await get_series_or_404(session, body.series_id), False

    # Looking the folder up before creating anything is what makes a second apply of the same
    # plan idempotent rather than a unique-constraint violation on `series.library_path`.
    existing = (
        (await session.execute(select(Series).where(Series.library_path == folder)))
        .scalars()
        .first()
    )
    if existing is not None:
        return existing, False

    folder_title, folder_year = _folder_identity(folder)
    title = (body.new_series_title or folder_title or folder.rsplit("/", 1)[-1]).strip()
    year = body.new_series_year if body.new_series_year is not None else folder_year
    series = Series(
        slug=await _unique_import_slug(session, title, year),
        title=title,
        sort_title=normalize(title) or title.casefold(),
        year=year,
        library_path=folder,
    )
    session.add(series)
    await session.flush()
    # Without this row the series would be invisible to search until TMDB is reached, which
    # for a library imported before an API key is configured means forever.
    session.add(
        SeriesTitle(
            series_id=series.id,
            language="",
            title=title,
            kind=TitleKind.OFFICIAL,
            source=TitleSource.FILENAME,
            normalized=normalize(title),
        )
    )
    await session.flush()
    return series, True


async def apply_import(
    session: AsyncSession, body: ImportApplyIn, *, actor_id: str | None = None
) -> ImportApplied:
    """Turn an approved plan into catalogue rows. Writes nothing under `LIBRARY_ROOT`.

    Every collision is found before the first row is written, so a refused apply leaves the
    catalogue exactly as it was rather than half-imported.
    """
    folder = _normalize_folder(body.folder)

    # Every present file in the folder, attached or not: a repeat apply names files the first
    # one already attached, and that has to read as "already done", not as a missing file.
    files = await _folder_media_files(session, folder, unmatched_only=False)
    by_id = {media.id: media for media in files}

    claimed: dict[tuple[int, int], str] = {}
    for item in body.assignments:
        if item.media_file_id not in by_id:
            raise AppError(
                "IMPORT_FILE_NOT_IN_FOLDER",
                f"Media file {item.media_file_id} is not a present file in {folder!r}",
                400,
            )
        slot = (item.season_number, item.episode_number)
        if claimed.setdefault(slot, item.media_file_id) != item.media_file_id:
            raise Conflict(
                "EPISODE_NUMBER_TAKEN",
                f"Two files are assigned to S{slot[0]:02d}E{slot[1]:02d}",
            )

    series, created_series = await _resolve_import_series(session, folder=folder, body=body)
    slots = await _episodes_by_slot(session, series.id)

    for item in body.assignments:
        episode = slots.get((item.season_number, item.episode_number))
        if episode is not None and _holds_another_file(episode, item.media_file_id):
            raise Conflict(
                "EPISODE_NUMBER_TAKEN",
                f"S{item.season_number:02d}E{item.episode_number:02d} of {series.title!r} "
                "already holds a different file",
            )

    created_episodes = 0
    attached = 0
    for item in body.assignments:
        slot = (item.season_number, item.episode_number)
        episode = slots.get(slot)
        if episode is None:
            episode = Episode(
                series_id=series.id,
                season_number=item.season_number,
                episode_number=item.episode_number,
            )
            session.add(episode)
            await session.flush()
            slots[slot] = episode
            created_episodes += 1
        media = by_id[item.media_file_id]
        if media.episode_id != episode.id:
            media.episode_id = episode.id
            attached += 1

    series.updated_at = utcnow()
    await session.flush()
    await reindex_series(session, series.id)

    job_ref: JobRef | None = None
    if created_series:
        job_ref = await _enqueue_import_metadata(session, series, actor_id)

    await audit.record(
        session,
        actor_id,
        "library.import_apply",
        "series",
        series.id,
        {
            "folder": folder,
            "createdSeries": created_series,
            "createdEpisodes": created_episodes,
            "attached": attached,
        },
    )
    return ImportApplied(
        series_id=series.id,
        series_slug=series.slug,
        series_title=series.title,
        created=ImportCreated(series=created_series, episodes=created_episodes),
        attached=attached,
        metadata_job=job_ref,
    )


async def _enqueue_import_metadata(
    session: AsyncSession, series: Series, actor_id: str | None
) -> JobRef:
    # Imported here rather than at module scope because the queue's own handlers import this
    # module to do their work; the scanner enqueues its metadata jobs the same way.
    from app.models import JobType
    from app.workers.queue import enqueue

    job = await enqueue(
        session,
        JobType.METADATA_REFRESH,
        {"seriesId": series.id},
        created_by_id=actor_id,
        series_id=series.id,
    )
    return JobRef(id=job.id, type=job.type, status=job.status)


# --- The rename script dramatarr will never run ---------------------------------------------


def _comment(text: str) -> str:
    return _CONTROL_CHARS.sub(" ", text)


def rename_script(plan: ImportPlanOut) -> str:
    """A shell script the *operator* may run to put tidy names on disk.

    dramatarr generates this and stops. `LIBRARY_ROOT` is mounted read-only, the catalogue
    already knows what every one of these files is under the name it has now, and renaming is
    therefore optional housekeeping rather than part of the import. The script refuses to
    overwrite anything and writes the mapping it performed beside the files so it is
    reversible.
    """
    directory = str(safe_media_path(plan.folder))
    title = plan.existing_series_title or plan.suggested_series_title or plan.folder
    header = [
        "#!/usr/bin/env bash",
        "#",
        f"# Rename plan for: {_comment(plan.folder)}",
        f"# Series         : {_comment(title)}",
        f"# Strategy       : {_comment(plan.strategy)} "
        f"(confidence {round(plan.confidence * 100)}%)",
        f"# Files          : {len(plan.assignments)}",
        "#",
        "# dramatarr did NOT run this and cannot: LIBRARY_ROOT is mounted read-only and no",
        "# code path in the application creates, moves, renames or deletes a file under it.",
        "#",
        "# You do not need to run it either. dramatarr has already catalogued these files",
        "# under the names they have now, exactly the way Plex and Jellyfin 'match' them, and",
        "# that mapping survives without a single byte on the NAS changing. This script exists",
        "# only for operators who also want the names on disk to be tidy. Read it first, make",
        "# sure the mount is writable, and run it yourself.",
        "#",
        "# It refuses to overwrite anything (mv -n, plus an explicit existence check) and",
        "# writes .rename-map.tsv beside the files so every rename can be undone.",
        "",
        "set -euo pipefail",
        "",
        "# Where the files live on the machine running this. dramatarr filled in the path it",
        "# knows; pass a different one as the first argument if your mount is elsewhere.",
        f"DEFAULT_DIR={shlex.quote(directory)}",
        'DIR="${1:-$DEFAULT_DIR}"',
        "",
        '[ -d "$DIR" ] || { printf \'not a directory: %s\\n\' "$DIR" >&2; exit 1; }',
        'cd -- "$DIR"',
        "",
        "MAP=.rename-map.tsv",
        ': > "$MAP"',
        "",
        "renamed=0",
        "skipped=0",
        "",
        "rename_one() {",
        "  src=$1",
        "  dst=$2",
        '  if [ "$src" = "$dst" ]; then',
        "    return 0",
        "  fi",
        '  if [ ! -f "$src" ]; then',
        "    printf 'skip, no such file: %s\\n' \"$src\" >&2",
        "    skipped=$((skipped + 1))",
        "    return 0",
        "  fi",
        '  if [ -e "$dst" ]; then',
        "    printf 'skip, target exists: %s\\n' \"$dst\" >&2",
        "    skipped=$((skipped + 1))",
        "    return 0",
        "  fi",
        '  mv -n -- "$src" "$dst"',
        '  printf \'%s\\t%s\\n\' "$src" "$dst" >> "$MAP"',
        "  renamed=$((renamed + 1))",
        "}",
        "",
    ]

    body = [
        f"rename_one {shlex.quote(item.filename)} {shlex.quote(item.proposed_filename)}"
        for item in plan.assignments
    ]

    footer = [
        "",
        'printf \'renamed %s file(s), skipped %s.\\n\' "$renamed" "$skipped"',
        'printf \'mapping: %s/%s\\n\' "$DIR" "$MAP"',
        "",
        "# To undo, from the same folder:",
        '#   while IFS=$\'\\t\' read -r a b; do mv -n -- "$b" "$a"; done < .rename-map.tsv',
        "",
    ]
    return "\n".join([*header, *body, *footer])


__all__ = [
    "SeriesCounts",
    "SortKey",
    "apply_import",
    "cleanup_catalogue",
    "continue_watching",
    "delete_series",
    "deletion_preview",
    "episode_detail",
    "episode_summary",
    "episodes_with_progress",
    "genre_facets",
    "get_episode_or_404",
    "get_series_or_404",
    "image_url",
    "list_series",
    "list_unmatched",
    "optional_progress_out",
    "plan_folder_import",
    "progress_out",
    "recently_added",
    "rename_script",
    "series_counts",
    "series_detail",
    "series_neighbours",
    "series_episodes",
    "series_progress",
    "series_summary",
    "stream_url",
    "subtitle_url",
    "thumbnails_url",
    "thumbnails_url_if_ready",
    "unmatched_file_out",
    "unmatched_groups",
    "watch_history",
]


async def apply_dramadex_title(
    session: AsyncSession,
    series: Series,
    found: Any,
    *,
    overwrite: bool = False,
) -> list[str]:
    """Write a DramaDex record onto a series. Returns the field names actually filled.

    This is the fill the application was missing. The sources this replaced could offer a year
    and, on a good day, an English plot; DramaDex knows the platform, the cast, sixteen
    namespaces of tags and the link to watch it — for a genre neither of the others catalogued.

    **Blanks only unless `overwrite`**: this household writes its synopses in Spanish, and
    coming back for the cast must not cost the paragraph somebody wrote.

    Cast and links are the exception, and deliberately so. They are a *cache* of DramaDex's
    answer rather than something an admin curates here, so they are replaced wholesale — a
    merge would leave an actor behind after the indexer corrected a bad scrape, and there is
    no local edit to protect.

    `declared_episode_count` goes through the same guard as every other writer: a count below
    what is already on the NAS is refused, because the file count is the number that is a
    fact. See `services/metadata._apply_series_fields`.
    """
    filled: list[str] = []

    def put(field_name: str, value: Any) -> None:
        if value in (None, "", (), []):
            return
        if not overwrite and getattr(series, field_name, None):
            return
        setattr(series, field_name, value)
        filled.append(field_name)

    put("overview", found.synopsis)
    put("year", found.year)
    put("genres", normalize_genres(found.genres()))
    put("network", found.studio_name)
    put("content_rating", canonical_content_rating(found.audience_rating))

    if found.synopsis and "overview" in filled:
        series.overview_source = OverviewSource.MANUAL
        series.overview_source_lang = "en"
        watch = next((s.watch_url for s in found.studios if s.is_watchable), None)
        if watch:
            series.overview_source_url = watch[:1000]

    files_on_disk = int(
        (
            await session.execute(
                select(func.count(func.distinct(Episode.id)))
                .select_from(Episode)
                .join(MediaFile, MediaFile.episode_id == Episode.id)
                .where(Episode.series_id == series.id)
            )
        ).scalar_one()
    )
    if found.episode_count and found.episode_count >= files_on_disk:
        put("declared_episode_count", found.episode_count)

    # --- The cached halves, replaced rather than merged ---------------------------------
    await session.execute(sql_delete(SeriesCast).where(SeriesCast.series_id == series.id))
    for member in found.cast:
        session.add(
            SeriesCast(
                series_id=series.id,
                dramadex_person_id=member.person_id,
                name=member.name[:255],
                role=(member.role or None),
                billing_order=member.billing_order,
            )
        )
    if found.cast:
        filled.append("cast")

    await session.execute(sql_delete(SeriesLink).where(SeriesLink.series_id == series.id))
    # One row per platform, and the first wins.
    #
    # A platform can list the same show twice: GoodShort carries "Found A Homeless Genius To
    # Save My Company" under two ids, and once DramaDex merged them into one title it returned
    # both links. Storing both violates the one-link-per-studio constraint, and it would put
    # two identical "Watch on GoodShort" buttons on the page even if it did not — a reader
    # cannot choose between two links to the same platform, so offering the choice is worse
    # than picking for them.
    seen_studios: set[str] = set()
    for link in found.studios:
        if not link.is_watchable or link.key in seen_studios:
            continue
        seen_studios.add(link.key)
        session.add(
            SeriesLink(
                series_id=series.id,
                studio_key=link.key[:32],
                studio_name=link.name[:64],
                external_id=link.external_id[:128],
                url=link.watch_url[:1000],
            )
        )
    if any(s.is_watchable for s in found.studios):
        filled.append("links")

    # Aliases become searchable title rows, which is what makes a member typing the platform's
    # own spelling find the series the household actually filed under another name.
    known = {
        row
        for row in (
            await session.execute(
                select(SeriesTitle.normalized).where(SeriesTitle.series_id == series.id)
            )
        ).scalars()
    }
    for alias in found.aliases:
        slug = normalize(alias)
        if not slug or slug in known:
            continue
        known.add(slug)
        session.add(
            SeriesTitle(
                series_id=series.id,
                language="",
                title=alias[:500],
                kind=TitleKind.ALTERNATE,
                source=TitleSource.MANUAL,
                normalized=slug,
            )
        )

    series.dramadex_id = found.id
    if "dramadex_id" not in filled:
        filled.append("dramadex_id")

    series.metadata_locked = True
    series.updated_at = utcnow()
    await session.flush()
    await reindex_series(session, series.id)
    await session.commit()
    return filled
