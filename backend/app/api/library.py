"""Library routes — `/api/library`.

Reads are `user`; every mutation is `admin`. Editing metadata by hand locks the row so the
metadata jobs stop overwriting it, which is the mechanism behind "the admin can edit, but the
program fetches when it can".

The removal routes at the bottom delete **catalogue rows only**. `LIBRARY_ROOT` is mounted
read-only and no route here ever writes to it, so a series whose files are still on the mount
comes back on the next scan — which is why `deletion-preview` exists and why `filesStillOnDisk`
is on the wire: the admin is told that before they confirm, not after.

The `/unmatched/*` import-assistant routes in the middle live under the same rule from the
other direction: applying a plan attaches episode rows to files under the names they already
carry, and the rename script is generated for the operator to run, never run here.

Metadata comes from DramaDex and from nowhere else. TMDB, OMDb and the SearXNG web search
were all removed once the household's own indexer resolved all seventeen series where between
them they resolved two. `series.overview_source` still records `wikipedia` and `tmdb` on rows
written while those existed; the column is history, not a live code path.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, Query, Response, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminUser, CurrentUser, SessionDep
from app.errors import AppError, Conflict, NotFound
from app.models import Episode, JobType, MediaFile, Series, SeriesTitle
from app.schemas.common import Ok, Page
from app.schemas.library import (
    ArtworkApplied,
    ArtworkCandidate,
    ArtworkCandidates,
    ArtworkSelectIn,
    ArtworkSourceOut,
    AssignIn,
    CleanupIn,
    CleanupResult,
    DiscoveryResults,
    DramadexApplyIn,
    DramadexHit,
    DramadexHits,
    PersonCredits,
    EpisodeDetail,
    EpisodePatch,
    EpisodeSummary,
    GenreFacet,
    ImdbApplyIn,
    ImportApplied,
    ImportApplyIn,
    ImportPlanIn,
    ImportPlanOut,
    JobRef,
    MatchIn,
    RenameItemOut,
    RenamePlanOut,
    SeriesBulkPatch,
    SeriesBulkResult,
    SeriesDeleted,
    SeriesDeletionPreview,
    SeriesDetail,
    SeriesPatch,
    SeriesSummary,
    SeriesTitleIn,
    SeriesTitleOut,
    SeriesUnlinked,
    TmdbMatch,
    UnmatchedFileOut,
    UnmatchedGroupOut,
)
from app.schemas.search import SearchResults
from app.services import dramadex, renamer
from app.services import library as library_service
from app.services.images import (
    BACKDROP_SIZE,
    MAX_ARTWORK_BYTES,
    POSTER_SIZE,
    ArtworkKind,
    cache_dramadex_poster,
    fetch_remote_image,
    get_image_search,
    read_capped,
    store_series_artwork,
)
from app.services.library import SortKey
from app.services.search.index import reindex_series
from app.services.search.normalize import normalize
from app.services.search.service import search_library
from app.services.settings import SettingsService
from app.services.vocab import (
    Provider,
    canonical_content_rating,
    canonical_network,
    configured_providers,
    normalize_genres,
    provider_domains,
)
from app.utils.time import utcnow
from app.workers.queue import enqueue

log = logging.getLogger(__name__)

router = APIRouter(prefix="/library", tags=["library"])


#: The series half of this list moved to `library_service.SERIES_METADATA_FIELDS`, where the
#: single and bulk patch routes share it. Episodes have no bulk route, so theirs stays here.
_EPISODE_METADATA_FIELDS = {
    "title",
    "overview",
    "air_date",
    "season_number",
    "episode_number",
    "absolute_number",
    "runtime_seconds",
}


# --- Reads ----------------------------------------------------------------------------------


@router.get("/series", response_model=Page[SeriesSummary])
async def list_series(
    session: SessionDep,
    user: CurrentUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    genre: Annotated[str | None, Query(max_length=100)] = None,
    status: Annotated[str | None, Query(max_length=32)] = None,
    language: Annotated[str | None, Query(max_length=16)] = None,
    sort: Annotated[SortKey, Query()] = "title",
    available_only: Annotated[bool, Query(alias="availableOnly")] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 40,
) -> Page[SeriesSummary]:
    return await library_service.list_series(
        session,
        user_id=user.id,
        q=q,
        genre=genre,
        status=status,
        language=language,
        sort=sort,
        available_only=available_only,
        page=page,
        page_size=page_size,
    )


@router.get("/search", response_model=SearchResults)
async def search(
    session: SessionDep,
    user: CurrentUser,
    q: Annotated[str, Query(max_length=200)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> SearchResults:
    return await search_library(session, q, limit=limit)


@router.get("/genres", response_model=list[GenreFacet])
async def genres(session: SessionDep, user: CurrentUser) -> list[GenreFacet]:
    return await library_service.genre_facets(session)


@router.get("/unmatched", response_model=Page[UnmatchedFileOut])
async def unmatched(
    session: SessionDep,
    admin: AdminUser,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> Page[UnmatchedFileOut]:
    return await library_service.list_unmatched(session, page=page, page_size=page_size)






@router.get("/series/{id_or_slug}", response_model=SeriesDetail)
async def series_detail(session: SessionDep, user: CurrentUser, id_or_slug: str) -> SeriesDetail:
    series = await library_service.get_series_or_404(session, id_or_slug)
    return await library_service.series_detail(session, series, user.id)


@router.get("/series/{series_id}/episodes", response_model=list[EpisodeSummary])
async def series_episodes(
    session: SessionDep, user: CurrentUser, series_id: str
) -> list[EpisodeSummary]:
    series = await library_service.get_series_or_404(session, series_id)
    return await library_service.series_episodes(session, series.id, user.id)


@router.get("/episodes/{episode_id}", response_model=EpisodeDetail)
async def episode_detail(session: SessionDep, user: CurrentUser, episode_id: str) -> EpisodeDetail:
    episode = await library_service.get_episode_or_404(session, episode_id)
    return await library_service.episode_detail(session, episode, user.id)


# --- Admin edits ----------------------------------------------------------------------------


@router.patch("/series/{series_id}", response_model=SeriesDetail)
async def patch_series(
    session: SessionDep, admin: AdminUser, series_id: str, body: SeriesPatch
) -> SeriesDetail:
    series = await library_service.get_series_or_404(session, series_id)
    # Canonicalizing, locking and re-indexing all live in the service, so that this route and
    # the bulk one below cannot drift apart. See `library_service.apply_series_patch`.
    await library_service.apply_series_patch(
        session,
        series,
        body.model_dump(exclude_unset=True),
        metadata_locked=body.metadata_locked,
    )
    return await library_service.series_detail(session, series, admin.id)


@router.post("/series/bulk", response_model=SeriesBulkResult)
async def bulk_patch_series(
    session: SessionDep, admin: AdminUser, body: SeriesBulkPatch
) -> SeriesBulkResult:
    """Apply one edit to many series.

    Exists because the fields an admin most often has to fix — status, year, network, rating —
    are the same for a whole batch of imports, and doing them one screen at a time is the sort
    of work a person abandons halfway through, leaving a catalogue half-corrected.

    Only the fields actually present in `patch` are written, so a bulk status change cannot
    quietly blank forty synopses. Ids that no longer exist come back in `missing` rather than
    failing the request: a stale selection — a row deleted in another tab — should not throw
    away the other thirty-nine edits.

    Not transactional per row on purpose. One `flush` per series through the shared path, one
    commit at the end: either the batch lands or the request failed, and a partially applied
    batch is not a state this leaves behind.
    """
    changes = body.patch.model_dump(exclude_unset=True)
    if not changes and body.patch.metadata_locked is None:
        raise AppError("SERIES_BULK_EMPTY", "The patch changes nothing", 400)

    found = (
        (await session.execute(select(Series).where(Series.id.in_(body.ids)))).scalars().all()
    )
    by_id = {series.id: series for series in found}

    for series in found:
        await library_service.apply_series_patch(
            session,
            series,
            dict(changes),
            metadata_locked=body.patch.metadata_locked,
        )

    missing = [series_id for series_id in body.ids if series_id not in by_id]
    log.info("bulk patch: %d series updated, %d missing", len(found), len(missing))
    return SeriesBulkResult(updated=len(found), missing=missing)


@router.patch("/episodes/{episode_id}", response_model=EpisodeDetail)
async def patch_episode(
    session: SessionDep, admin: AdminUser, episode_id: str, body: EpisodePatch
) -> EpisodeDetail:
    episode = await library_service.get_episode_or_404(session, episode_id)
    changes = body.model_dump(exclude_unset=True)

    titles_changed = False
    metadata_changed = False
    for field, value in changes.items():
        if field == "metadata_locked":
            continue
        if getattr(episode, field) == value:
            continue
        setattr(episode, field, value)
        metadata_changed = metadata_changed or field in _EPISODE_METADATA_FIELDS
        titles_changed = titles_changed or field == "title"

    if metadata_changed:
        episode.metadata_locked = True
    if body.metadata_locked is not None:
        episode.metadata_locked = body.metadata_locked

    episode.updated_at = utcnow()
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(
            "EPISODE_NUMBER_TAKEN", "Another episode already uses that season/episode number"
        ) from exc
    if titles_changed:
        await reindex_series(session, episode.series_id)
    return await library_service.episode_detail(session, episode, admin.id)


@router.post("/series/{series_id}/titles", response_model=SeriesTitleOut, status_code=201)
async def add_series_title(
    session: SessionDep, admin: AdminUser, series_id: str, body: SeriesTitleIn
) -> SeriesTitleOut:
    series = await library_service.get_series_or_404(session, series_id)
    title = SeriesTitle(
        series_id=series.id,
        language=body.language,
        title=body.title,
        kind=body.kind,
        source="manual",
        normalized=normalize(body.title),
    )
    session.add(title)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("TITLE_EXISTS", "That title already exists for this series") from exc
    await reindex_series(session, series.id)
    return SeriesTitleOut(
        id=title.id,
        language=title.language,
        title=title.title,
        kind=title.kind,
        source=title.source,
        normalized=title.normalized,
    )


@router.delete("/series/{series_id}/titles/{title_id}", response_model=Ok)
async def delete_series_title(
    session: SessionDep, admin: AdminUser, series_id: str, title_id: str
) -> Ok:
    series = await library_service.get_series_or_404(session, series_id)
    title = await session.get(SeriesTitle, title_id)
    if title is None or title.series_id != series.id:
        raise NotFound("Title", "TITLE_NOT_FOUND")
    await session.delete(title)
    await session.flush()
    await reindex_series(session, series.id)
    return Ok()


@router.post("/series/{series_id}/refresh", response_model=JobRef, status_code=202)
async def refresh_series(session: SessionDep, admin: AdminUser, series_id: str) -> JobRef:
    series = await library_service.get_series_or_404(session, series_id)
    job = await enqueue(
        session,
        JobType.METADATA_REFRESH,
        {"seriesId": series.id},
        created_by_id=admin.id,
        series_id=series.id,
    )
    return JobRef(id=job.id, type=job.type, status=job.status)



@router.post("/series/{series_id}/unlink", response_model=SeriesUnlinked)
async def unlink_series(session: SessionDep, admin: AdminUser, series_id: str) -> SeriesUnlinked:
    """Forget a wrong TMDB match: clear the id and stop the metadata jobs rewriting the row.

    Both halves are needed and neither is sufficient. Clearing `tmdb_id` alone leaves a row the
    scheduled refresh re-matches by title — which is how it got the wrong entry in the first
    place. Setting `metadata_locked` alone leaves the wrong id in place for everything that
    reads it. Together they mean the fields are the admin's now, and the series is no longer
    claiming to be a title it is not.

    The pair is also what `refresh_series_metadata` reads to leave the series alone: locked
    with no id means an admin said no to a match, so an automatic refresh does not fuzzy-match
    it again. An explicit id still binds — that is `POST /match`, which clears the lock.

    The inverse of `POST /match`, which clears the lock and binds an id.
    """
    series = await library_service.get_series_or_404(session, series_id)
    previous = series.tmdb_id
    series.tmdb_id = None
    series.metadata_locked = True
    series.updated_at = utcnow()
    await session.flush()
    return SeriesUnlinked(
        series_id=series.id, previous_tmdb_id=previous, metadata_locked=series.metadata_locked
    )


# --- Artwork ----------------------------------------------------------------------------------
#
# The metadata jobs pick whichever poster TMDB lists first, and for a drama with six regional
# posters that is a coin toss. These three routes are the admin overruling it: see everything
# available, choose one, and have the choice stick.
#
# "Stick" is `metadata_locked`. Without it the next scheduled refresh would put the coin-toss
# poster back, and an admin who has to re-pick their poster every night stops using the feature.


def _artwork_candidates(
    rows: list[dict[str, Any]], kind: ArtworkKind
) -> list[ArtworkCandidate]:
    """TMDB image rows → candidates, best first.

    TMDB's own order is by vote and then by language; it is re-sorted here so that a textless
    image with no votes does not sink below a low-resolution one that happens to have two.
    """
    out: list[ArtworkCandidate] = []
    for row in rows:
        path = row.get("file_path")
        if not isinstance(path, str) or not path:
            continue
        out.append(
            ArtworkCandidate(
                source="tmdb",
                kind=kind,
                url=f"{IMAGE_BASE}/{ARTWORK_PICK_SIZE}{path}",
                thumbnail_url=f"{IMAGE_BASE}/{ARTWORK_THUMB_SIZE}{path}",
                width=row.get("width"),
                height=row.get("height"),
                # TMDB writes `null` (the JSON one) for textless artwork; keep that meaning.
                language=row.get("iso_639_1") or None,
                vote_average=row.get("vote_average"),
                vote_count=row.get("vote_count"),
            )
        )
    out.sort(key=lambda item: (item.vote_average or 0.0, item.width or 0), reverse=True)
    return out


@router.get("/series/{series_id}/artwork", response_model=ArtworkCandidates)
async def series_artwork(
    session: SessionDep,
    admin: AdminUser,
    series_id: str,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=10)] = 10,
) -> ArtworkCandidates:
    """Every poster and backdrop worth choosing between, from every source that can answer.

    Both sources are optional and **neither failing is an error**. TMDB needs the series to be
    linked; the web search needs configuration a fresh install does not have. The response says
    which sources answered and why the others did not, and an admin opening this on an unlinked
    series with no search key gets an empty picker and a reason — not a 503 on the one screen
    they would use to fix it.
    """
    series = await library_service.get_series_or_404(session, series_id)
    out = ArtworkCandidates(series_id=series.id, tmdb_id=series.tmdb_id)

    client = await try_tmdb_client(session)
    if client is None:
        out.sources.append(
            ArtworkSourceOut(name="tmdb", provider="tmdb", reason="NOT_CONFIGURED")
        )
    elif series.tmdb_id is None:
        out.sources.append(ArtworkSourceOut(name="tmdb", provider="tmdb", reason="NOT_LINKED"))
    else:
        try:
            gallery = await client.get_tv_images(series.tmdb_id)
        except AppError as exc:
            log.warning("TMDB image gallery failed for series %s: %s", series.id, exc.code)
            out.sources.append(
                ArtworkSourceOut(name="tmdb", provider="tmdb", reason="PROVIDER_ERROR")
            )
        else:
            out.posters = _artwork_candidates(gallery.get("posters", []), "poster")
            out.backdrops = _artwork_candidates(gallery.get("backdrops", []), "backdrop")
            out.sources.append(
                ArtworkSourceOut(
                    name="tmdb",
                    provider="tmdb",
                    available=True,
                    count=len(out.posters) + len(out.backdrops),
                )
            )

    provider = await get_image_search(session)
    if provider is None:
        out.sources.append(ArtworkSourceOut(name="web", reason="NOT_CONFIGURED"))
        return out

    query = (q or "").strip() or " ".join(
        part for part in (series.title, str(series.year) if series.year else "", "poster") if part
    )
    try:
        results = await provider.search(query, limit=limit)
    except AppError as exc:
        log.warning("Image search (%s) failed: %s", provider.name, exc.code)
        out.sources.append(
            ArtworkSourceOut(name="web", provider=provider.name, reason="PROVIDER_ERROR")
        )
        return out

    # These are candidates, not downloads. Every `url` here came from the open internet by way
    # of a search engine and is trusted by nobody; it becomes bytes on disk only through
    # `POST .../artwork` below, which puts it through the same address guard a pasted URL gets.
    out.web = [
        ArtworkCandidate(
            source=provider.name,
            url=item.url,
            thumbnail_url=item.thumbnail_url or item.url,
            width=item.width,
            height=item.height,
            title=item.title,
            context_url=item.context_url,
            engine=item.engine,
        )
        for item in results
    ]
    out.sources.append(
        ArtworkSourceOut(
            name="web", provider=provider.name, available=True, count=len(out.web)
        )
    )
    return out


async def _apply_artwork(
    session: AsyncSession, series: Series, kind: ArtworkKind, data: bytes
) -> ArtworkApplied:
    """Store chosen bytes as the series' poster or backdrop, and lock the row.

    The file lands under the same stem `image_cache` uses, so from here on the admin's choice
    simply *is* the series' poster: `is_cached_path` reports it local and the caching job leaves
    local files alone.
    """
    stored = await store_series_artwork(series.id, kind, data)
    setattr(series, f"{kind}_path", stored)
    series.metadata_locked = True
    series.updated_at = utcnow()
    await session.flush()
    return ArtworkApplied(
        series_id=series.id,
        kind=kind,
        stored_path=stored,
        url=library_service.image_url(
            stored, POSTER_SIZE if kind == "poster" else BACKDROP_SIZE
        ),
        metadata_locked=series.metadata_locked,
    )


@router.post("/series/{series_id}/artwork", response_model=ArtworkApplied)
async def set_series_artwork(
    session: SessionDep, admin: AdminUser, series_id: str, body: ArtworkSelectIn
) -> ArtworkApplied:
    """Download a chosen image and make it the series' poster or backdrop.

    The URL comes from outside, which is the whole reason `fetch_remote_image` exists: this
    server sits on the household network next to the NAS and the router's admin page, and an
    unguarded fetch-this-URL endpoint is a port scanner with a login form. Scheme, host and
    every redirect hop are checked before a request is made.

    That applies to a *web search result* exactly as it does to a pasted link. A configured
    SearXNG instance is trusted — an admin typed its address and it normally lives on a private
    one — but the `img_src` values it hands back were chosen by an engine crawling the open
    internet, so they arrive here with no more standing than a link off a forum. The two are
    deliberately governed by different rules; see the SSRF note in `app.services.images`.
    """
    series = await library_service.get_series_or_404(session, series_id)
    data = await fetch_remote_image(body.url, max_bytes=MAX_ARTWORK_BYTES)
    return await _apply_artwork(session, series, body.kind, data)


@router.post("/series/{series_id}/artwork/upload", response_model=ArtworkApplied)
async def upload_series_artwork(
    session: SessionDep,
    admin: AdminUser,
    series_id: str,
    kind: Annotated[ArtworkKind, Query()],
    file: Annotated[UploadFile, File()],
) -> ArtworkApplied:
    """Same pipeline for a file the admin has locally — a scan, or art they made themselves."""
    series = await library_service.get_series_or_404(session, series_id)
    data = await read_capped(file, MAX_ARTWORK_BYTES, "ARTWORK_TOO_LARGE")
    return await _apply_artwork(session, series, kind, data)


@router.post("/unmatched/{file_id}/assign", response_model=EpisodeSummary)
async def assign_unmatched(
    session: SessionDep, admin: AdminUser, file_id: str, body: AssignIn
) -> EpisodeSummary:
    media = await session.get(MediaFile, file_id)
    if media is None:
        raise NotFound("Media file", "MEDIA_FILE_NOT_FOUND")
    series = await library_service.get_series_or_404(session, body.series_id)

    episode = (
        (
            await session.execute(
                select(Episode).where(
                    Episode.series_id == series.id,
                    Episode.season_number == body.season_number,
                    Episode.episode_number == body.episode_number,
                )
            )
        )
        .scalars()
        .first()
    )
    if episode is None:
        episode = Episode(
            series_id=series.id,
            season_number=body.season_number,
            episode_number=body.episode_number,
        )
        session.add(episode)
        await session.flush()

    media.episode_id = episode.id
    await session.flush()
    # Re-read so the selectin loader repopulates `media_files`; without it the response would
    # report the episode as still unavailable.
    episode = (
        (
            await session.execute(
                select(Episode)
                .where(Episode.id == episode.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    return library_service.episode_summary(episode, None)


# --- The import assistant --------------------------------------------------------------------
#
# `POST /unmatched/{fileId}/assign` above handles one file. Real libraries do not produce one
# file — they produce a folder of 58 opaque numeric ids at once, and assigning those one at a
# time is not a workflow. The four routes here read such a folder as a group: propose a
# complete mapping, let the admin turn the strategy and the numbering, then commit the whole
# thing in one action.
#
# "Committing" means creating episode rows that point at the files under the names they
# already have. `LIBRARY_ROOT` is mounted read-only and nothing in this application renames,
# moves, writes or deletes anything on it — which is not a workaround but the better design:
# the match is reversible and a mistaken plan cannot damage the library. `rename-script` is
# for the operator who wants tidy names anyway; dramatarr writes the text and nothing else.


@router.get("/unmatched/groups", response_model=list[UnmatchedGroupOut])
async def unmatched_groups(
    session: SessionDep,
    admin: AdminUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[UnmatchedGroupOut]:
    """Unmatched files grouped by folder, each with a ready-to-review plan.

    Built entirely from what the scan already stored — no file is opened and nothing is
    probed — so this stays cheap enough to render as a list.
    """
    return await library_service.unmatched_groups(session, limit=limit)


@router.post("/unmatched/plan", response_model=ImportPlanOut)
async def plan_unmatched(
    session: SessionDep, admin: AdminUser, body: ImportPlanIn
) -> ImportPlanOut:
    """Recompute one folder's plan with a different strategy, season or starting number."""
    return await library_service.plan_folder_import(
        session,
        folder=body.folder,
        strategy=body.strategy,
        season_number=body.season_number,
        start_episode=body.start_episode,
    )


