"""Wire shapes for the library API.

Everything here inherits `APIModel`, so Python stays snake_case and the JSON is camelCase.

Every image and playback field is exposed as a **ready-to-use URL** (`posterUrl`, `stillUrl`,
`streamUrl`, the subtitle track `url`): the client drops the value straight into `src=`, so it
must never have to know where the bytes live. The raw stored paths (`posterPath`, `stillPath`,
`mediaFile.path`) are kept alongside for the admin surfaces that show them, but they are not
addresses. `app.services.library` builds the URLs; see `image_url()` there for the mapping
between a DATA_DIR-relative path and `/api/stream/images/{kind}/{name}`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import APIModel

# --- Tracks and files -----------------------------------------------------------------------


class AudioTrackOut(APIModel):
    id: str
    stream_index: int
    language: str | None = None
    title: str | None = None
    codec: str | None = None
    channels: int | None = None
    is_default: bool = False


class SubtitleTrackOut(APIModel):
    id: str
    stream_index: int | None = None
    language: str | None = None
    title: str | None = None
    codec: str | None = None
    is_default: bool = False
    is_forced: bool = False
    is_sidecar: bool = False
    #: WebVTT rendition, fed straight to `<track src>`. Extraction happens on first request,
    #: so this is always present — the file behind it may not exist yet.
    url: str


class MediaFileOut(APIModel):
    id: str
    path: str
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    bitrate: int | None = None
    size_bytes: int = 0
    direct_play_ok: bool = True
    status: str = "present"


# --- Watch state ----------------------------------------------------------------------------


class WatchProgressOut(APIModel):
    episode_id: str
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    percent: float = 0.0
    completed: bool = False
    play_count: int = 0
    #: None whenever the UI must not offer "Resume" (§4: < 15s in, or already completed).
    resume_position_seconds: float | None = None
    first_played_at: datetime | None = None
    last_played_at: datetime | None = None


# --- Episodes -------------------------------------------------------------------------------


class EpisodeSummary(APIModel):
    id: str
    series_id: str
    season_number: int
    #: Wire name `episodeNumber`; `number` alone was ambiguous next to `absoluteNumber`.
    episode_number: int
    absolute_number: int | None = None
    title: str | None = None
    air_date: date | None = None
    #: Stored path (relative to DATA_DIR, or a TMDB source path until the cache job runs).
    still_path: str | None = None
    #: The address for that image. Null when there is no still at all.
    still_url: str | None = None
    runtime_seconds: int | None = None
    available: bool = False
    direct_play_ok: bool = True
    watched: bool = False
    in_progress: bool = False
    percent: float = 0.0
    resume_position_seconds: float | None = None
    play_count: int = 0


class EpisodeDetail(EpisodeSummary):
    overview: str | None = None
    metadata_locked: bool = False
    tmdb_id: int | None = None
    #: Always populated in practice (`series_id` is a non-null FK); "" rather than null keeps
    #: the wire type a string for the player header.
    series_title: str = ""
    series_slug: str = ""
    media_file: MediaFileOut | None = None
    #: Media-file facts, flattened for the player. All null when no file is present — the
    #: episode is declared but not on disk.
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    container: str | None = None
    #: Canonical playback route. Always present; it 404s while the episode has no file.
    stream_url: str
    #: Scrubbing cues. Null until the `thumbnail_sprite` job has actually written the file.
    thumbnails_url: str | None = None
    audio_tracks: list[AudioTrackOut] = Field(default_factory=list)
    subtitle_tracks: list[SubtitleTrackOut] = Field(default_factory=list)
    progress: WatchProgressOut | None = None
    previous_episode_id: str | None = None
    next_episode_id: str | None = None


class SeasonGroup(APIModel):
    season_number: int
    episode_count: int = 0
    available_episode_count: int = 0
    watched_episode_count: int = 0
    episodes: list[EpisodeSummary] = Field(default_factory=list)


# --- Series ---------------------------------------------------------------------------------


class SeriesProgress(APIModel):
    episode_count: int = 0
    available_episode_count: int = 0
    declared_episode_count: int | None = None
    watched_episode_count: int = 0
    percent: float = 0.0
    completed: bool = False
    #: Episode the user is part-way through, if any.
    resume_episode_id: str | None = None
    #: First available episode that has not been watched.
    next_episode_id: str | None = None


class SeriesTitleOut(APIModel):
    id: str
    language: str = ""
    title: str
    kind: str = "alternate"
    source: str = "manual"
    normalized: str = ""


class SeriesSummary(APIModel):
    id: str
    slug: str
    title: str
    sort_title: str = ""
    original_title: str | None = None
    year: int | None = None
    status: str = "unknown"
    #: Stored paths (relative to DATA_DIR, or TMDB source paths until the cache job runs).
    poster_path: str | None = None
    backdrop_path: str | None = None
    #: The address for the poster — what the browse grid renders. Null when there is none.
    poster_url: str | None = None
    genres: list[str] = Field(default_factory=list)
    network: str | None = None
    content_rating: str | None = None
    episode_count: int = 0
    available_episode_count: int = 0
    declared_episode_count: int | None = None
    watched_episode_count: int = 0
    progress_percent: float = 0.0
    #: NOT NULL in the database, so it is never null on the wire either.
    added_at: datetime
    #: This user's last activity on the series; null until they have played something.
    last_played_at: datetime | None = None


class SeriesDetail(SeriesSummary):
    overview: str | None = None
    original_language: str | None = None
    #: The address for the backdrop — the hero image behind the series header.
    backdrop_url: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    library_path: str = ""
    metadata_locked: bool = False
    updated_at: datetime | None = None
    titles: list[SeriesTitleOut] = Field(default_factory=list)
    seasons: list[SeasonGroup] = Field(default_factory=list)
    progress: SeriesProgress = Field(default_factory=SeriesProgress)
    #: What Play/Resume starts: the episode in flight, else the first unwatched available
    #: one, else null when nothing is playable. Same object as its entry in `seasons`.
    next_episode: EpisodeSummary | None = None
    #: Where `overview` came from — `"tmdb" | "wikipedia" | "manual"`. Null on rows written
    #: before provenance was recorded, which are TMDB's by construction.
    overview_source: str | None = None
    #: The page the text came from. For Wikipedia this is the attribution its licence
    #: requires, so anything rendering the overview has to be able to show it.
    overview_source_url: str | None = None
    #: The language of the *text*, which need not be the household's: a drama with no Spanish
    #: synopsis anywhere may deliberately carry a Korean or English one.
    overview_source_lang: str | None = None


class ContinueWatchingItem(APIModel):
    series: SeriesSummary
    episode: EpisodeSummary
    progress: WatchProgressOut | None = None
    #: The activity that put this row where it is in the list — the newest of the series'
    #: play events and watch-progress timestamps, which is exactly the sort key.
    last_played_at: datetime


class HistoryItem(APIModel):
    series: SeriesSummary
    episode: EpisodeSummary
    progress: WatchProgressOut


class GenreFacet(APIModel):
    #: The genre itself; the wire name is `genre`, not `name`.
    genre: str
    count: int


# --- Admin-side library shapes --------------------------------------------------------------


class UnmatchedFileOut(APIModel):
    id: str
    path: str
    size_bytes: int = 0
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    direct_play_ok: bool = True
    status: str = "present"
    scan_error: str | None = None
    #: Both NOT NULL in the database. `first_seen_at` is what the admin list sorts "new" by.
    first_seen_at: datetime
    scanned_at: datetime | None = None


class JobRef(APIModel):
    """Minimal handle on an enqueued job so the UI can follow it over SSE."""

    id: str
    type: str
    status: str


# --- The import assistant ---------------------------------------------------------------------
#
# A folder whose filenames carry no episode signal — opaque numeric ids, CDN rendition names,
# camera exports — is useless one file at a time. These shapes describe the same folder read as
# a **group**: a proposed ordering, the strategy that produced it, and what applying it would
# do. Applying creates catalogue rows that point at the files under the names they already
# have; `LIBRARY_ROOT` is read-only and nothing in this application renames anything on it.
# `proposed_filename` exists only so the admin can see what tidy names *would* look like, and
# to build the shell script they may choose to run themselves.


class ImportStrategyOut(APIModel):
    """One way of reading the folder, and how much the planner believes in it.

    `confidence` is 0..1 and `name` is one of `app.services.importer.STRATEGY_NAMES`, which
    is the value `POST /unmatched/plan` takes back as `strategy`. Every applicable strategy
    is listed, best first, so the admin can overrule the winner.
    """

    name: str
    confidence: float = 0.0


class ImportAssignmentOut(APIModel):
    """One file, and the episode slot the planner puts it in."""

    media_file_id: str
    #: Relative to `LIBRARY_ROOT`, exactly as stored — this is the name that stays on disk.
    path: str
    filename: str
    size_bytes: int = 0
    season_number: int = 1
    episode_number: int
    confidence: float = 0.0
    #: Why this file landed on this number, for the admin's review column.
    reason: str = ""
    #: The value the strategy sorted on — the extracted integer, the parsed date, the
    #: modification timestamp. `null` when the order is the filename itself. `reason` is the
    #: English prose about it; this is the value, for a client that renders its own sentence.
    order_key: str | None = None
    #: What a rename *would* produce. Advisory only: dramatarr never writes it.
    proposed_filename: str = ""
    #: The episode already sitting at that season/number, when one does.
    existing_episode_id: str | None = None
    #: True when that episode already holds a different present file, so applying this row
    #: as-is would be refused with `EPISODE_NUMBER_TAKEN`. Surfaced here so the admin sees
    #: the collision in the review table instead of hitting it at the end.
    conflict: bool = False


class ImportPlanOut(APIModel):
    """A complete proposal for one folder — every file gets a number, or a warning says why."""

    folder: str
    strategy: str = ""
    confidence: float = 0.0
    #: Why this strategy read the folder the way it did, in one sentence — "58 files share
    #: the form 7011512N.720p.narrowv3.mp4 with numbers 701151231 to 701151288, contiguous".
    #: The single most useful thing on the review screen, and the reason the admin can
    #: approve 58 rows at once instead of reading all of them.
    explanation: str = ""
    #: Stable identifier for the shape of `explanation`, one of
    #: `app.services.importer.EXPLANATION_CODES`. The UI renders the sentence from this in the
    #: reader's language and falls back to `explanation` for a code it does not know yet, the
    #: same degradation the error codes get — otherwise an English sentence lands on a Spanish
    #: page. `warnings` carry no codes yet and are still read as detail text.
    explanation_code: str = ""
    season_number: int = 1
    start_episode: int = 1
    file_count: int = 0
    suggested_series_title: str = ""
    suggested_year: int | None = None
    #: Series already indexed at this folder (or at an ancestor of it, for a season subfolder).
    #: The UI defaults "attach to" to this instead of offering to create a duplicate.
    existing_series_id: str | None = None
    existing_series_title: str | None = None
    strategies: list[ImportStrategyOut] = Field(default_factory=list)
    assignments: list[ImportAssignmentOut] = Field(default_factory=list)
    #: Human-readable caveats: gaps in the run, files the planner could not place, a
    #: numbering that would collide. Never fatal — the admin decides.
    warnings: list[str] = Field(default_factory=list)
    #: True when at least one assignment carries `conflict`.
    has_conflicts: bool = False


class UnmatchedGroupOut(APIModel):
    """A folder of unmatched files, ready to render as one row in the admin's list.

    Built from what the scan already stored — no file on the mount is opened to produce it.
    """

    folder: str
    file_count: int = 0
    total_bytes: int = 0
    suggested_series_title: str = ""
    suggested_year: int | None = None
    existing_series_id: str | None = None
    existing_series_title: str | None = None
    strategies: list[ImportStrategyOut] = Field(default_factory=list)
    plan: ImportPlanOut


class ImportCreated(APIModel):
    series: bool = False
    episodes: int = 0


class ImportApplied(APIModel):
    """Outcome of an apply. Every number counts **catalogue rows**; the mount was not touched."""

    series_id: str
    series_slug: str
    #: The series the files now belong to, so the success screen can name it without a
    #: second request — it may be one that already existed rather than the title requested.
    series_title: str = ""
    created: ImportCreated = Field(default_factory=ImportCreated)
    #: Media files newly pointed at an episode. Zero on a repeat apply of the same plan,
    #: which is what makes the operation idempotent rather than merely harmless.
    attached: int = 0
    #: Enqueued for a series this call created, so the UI can follow it over SSE.
    metadata_job: JobRef | None = None


# --- Deletion and cleanup ---------------------------------------------------------------------
#
# Deleting a series removes *catalogue* rows and nothing else. The library mount is read-only:
# no route in this application creates, moves or deletes a file under `LIBRARY_ROOT`, and that
# is precisely why `files_still_on_disk` has to be on the wire — see below.


class ImdbApplyIn(APIModel):
    """Which IMDb record to fill this series from."""

    imdb_id: str = Field(min_length=7, max_length=32, pattern=r"^(?i:tt)\d{5,12}$")
    #: False fills only the empty fields. True replaces what is there — the admin asking for
    #: that has usually just realized the row was matched to the wrong title.
    overwrite: bool = False


class RenameItemOut(APIModel):
    """One file's before and after, or the reason it will be left alone.

    The skipped rows travel with the rest deliberately. An admin deciding whether to run this
    needs to see the collision that will be refused, not discover it in a job log afterwards.
    """

    media_file_id: str
    current_path: str
    #: Equal to `current_path` when the file is already correctly named.
    proposed_path: str
    #: `ok` | `unchanged` | `collision` | `missing` | `failed`.
    status: str
    reason: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    size_bytes: int = 0


class RenamePlanOut(APIModel):
    series_id: str
    series_title: str
    #: How many rows would actually be renamed — the number the confirm button should show.
    renamable_count: int = 0
    items: list[RenameItemOut] = Field(default_factory=list)


class SeriesDeletionPreview(APIModel):
    """What `DELETE /series/{id}` would take, and whether taking it would stick.

    The catalogue is a projection of the mount, so deleting a series whose files are still
    there lasts exactly until the next scan re-creates it. `files_still_on_disk` is that fact,
    measured against the filesystem rather than read off `media_files.status`, so the admin
    can be told before they confirm instead of discovering it an hour later.
    """

    series_title: str
    episode_count: int = 0
    media_file_count: int = 0
    #: Rows the scanner last saw on the mount.
    present_file_count: int = 0
    #: Rows the scanner could not find. The scanner never deletes them — an unplugged disk
    #: must not wipe the library — which is why they need a deliberate way out.
    missing_file_count: int = 0
    #: Distinct users whose watch progress the delete would throw away.
    users_with_progress: int = 0
    #: True when at least one `present` media file still resolves to a real file under
    #: `LIBRARY_ROOT`. True means "this series will come back on the next scan".
    files_still_on_disk: bool = False


class SeriesDeleted(APIModel):
    """Outcome of a delete. Counts are of catalogue rows; no media file was touched."""

    ok: bool = True
    deleted_episodes: int = 0
    deleted_media_files: int = 0
    #: Same signal as the preview, re-measured at the moment of deletion: the series is gone
    #: from the catalogue but the files that produced it are not, so a scan will bring it back.
    files_still_on_disk: bool = False


class CleanupIn(APIModel):
    #: Compute the same numbers, delete nothing.
    dry_run: bool = False


class CleanupResult(APIModel):
    #: Titles rather than ids — this is what the admin reads back afterwards.
    removed_series: list[str] = Field(default_factory=list)
    removed_episodes: int = 0
    removed_media_files: int = 0
    dry_run: bool = False


class TmdbMatch(APIModel):
    """One candidate in the match dialog.

    Everything here exists so an admin can pick the right row *without leaving the dialog*.
    `originalName` and `originalLanguage` are what separate a Korean drama from its three
    remakes; `popularity` and `voteCount` are what separate the real show from the fan-made
    entry with the same name and no votes.
    """

    tmdb_id: int
    name: str
    original_name: str | None = None
    original_language: str | None = None
    overview: str | None = None
    first_air_date: str | None = None
    year: int | None = None
    poster_path: str | None = None
    poster_url: str | None = None
    backdrop_path: str | None = None
    popularity: float | None = None
    vote_average: float | None = None
    vote_count: int | None = None


# --- Artwork ----------------------------------------------------------------------------------
#
# The metadata jobs pick a poster on their own and are usually right. These shapes are for when
# they are not: TMDB's whole gallery, plus an optional web search, laid out so an admin can look
# at the candidates and choose one. Choosing sets `metadata_locked` — the choice would be
# pointless if the next refresh silently replaced it.


class ArtworkCandidate(APIModel):
    """One image an admin could choose. `url` is what `POST .../artwork` takes back."""

    #: `tmdb` or the image-search provider's name.
    source: str
    #: `poster` or `backdrop` for TMDB candidates; `""` for a web result, which carries no
    #: shape the provider vouches for — the admin says which slot it fills.
    kind: str = ""
    url: str
    #: Smaller rendition for the picker grid. Falls back to `url` when the source has none.
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None
    #: ISO-639-1, or null for textless artwork — usually the most useful kind, and the reason
    #: `include_image_language` asks TMDB for `null` explicitly.
    language: str | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    #: Web results only: the page the image was found on, so the admin can check the source.
    title: str = ""
    context_url: str | None = None
    #: Web results only, and only when the provider says: which back-end engine produced the
    #: hit. SearXNG reports it per result, and it is what tells an admin which of their engines
    #: is worth keeping in `images.search.engines`.
    engine: str = ""


class ArtworkSourceOut(APIModel):
    """Whether a source contributed, and why not when it did not.

    An unavailable source is **not** an error. A fresh install has no web search configured and
    a series may not be linked to TMDB yet; both are ordinary states, and a 503 for either
    would make the picker unusable exactly when the admin is trying to fix the situation.
    """

    #: The channel: `tmdb` or `web`.
    name: str
    #: Which implementation answered for that channel — `searxng`, `google_cse`, or `""` for
    #: none.
    provider: str = ""
    available: bool = False
    #: Stable code the UI localizes: `NOT_LINKED`, `NOT_CONFIGURED`, `PROVIDER_ERROR`, or `""`.
    reason: str = ""
    count: int = 0


class ArtworkCandidates(APIModel):
    series_id: str
    tmdb_id: int | None = None
    posters: list[ArtworkCandidate] = Field(default_factory=list)
    backdrops: list[ArtworkCandidate] = Field(default_factory=list)
    #: Web-search hits, kept apart from the two above because they carry no declared kind.
    web: list[ArtworkCandidate] = Field(default_factory=list)
    sources: list[ArtworkSourceOut] = Field(default_factory=list)


class ArtworkSelectIn(APIModel):
    kind: Literal["poster", "backdrop"]
    url: str = Field(min_length=1, max_length=2000)


class ArtworkApplied(APIModel):
    """What was stored. `url` is the address to render immediately — the picker swaps to it."""

    series_id: str
    kind: str
    #: DATA_DIR-relative, as stored on the series row.
    stored_path: str
    url: str | None = None
    #: Always true. Stated on the wire because it is a consequence the admin should see: their
    #: choice now also stops metadata refreshes from overwriting the series' other fields.
    metadata_locked: bool = True


# --- Web discovery ------------------------------------------------------------------------------
#
# What `GET /discover` answers with. TMDB's title search cannot find a lot of short-form Asian
# drama, but a web search finds the *page* — and the page's URL carries an id that
# `GET /tmdb/lookup` can resolve. So these shapes describe results as **identifiers with
# provenance**, not as content.
#
# `snippet` is display text and nothing else. It is a truncated extract of a page dramatarr
# does not control, frequently cut mid-sentence and sometimes carrying navigation furniture; it
# exists so a human can tell two results apart, and it never becomes a series overview. That
# comes from TMDB, which is built to be read by a program.


class DiscoveredIds(APIModel):
    """Every identifier one result URL carries. All fields empty is a perfectly normal result.

    Produced by `app.services.discovery.extract_ids`, which reads them out of the URL and never
    fetches the page behind it.
    """

    #: TMDB's numeric id, plus which of TMDB's two catalogues it belongs to — a `/movie/` link
    #: and a `/tv/` link can hold the same number and mean different titles.
    tmdb_id: int | None = None
    tmdb_kind: Literal["tv", "movie"] | None = None
    #: `tt0903747`, lower-cased. Shaped so it can be handed straight to `GET /tmdb/lookup`.
    imdb_id: str | None = None
    #: Wikipedia is recorded as the pair that addresses an article: the language subdomain and
    #: the title, percent-decoded with underscores turned back into spaces.
    wikipedia_lang: str | None = None
    wikipedia_title: str | None = None
    wikidata_id: str | None = None
    #: MyDramaList's id. Recorded because it is often the *only* site that has an obscure
    #: drama, and shown for that reason — nothing in dramatarr resolves it.
    mydramalist_id: str | None = None
    #: DramaBoxDB's id and slug. Recorded for the same reason as MyDramaList and used the same
    #: way: it is a link the admin follows, never a page this backend fetches. The site
    #: publishes no API and its robots.txt closes its search, so treating it as a lead rather
    #: than a source is deliberate.
    dramaboxdb_id: str | None = None
    dramaboxdb_slug: str | None = None


class DiscoveryResult(APIModel):
    """One web result, as something to choose between rather than something to read."""

    title: str = ""
    url: str
    #: The engine's extract, whitespace-collapsed and bounded. Display only.
    snippet: str = ""
    #: Which of the instance's engines produced the hit, when it says.
    engine: str = ""
    #: `themoviedb.org`, `mydramalist.com` — rendered next to the snippet so the admin can see
    #: whose words they are reading before they trust them.
    source_domain: str = ""
    #: The platform this page belongs to, when `source_domain` is one the household configured
    #: — `dramabox.com` → `DramaBox`.
    #:
    #: The one structured fact a platform page reliably carries. Its snippet is somebody else's
    #: prose cut mid-sentence and is never stored, but *which platform published it* is not a
    #: guess: it is the host, read through the same provider table the network picker uses. So
    #: a row that can never fill a synopsis can still fill the network, which is the field this
    #: library is emptiest in.
    network: str | None = None
    ids: DiscoveredIds = Field(default_factory=DiscoveredIds)
    #: True when `ids` carries a TMDB or IMDb id, i.e. when this row is one click from a real
    #: match. The UI leads with these; the rest are context.
    resolvable: bool = False


class DiscoveryResults(APIModel):
    query: str = ""
    #: Which provider answered. Only `searxng` does today.
    provider: str = ""
    #: Set by the series-scoped route, so a client can tell an answer about *this* series from
    #: a free-form search.
    series_id: str | None = None
    results: list[DiscoveryResult] = Field(default_factory=list)
    #: How many of `results` are resolvable, so the UI can say "nothing here can be matched
    #: yet" without walking the list.
    resolvable_count: int = 0
    #: True when the query was restricted to the household's configured platform domains. It
    #: is what lets the UI distinguish "your platforms have nothing about this" from "the web
    #: has nothing about this" — two very different things to tell an admin, and the reason
    #: this search no longer widens itself when the narrow one comes back empty.
    narrowed: bool = False
    #: Engines the SearXNG instance reported as failing for this query. Present so that an
    #: empty result list can be attributed: "your platforms have nothing" and "the engines
    #: that could have answered were blocked" look identical without it, and this household
    #: hit the second one for days while reading it as the first.
    unresponsive_engines: list[str] = Field(default_factory=list)


class SeriesUnlinked(APIModel):
    """Outcome of forgetting a wrong TMDB match."""

    series_id: str
    #: The id that was cleared, so the admin can see what it had been bound to.
    previous_tmdb_id: int | None = None
    metadata_locked: bool = True


# --- Request bodies -------------------------------------------------------------------------


class SeriesPatch(APIModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    sort_title: str | None = Field(default=None, max_length=500)
    original_title: str | None = Field(default=None, max_length=500)
    original_language: str | None = Field(default=None, max_length=16)
    overview: str | None = None
    year: int | None = Field(default=None, ge=1800, le=2200)
    status: str | None = None
    content_rating: str | None = Field(default=None, max_length=32)
    genres: list[str] | None = None
    network: str | None = Field(default=None, max_length=200)
    declared_episode_count: int | None = Field(default=None, ge=0)
    imdb_id: str | None = Field(default=None, max_length=32)
    #: Sent on its own to hand a series back to the metadata jobs.
    metadata_locked: bool | None = None


class SeriesBulkPatch(APIModel):
    """One edit, many series.

    Bounded at 200 ids because this runs inside a single request: a selection larger than a
    page of results is almost always a mis-click on "select all" against an unfiltered library,
    and a bounded refusal is friendlier than a request that holds the session open for a
    minute. The client can send batches if it genuinely means thousands.
    """

    ids: list[str] = Field(min_length=1, max_length=200)
    patch: SeriesPatch


class SeriesBulkResult(APIModel):
    updated: int = 0
    #: Ids that no longer exist — a selection made before someone else deleted a row. Reported
    #: rather than raised, so one stale id does not discard the rest of the batch.
    missing: list[str] = Field(default_factory=list)


class EpisodePatch(APIModel):
    title: str | None = Field(default=None, max_length=500)
    overview: str | None = None
    air_date: date | None = None
    season_number: int | None = Field(default=None, ge=0)
    episode_number: int | None = Field(default=None, ge=0)
    absolute_number: int | None = Field(default=None, ge=0)
    runtime_seconds: int | None = Field(default=None, ge=0)
    metadata_locked: bool | None = None


class SeriesTitleIn(APIModel):
    title: str = Field(min_length=1, max_length=500)
    language: str = Field(default="", max_length=16)
    kind: str = "alternate"


class MatchIn(APIModel):
    tmdb_id: int = Field(ge=1)


class AssignIn(APIModel):
    series_id: str
    season_number: int = Field(default=1, ge=0)
    episode_number: int = Field(ge=0)


class ImportPlanIn(APIModel):
    """Ask for the plan again with one knob turned.

    All three overrides are optional and independent: omitting `strategy` re-picks the best
    one, omitting the numbers lets the planner keep whatever it inferred from the folder.
    """

    folder: str = Field(min_length=1, max_length=1000)
    strategy: str | None = Field(default=None, max_length=64)
    season_number: int | None = Field(default=None, ge=0, le=999)
    start_episode: int | None = Field(default=None, ge=0, le=100_000)


class ImportAssignmentIn(APIModel):
    media_file_id: str = Field(min_length=1, max_length=64)
    season_number: int = Field(default=1, ge=0, le=999)
    episode_number: int = Field(ge=0, le=100_000)


class ImportApplyIn(APIModel):
    """What the admin approved.

    The assignments are sent back rather than recomputed server-side on purpose: the admin
    may have edited individual rows, and applying a freshly recomputed plan would quietly
    discard those edits. `folder` still scopes the operation — every `media_file_id` must
    belong to it.
    """

    folder: str = Field(min_length=1, max_length=1000)
    #: Attach to this series. When null a series is resolved by folder, or created.
    series_id: str | None = Field(default=None, max_length=64)
    new_series_title: str | None = Field(default=None, min_length=1, max_length=500)
    new_series_year: int | None = Field(default=None, ge=1800, le=2200)
    assignments: list[ImportAssignmentIn] = Field(min_length=1, max_length=5000)


__all__ = [
    "ArtworkApplied",
    "ArtworkCandidate",
    "ArtworkCandidates",
    "ArtworkSelectIn",
    "ArtworkSourceOut",
    "AssignIn",
    "AudioTrackOut",
    "CleanupIn",
    "CleanupResult",
    "ContinueWatchingItem",
    "DiscoveredIds",
    "DiscoveryResult",
    "DiscoveryResults",
    "SeriesBulkPatch",
    "SeriesBulkResult",
    "EpisodeDetail",
    "EpisodePatch",
    "EpisodeSummary",
    "GenreFacet",
    "HistoryItem",
    "ImdbApplyIn",
    "ImportApplied",
    "ImportApplyIn",
    "ImportAssignmentIn",
    "ImportAssignmentOut",
    "ImportCreated",
    "ImportPlanIn",
    "ImportPlanOut",
    "ImportStrategyOut",
    "JobRef",
    "MatchIn",
    "MediaFileOut",
    "SeasonGroup",
    "SeriesDeleted",
    "SeriesDeletionPreview",
    "SeriesDetail",
    "SeriesPatch",
    "SeriesProgress",
    "SeriesSummary",
    "SeriesTitleIn",
    "SeriesTitleOut",
    "SeriesUnlinked",
    "SubtitleTrackOut",
    "TmdbMatch",
    "UnmatchedFileOut",
    "UnmatchedGroupOut",
    "WatchProgressOut",
]
