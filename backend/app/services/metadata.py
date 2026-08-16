"""Metadata for a single series, from TMDB.

Everything here is idempotent and re-runnable: a refresh may add rows, and may update
TMDB-sourced columns, but it never touches a row whose `metadata_locked` is set and never
rewrites or deletes a title an admin typed in.

**Provenance.** Every path that writes `overview` also writes where it came from
(`overview_source`, `overview_source_url`, `overview_source_lang`), so no row holds text whose
origin has to be guessed at. This module stamps `tmdb`; an admin editing the field stamps
`manual`. NULL means the row predates the columns, which is TMDB by construction.

`wikipedia` is a fourth value those columns can still hold. There was once a route that took a
Wikipedia article's lead section as a synopsis; it was removed because it answered nothing for
this library — Wikipedia catalogues television, and almost no vertical short-form drama. Rows
written while it existed keep their attribution, which is why `OverviewSource.WIKIPEDIA`
remains: it describes stored data, not a code path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, NotFound
from app.models import (
    Episode,
    EpisodeTitle,
    JobType,
    MediaFile,
    Series,
    SeriesStatus,
    SeriesTitle,
    SeriesTranslation,
    TitleKind,
    TitleSource,
)
from app.models.library import OverviewSource
from app.services.images import ProgressCtx, is_cached_path
from app.services.search.index import reindex_series
from app.services.search.normalize import normalize, search_keys
from app.services.tmdb import TMDBClient, try_tmdb_client
from app.services.vocab import canonical_content_rating, canonical_network, normalize_genres
from app.utils.ids import new_id

log = logging.getLogger(__name__)

#: Where a TMDB overview came from, for the provenance columns. The API base is
#: `api.themoviedb.org`; this is the page a person can actually open.
TMDB_WEB_BASE = "https://www.themoviedb.org"

#: rapidfuzz WRatio below which an automatic match is refused. A wrong series is far more
#: expensive to undo than an unmatched one, so the bar sits deliberately high.
MATCH_THRESHOLD = 72.0
MAX_CANDIDATES = 10

_STATUS_MAP: dict[str, SeriesStatus] = {
    "returning series": SeriesStatus.ONGOING,
    "in production": SeriesStatus.ONGOING,
    "planned": SeriesStatus.ONGOING,
    "pilot": SeriesStatus.ONGOING,
    "ended": SeriesStatus.COMPLETED,
    "canceled": SeriesStatus.COMPLETED,
    "cancelled": SeriesStatus.COMPLETED,
}


# --- Small helpers -------------------------------------------------------------------------


async def _emit(ctx: ProgressCtx | None, percent: float, message: str) -> None:
    if ctx is not None:
        await ctx.progress(percent, message)


async def _log(ctx: ProgressCtx | None, level: str, message: str) -> None:
    log.log({"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 20), message)
    if ctx is not None:
        await ctx.log(level, message)


def _year_of(value: str | None) -> int | None:
    if not value or len(value) < 4 or not value[:4].isdigit():
        return None
    return int(value[:4])


def _date_of(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _lang_tag(language: str | None, region: str | None) -> str:
    lang = (language or "").strip()
    reg = (region or "").strip().upper()
    if lang and reg:
        return f"{lang}-{reg}"
    return (lang or reg)[:16]


def _compute_variants(titles: Sequence[str]) -> dict[str, list[str]]:
    return {title: search_keys(title) for title in titles}


async def _variants_for(titles: Iterable[str]) -> dict[str, list[str]]:
    """Romanization is pure CPU work (pypinyin, pykakasi); batch it off the event loop."""
    unique = [title for title in dict.fromkeys(titles) if title and title.strip()]
    if not unique:
        return {}
    return await asyncio.to_thread(_compute_variants, unique)


class _TitleCollector:
    """Builds the title rows to insert.

    Two rules: the (owner, language, title) unique constraint is honoured, and a title whose
    normalized form is already indexed is dropped — an extra row with identical `normalized`
    adds nothing to the search index and a lot of noise to the table.
    """

    def __init__(self, existing: Iterable[tuple[str, str, str]] = ()) -> None:
        self._keys: set[tuple[str, str]] = set()
        self._norms: set[str] = set()
        for language, title, normalized in existing:
            self._keys.add((language or "", title))
            if normalized:
                self._norms.add(normalized)
        self.rows: list[dict[str, str]] = []

    def add(self, language: str | None, title: str | None, kind: str, source: str) -> None:
        text = (title or "").strip()[:500]
        if not text:
            return
        lang = (language or "").strip()[:16]
        key = (lang, text)
        if key in self._keys:
            return
        normalized = normalize(text)[:500]
        if normalized and normalized in self._norms:
            return
        self._keys.add(key)
        if normalized:
            self._norms.add(normalized)
        self.rows.append(
            {
                "language": lang,
                "title": text,
                "kind": str(kind),
                "source": str(source),
                "normalized": normalized,
            }
        )

    def add_with_variants(
        self,
        language: str | None,
        title: str | None,
        kind: str,
        source: str,
        variants: Mapping[str, list[str]],
    ) -> None:
        self.add(language, title, kind, source)
        for variant in variants.get((title or "").strip(), ()):
            # Every generated form is its own language-neutral row, per the search contract.
            self.add("", variant, TitleKind.ROMANIZATION, source)


# --- Matching ------------------------------------------------------------------------------


def _best_ratio(left: Sequence[str], right: Sequence[str]) -> float:
    best = 0.0
    for a in left:
        for b in right:
            if a and b:
                best = max(best, float(fuzz.WRatio(a, b)))
    return best


async def _match_tmdb_id(
    client: TMDBClient,
    series: Series,
    *,
    ctx: ProgressCtx | None,
) -> int | None:
    await _emit(ctx, 5, f"Searching TMDB for {series.title}")
    results = await client.search_tv(series.title, year=series.year)
    if not results:
        await _log(ctx, "warning", f"No TMDB result for {series.title!r}")
        return None

    candidates = results[:MAX_CANDIDATES]
    titles = [series.title]
    for candidate in candidates:
        titles.extend([candidate.get("name") or "", candidate.get("original_name") or ""])
    variants = await _variants_for(titles)
    ours = variants.get(series.title.strip(), [normalize(series.title)])

    best: dict[str, Any] | None = None
    best_score = 0.0
    for candidate in candidates:
        theirs: list[str] = []
        for name in (candidate.get("name"), candidate.get("original_name")):
            theirs.extend(variants.get((name or "").strip(), []))
        score = _best_ratio(ours, theirs)
        candidate_year = _year_of(candidate.get("first_air_date"))
        if series.year and candidate_year:
            if candidate_year == series.year:
                score += 8.0
            elif abs(candidate_year - series.year) > 1:
                score -= 20.0
        if score > best_score:
            best, best_score = candidate, score

    if best is None or best_score < MATCH_THRESHOLD:
        name = (best or {}).get("name", "-")
        await _log(
            ctx,
            "warning",
            f"No confident TMDB match for {series.title!r} "
            f"(best {name!r} scored {best_score:.0f}, threshold {MATCH_THRESHOLD:.0f})",
        )
        return None

    tmdb_id = best.get("id")
    if not isinstance(tmdb_id, int):
        return None
    await _log(ctx, "info", f"Matched {series.title!r} to TMDB {tmdb_id} ({best_score:.0f})")
    return tmdb_id


# --- Series fields ---------------------------------------------------------------------------


def _pick_translation_text(
    translations: Sequence[dict[str, Any]],
    field: str,
    preferred: Sequence[str],
) -> tuple[str, str] | None:
    """The first of `preferred` TMDB has text for, as `(text, language tag)`.

    The tag comes back with the text because the caller records provenance: a synopsis that
    fell through to English is stored saying so, rather than claiming to be in the household's
    language.
    """
    by_tag: dict[str, str] = {}
    for entry in translations:
        data = entry.get("data") or {}
        value = (data.get(field) or "").strip()
        if not value:
            continue
        tag = _lang_tag(entry.get("iso_639_1"), entry.get("iso_3166_1"))
        by_tag.setdefault(tag.lower(), value)
        by_tag.setdefault((entry.get("iso_639_1") or "").lower(), value)
    for want in preferred:
        hit = by_tag.get(want.lower())
        if hit:
            return hit, want
    return None


def _content_rating(details: Mapping[str, Any], language: str) -> str | None:
    results = (details.get("content_ratings") or {}).get("results") or []
    region = language.split("-")[-1].upper() if "-" in language else ""
    by_region = {
        (item.get("iso_3166_1") or "").upper(): (item.get("rating") or "").strip()
        for item in results
        if isinstance(item, dict) and (item.get("rating") or "").strip()
    }
    for want in (region, "US", "GB"):
        if want and by_region.get(want):
            return by_region[want][:32]
    return next(iter(by_region.values()), None)


async def _episodes_with_files(session: AsyncSession, series_id: str) -> int:
    """How many episodes of this series actually have a file on the mount."""
    stmt = (
        select(func.count(func.distinct(Episode.id)))
        .select_from(Episode)
        .join(MediaFile, MediaFile.episode_id == Episode.id)
        .where(Episode.series_id == series_id)
    )
    return int((await session.execute(stmt)).scalar_one())


def _apply_series_fields(
    series: Series,
    details: Mapping[str, Any],
    translations: Sequence[dict[str, Any]],
    language: str,
    *,
    episodes_on_disk: int = 0,
) -> None:
    overview = (details.get("overview") or "").strip()
    overview_lang = language
    if not overview:
        fallback = _pick_translation_text(translations, "overview", [language, "en", "en-US"])
        if fallback is not None:
            overview, overview_lang = fallback
    if overview:
        series.overview = overview
        # Written with the text, never separately: a row whose overview and provenance
        # disagree is worse than one with no provenance at all.
        series.overview_source = OverviewSource.TMDB
        tmdb_id = details.get("id")
        series.overview_source_url = (
            f"{TMDB_WEB_BASE}/tv/{tmdb_id}" if isinstance(tmdb_id, int) else None
        )
        series.overview_source_lang = overview_lang or None

    year = _year_of(details.get("first_air_date"))
    if year:
        series.year = year

    status = _STATUS_MAP.get((details.get("status") or "").strip().lower())
    if status:
        series.status = status

    genres = [g.get("name") for g in details.get("genres") or [] if (g or {}).get("name")]
    if genres:
        # Normalized on the way in, like every other writer of this column. TMDB's own names
        # are already well-cased, so this is a no-op for them — it is here so that a refresh
        # cannot reintroduce a spelling the editor would have cleaned up.
        series.genres = normalize_genres(str(name) for name in genres)

    networks = [n.get("name") for n in details.get("networks") or [] if (n or {}).get("name")]
    if networks:
        series.network = (canonical_network(str(networks[0])) or "")[:200] or None

    original_title = (details.get("original_name") or "").strip()
    if original_title:
        series.original_title = original_title[:500]

    original_language = (details.get("original_language") or "").strip()
    if original_language:
        series.original_language = original_language[:16]

    # TMDB's episode count, but only while it can still be read as a total.
    #
    # Vertical short drama reaches TMDB as a *stub*: someone creates the show and one episode
    # and never returns. This household's catalogue holds the evidence — an entry declaring 1
    # episode against 66 files on the NAS, another declaring 14 against 62. Written through,
    # that number turned the library screen into "66 of 1 available", which is not a scan that
    # has not finished; it is the application repeating a claim it can see is false.
    #
    # So a declared total below what is already on disk is discarded rather than stored. The
    # file count is the one of the two numbers that is a fact.
    declared = details.get("number_of_episodes")
    if isinstance(declared, int) and declared > 0 and declared >= episodes_on_disk:
        series.declared_episode_count = declared

    imdb_id = ((details.get("external_ids") or {}).get("imdb_id") or "").strip()
    if imdb_id:
        series.imdb_id = imdb_id[:32]

    rating = canonical_content_rating(_content_rating(details, language))
    if rating:
        series.content_rating = rating

    # The TMDB source path is stored as-is; `image_cache` swaps it for the local path once
    # the bytes are on disk, and a value that is already local is left alone.
    for attr, key in (("poster_path", "poster_path"), ("backdrop_path", "backdrop_path")):
        remote = (details.get(key) or "").strip()
        if remote and not is_cached_path(getattr(series, attr)):
            setattr(series, attr, remote)


def _apply_episode_fields(episode: Episode, entry: Mapping[str, Any]) -> None:
    title = (entry.get("name") or "").strip()
    if title:
        episode.title = title[:500]

    overview = (entry.get("overview") or "").strip()
    if overview:
        episode.overview = overview

    air_date = _date_of(entry.get("air_date"))
    if air_date:
        episode.air_date = air_date

    runtime = entry.get("runtime")
    if isinstance(runtime, int) and runtime > 0:
        episode.runtime_seconds = runtime * 60

    tmdb_id = entry.get("id")
    if isinstance(tmdb_id, int):
        episode.tmdb_id = tmdb_id

    still = (entry.get("still_path") or "").strip()
    if still and not is_cached_path(episode.still_path):
        episode.still_path = still


# --- Sync steps ------------------------------------------------------------------------------


async def _sync_series_titles(
    session: AsyncSession,
    series: Series,
    details: Mapping[str, Any],
    alt_titles: Sequence[dict[str, Any]],
    translations: Sequence[dict[str, Any]],
    language: str,
) -> int:
    existing = (
        await session.execute(
            select(SeriesTitle.language, SeriesTitle.title, SeriesTitle.normalized).where(
                SeriesTitle.series_id == series.id
            )
        )
    ).all()
    collector = _TitleCollector((row[0], row[1], row[2]) for row in existing)

    # Order matters: the first row to claim a normalized form keeps it, so the most
    # authoritative spellings come first.
    ordered: list[tuple[str, str, str]] = [("", series.title, TitleKind.OFFICIAL)]
    name = (details.get("name") or "").strip()
    if name:
        ordered.append((language, name, TitleKind.OFFICIAL))
    original = (details.get("original_name") or "").strip()
    if original:
        ordered.append((details.get("original_language") or "", original, TitleKind.NATIVE))
    for alt in alt_titles:
        # `/alternative_titles` only reports a country (`iso_3166_1`), never a language, and
        # §4 types this column as BCP-47 or "". A bare region is neither, so these rows go in
        # language-neutral rather than claiming a language TMDB did not give us.
        ordered.append(("", (alt.get("title") or "").strip(), TitleKind.ALTERNATE))
    for entry in translations:
        data = entry.get("data") or {}
        ordered.append(
            (
                _lang_tag(entry.get("iso_639_1"), entry.get("iso_3166_1")),
                (data.get("name") or "").strip(),
                TitleKind.OFFICIAL,
            )
        )

    variants = await _variants_for(title for _, title, _ in ordered)
    for lang, title, kind in ordered:
        collector.add_with_variants(lang, title, kind, TitleSource.TMDB, variants)

    for row in collector.rows:
        session.add(SeriesTitle(series_id=series.id, **row))
    return len(collector.rows)


async def _sync_series_translations(
    session: AsyncSession,
    series: Series,
    translations: Sequence[dict[str, Any]],
) -> None:
    existing = {
        row.language: row
        for row in (
            await session.execute(
                select(SeriesTranslation).where(SeriesTranslation.series_id == series.id)
            )
        ).scalars()
    }
    for entry in translations:
        tag = _lang_tag(entry.get("iso_639_1"), entry.get("iso_3166_1"))
        data = entry.get("data") or {}
        overview = (data.get("overview") or "").strip() or None
        tagline = (data.get("tagline") or "").strip() or None
        if not tag or (overview is None and tagline is None):
            continue
        row = existing.get(tag)
        if row is None:
            row = SeriesTranslation(
                series_id=series.id, language=tag, overview=overview, tagline=tagline
            )
            session.add(row)
            existing[tag] = row
        else:
            row.overview = overview or row.overview
            row.tagline = tagline or row.tagline


def _seasons_to_sync(details: Mapping[str, Any], has_specials: bool) -> list[int]:
    numbers: list[int] = []
    for season in details.get("seasons") or []:
        number = (season or {}).get("season_number")
        if not isinstance(number, int) or number in numbers:
            continue
        # Season 0 is specials; only pull it when the library actually holds specials,
        # otherwise every series grows a tail of unavailable episodes.
        if number == 0 and not has_specials:
            continue
        numbers.append(number)
    return sorted(numbers)


async def _sync_episodes(
    session: AsyncSession,
    client: TMDBClient,
    series: Series,
    tmdb_id: int,
    details: Mapping[str, Any],
    *,
    ctx: ProgressCtx | None,
) -> None:
    episodes = list(
        (
            await session.execute(select(Episode).where(Episode.series_id == series.id))
        ).scalars()
    )
    by_number = {(e.season_number, e.episode_number): e for e in episodes}

    existing_titles: dict[str, set[tuple[str, str, str]]] = {}
    if episodes:
        rows = (
            await session.execute(
                select(
                    EpisodeTitle.episode_id,
                    EpisodeTitle.language,
                    EpisodeTitle.title,
                    EpisodeTitle.normalized,
                ).where(EpisodeTitle.episode_id.in_([e.id for e in episodes]))
            )
        ).all()
        for episode_id, language, title, normalized in rows:
            existing_titles.setdefault(episode_id, set()).add((language, title, normalized))

    seasons = _seasons_to_sync(details, any(key[0] == 0 for key in by_number))
    if not seasons:
        return

    # A second pass in the original language is what gives CJK shows their native episode
    # titles — and therefore their romanizations — without a request per episode.
    native = (details.get("original_language") or "").strip()
    alt_language = native or None
    if native and client.language.lower().startswith(native.lower()):
        alt_language = None

    created = 0
    for index, season_number in enumerate(seasons):
        if ctx is not None and ctx.cancelled():
            await _log(ctx, "info", "Metadata refresh cancelled")
            break

        payload = await client.get_season(tmdb_id, season_number)
        if payload is None:
            continue
        entries = [e for e in payload.get("episodes") or [] if isinstance(e, dict)]

        alt_names: dict[int, str] = {}
        if alt_language:
            alt_payload = await client.get_season(tmdb_id, season_number, language=alt_language)
            for entry in (alt_payload or {}).get("episodes") or []:
                number = (entry or {}).get("episode_number")
                name = (entry.get("name") or "").strip()
                if isinstance(number, int) and name:
                    alt_names[number] = name

        variants = await _variants_for(
            [(e.get("name") or "").strip() for e in entries] + list(alt_names.values())
        )

        for entry in entries:
            number = entry.get("episode_number")
            if not isinstance(number, int):
                continue
            episode = by_number.get((season_number, number))
            if episode is None:
                episode = Episode(
                    id=new_id(),
                    series_id=series.id,
                    season_number=season_number,
                    episode_number=number,
                )
                session.add(episode)
                by_number[(season_number, number)] = episode
                created += 1
            if not episode.metadata_locked:
                _apply_episode_fields(episode, entry)

            collector = _TitleCollector(existing_titles.get(episode.id, set()))
            collector.add_with_variants(
                client.language, entry.get("name"), TitleKind.OFFICIAL, TitleSource.TMDB, variants
            )
            if alt_language and number in alt_names:
                collector.add_with_variants(
                    alt_language, alt_names[number], TitleKind.NATIVE, TitleSource.TMDB, variants
                )
            for row in collector.rows:
                session.add(EpisodeTitle(episode_id=episode.id, **row))

        await session.flush()
        await _emit(
            ctx,
            45 + 45 * (index + 1) / len(seasons),
            f"Season {season_number}: {len(entries)} episodes",
        )

    if created:
        await _log(ctx, "info", f"Added {created} episode(s) from TMDB")


# --- Entry point -----------------------------------------------------------------------------


async def refresh_series_metadata(
    session: AsyncSession,
    series_id: str,
    *,
    tmdb_id: int | None = None,
    ctx: ProgressCtx | None = None,
) -> None:
    series = await session.get(Series, series_id)
    if series is None:
        raise NotFound("Series", "SERIES_NOT_FOUND")

    client = await try_tmdb_client(session)
    if client is None:
        await _log(ctx, "warning", "TMDB is not configured; skipping metadata refresh")
        return

    resolved = tmdb_id or series.tmdb_id
    if resolved is None:
        if series.metadata_locked:
            # Locked *and* holding no id is what `POST /series/{id}/unlink` leaves behind, and
            # it means an admin looked at a wrong match and said no. Fuzzy-matching the title
            # again is how that wrong match was made in the first place, so the nightly
            # refresh would hand the series straight back to the entry it was unlinked from —
            # and the admin would have to unlink it again every night. An explicit id still
            # binds below: `POST /match` clears the lock and names the id it wants.
            await _log(
                ctx, "info", "Series was unlinked from TMDB by an admin; not re-matching"
            )
            return
        resolved = await _match_tmdb_id(client, series, ctx=ctx)
        if resolved is None:
            return

    await _emit(ctx, 15, f"Fetching TMDB {resolved}")
    details = await client.get_tv(resolved)
    if not details:
        await _log(ctx, "warning", f"TMDB {resolved} returned no details")
        return
    alt_titles = await client.get_tv_alternative_titles(resolved)
    translations = await client.get_tv_translations(resolved)

    # An explicit id is an operator decision (the match dialog), so the binding is written
    # even on a locked series — the lock protects edited *content*, not the identity.
    if tmdb_id is not None or series.tmdb_id is None:
        series.tmdb_id = resolved

    if series.metadata_locked:
        await _log(ctx, "info", "Series metadata is locked; only titles and episodes updated")
    else:
        _apply_series_fields(
            series,
            details,
            translations,
            client.language,
            # Read here rather than inside, which is sync: touching a lazy relationship from
            # there raises MissingGreenlet. See `_apply_series_fields` for what it guards.
            episodes_on_disk=await _episodes_with_files(session, series.id),
        )

    await _emit(ctx, 30, "Writing titles")
    added = await _sync_series_titles(
        session, series, details, alt_titles, translations, client.language
    )
    await _sync_series_translations(session, series, translations)
    await session.flush()
    await _log(ctx, "debug", f"{added} new title row(s)")

    await _emit(ctx, 45, "Syncing episodes")
    await _sync_episodes(session, client, series, resolved, details, ctx=ctx)

    await _emit(ctx, 92, "Reindexing search")
    await reindex_series(session, series.id)

    # Imported here to keep `app.workers.queue` free to import the handlers that call us.
    from app.workers.queue import enqueue

    await enqueue(
        session,
        JobType.IMAGE_CACHE,
        {"seriesId": series.id},
        series_id=series.id,
    )
    await session.commit()
    await _emit(ctx, 100, f"Metadata updated for {series.title}")


async def search_tmdb(
    session: AsyncSession,
    query: str,
    year: int | None = None,
) -> list[dict[str, Any]]:
    """Thin passthrough for the admin match dialog."""
    client = await try_tmdb_client(session)
    if client is None:
        raise AppError("TMDB_NOT_CONFIGURED", "TMDB is not configured", 503)
    return await client.search_tv(query, year=year)