@router.post("/unmatched/apply", response_model=ImportApplied)
async def apply_unmatched(
    session: SessionDep, admin: AdminUser, body: ImportApplyIn
) -> ImportApplied:
    """Commit an approved plan as catalogue rows. Idempotent; touches no file on the mount."""
    return await library_service.apply_import(session, body, actor_id=admin.id)


@router.get("/unmatched/rename-script", response_class=PlainTextResponse)
async def unmatched_rename_script(
    session: SessionDep,
    admin: AdminUser,
    folder: Annotated[str, Query(min_length=1, max_length=1000)],
    strategy: Annotated[str | None, Query(max_length=64)] = None,
    season_number: Annotated[int | None, Query(alias="seasonNumber", ge=0, le=999)] = None,
    start_episode: Annotated[int | None, Query(alias="startEpisode", ge=0, le=100_000)] = None,
) -> PlainTextResponse:
    """The rename the admin may want to run themselves, as text.

    Generated, never executed: the mount is read-only, the catalogue already knows these
    files under their current names, and the script says so in its own header.
    """
    plan = await library_service.plan_folder_import(
        session,
        folder=folder,
        strategy=strategy,
        season_number=season_number,
        start_episode=start_episode,
    )
    return PlainTextResponse(library_service.rename_script(plan))


