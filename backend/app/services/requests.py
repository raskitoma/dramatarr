"""The wishlist: searching for something to ask for, asking, and answering.

This module is the whole of the requests feature's logic, and it is bounded by one rule stated
once here rather than repeated at every function: **nothing in it fetches, downloads or
schedules anything.** A request is a note. The search below reads catalogues the app is already
configured to read — TMDB's API and the household's own SearXNG instance — so that the note
carries a title, a picture and a link instead of a half-remembered phrase; then it stops. The
admin reads the note and acts in the world.

Two things follow from that and are worth naming before the code:

*The search merges two sources that disagree about what a result is.* TMDB answers with
records: a title, a year, a poster, an id. SearXNG answers with pages: a URL, a headline, an
extract. Both are useful and neither is sufficient — TMDB routinely does not have the household's
short-form Asian drama at all, which is the reason `app.services.discovery` exists, and a bare
web link is a poor thing to put on a card. So TMDB's rows go first, web results that name a TMDB
id are promoted into records by looking that id up, and what is left stays a link with its
domain printed on it. A member choosing between five cards can always see which source each one
came from.

*Identity is a key, not a fuzzy match.* Every candidate and every stored row carries a
`dedupe_key` derived from the strongest identifier it has. That is what lets the search say "you
already asked for this" truthfully, what makes the unique constraint meaningful, and what lets
the admin see that three people asked for the same drama under three different spellings.
Falling back to a normalized title is the weakest tier and it is honest about being weak: two
unrelated shows with the same normalized name in the same year would collide, which in a
household of six people is a price worth paying for never showing the same card twice.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, Conflict, NotFound
from app.models import RequestStatus, Series, SeriesRequest, SeriesTitle, User
from app.schemas.library import DiscoveredIds
from app.schemas.requests import (
    AdminSeriesRequestOut,
    RequestCandidate,
    RequestCounts,
    RequestCreateIn,
    RequestPatchIn,
    RequestRequester,
    RequestSearchResults,
    RequestSeriesRef,
    SeriesRequestOut,
)
from app.services import discovery as discovery_service
from app.services.images import (
    SearxngConfig,
    _configured_searxng,
    avatar_url,
    cache_request_poster,
    get_image_search,
    is_request_poster,
)
from app.services.renamer import sanitize_filename
from app.services.scanner.scan import safe_media_path
from app.services.search.normalize import normalize
from app.services.settings import SettingsService
from app.services.tmdb import IMAGE_BASE, TMDBClient, try_tmdb_client
from app.services.vocab import (
    Provider,
    configured_providers,
    provider_domains,
    provider_for_domain,
)
from app.utils.time import utcnow

log = logging.getLogger(__name__)

#: Poster rendition for a request card. Small — these are thumbnails in a grid, and the file
#: is fetched by every member's browser rather than cached here.
POSTER_SIZE = "w342"

#: The only host a request's `poster_url` may point at.
#:
#: A member's chosen poster is rendered in an `<img>` **in the admin's browser**, which makes
#: an arbitrary URL in that column a beacon: whoever controls it learns when an admin opened
#: the requests page and from which address. TMDB's image CDN is already loaded on every other
#: screen of this app, so allowing it adds no exposure that does not already exist, and
#: allowing nothing else keeps the column from becoming a way to point somebody else's browser
#: anywhere. A URL that fails this is dropped, not rejected — the request is the point, the
#: picture is decoration.
_POSTER_HOST = (urlsplit(IMAGE_BASE).hostname or "").lower()

#: How many web results carrying a TMDB id are promoted into full records. Each promotion is
#: one TMDB request, made while a member waits, so this is a latency budget rather than a
#: completeness one — the rows past it still appear, as links.
_MAX_TMDB_ENRICH = 6

#: Upper bound on a member's own list. A household wishlist that reaches this has a different
#: problem than pagination would solve.
MAX_OWN_REQUESTS = 200

DEFAULT_SEARCH_LIMIT = 12
MAX_SEARCH_LIMIT = 30


# --- Identity ----------------------------------------------------------------------------------


def _clean_ids(ids: DiscoveredIds) -> dict[str, Any]:
    """The non-TMDB, non-IMDb identifiers, as the JSON blob the row stores.

    Empty keys are dropped rather than stored as null, so `external_ids` is either absent or
    meaningful and the admin UI can iterate it without filtering.
    """
    out: dict[str, Any] = {}
    if ids.mydramalist_id:
        out["mydramalist"] = ids.mydramalist_id
    if ids.dramaboxdb_id:
        out["dramaboxdb"] = ids.dramaboxdb_id
        if ids.dramaboxdb_slug:
            out["dramaboxdbSlug"] = ids.dramaboxdb_slug
    if ids.wikidata_id:
        out["wikidata"] = ids.wikidata_id
    if ids.wikipedia_title:
        out["wikipedia"] = ids.wikipedia_title
        if ids.wikipedia_lang:
            out["wikipediaLang"] = ids.wikipedia_lang
    return out


def dedupe_key(ids: DiscoveredIds, title: str, year: int | None = None) -> str:
    """The strongest identity this candidate has, as one comparable string.

    The tiers are ordered by how much they actually pin down a title. A TMDB id is the same
    show for everybody; a MyDramaList id is the same show for everybody who uses MyDramaList;
    a normalized title is a guess that is right almost always and cheap to be wrong about.
    Wikipedia sits below the drama databases deliberately — an article can cover a franchise
    rather than a season.
    """
    if ids.tmdb_id:
        # The kind is part of the key: `/movie/1396` and `/tv/1396` are different titles.
        return f"tmdb:{ids.tmdb_kind or 'tv'}:{ids.tmdb_id}"
    if ids.imdb_id:
        return f"imdb:{ids.imdb_id}"
    if ids.mydramalist_id:
        return f"mydramalist:{ids.mydramalist_id}"
    if ids.dramaboxdb_id:
        return f"dramaboxdb:{ids.dramaboxdb_id}"
    if ids.wikidata_id:
        return f"wikidata:{ids.wikidata_id}"
    if ids.wikipedia_title:
        return f"wikipedia:{ids.wikipedia_lang or ''}:{ids.wikipedia_title}"

    normalized = normalize(title) or (title or "").strip().lower()
    # The year joins the key only when there is one. Adding a placeholder would split "the
    # same title, asked for twice, once with a year" into two rows.
    return f"title:{normalized}:{year}" if year else f"title:{normalized}"


def _domain(url: str | None) -> str:
    """`https://www.themoviedb.org/tv/1396` → `themoviedb.org`. Derived, never accepted."""
    if not url:
        return ""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_http_url(value: str | None) -> bool:
    return bool(value) and str(value).startswith(("http://", "https://"))


def safe_poster_url(value: str | None) -> str | None:
    """The poster if it comes from somewhere this app vouches for, otherwise nothing.

    Exactly two sources qualify, and everything else is dropped:

    * TMDB's image CDN, which every other screen in the app already loads from, so allowing it
      adds no exposure that does not already exist.
    * A path this server serves itself (`/api/stream/images/requests/…`), written by
      `images.cache_request_poster` after the bytes went through the guarded fetch and were
      re-encoded by ffmpeg. Same-origin, so it also works for a member outside the house.

    Anything else would put a third party's URL in the *admin's* browser when they open the
    queue, which is a beacon: whoever controls it learns when the page was opened and from
    where. See `_POSTER_HOST`.
    """
    if is_request_poster(value):
        return str(value)
    if not _is_http_url(value):
        return None
    try:
        host = (urlsplit(str(value)).hostname or "").lower()
    except ValueError:
        return None
    return str(value) if host == _POSTER_HOST else None


# --- Poster lookup ------------------------------------------------------------------------------
#
# The reason this exists: for the drama this library is made of, TMDB has no entry, so a card
# assembled purely from web results has no picture at all — which is exactly what the household
# saw. The household's own image search does find these posters, and it is already configured
# for the artwork picker, so it is the natural source.
#
# It is a **separate endpoint, fetched per card**, rather than part of the search response. Two
# reasons, both about how the screen feels: an image search per candidate would make the whole
# result list wait on the slowest of a dozen network calls, and one engine having a bad moment
# would take out the entire list instead of one thumbnail.

#: Memo of query → served poster path (or None). Bounded and process-local; the durable cache
#: is the file on disk, this only saves asking SearXNG the same question twice in a session.
_POSTER_MEMO: dict[str, str | None] = {}
_POSTER_MEMO_MAX = 512


def _poster_query(title: str, year: int | None) -> str:
    """What actually goes to the image engine.

    The word "poster" is appended because without it an image search for a drama's title
    returns stills, cast photographs and thumbnails from the very sites this app does not deal
    with. It is the one term that biases the result toward artwork.
    """
    text = " ".join((title or "").split())
    if not text:
        return ""
    stamp = str(year) if year else ""
    parts = [text, stamp, "poster"] if stamp and stamp not in text.split() else [text, "poster"]
    return " ".join(part for part in parts if part)


async def find_poster(session: AsyncSession, title: str, year: int | None = None) -> str | None:
    """A poster for a title that has none, served from this origin. None when there is none.

    Every failure is None rather than an error. A missing picture is the ordinary case for
    this library, and it must never be the reason a member cannot ask for something.
    """
    query = _poster_query(title, year)
    if not query:
        return None
    if query in _POSTER_MEMO:
        return _POSTER_MEMO[query]

    result: str | None = None
    try:
        provider = await get_image_search(session)
        if provider is not None:
            hits = await provider.search(query, limit=4)
            for hit in hits:
                # `hit.url` is the full-size image on the open web; the guarded fetch inside
                # `cache_request_poster` is what makes downloading it safe, and it is the same
                # guard an admin-supplied artwork URL goes through.
                cached = await cache_request_poster(hit.url)
                if cached is not None:
                    result = cached
                    break
    except AppError as exc:
        log.info("poster lookup failed (%s) for %r", exc.code, query[:80])
    except Exception:  # pragma: no cover - a thumbnail must never break the screen
        log.warning("poster lookup failed unexpectedly for %r", query[:80], exc_info=True)

    if len(_POSTER_MEMO) >= _POSTER_MEMO_MAX:
        _POSTER_MEMO.clear()
    _POSTER_MEMO[query] = result
    return result


# --- Searching ---------------------------------------------------------------------------------


def _tmdb_row_facts(row: dict[str, Any]) -> tuple[str, int | None, str | None, str]:
    """`(title, year, poster_path, kind)` from a row of either TMDB catalogue.

    Film and series rows use different field names for the same three things — `title` /
    `name`, `release_date` / `first_air_date` — so every reader of a `/search/multi` or
    `/find` row needs this, and having it in one place is what stops a film row from silently
    losing its title.
    """
    kind = "movie" if row.get("_kind") == "movie" else "tv"
    if kind == "movie":
        title = str(row.get("title") or row.get("original_title") or "").strip()
        date = str(row.get("release_date") or "")
    else:
        title = str(row.get("name") or row.get("original_name") or "").strip()
        date = str(row.get("first_air_date") or "")
    year = int(date[:4]) if date[:4].isdigit() else None
    poster = row.get("poster_path")
    return title, year, (str(poster) if poster else None), kind


def _tmdb_candidate(row: dict[str, Any]) -> RequestCandidate | None:
    """One TMDB row as a card, or None when it carries no id or title to show."""
    tmdb_id = row.get("id")
    if not isinstance(tmdb_id, int):
        return None
    title, year, poster, kind = _tmdb_row_facts(row)
    if not title:
        return None

    ids = DiscoveredIds(tmdb_id=tmdb_id, tmdb_kind=kind)  # type: ignore[arg-type]
    return RequestCandidate(
        key=dedupe_key(ids, title, year),
        title=title,
        year=year,
        poster_url=f"{IMAGE_BASE}/{POSTER_SIZE}{poster}" if poster else None,
        # The canonical page for the record, built rather than crawled. It is what the admin
        # opens, and TMDB is the one source whose URL shape this app already knows.
        source_url=f"https://www.themoviedb.org/{kind}/{tmdb_id}",
        provider="themoviedb.org",
        snippet=str(row.get("overview") or "").strip()[:400],
        ids=ids,
    )


def _discovery_candidate(
    result: Any, platforms: Sequence[Provider] = ()
) -> RequestCandidate:
    """One web result as a card. Keeps its own words: the headline is all it has.

    `provider` prefers the *platform's* name over the bare host when the domain is one the
    household configured, so a card reads `ReelShort` rather than `reelshort.com`. That is the
    one piece of provenance a member has to judge two similar hits by.
    """
    ids = result.ids
    title = (result.title or "").strip() or result.source_domain
    platform = provider_for_domain(result.source_domain, platforms)
    return RequestCandidate(
        key=dedupe_key(ids, title),
        title=title,
        year=None,
        poster_url=None,
        source_url=result.url,
        provider=platform or result.source_domain,
        snippet=result.snippet,
        ids=ids,
    )


async def _tmdb_search(client: TMDBClient | None, query: str) -> list[RequestCandidate]:
    """TMDB's own answer, or nothing at all if it is unconfigured or unwell.

    Every failure here is swallowed, which is deliberate and is not the usual rule: this is one
    of two sources, a member is waiting, and a search that returns SearXNG's results is a
    better outcome than one that returns a 503 because a third party is having a bad minute.
    The failure is logged so it is still visible to whoever runs the box.

    Takes a resolved client rather than a session — see `search` for why that matters.
    """
    if client is None:
        return []
    try:
        # `search_multi`, not `search_tv`: vertical short-form drama is catalogued as a film
        # on TMDB at least as often as it is catalogued as a series, and asking only the TV
        # index is why a lot of this household's library came back empty.
        rows = await client.search_multi(query)
    except AppError as exc:
        log.info("request search: TMDB unavailable (%s)", exc.code)
        return []
    return [card for row in rows[:12] if (card := _tmdb_candidate(row)) is not None]


async def _discovery_search(
    config: SearxngConfig | None,
    query: str,
    limit: int,
    *,
    domains: Sequence[str] = (),
    platforms: Sequence[Provider] = (),
) -> list[RequestCandidate]:
    """The household's SearXNG instance, against an already-resolved configuration.

    When `domains` is given the query is narrowed to the platforms and **stays narrowed**. It
    used to widen itself when the narrow search found nothing, which sounded protective and was
    not: a household whose platform domains are thinly indexed hits that path constantly, so
    the mode advertised as "short drama only" quietly answered with a generic web search. An
    empty answer is the honest one; the member's own toggle is how the search widens.

    The narrowing is done **here, on the hosts that came back**, not by trusting the `site:`
    clause. This household's instance ignores that operator outright — asked for
    `site:dramabox.com` it returns Dailymotion and YouTube — so a mode that relied on it was
    labelling unfiltered results as narrowed. Same correction the editor's search already
    carries; this path had been left behind.
    """
    if config is None:
        return []

    async def ask(text: str) -> list[RequestCandidate]:
        found = await discovery_service.discover_with(config, text, limit=limit)
        return [_discovery_candidate(row, platforms) for row in found.results]

    try:
        found = await ask(discovery_service.scoped_query(query, domains) if domains else query)
        if not domains:
            return found
        return [
            card
            for card in found
            if card.source_url and discovery_service._on_a_platform(card.source_url, domains)
        ]
    except AppError as exc:
        # A configured instance that is unwell is downgraded to "no results" for the same
        # reason TMDB's outage is: the other source may still have the answer, and the member
        # is owed a list rather than a red box. Logged so the operator can still see it.
        log.info("request search: SearXNG failed (%s)", exc.code)
        return []


async def _searxng_or_none(session: AsyncSession) -> SearxngConfig | None:
    """The configured instance, or None when this household has not set one up."""
    try:
        return await _configured_searxng(session)
    except AppError as exc:
        if exc.code == "SEARCH_NOT_CONFIGURED":
            return None
        raise


def _apply_details(card: RequestCandidate, row: dict[str, Any]) -> None:
    """Fill a card's blanks from a TMDB record. Never overwrites something already better."""
    title, year, poster, _kind = _tmdb_row_facts(row)
    if poster:
        card.poster_url = f"{IMAGE_BASE}/{POSTER_SIZE}{poster}"
    if title:
        card.title = title
    if year is not None:
        card.year = year
    overview = str(row.get("overview") or "").strip()
    if overview and not card.snippet:
        card.snippet = overview[:400]


