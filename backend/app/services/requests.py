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
from app.services.images import avatar_url, is_request_poster
from app.services.renamer import sanitize_filename
from app.services.scanner.scan import safe_media_path
from app.services.search.normalize import normalize
from app.services import dramadex
from app.services.settings import SettingsService
from app.services.vocab import (
    Provider,
    configured_providers,
    provider_domains,
    provider_for_domain,
)
from app.utils.time import utcnow

log = logging.getLogger(__name__)

#: The only host a request's `poster_url` may point at.
#:
#: A member's chosen poster is rendered in an `<img>` **in the admin's browser**, which makes
#: an arbitrary URL in that column a beacon: whoever controls it learns when an admin opened
#: the requests page and from which address. TMDB's image CDN is already loaded on every other
#: screen of this app, so allowing it adds no exposure that does not already exist, and
#: allowing nothing else keeps the column from becoming a way to point somebody else's browser
#: anywhere. A URL that fails this is dropped, not rejected — the request is the point, the
#: picture is decoration.
#: Posters now come from this application's own DramaDex proxy, never a third-party CDN, so
#: "is this URL safe to render" collapses to "is it one of ours". With TMDB gone there is no
#: external host on the allowlist at all, which is strictly the safer shape.
_POSTER_PREFIX = "/api/library/dramadex/titles/"

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
    where. See `_POSTER_PREFIX`.
    """
    if is_request_poster(value):
        return str(value)
    if not _is_http_url(value):
        return None
    text = str(value or "")
    return text if text.startswith(_POSTER_PREFIX) else None


# --- Searching ---------------------------------------------------------------------------------


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


def _candidate_from(
    found: dramadex.DramadexTitle, *, person: str = "", by_cast: bool = False
) -> RequestCandidate:
    watch = next((s for s in found.studios if s.is_watchable), None)
    return RequestCandidate(
        key=f"dramadex:{found.id}",
        title=found.primary_title,
        year=found.year,
        # Proxied through this app: DramaDex needs an API key on every route, and that key
        # must never reach a browser.
        poster_url=f"/api/library/dramadex/titles/{found.id}/poster",
        source_url=watch.watch_url if watch else None,
        provider=watch.name if watch else "DramaDex",
        # A real synopsis, not a search-engine extract — so unlike the web path, this one is
        # safe to store on the request.
        snippet=(found.synopsis or "")[:400],
        matched_person=person,
        matched_by_cast=by_cast or bool(person),
    )


async def _dramadex_page(
    client: dramadex.DramadexClient, limit: int, **params: Any
) -> list[dramadex.DramadexTitle] | None:
    """One call. `None` on failure, which is *not* the same as an empty page.

    Not an exception, because a member typing a title should never meet a stack trace because a
    container is restarting. But not `[]` either: that made a dead indexer, a rotated key and a
    5xx all indistinguishable from "no such show", and the screen then told the member to check
    their spelling for a title the household already owns.
    """
    try:
        page = await client.search(limit=limit, **params)
    except AppError as exc:
        log.info("dramadex wishlist search failed (%s)", exc.code)
        return None
    return page.items


async def _dramadex_people(
    client: dramadex.DramadexClient, text: str
) -> tuple[dramadex.Person, ...]:
    """Who the typed text is the name of, or nothing. Empty on failure, like its siblings."""
    try:
        return await client.people(text, limit=5)
    except AppError as exc:
        log.info("dramadex people lookup failed (%s)", exc.code)
        return ()


#: The share of a wishlist page that cast matches may claim when there are any. Half, because
#: neither reading of a typed name is the obviously right one — "Adam" is a plausible title
#: word and a real actor — and starving either would make one of the two searches invisible.
PERSON_SHARE = 2


async def _dramadex_search(
    session: AsyncSession, text: str, limit: int
) -> tuple[list[RequestCandidate], bool]:
    """Wishlist cards from the household's own indexer, or an empty list.

    Asked **twice**: once as a title and once as a person. DramaDex indexes the cast of ~2300
    titles and answers `?person=` on a case-insensitive partial name, and short drama is the
    genre where the actor is the thing a member remembers — these shows are retitled between
    platforms and the face is not. The two calls are independent HTTP requests and touch no
    session, so they are raced.

    `key` is the DramaDex id, which makes it stable across spellings — the same show found by
    two different queries collapses to one wishlist row instead of two, and it is also what
    de-duplicates a title that both searches return.
    """
    client = await dramadex.try_client(session)
    if client is None:
        return [], False

    # Three calls, raced. The third is what stops the card lying about whose name it is: the
    # indexer's `?person=` takes a *partial* name and never says which person it matched, so
    # echoing the query back printed "Reparto: «herr»" on twelve cards whose actual actor was
    # Marc Herrmann every time. `GET /people?q=` resolves that, and only when it resolves to
    # exactly one person is anybody named — with several, the card says it matched the cast and
    # stops, because attributing one of them per title would be a guess.
    by_title, by_person, named = await asyncio.gather(
        _dramadex_page(client, limit, query=text),
        _dramadex_page(client, limit, person=text),
        _dramadex_people(client, text),
    )
    person_name = named[0].name if len(named) == 1 else ""

    # Answered at all? `try_client` only reads two settings rows — it never opens a socket — so
    # without this the screen could say "we looked and found nothing" while nothing had been
    # asked. Either title search reaching the indexer is enough to call it reachable; the name
    # lookup is not, because an indexer with no `/people` route would read as an outage.
    reached = by_title is not None or by_person is not None
    by_title = by_title or []
    by_person = by_person or []

    # A title the *person* search also returned is marked even though the title search reached
    # it first. Measured on "Adam Daniel": seven cards came back by title, five of them his and
    # two coincidental, and without this the two look exactly as relevant as the five.
    confirmed = {found.id for found in by_person}
    titled = [
        _candidate_from(
            found,
            person=person_name if found.id in confirmed else "",
            by_cast=found.id in confirmed,
        )
        for found in by_title
    ]
    seen = {card.key for card in titled}
    credited = [
        _candidate_from(found, person=person_name, by_cast=True)
        for found in by_person
        if f"dramadex:{found.id}" not in seen
    ]

    # Titles first, but never so many that the cast matches fall off the page entirely.
    room = limit - min(len(credited), limit // PERSON_SHARE)
    return [*titled[:room], *credited][:limit], reached


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

    # --- DramaDex first, and usually last ---
    #
    # The household's own index of DramaBox and ReelShort. When it is connected it answers
    # this question completely: a title, a year, a synopsis, a poster and the link to the
    # platform — for the exact genre the two providers below do not carry. Measured against
    # this library it resolves 11 of 17 series where the web search resolved 1.
    #
    # So when it answers, nothing else is asked. The providers below are not a second opinion;
    # they are what this screen did before an index existed, and mixing a real record with
    # search-engine guesses gives a member two cards for one show and no way to tell which is
    # the show.
    dramadex_cards, reached = await _dramadex_search(session, text, limit)
    if dramadex_cards:
        await _mark_in_library(session, dramadex_cards)
        await _mark_requested(session, user.id, dramadex_cards)
        return RequestSearchResults(
            query=text,
            providers=["dramadex"],
            configured=True,
            results=dramadex_cards[:limit],
        )

    # Nothing else to ask.
    #
    # This used to fall through to TMDB and a SearXNG web search. Both are gone: TMDB does not
    # catalogue vertical short drama, and the web search returned links rather than records —
    # between them they resolved two of this library's seventeen series, where DramaDex
    # resolves all seventeen. An empty answer is now an honest one, and asking by name is the
    # path that never needed a provider at all.
    # `providers` names who actually *answered*, not who is configured. An indexer that is set
    # up but unreachable reports an empty list, which is how the page tells "we asked and there
    # is nothing" apart from "we could not ask" — the two used to render as the same sentence,
    # the one that blames the member's spelling.
    connected = await dramadex.try_client(session) is not None
    return RequestSearchResults(
        query=text,
        providers=["dramadex"] if reached else [],
        configured=connected,
        results=[],
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
            (await session.execute(select(Series).where(Series.tmdb_id == payload.ids.tmdb_id)))
            .scalars()
            .first()
        )
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


async def create(session: AsyncSession, user: User, payload: RequestCreateIn) -> SeriesRequestOut:
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
        (
            await session.execute(
                select(SeriesRequest).where(
                    SeriesRequest.user_id == user.id, SeriesRequest.dedupe_key == key
                )
            )
        )
        .scalars()
        .first()
    )
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
            (
                await session.execute(
                    select(SeriesRequest).where(
                        SeriesRequest.user_id == user.id, SeriesRequest.dedupe_key == key
                    )
                )
            )
            .scalars()
            .first()
        )
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
        await session.scalar(select(func.count()).select_from(SeriesRequest).where(*where)) or 0
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
                    SeriesRequest.status.notin_([RequestStatus.PENDING, RequestStatus.APPROVED]),
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