# --- Removing things from the catalogue -----------------------------------------------------
#
# Two situations need this: a series indexed by mistake, and one whose files vanished from the
# NAS. The scanner marks those files `missing` and never deletes their rows — an unplugged disk
# must not wipe the library — so the stale entries need a deliberate way out. That is these
# routes, and they remove database rows only.


# --- Renaming on disk -------------------------------------------------------------------------
#
# The one pair of routes in this application that changes the library mount. Everything above
# reads it. The split is the whole safety model: `rename-plan` is a pure read that shows exactly
# what would happen, including the files that will be skipped and why, and `rename` enqueues the
# work that does it. Nothing renames as a side effect of matching, scanning or editing.
#
# See `app.services.renamer` for the rules the write obeys — rename in place, never across
# directories; never overwrite; database follows disk, one file at a time.



@router.get("/series/{series_id}/rename-plan", response_model=RenamePlanOut)
async def series_rename_plan(
    session: SessionDep, admin: AdminUser, series_id: str
) -> RenamePlanOut:
    """What renaming this series would do. Reads the disk; writes nothing."""
    plan = await renamer.plan(session, series_id)
    return RenamePlanOut(
        series_id=plan.series_id,
        series_title=plan.series_title,
        renamable_count=len(plan.renamable),
        items=[
            RenameItemOut(
                media_file_id=item.media_file_id,
                current_path=item.current_path,
                proposed_path=item.proposed_path,
                status=item.status,
                reason=item.reason,
                season_number=item.season_number,
                episode_number=item.episode_number,
                size_bytes=item.size_bytes,
            )
            for item in plan.items
        ],
    )