async def _enrich_from_tmdb(
    client: TMDBClient | None, candidates: Sequence[RequestCandidate]
) -> None:
    """Turn web results that name a TMDB **or IMDb** id into records, in place.

    A `themoviedb.org` or `imdb.com` link found by a web search is very often the same title
    TMDB's own search failed to return — the query matched the show's Wikipedia or IMDb page
    rather than its TMDB title. Resolving the id gives the card the poster and the year it
    should have had.

    IMDb ids go through TMDB's `/find`, which is the only bridge between the two catalogues
    available here: IMDb publishes no free API, so an IMDb id is worth exactly as much as what
    TMDB can be asked about it — which turns out to be a lot, because most things with an IMDb
    id have a TMDB record too.

    Bounded by `_MAX_TMDB_ENRICH` and entirely best-effort: a lookup that fails leaves the row
    as it was, a link with a domain. The dedupe key is **not** recomputed — it is derived from
    the id, which has not changed, and leaving it alone is what keeps a card's identity stable
    across enrichment.
    """
    if client is None:
        return
    targets = [
        card
        for card in candidates
        if card.poster_url is None and (card.ids.tmdb_id or card.ids.imdb_id)
    ][:_MAX_TMDB_ENRICH]
    if not targets:
        return

    async def fetch(card: RequestCandidate) -> None:
        try:
            if card.ids.tmdb_id:
                kind = card.ids.tmdb_kind or "tv"
                details = (
                    await client.get_movie(int(card.ids.tmdb_id))
                    if kind == "movie"
                    else await client.get_tv(int(card.ids.tmdb_id))
                )
                if details:
                    _apply_details(card, {**details, "_kind": kind})
                return

            # IMDb only: ask TMDB what it knows about that id, keeping film results too —
            # a short drama is filed as a film on IMDb about as often as as a series.
            found = await client.find_any_by_external_id(str(card.ids.imdb_id))
            first = found[0] if found else None
            if first is not None:
                _apply_details(card, first)
                # Now that it resolves to a TMDB record, record the id so the admin's later
                # match is one click rather than another search.
                if isinstance(first.get("id"), int):
                    card.ids.tmdb_id = int(first["id"])
                    card.ids.tmdb_kind = "movie" if first.get("_kind") == "movie" else "tv"
        except AppError:
            return  # 404, rate limit, outage — the card stands as it is

    await asyncio.gather(*(fetch(card) for card in targets))