@router.post("/series/{series_id}/rename", response_model=JobRef, status_code=202)
async def rename_series_files(session: SessionDep, admin: AdminUser, series_id: str) -> JobRef:
    """Rename this series' files to match its metadata.

    Enqueued rather than done inline, and that is not only about long operations: the queue
    runs exactly one job at a time, which is what keeps a rename from racing the scanner. The
    per-file outcome lands in the job's log, so what happened to each file is answerable after
    the fact rather than only in the moment.

    The plan is recomputed by the worker. What an admin approved may be minutes old, and the
    filesystem is the authority on what is safe to do now.
    """
    series = await library_service.get_series_or_404(session, series_id)
    job = await enqueue(
        session,
        JobType.LIBRARY_RENAME,
        payload={"seriesId": series.id},
        series_id=series.id,
        created_by_id=admin.id,
    )
    return JobRef(id=job.id, type=job.type, status=job.status)


@router.get("/series/{series_id}/deletion-preview", response_model=SeriesDeletionPreview)
async def series_deletion_preview(
    session: SessionDep, admin: AdminUser, series_id: str
) -> SeriesDeletionPreview:
    """What a delete would take, and whether it would stick.

    `filesStillOnDisk` is measured against the mount rather than read off `media_files.status`,
    because a delete whose files are still there is undone by the next scan and the admin has
    to know that before they confirm.
    """
    series = await library_service.get_series_or_404(session, series_id)
    return await library_service.deletion_preview(session, series)