def _merge(groups: Iterable[Sequence[RequestCandidate]]) -> list[RequestCandidate]:
    """Concatenate the sources, first occurrence of each key wins.

    TMDB's rows are passed first, so when both sources found the same title the card keeps the
    poster and the year rather than the web headline.
    """
    seen: set[str] = set()
    out: list[RequestCandidate] = []
    for group in groups:
        for card in group:
            if card.key in seen:
                continue
            seen.add(card.key)
            out.append(card)
    return out


async def _mark_in_library(session: AsyncSession, candidates: Sequence[RequestCandidate]) -> None:
    """Flag the cards the household already owns.

    Two ways in, because the two sources identify things differently: a TMDB id matches
    `series.tmdb_id` exactly, and everything else is matched on the normalized title against
    `series_titles`, which is the same index the library's own multilingual search uses — so a
    show filed under its Korean title is still recognized when somebody asks for it in
    romanization.
    """
    if not candidates:
        return

    tmdb_ids = {int(c.ids.tmdb_id) for c in candidates if c.ids.tmdb_id}
    normalized = {n: c.title for c in candidates if (n := normalize(c.title))}

    by_tmdb: dict[int, tuple[str, str]] = {}
    if tmdb_ids:
        rows = await session.execute(
            select(Series.tmdb_id, Series.id, Series.slug).where(Series.tmdb_id.in_(tmdb_ids))
        )
        by_tmdb = {int(t): (sid, slug) for t, sid, slug in rows if t is not None}

    by_title: dict[str, tuple[str, str]] = {}
    if normalized:
        rows = await session.execute(
            select(SeriesTitle.normalized, Series.id, Series.slug)
            .join(Series, Series.id == SeriesTitle.series_id)
            .where(SeriesTitle.normalized.in_(list(normalized)))
        )
        for norm, sid, slug in rows:
            by_title.setdefault(str(norm), (sid, slug))

    for card in candidates:
        hit = None
        if card.ids.tmdb_id:
            hit = by_tmdb.get(int(card.ids.tmdb_id))
        if hit is None:
            hit = by_title.get(normalize(card.title))
        if hit is not None:
            card.in_library = True
            card.in_library_slug = hit[1]


async def _mark_requested(
    session: AsyncSession, user_id: str, candidates: Sequence[RequestCandidate]
) -> None:
    """Flag the cards this member has already asked for, with the answer they got."""
    if not candidates:
        return
    keys = [card.key for card in candidates]
    rows = await session.execute(
        select(SeriesRequest.dedupe_key, SeriesRequest.status).where(
            SeriesRequest.user_id == user_id, SeriesRequest.dedupe_key.in_(keys)
        )
    )
    known = {str(key): str(status) for key, status in rows}
    for card in candidates:
        status = known.get(card.key)
        if status is not None:
            card.requested = True
            card.request_status = status  # type: ignore[assignment]


async def search(
    session: AsyncSession,
    user: User,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    short_drama: bool = False,
) -> RequestSearchResults:
    """What a member sees when they type a title into the request box.

    The two providers are asked **at once**, and the shape of this function is what makes that
    safe. An `AsyncSession` cannot be used by two coroutines at the same time, so both
    configurations are resolved first, sequentially, off the session; only the network calls
    are raced; and every database read afterwards is sequential again. Gathering the two
    provider calls with the session still in hand would interleave two sets of queries on one
    connection, which fails somewhere else entirely and looks like anything but its cause.
    """
    text = " ".join((query or "").split())
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    if not text:
        return RequestSearchResults(query="")

    # --- Sequential: everything that touches the session ---
    tmdb = await try_tmdb_client(session)
    searxng = await _searxng_or_none(session)
    platforms = await configured_providers(SettingsService(session))
    # The reference databases ride along with the platforms. A platform page says the show
    # exists; IMDb says what *year* it is and carries an id that turns the card into a record —
    # which is the difference between a wishlist entry and something the admin can act on.
    domains = provider_domains(platforms, with_metadata_sites=True) if short_drama else ()

    # In short-drama mode TMDB is not asked at all. That is the fix for the complaint this
    # mode exists for: TMDB's catalogue contains only conventional television, so for a
    # vertical-drama query the best it can offer is the ordinary series that most resembles
    # it — which then crowds out the results that are actually the show.
    tmdb_cards, web_cards = await asyncio.gather(
        _tmdb_search(None if short_drama else tmdb, text),
        _discovery_search(searxng, text, limit, domains=domains, platforms=platforms),
    )

    providers: list[str] = []
    if tmdb is not None and not short_drama:
        providers.append("tmdb")
    if searxng is not None:
        providers.append("searxng")

    merged = _merge((tmdb_cards, web_cards))[:limit]
    await _enrich_from_tmdb(tmdb, merged)
    # Re-merge after enrichment: a promoted web row can now carry the same title as a TMDB
    # row it did not match before. Keys are id-derived and unchanged, so this only ever
    # collapses rows that were always the same record.
    merged = _merge((merged,))

    # --- Sequential again ---
    await _mark_in_library(session, merged)
    await _mark_requested(session, user.id, merged)

    return RequestSearchResults(
        query=text,
        providers=providers,
        # A member can always ask by name, so "nothing is configured" is reported without
        # making the page look broken — the free-text path below needs no provider at all.
        configured=bool(providers),
        results=merged,
    )