@router.delete("/series/{series_id}", response_model=SeriesDeleted)
async def delete_series(session: SessionDep, admin: AdminUser, series_id: str) -> SeriesDeleted:
    """Remove a series from the catalogue. No file under `LIBRARY_ROOT` is touched."""
    series = await library_service.get_series_or_404(session, series_id)
    return await library_service.delete_series(session, series, actor_id=admin.id)


@router.post("/cleanup", response_model=CleanupResult)
async def cleanup(
    session: SessionDep, admin: AdminUser, body: CleanupIn | None = None
) -> CleanupResult:
    """Drop the catalogue entries whose media is entirely gone.

    The body is optional so a bare POST means "clean up for real"; `{"dryRun": true}` returns
    the same numbers without deleting anything.
    """
    return await library_service.cleanup_catalogue(
        session, dry_run=bool(body and body.dry_run), actor_id=admin.id
    )


__all__ = ["router"]


# --- DramaDex ---------------------------------------------------------------------------------
#
# The indexer that makes the rest of this file's metadata plumbing legacy. TMDB, OMDb and the
# SearXNG discovery search all exist because nothing catalogued vertical short drama; DramaDex
# does, and it carries what none of them could — cast, platform links and structured tags.


def _hit(payload: dict[str, Any], found: dramadex.DramadexTitle) -> DramadexHit:
    """Attach the proxied poster path, which only the API layer knows."""
    payload = dict(payload)
    payload["poster_url"] = f"/api/library/dramadex/titles/{found.id}/poster"
    return DramadexHit(**payload)