# --- Reading -----------------------------------------------------------------------------------


def _ids_of(row: SeriesRequest) -> DiscoveredIds:
    """Reassemble the identifiers from the two columns and the blob they live in."""
    extra = row.external_ids or {}
    return DiscoveredIds(
        tmdb_id=row.tmdb_id,
        tmdb_kind="tv" if row.tmdb_id else None,
        imdb_id=row.imdb_id,
        mydramalist_id=extra.get("mydramalist"),
        dramaboxdb_id=extra.get("dramaboxdb"),
        dramaboxdb_slug=extra.get("dramaboxdbSlug"),
        wikidata_id=extra.get("wikidata"),
        wikipedia_title=extra.get("wikipedia"),
        wikipedia_lang=extra.get("wikipediaLang"),
    )


def _series_ref(row: SeriesRequest) -> RequestSeriesRef | None:
    series = row.series
    if series is None:
        return None
    return RequestSeriesRef(id=series.id, slug=series.slug, title=series.title)


def request_out(row: SeriesRequest, *, already_existed: bool = False) -> SeriesRequestOut:
    return SeriesRequestOut(
        id=row.id,
        title=row.title,
        year=row.year,
        poster_url=row.poster_url,
        source_url=row.source_url,
        provider=row.source_domain,
        ids=_ids_of(row),
        note=row.note,
        status=row.status,  # type: ignore[arg-type]
        admin_note=row.admin_note,
        series=_series_ref(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
        resolved_at=row.resolved_at,
        already_existed=already_existed,
    )


def admin_request_out(row: SeriesRequest, *, also_requested_by: int = 0) -> AdminSeriesRequestOut:
    base = request_out(row)
    requester = row.user
    return AdminSeriesRequestOut(
        **base.model_dump(),
        requester=RequestRequester(
            id=requester.id,
            display_name=requester.display_name,
            email=requester.email,
            avatar_url=avatar_url(requester.id),
        ),
        resolved_by_name=row.resolved_by.display_name if row.resolved_by else None,
        also_requested_by=also_requested_by,
    )


async def list_for_user(session: AsyncSession, user: User) -> list[SeriesRequestOut]:
    """A member's own list, newest first. Open requests float to the top of it."""
    rows = (
        await session.execute(
            select(SeriesRequest)
            .where(SeriesRequest.user_id == user.id)
            .order_by(SeriesRequest.created_at.desc())
            .limit(MAX_OWN_REQUESTS)
        )
    ).scalars()
    return [request_out(row) for row in rows]


async def get_or_404(session: AsyncSession, request_id: str) -> SeriesRequest:
    row = await session.get(SeriesRequest, request_id)
    if row is None:
        raise NotFound("Request", "REQUEST_NOT_FOUND")
    return row


# --- Asking ------------------------------------------------------------------------------------


async def _already_in_library(session: AsyncSession, payload: RequestCreateIn) -> Series | None:
    """The series the household already has, when this request names one.

    Same two routes as the search's badge, run again at write time rather than trusted from
    the client: the search result a member is acting on may be minutes old, and in that window
    somebody may have added the show.
    """
    if payload.ids.tmdb_id:
        found = (
            await session.execute(select(Series).where(Series.tmdb_id == payload.ids.tmdb_id))
        ).scalars().first()
        if found is not None:
            return found

    normalized = normalize(payload.title)
    if not normalized:
        return None
    return (
        (
            await session.execute(
                select(Series)
                .join(SeriesTitle, SeriesTitle.series_id == Series.id)
                .where(
                    or_(
                        SeriesTitle.normalized == normalized,
                        Series.sort_title == normalized,
                    )
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def create(
    session: AsyncSession, user: User, payload: RequestCreateIn
) -> SeriesRequestOut:
    """Write the note, or hand back the one that is already there.

    Three outcomes, and only one of them is a new row:

    * The library already has it → `REQUEST_ALREADY_IN_LIBRARY`, carrying the slug. Being told
      "it is here, go watch it" is the answer the member actually wanted.
    * This member already asked → their existing row, with `alreadyExisted`. Including when the
      answer was *no*: burying a decline under a fresh identical request helps nobody.
    * Otherwise → a new pending row.
    """
    existing_series = await _already_in_library(session, payload)
    if existing_series is not None:
        raise Conflict(
            "REQUEST_ALREADY_IN_LIBRARY",
            f"{payload.title} is already in the library",
        )

    key = dedupe_key(payload.ids, payload.title, payload.year)
    duplicate = (
        await session.execute(
            select(SeriesRequest).where(
                SeriesRequest.user_id == user.id, SeriesRequest.dedupe_key == key
            )
        )
    ).scalars().first()
    if duplicate is not None:
        return request_out(duplicate, already_existed=True)

    source_url = payload.source_url if _is_http_url(payload.source_url) else None
    row = SeriesRequest(
        user_id=user.id,
        title=payload.title,
        year=payload.year,
        poster_url=safe_poster_url(payload.poster_url),
        source_url=source_url,
        # Derived from the URL rather than taken from the client: this is the field an admin
        # reads to decide how much to trust the row, so it has to describe the link that is
        # actually on it.
        source_domain=_domain(source_url),
        tmdb_id=payload.ids.tmdb_id,
        imdb_id=payload.ids.imdb_id,
        external_ids=_clean_ids(payload.ids),
        dedupe_key=key,
        note=payload.note,
        status=RequestStatus.PENDING,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        # Lost a race with the member's own second click. The unique constraint did its job;
        # read the winner back and report it the same way the duplicate branch above does.
        await session.rollback()
        winner = (
            await session.execute(
                select(SeriesRequest).where(
                    SeriesRequest.user_id == user.id, SeriesRequest.dedupe_key == key
                )
            )
        ).scalars().first()
        if winner is None:
            raise
        return request_out(winner, already_existed=True)

    await session.commit()
    await session.refresh(row)
    return request_out(row)


async def delete_own(session: AsyncSession, user: User, request_id: str) -> None:
    """A member withdrawing their own request, in any state.

    Any state including declined: the list is theirs, and a member who wants to clear a "no"
    off their own screen should not have to ask an admin to do it. It does not un-decline
    anything — asking again writes a new pending row, which is the correct outcome when
    circumstances have changed.
    """
    row = await get_or_404(session, request_id)
    if row.user_id != user.id:
        raise NotFound("Request", "REQUEST_NOT_FOUND")
    await session.delete(row)
    await session.commit()


# --- Preparing the landing place ---------------------------------------------------------------
#
# The one place the wishlist touches the library mount, and it creates an empty directory —
# nothing more. It exists to remove a step the admin was doing by hand and getting wrong: a
# folder named however it happened to be typed produces a series the scanner reads under that
# name, which then has to be corrected in the editor and renamed on disk afterwards.
#
# Named canonically up front, the folder parses straight back into the title and year it was
# built from, so the scan that picks the files up already knows what they are. The round trip
# through `parse_series_folder` is asserted in the tests, because a name that does not survive
# it would defeat the entire point while looking perfectly reasonable in a listing.


def folder_name_for(title: str, year: int | None) -> str:
    """`Fruit Dimension: Orange Light Redemption` + 2025 → `Fruit Dimension Orange Light
    Redemption (2025)`.

    The same sanitizer the renamer uses, so a title cannot introduce a path separator, and the
    `(year)` suffix the scanner's folder parser reads. Without a year the name is the title
    alone, which parses back with `year=None` — correct rather than invented.
    """
    safe = sanitize_filename(title) or "Untitled"
    return f"{safe} ({year})" if year else safe


async def prepare_folder(session: AsyncSession, request_id: str) -> tuple[str, bool]:
    """Create the destination directory for a request. Returns `(relative path, created)`.

    Idempotent: an existing directory is reported rather than treated as an error, because the
    admin pressing this twice means "make sure it is there", not "make a second one".

    Only ever `mkdir`. It does not write, move or delete a file, and the path is resolved
    through the same traversal guard the streaming routes use — a crafted title cannot address
    anything outside `LIBRARY_ROOT`.
    """
    row = await get_or_404(session, request_id)
    name = folder_name_for(row.title, row.year)

    target = safe_media_path(name)
    existed = await asyncio.to_thread(target.exists)
    if not existed:
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    return name, not existed


# --- Answering ---------------------------------------------------------------------------------


async def _also_requested_counts(
    session: AsyncSession, rows: Sequence[SeriesRequest]
) -> dict[str, int]:
    """For each row's key, how many *other* members asked for the same thing.

    One grouped query for the whole page rather than one per row. The count excludes the row's
    own requester, so the number reads as "and N others".
    """
    keys = {row.dedupe_key for row in rows}
    if not keys:
        return {}
    found = await session.execute(
        select(SeriesRequest.dedupe_key, func.count(func.distinct(SeriesRequest.user_id)))
        .where(SeriesRequest.dedupe_key.in_(list(keys)))
        .group_by(SeriesRequest.dedupe_key)
    )
    return {str(key): max(0, int(count) - 1) for key, count in found}


async def list_all(
    session: AsyncSession,
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[AdminSeriesRequestOut], int]:
    """The admin's queue. Open requests first, then newest — the order they get worked in."""
    where = [SeriesRequest.status == status] if status else []

    total = int(
        await session.scalar(
            select(func.count()).select_from(SeriesRequest).where(*where)
        )
        or 0
    )
    rows = list(
        (
            await session.execute(
                select(SeriesRequest)
                .where(*where)
                .order_by(
                    # `pending` and `approved` sort before the settled two. A CASE rather than
                    # alphabetical luck: "approved" happens to sort first alphabetically and
                    # "declined" would land between the open ones.
                    SeriesRequest.status.notin_(
                        [RequestStatus.PENDING, RequestStatus.APPROVED]
                    ),
                    SeriesRequest.created_at.desc(),
                )
                .offset(max(0, (page - 1) * page_size))
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    counts = await _also_requested_counts(session, rows)
    return (
        [admin_request_out(row, also_requested_by=counts.get(row.dedupe_key, 0)) for row in rows],
        total,
    )


async def counts(session: AsyncSession) -> RequestCounts:
    rows = await session.execute(
        select(SeriesRequest.status, func.count()).group_by(SeriesRequest.status)
    )
    tally = {str(status): int(count) for status, count in rows}
    return RequestCounts(
        pending=tally.get(RequestStatus.PENDING, 0),
        approved=tally.get(RequestStatus.APPROVED, 0),
        fulfilled=tally.get(RequestStatus.FULFILLED, 0),
        declined=tally.get(RequestStatus.DECLINED, 0),
        total=sum(tally.values()),
    )


async def update(
    session: AsyncSession, admin: User, request_id: str, patch: RequestPatchIn
) -> AdminSeriesRequestOut:
    """The admin's answer.

    `resolved_at` and `resolved_by` are stamped when the status moves to a settled state and
    cleared when it moves back to an open one, so a request reopened after a mistaken decline
    does not keep claiming it was answered.
    """
    row = await get_or_404(session, request_id)

    if patch.series_id is not None:
        if patch.series_id == "":
            row.series_id = None
        else:
            series = await session.get(Series, patch.series_id)
            if series is None:
                raise NotFound("Series", "SERIES_NOT_FOUND")
            row.series_id = series.id

    if patch.admin_note is not None:
        row.admin_note = patch.admin_note.strip() or None

    if patch.status is not None and patch.status != row.status:
        row.status = patch.status
        if patch.status in (RequestStatus.FULFILLED, RequestStatus.DECLINED):
            row.resolved_at = utcnow()
            row.resolved_by_user_id = admin.id
        else:
            row.resolved_at = None
            row.resolved_by_user_id = None

    await session.commit()
    await session.refresh(row)
    tally = await _also_requested_counts(session, [row])
    return admin_request_out(row, also_requested_by=tally.get(row.dedupe_key, 0))


async def delete_any(session: AsyncSession, request_id: str) -> None:
    row = await get_or_404(session, request_id)
    await session.delete(row)
    await session.commit()


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "MAX_OWN_REQUESTS",
    "MAX_SEARCH_LIMIT",
    "admin_request_out",
    "counts",
    "create",
    "dedupe_key",
    "delete_any",
    "delete_own",
    "find_poster",
    "folder_name_for",
    "get_or_404",
    "list_all",
    "list_for_user",
    "prepare_folder",
    "request_out",
    "safe_poster_url",
    "search",
    "update",
]