async def _known_dramadex_ids(session: AsyncSession, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    rows = await session.execute(
        select(Series.dramadex_id).where(Series.dramadex_id.in_(ids))
    )
    return {row for row in rows.scalars() if row is not None}


@router.get("/dramadex/search", response_model=DramadexHits)
async def dramadex_search(
    session: SessionDep,
    admin: AdminUser,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> DramadexHits:
    """Search the household's indexer.

    Replaces the web-search panel entirely. That one scored search-engine results and could
    only ever hand back a link; this returns a record with a synopsis, a cast and a platform.

    `matches` is set per row from the normalised title, so the UI can lead with the one that
    *is* this series. Ranking alone is not enough — a query DramaDex cannot satisfy still
    returns its nearest neighbours, and binding one of those is how a series ends up with
    another show's metadata.
    """
    client = await dramadex.try_client(session)
    if client is None:
        return DramadexHits(configured=False, query=q)

    page = await client.search(q, limit=limit)
    known = await _known_dramadex_ids(session, [t.id for t in page.items])
    return DramadexHits(
        configured=True,
        query=q,
        items=[
            _hit(dramadex.hit_from(t, wanted=q, in_library=t.id in known), t)
            for t in page.items
        ],
    )


@router.get("/dramadex/titles/{title_id}/poster")
async def dramadex_poster(session: SessionDep, admin: AdminUser, title_id: int) -> Response:
    """Proxy key art from DramaDex.

    Proxied rather than linked because the indexer requires an API key on every route, and
    that key must not reach a browser. DramaDex serves its own posters for the same reason
    dramatarr does — so rendering a page never leaks a viewer's IP to a platform CDN.
    """
    client = await dramadex.require_client(session)
    fetched = await client.poster_bytes(title_id)
    if fetched is None:
        raise NotFound("Poster", "DRAMADEX_POSTER_NOT_FOUND")
    raw, content_type = fetched
    return Response(
        content=raw,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/series/{series_id}/metadata/dramadex", response_model=SeriesDetail)
async def apply_dramadex(
    session: SessionDep, admin: AdminUser, series_id: str, body: DramadexApplyIn
) -> SeriesDetail:
    """Fill a series from a DramaDex title, and bind the two.

    Blanks only unless `overwrite`, like every other fill here. The cast and the watch links
    are the exception and are replaced outright: they are a cache of the indexer's answer, not
    something an admin curates, so a merge would only preserve a mistake.
    """
    series = await library_service.get_series_or_404(session, series_id)
    client = await dramadex.require_client(session)

    found = await client.title(body.dramadex_id)
    if found is None:
        raise NotFound("DramaDex title", "DRAMADEX_TITLE_NOT_FOUND")

    filled = await library_service.apply_dramadex_title(
        session, series, found, overwrite=body.overwrite
    )

    # Artwork last and never fatally: a poster that will not decode must not undo a fill that
    # already succeeded.
    if not series.poster_path or body.overwrite:
        fetched = await client.poster_bytes(found.id)
        if fetched is not None:
            stored = await cache_dramadex_poster(fetched[0], title_id=found.id)
            if stored:
                series.poster_path = stored
                await session.commit()

    log.info("dramadex: filled %s on %s", ", ".join(filled) or "nothing", series.id)
    return await library_service.series_detail(session, series, admin.id)


@router.get("/dramadex/people/{person_id}", response_model=PersonCredits)
async def dramadex_person(
    session: SessionDep, user: CurrentUser, person_id: int
) -> PersonCredits:
    """An actor and everything the indexer has them in.

    Open to any signed-in member, not just admins: following a performer from a series to
    their other shorts is browsing, and it is the one thing this genre never had anywhere.
    Each credit is flagged `in_library`, which turns "who else is in this" into "which of
    those do we already have".
    """
    client = await dramadex.require_client(session)
    person = await client.person(person_id)
    if person is None:
        raise NotFound("Person", "DRAMADEX_PERSON_NOT_FOUND")

    known = await _known_dramadex_ids(session, [t.id for t in person.titles])
    return PersonCredits(
        id=person.id,
        name=person.name,
        bio=person.bio,
        credits=[
            _hit(dramadex.hit_from(t, in_library=t.id in known), t) for t in person.titles
        ],
    )
