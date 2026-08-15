"""The wishlist: asking for something, and the four ways that can go wrong.

The feature is small and its failure modes are all about *identity and audience*, so that is
what this file is mostly about rather than CRUD.

**Identity.** Everything — "you already asked for this", the unique constraint, "three people
want this one" — rests on `dedupe_key` picking the same string for the same show and different
strings for different shows. The tiers are asserted directly, including the two that are easy
to get wrong: a TMDB movie and a TMDB series can carry the same number, and a title with a year
must not collide with the same title without one.

**Audience.** A member sees their own rows and nobody else's, and the routes that say *who
asked* live behind `require_admin`. Both are asserted from the attacker's side: another
member's request id answers "not found", not "forbidden", because the existence of a request
is itself private.

**The boundary.** This is a wishlist, not a downloader. Two tests pin that down: the search
talks to exactly two hosts (the configured SearXNG instance and TMDB) and nothing else, and a
poster URL pointing anywhere but the allowlisted CDN is dropped rather than stored — the column
is rendered in an `<img>` in the *admin's* browser, which makes an arbitrary URL there a
beacon.

**Degradation.** A household that has configured neither provider must still be able to ask
for a title by name. A TMDB outage must not take the SearXNG results down with it. Both are
ordinary states here, not errors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RequestStatus, Series, SeriesRequest, SeriesTitle, User, UserRole
from app.schemas.library import DiscoveredIds
from app.services import images as images_service
from app.services import requests as requests_service
from app.services import tmdb as tmdb_service
from app.services.requests import dedupe_key, safe_poster_url

pytestmark = pytest.mark.anyio

SEARXNG_URL = "http://10.0.50.30:8080"


# ==========================================================================================
# dedupe_key — the whole feature's notion of "the same show"
# ==========================================================================================


def ids(**fields: Any) -> DiscoveredIds:
    return DiscoveredIds(**fields)


def test_the_strongest_identifier_wins() -> None:
    """A row carrying several ids keys on the one that pins the title down hardest."""
    every = ids(
        tmdb_id=1396,
        tmdb_kind="tv",
        imdb_id="tt0903747",
        mydramalist_id="12345",
        dramaboxdb_id="987",
        wikidata_id="Q1079",
        wikipedia_title="Breaking Bad",
    )
    assert dedupe_key(every, "Breaking Bad") == "tmdb:tv:1396"
    assert dedupe_key(ids(imdb_id="tt0903747"), "x") == "imdb:tt0903747"
    assert dedupe_key(ids(mydramalist_id="12345"), "x") == "mydramalist:12345"
    assert dedupe_key(ids(dramaboxdb_id="987"), "x") == "dramaboxdb:987"
    assert dedupe_key(ids(wikidata_id="Q1079"), "x") == "wikidata:Q1079"


def test_a_movie_and_a_series_with_the_same_number_are_different_titles() -> None:
    """TMDB's two catalogues number independently, so the kind is part of the key.

    Without it, asking for the film `550` and the series `550` would be the same request and
    the second member would silently receive the first one's answer.
    """
    movie = dedupe_key(ids(tmdb_id=550, tmdb_kind="movie"), "Fight Club")
    series = dedupe_key(ids(tmdb_id=550, tmdb_kind="tv"), "Something Else")
    assert movie != series


def test_an_id_less_row_keys_on_its_normalized_title() -> None:
    """The weakest tier, and the one that has to survive punctuation and case."""
    assert dedupe_key(ids(), "Ruta de Cristal") == dedupe_key(ids(), "  ruta  DE cristal ")
    assert dedupe_key(ids(), "Ruta de Cristal") != dedupe_key(ids(), "Otra Ruta")


def test_the_year_only_joins_the_key_when_there_is_one() -> None:
    """A placeholder year would split one title into two rows the moment somebody typed it."""
    with_year = dedupe_key(ids(), "Ruta de Cristal", 2024)
    without = dedupe_key(ids(), "Ruta de Cristal")
    assert with_year != without
    assert "None" not in without
    # Two different years are two different shows — remakes are the common case here.
    assert with_year != dedupe_key(ids(), "Ruta de Cristal", 2019)


def test_a_cjk_title_keys_the_same_way_it_is_searched() -> None:
    """`normalize` is the library's own index function, so a Hangul title keys stably."""
    assert dedupe_key(ids(), "미생") == dedupe_key(ids(), "미생")
    assert dedupe_key(ids(), "미생") != dedupe_key(ids(), "도깨비")


# ==========================================================================================
# The poster allowlist
# ==========================================================================================


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://image.tmdb.org/t/p/w342/abc.jpg", True),
        ("https://image.tmdb.org/t/p/original/abc.jpg", True),
        # Served by this app itself, from bytes that already went through the guarded fetch
        # and ffmpeg. Same-origin, so it also works for a member outside the house.
        ("/api/stream/images/requests/abc123.webp", True),
        # A near-miss on that path must not qualify — it would be an open redirect into the
        # allowlist, which is the whole point of having one.
        ("/api/stream/images/../../etc/passwd", False),
        ("https://evil.example/api/stream/images/requests/x.webp", False),
        # Everything below would put a third party's URL in an admin's browser.
        ("https://evil.example/beacon.png", False),
        ("http://10.0.50.99/tracker.gif", False),
        ("https://image.tmdb.org.evil.example/abc.jpg", False),
        ("javascript:alert(1)", False),
        ("data:image/png;base64,AAAA", False),
        ("//image.tmdb.org/t/p/w342/abc.jpg", False),
        ("", False),
        (None, False),
    ],
)
def test_only_the_allowlisted_cdn_survives(url: str | None, kept: bool) -> None:
    assert (safe_poster_url(url) is not None) is kept


# ==========================================================================================
# Fixtures
# ==========================================================================================


def mock_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> AsyncClient:
    return AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


@pytest.fixture
def serve_searxng(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    """A fake instance that records every request leaving the process."""

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        client = mock_client(wrapped, follow_redirects=True)

        async def shared() -> AsyncClient:
            return client

        monkeypatch.setattr(images_service, "_shared_client", shared)
        return seen

    return install


@pytest.fixture
def serve_tmdb(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        client = mock_client(
            wrapped, base_url=tmdb_service.API_BASE, follow_redirects=True
        )

        async def shared() -> AsyncClient:
            return client

        monkeypatch.setattr(tmdb_service, "_shared_client", shared)
        return seen

    return install


def searxng_rows(rows: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(200, json={"query": "x", "results": rows})
        return httpx.Response(404, text="not found")

    return handler


def tmdb_rows(
    rows: list[dict[str, Any]],
    details: dict[int, dict[str, Any]] | None = None,
    *,
    find: dict[str, dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A TMDB stand-in for the three calls the wishlist makes.

    `/search/multi` rather than `/search/tv`: the wishlist asks both catalogues, because a
    vertical drama is filed as a film on TMDB about as often as it is filed as a series.
    Search rows are echoed back with a `media_type` so the client can tell which is which,
    matching what the real endpoint sends.
    """
    lookup = details or {}
    found = find or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search/multi"):
            return httpx.Response(
                200,
                json={"results": [{"media_type": "tv", **row} for row in rows]},
            )
        for imdb_id, payload in found.items():
            if path.endswith(f"/find/{imdb_id}"):
                return httpx.Response(200, json=payload)
        for tmdb_id, payload in lookup.items():
            if path.endswith(f"/tv/{tmdb_id}") or path.endswith(f"/movie/{tmdb_id}"):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"status_message": "not found"})

    return handler


async def configure_searxng(db: AsyncSession, base_url: str = SEARXNG_URL) -> None:
    from app.services.settings import SettingsService

    settings = SettingsService(db)
    await settings.set("images.search.provider", "searxng")
    await settings.set("images.search.base_url", base_url)


async def configure_tmdb(db: AsyncSession) -> None:
    from app.services.settings import SettingsService

    await SettingsService(db).set("tmdb.api_key", "a-key")


@pytest.fixture
async def other_user(db: AsyncSession) -> User:
    user = User(
        email="otro@example.com",
        display_name="Otro",
        role=UserRole.VIEWER,
        locale="es",
    )
    db.add(user)
    await db.commit()
    return user


def make_client(db: AsyncSession, user: User, *, admin: User | None = None) -> AsyncClient:
    from app.api.deps import get_current_user, require_admin
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: user
    if admin is not None:
        app.dependency_overrides[require_admin] = lambda: admin
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def viewer_client(db: AsyncSession, viewer_user: User) -> AsyncIterator[AsyncClient]:
    from app.main import app

    try:
        async with make_client(db, viewer_user) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def admin_client(db: AsyncSession, admin_user: User) -> AsyncIterator[AsyncClient]:
    from app.main import app

    try:
        async with make_client(db, admin_user, admin=admin_user) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


def error_code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


async def ask(client: AsyncClient, **body: Any) -> httpx.Response:
    payload: dict[str, Any] = {"title": "Ruta de Cristal", **body}
    return await client.post("/api/requests", json=payload)


# ==========================================================================================
# Asking
# ==========================================================================================


async def test_a_bare_title_is_a_perfectly_good_request(viewer_client: AsyncClient) -> None:
    """The path that must work when no provider is configured and nothing resolved.

    Everything else in the feature is an improvement on this: if a member can describe what
    they want, the admin can act on it.
    """
    created = await ask(viewer_client, note="la coreana, no el remake")
    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "Ruta de Cristal"
    assert body["status"] == "pending"
    assert body["note"] == "la coreana, no el remake"
    assert body["alreadyExisted"] is False
    assert body["provider"] == ""


async def test_asking_twice_returns_the_first_row_rather_than_a_second(
    db: AsyncSession, viewer_client: AsyncClient
) -> None:
    """Including when it was already answered — burying a decline helps nobody."""
    first = (await ask(viewer_client)).json()

    row = await db.get(SeriesRequest, first["id"])
    assert row is not None
    row.status = RequestStatus.DECLINED
    row.admin_note = "no la conseguí"
    await db.commit()

    again = await ask(viewer_client)
    assert again.status_code == 201
    body = again.json()
    assert body["id"] == first["id"]
    assert body["alreadyExisted"] is True
    # The answer travels back with it: the member finds out why, instead of waiting again.
    assert body["status"] == "declined"
    assert body["adminNote"] == "no la conseguí"

    total = len((await db.execute(select(SeriesRequest))).scalars().all())
    assert total == 1


async def test_two_members_asking_for_the_same_thing_are_two_rows(
    db: AsyncSession, viewer_user: User, other_user: User
) -> None:
    """The unique constraint is per member. Demand is the thing the admin needs to see."""
    from app.schemas.requests import RequestCreateIn

    payload = RequestCreateIn(title="Ruta de Cristal", ids={})
    await requests_service.create(db, viewer_user, payload)
    await requests_service.create(db, other_user, payload)

    rows = (await db.execute(select(SeriesRequest))).scalars().all()
    assert len(rows) == 2
    assert len({row.dedupe_key for row in rows}) == 1

    items, _ = await requests_service.list_all(db)
    assert [item.also_requested_by for item in items] == [1, 1]


async def test_asking_for_something_the_library_already_has_is_refused_with_the_reason(
    db: AsyncSession, viewer_client: AsyncClient, sample_series: Any
) -> None:
    """"We have it" is the answer the member actually wanted, and it is available now."""
    series, _ = sample_series
    db.add(
        SeriesTitle(
            series_id=series.id,
            title=series.title,
            normalized="ruta de cristal",
            language="es",
        )
    )
    await db.commit()

    refused = await ask(viewer_client, title="Ruta de Cristal")
    assert refused.status_code == 409
    assert error_code(refused) == "REQUEST_ALREADY_IN_LIBRARY"
    assert (await db.execute(select(SeriesRequest))).scalars().first() is None


async def test_the_library_check_also_matches_on_a_tmdb_id(
    db: AsyncSession, viewer_client: AsyncClient, sample_series: Any
) -> None:
    """A show filed under a title nobody types is still the show the household owns."""
    series, _ = sample_series
    series.tmdb_id = 1396
    await db.commit()

    refused = await ask(
        viewer_client, title="Completely Different Name", ids={"tmdbId": 1396, "tmdbKind": "tv"}
    )
    assert refused.status_code == 409
    assert error_code(refused) == "REQUEST_ALREADY_IN_LIBRARY"


async def test_a_poster_from_anywhere_but_the_cdn_is_dropped_not_stored(
    db: AsyncSession, viewer_client: AsyncClient
) -> None:
    """And the request still succeeds: the picture is decoration, the ask is the point."""
    created = await ask(viewer_client, posterUrl="https://evil.example/beacon.png")
    assert created.status_code == 201
    assert created.json()["posterUrl"] is None

    row = (await db.execute(select(SeriesRequest))).scalars().one()
    assert row.poster_url is None


async def test_the_provider_is_derived_from_the_link_not_taken_from_the_client(
    db: AsyncSession, viewer_client: AsyncClient
) -> None:
    """It is the field an admin reads to decide how far to trust the row."""
    created = await ask(
        viewer_client, sourceUrl="https://www.mydramalist.com/12345-algo", provider="themoviedb.org"
    )
    assert created.status_code == 201
    assert created.json()["provider"] == "mydramalist.com"


async def test_a_source_url_that_is_not_http_is_discarded(
    db: AsyncSession, viewer_client: AsyncClient
) -> None:
    """`javascript:` in an href would run in whichever browser opened the admin screen."""
    created = await ask(viewer_client, sourceUrl="javascript:alert(1)")
    assert created.status_code == 201
    assert created.json()["sourceUrl"] is None


async def test_the_ids_survive_the_round_trip(
    db: AsyncSession, viewer_client: AsyncClient
) -> None:
    """They are the leads the admin follows; losing them turns the row back into a phrase."""
    created = await ask(
        viewer_client,
        ids={
            "tmdbId": 1396,
            "tmdbKind": "tv",
            "imdbId": "tt0903747",
            "mydramalistId": "12345",
            "dramaboxdbId": "987",
            "dramaboxdbSlug": "algo",
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["ids"]["tmdbId"] == 1396
    assert body["ids"]["imdbId"] == "tt0903747"
    assert body["ids"]["mydramalistId"] == "12345"
    assert body["ids"]["dramaboxdbId"] == "987"
    assert body["ids"]["dramaboxdbSlug"] == "algo"


# ==========================================================================================
# Audience
# ==========================================================================================


async def test_a_member_sees_only_their_own_list(
    db: AsyncSession, viewer_user: User, other_user: User, viewer_client: AsyncClient
) -> None:
    from app.schemas.requests import RequestCreateIn

    await requests_service.create(db, other_user, RequestCreateIn(title="Lo de Otro", ids={}))
    await requests_service.create(db, viewer_user, RequestCreateIn(title="Lo Mío", ids={}))

    listed = await viewer_client.get("/api/requests")
    assert listed.status_code == 200
    assert [row["title"] for row in listed.json()] == ["Lo Mío"]


async def test_another_members_request_answers_not_found_rather_than_forbidden(
    db: AsyncSession, other_user: User, viewer_client: AsyncClient
) -> None:
    """Whether a request exists is itself something only its owner should learn."""
    from app.schemas.requests import RequestCreateIn

    theirs = await requests_service.create(
        db, other_user, RequestCreateIn(title="Lo de Otro", ids={})
    )

    refused = await viewer_client.delete(f"/api/requests/{theirs.id}")
    assert refused.status_code == 404
    assert error_code(refused) == "REQUEST_NOT_FOUND"
    assert await db.get(SeriesRequest, theirs.id) is not None


async def test_a_member_can_withdraw_their_own(
    db: AsyncSession, viewer_client: AsyncClient
) -> None:
    created = (await ask(viewer_client)).json()
    gone = await viewer_client.delete(f"/api/requests/{created['id']}")
    assert gone.status_code == 200
    assert await db.get(SeriesRequest, created["id"]) is None


async def test_the_admin_queue_is_not_reachable_by_a_viewer(
    db: AsyncSession, viewer_user: User
) -> None:
    """`require_admin` sits on the router, and this is the assertion that it does."""
    from app.main import app

    try:
        async with make_client(db, viewer_user) as http:
            for response in (
                await http.get("/api/admin/requests"),
                await http.get("/api/admin/requests/counts"),
                await http.patch("/api/admin/requests/x", json={"status": "approved"}),
                await http.delete("/api/admin/requests/x"),
            ):
                assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


async def test_only_the_admin_view_names_the_requester(
    db: AsyncSession, viewer_user: User, admin_client: AsyncClient
) -> None:
    """The member-facing model has no such field at all — see the schema module docstring."""
    from app.schemas.requests import RequestCreateIn

    await requests_service.create(db, viewer_user, RequestCreateIn(title="Lo Mío", ids={}))

    listed = await admin_client.get("/api/admin/requests")
    assert listed.status_code == 200
    row = listed.json()["items"][0]
    assert row["requester"]["email"] == viewer_user.email
    assert row["requester"]["displayName"] == viewer_user.display_name


# ==========================================================================================
# Answering
# ==========================================================================================


async def test_resolving_stamps_who_answered_and_when(
    db: AsyncSession, viewer_user: User, admin_user: User, admin_client: AsyncClient
) -> None:
    from app.schemas.requests import RequestCreateIn

    made = await requests_service.create(db, viewer_user, RequestCreateIn(title="Lo Mío", ids={}))

    answered = await admin_client.patch(
        f"/api/admin/requests/{made.id}",
        json={"status": "declined", "adminNote": "no está en ningún lado"},
    )
    assert answered.status_code == 200
    body = answered.json()
    assert body["status"] == "declined"
    assert body["adminNote"] == "no está en ningún lado"
    assert body["resolvedAt"] is not None
    assert body["resolvedByName"] == admin_user.display_name


async def test_reopening_clears_the_resolution(
    db: AsyncSession, viewer_user: User, admin_client: AsyncClient
) -> None:
    """A request reopened after a mistaken decline must stop claiming it was answered."""
    from app.schemas.requests import RequestCreateIn

    made = await requests_service.create(db, viewer_user, RequestCreateIn(title="Lo Mío", ids={}))
    await admin_client.patch(f"/api/admin/requests/{made.id}", json={"status": "declined"})

    reopened = await admin_client.patch(
        f"/api/admin/requests/{made.id}", json={"status": "pending"}
    )
    assert reopened.json()["resolvedAt"] is None
    assert reopened.json()["resolvedByName"] is None


async def test_fulfilling_can_link_the_series_it_became(
    db: AsyncSession, viewer_user: User, admin_client: AsyncClient, sample_series: Any
) -> None:
    from app.schemas.requests import RequestCreateIn

    series, _ = sample_series
    made = await requests_service.create(db, viewer_user, RequestCreateIn(title="Lo Mío", ids={}))

    done = await admin_client.patch(
        f"/api/admin/requests/{made.id}",
        json={"status": "fulfilled", "seriesId": series.id},
    )
    assert done.status_code == 200
    assert done.json()["series"]["slug"] == series.slug

    # And the member sees the link on their own row without being told separately.
    from app.main import app

    try:
        async with make_client(db, viewer_user) as http:
            mine = (await http.get("/api/requests")).json()
    finally:
        app.dependency_overrides.clear()
    assert mine[0]["series"]["slug"] == series.slug


async def test_linking_a_series_that_does_not_exist_is_refused(
    db: AsyncSession, viewer_user: User, admin_client: AsyncClient
) -> None:
    from app.schemas.requests import RequestCreateIn

    made = await requests_service.create(db, viewer_user, RequestCreateIn(title="Lo Mío", ids={}))
    refused = await admin_client.patch(
        f"/api/admin/requests/{made.id}", json={"seriesId": "nope"}
    )
    assert refused.status_code == 404
    assert error_code(refused) == "SERIES_NOT_FOUND"


async def test_deleting_the_series_keeps_the_record_that_somebody_asked(
    db: AsyncSession, viewer_user: User, sample_series: Any
) -> None:
    """`SET NULL`, not cascade. The request outlives whatever it was pointed at."""
    from app.schemas.requests import RequestCreateIn

    series, _ = sample_series
    made = await requests_service.create(db, viewer_user, RequestCreateIn(title="Lo Mío", ids={}))
    row = await db.get(SeriesRequest, made.id)
    assert row is not None
    row.series_id = series.id
    await db.commit()

    await db.delete(await db.get(Series, series.id))
    await db.commit()
    await db.refresh(row)

    assert row.series_id is None
    assert row.title == "Lo Mío"


async def test_the_counts_drive_the_badge(
    db: AsyncSession, viewer_user: User, other_user: User, admin_client: AsyncClient
) -> None:
    from app.schemas.requests import RequestCreateIn

    a = await requests_service.create(db, viewer_user, RequestCreateIn(title="Uno", ids={}))
    await requests_service.create(db, other_user, RequestCreateIn(title="Dos", ids={}))
    await admin_client.patch(f"/api/admin/requests/{a.id}", json={"status": "fulfilled"})

    counts = (await admin_client.get("/api/admin/requests/counts")).json()
    assert counts == {"pending": 1, "approved": 0, "fulfilled": 1, "declined": 0, "total": 2}

    # The dashboard tile counts only what still needs a decision.
    stats = (await admin_client.get("/api/admin/stats")).json()
    assert stats["pendingRequestCount"] == 1


async def test_open_requests_sort_above_settled_ones(
    db: AsyncSession, viewer_user: User, other_user: User, admin_client: AsyncClient
) -> None:
    """Alphabetical luck would put `approved` first and `declined` between the open two."""
    from app.schemas.requests import RequestCreateIn

    done = await requests_service.create(db, viewer_user, RequestCreateIn(title="Vieja", ids={}))
    await admin_client.patch(f"/api/admin/requests/{done.id}", json={"status": "fulfilled"})
    await requests_service.create(db, other_user, RequestCreateIn(title="Nueva", ids={}))

    listed = (await admin_client.get("/api/admin/requests")).json()
    assert [row["title"] for row in listed["items"]] == ["Nueva", "Vieja"]


async def test_removing_a_member_removes_their_wishlist(
    db: AsyncSession, viewer_user: User
) -> None:
    """CASCADE, like every other per-member row. A queue with nobody to deliver to is noise."""
    from app.schemas.requests import RequestCreateIn

    await requests_service.create(db, viewer_user, RequestCreateIn(title="Lo Mío", ids={}))
    await db.delete(await db.get(User, viewer_user.id))
    await db.commit()

    assert (await db.execute(select(SeriesRequest))).scalars().first() is None


# ==========================================================================================
# Searching
# ==========================================================================================


async def test_the_search_reaches_exactly_two_hosts_and_fetches_no_page(
    db: AsyncSession,
    viewer_user: User,
    serve_searxng: Callable[..., list[httpx.Request]],
    serve_tmdb: Callable[..., list[httpx.Request]],
) -> None:
    """The boundary, asserted by the transports rather than by reading the code.

    Every request the feature makes lands in one of these two lists. A result page being
    fetched — to scrape a poster, a synopsis, anything — would show up as a third host.
    """
    await configure_searxng(db)
    await configure_tmdb(db)

    web = serve_searxng(
        searxng_rows(
            [
                {
                    "url": "https://www.mydramalist.com/12345-ruta",
                    "title": "Ruta de Cristal",
                    "content": "Un drama",
                    "engine": "duckduckgo",
                }
            ]
        )
    )
    tmdb = serve_tmdb(tmdb_rows([]))

    await requests_service.search(db, viewer_user, "ruta de cristal")

    hosts = {request.url.host for request in web + tmdb}
    assert hosts <= {"10.0.50.30", httpx.URL(tmdb_service.API_BASE).host}
    assert "mydramalist.com" not in hosts


async def test_tmdb_rows_come_back_as_cards_with_posters(
    db: AsyncSession, viewer_user: User, serve_tmdb: Callable[..., list[httpx.Request]]
) -> None:
    await configure_tmdb(db)
    serve_tmdb(
        tmdb_rows(
            [
                {
                    "id": 1396,
                    "name": "Ruta de Cristal",
                    "first_air_date": "2024-03-01",
                    "poster_path": "/abc.jpg",
                    "overview": "Un drama corto",
                }
            ]
        )
    )

    found = await requests_service.search(db, viewer_user, "ruta")
    assert found.providers == ["tmdb"]
    card = found.results[0]
    assert card.title == "Ruta de Cristal"
    assert card.year == 2024
    assert card.poster_url == "https://image.tmdb.org/t/p/w342/abc.jpg"
    assert card.provider == "themoviedb.org"
    assert card.ids.tmdb_id == 1396


async def test_a_web_result_naming_a_tmdb_id_is_promoted_into_a_record(
    db: AsyncSession,
    viewer_user: User,
    serve_searxng: Callable[..., list[httpx.Request]],
    serve_tmdb: Callable[..., list[httpx.Request]],
) -> None:
    """The case this household hits constantly: TMDB search misses, the web finds the page.

    The card that comes out has the poster and the year TMDB search would have given, because
    the id was in the URL all along.
    """
    await configure_searxng(db)
    await configure_tmdb(db)
    serve_searxng(
        searxng_rows(
            [{"url": "https://www.themoviedb.org/tv/1396", "title": "Some page", "content": ""}]
        )
    )
    serve_tmdb(
        tmdb_rows(
            [],
            details={
                1396: {
                    "id": 1396,
                    "name": "Ruta de Cristal",
                    "first_air_date": "2024-03-01",
                    "poster_path": "/abc.jpg",
                }
            },
        )
    )

    found = await requests_service.search(db, viewer_user, "ruta")
    card = found.results[0]
    assert card.title == "Ruta de Cristal"
    assert card.year == 2024
    assert card.poster_url == "https://image.tmdb.org/t/p/w342/abc.jpg"


async def test_a_web_result_with_no_id_stays_a_link_with_its_domain(
    db: AsyncSession, viewer_user: User, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """Still worth showing: a MyDramaList page is how the admin learns the real title."""
    await configure_searxng(db)
    serve_searxng(
        searxng_rows(
            [
                {
                    "url": "https://www.mydramalist.com/12345-ruta",
                    "title": "Ruta de Cristal (2024)",
                    "content": "Un drama corto",
                    "engine": "duckduckgo",
                }
            ]
        )
    )

    found = await requests_service.search(db, viewer_user, "ruta")
    card = found.results[0]
    assert card.provider == "mydramalist.com"
    assert card.poster_url is None
    assert card.ids.mydramalist_id == "12345"


async def test_the_same_title_from_both_sources_is_one_card(
    db: AsyncSession,
    viewer_user: User,
    serve_searxng: Callable[..., list[httpx.Request]],
    serve_tmdb: Callable[..., list[httpx.Request]],
) -> None:
    """And it is TMDB's version, which is the one with the picture."""
    await configure_searxng(db)
    await configure_tmdb(db)
    serve_searxng(
        searxng_rows([{"url": "https://www.themoviedb.org/tv/1396", "title": "web copy"}])
    )
    serve_tmdb(
        tmdb_rows(
            [{"id": 1396, "name": "Ruta de Cristal", "poster_path": "/abc.jpg"}]
        )
    )

    found = await requests_service.search(db, viewer_user, "ruta")
    assert len(found.results) == 1
    assert found.results[0].title == "Ruta de Cristal"
    assert found.results[0].poster_url is not None


async def test_cards_say_what_the_library_already_has(
    db: AsyncSession, viewer_user: User, sample_series: Any,
    serve_tmdb: Callable[..., list[httpx.Request]],
) -> None:
    """Answering "we have it" at the moment of asking beats answering it a day later."""
    series, _ = sample_series
    series.tmdb_id = 1396
    await db.commit()
    await configure_tmdb(db)
    serve_tmdb(tmdb_rows([{"id": 1396, "name": "Ruta de Cristal"}]))

    found = await requests_service.search(db, viewer_user, "ruta")
    assert found.results[0].in_library is True
    assert found.results[0].in_library_slug == series.slug


async def test_cards_say_what_this_member_already_asked_for(
    db: AsyncSession, viewer_user: User, serve_tmdb: Callable[..., list[httpx.Request]]
) -> None:
    from app.schemas.requests import RequestCreateIn

    await configure_tmdb(db)
    serve_tmdb(tmdb_rows([{"id": 1396, "name": "Ruta de Cristal"}]))
    await requests_service.create(
        db,
        viewer_user,
        RequestCreateIn(title="Ruta de Cristal", ids={"tmdbId": 1396, "tmdbKind": "tv"}),
    )

    found = await requests_service.search(db, viewer_user, "ruta")
    assert found.results[0].requested is True
    assert found.results[0].request_status == "pending"


async def test_another_members_request_does_not_mark_your_card(
    db: AsyncSession,
    viewer_user: User,
    other_user: User,
    serve_tmdb: Callable[..., list[httpx.Request]],
) -> None:
    from app.schemas.requests import RequestCreateIn

    await configure_tmdb(db)
    serve_tmdb(tmdb_rows([{"id": 1396, "name": "Ruta de Cristal"}]))
    await requests_service.create(
        db,
        other_user,
        RequestCreateIn(title="Ruta de Cristal", ids={"tmdbId": 1396, "tmdbKind": "tv"}),
    )

    found = await requests_service.search(db, viewer_user, "ruta")
    assert found.results[0].requested is False


async def test_nothing_configured_is_a_state_not_an_error(
    db: AsyncSession, viewer_client: AsyncClient
) -> None:
    """A household on its first day can still write down what it wants."""
    found = await viewer_client.get("/api/requests/search", params={"q": "ruta"})
    assert found.status_code == 200
    body = found.json()
    assert body["configured"] is False
    assert body["providers"] == []
    assert body["results"] == []


async def test_a_tmdb_outage_does_not_take_the_web_results_down(
    db: AsyncSession, viewer_user: User,
    serve_searxng: Callable[..., list[httpx.Request]],
    serve_tmdb: Callable[..., list[httpx.Request]],
) -> None:
    """Two sources exist so that one of them can be having a bad minute.

    `providers` lists what is *configured*, not what happened to answer this second — a member
    typing should not watch it flicker — so the assertion that matters here is that the web
    results arrived at all.
    """
    await configure_searxng(db)
    await configure_tmdb(db)
    serve_searxng(
        searxng_rows([{"url": "https://www.mydramalist.com/12345-ruta", "title": "Ruta"}])
    )
    serve_tmdb(lambda _request: httpx.Response(503, json={"status_message": "down"}))

    found = await requests_service.search(db, viewer_user, "ruta")
    assert found.configured is True
    assert [card.provider for card in found.results] == ["mydramalist.com"]


async def test_tmdb_films_are_candidates_too(
    db: AsyncSession, viewer_user: User, serve_tmdb: Callable[..., list[httpx.Request]]
) -> None:
    """The bug that made this library come back empty.

    Vertical short-form drama is catalogued as a *film* on TMDB at least as often as it is
    catalogued as a series. `search_tv` alone therefore misses it — and a film row names its
    title and date under different keys, so reading it as a TV row loses both.
    """
    await configure_tmdb(db)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/multi"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "media_type": "movie",
                            "id": 550,
                            "title": "Amor en la Oficina",
                            "release_date": "2024-05-02",
                            "poster_path": "/film.jpg",
                        }
                    ]
                },
            )
        return httpx.Response(404, json={})

    serve_tmdb(handler)

    found = await requests_service.search(db, viewer_user, "amor")
    card = found.results[0]
    assert card.title == "Amor en la Oficina"
    assert card.year == 2024
    assert card.poster_url == "https://image.tmdb.org/t/p/w342/film.jpg"
    # The kind travels with the number, in the key and in the link.
    assert card.ids.tmdb_kind == "movie"
    assert card.key == "tmdb:movie:550"
    assert card.source_url == "https://www.themoviedb.org/movie/550"


async def test_an_imdb_only_result_is_resolved_through_tmdb(
    db: AsyncSession,
    viewer_user: User,
    serve_searxng: Callable[..., list[httpx.Request]],
    serve_tmdb: Callable[..., list[httpx.Request]],
) -> None:
    """IMDb publishes no free API, so an IMDb id is worth what TMDB can be asked about it.

    A web hit on an IMDb page carries the id in its URL and nothing else. `/find` turns that
    into a real record — and the TMDB id it comes back with is recorded on the card, so the
    admin's later match is one click instead of another search.
    """
    await configure_searxng(db)
    await configure_tmdb(db)
    serve_searxng(
        searxng_rows([{"url": "https://www.imdb.com/title/tt0903747/", "title": "IMDb page"}])
    )
    serve_tmdb(
        tmdb_rows(
            [],
            find={
                "tt0903747": {
                    "tv_results": [
                        {
                            "id": 1396,
                            "name": "Ruta de Cristal",
                            "first_air_date": "2024-03-01",
                            "poster_path": "/abc.jpg",
                        }
                    ]
                }
            },
        )
    )

    found = await requests_service.search(db, viewer_user, "ruta")
    card = found.results[0]
    assert card.title == "Ruta de Cristal"
    assert card.year == 2024
    assert card.poster_url == "https://image.tmdb.org/t/p/w342/abc.jpg"
    assert card.ids.tmdb_id == 1396
    assert card.ids.imdb_id == "tt0903747"


# ==========================================================================================
# Posters
# ==========================================================================================
#
# The download itself is `test_artwork.py`'s subject — same guarded fetch, same re-encode. What
# is tested here is the orchestration around it, because that is what decides whether a card
# ends up with a picture: which query goes to the engine, which hit is taken, and what happens
# when the answer is useless.


@pytest.fixture
def fake_image_search(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    """Stand in for the household's image search and record what it was asked."""

    def install(urls: list[str], *, cached: str | None = "/api/stream/images/requests/x.webp"):
        asked: list[str] = []

        class _Provider:
            name = "searxng"

            async def search(self, query: str, *, limit: int = 10) -> list[Any]:
                asked.append(query)
                return [images_service.ImageSearchResult(url=url) for url in urls[:limit]]

        async def _get(_session: Any) -> Any:
            return _Provider()

        async def _cache(url: str) -> str | None:
            # `None` models the real failure modes — a dead link, an HTML page, a file too
            # large — all of which `cache_request_poster` folds into "no picture".
            return cached if url else None

        monkeypatch.setattr(requests_service, "get_image_search", _get)
        monkeypatch.setattr(requests_service, "cache_request_poster", _cache)
        requests_service._POSTER_MEMO.clear()
        return asked

    return install


async def test_the_poster_query_asks_for_a_poster(
    db: AsyncSession, fake_image_search: Callable[..., list[str]]
) -> None:
    """Without the word, an image search for a drama returns stills and cast photographs."""
    asked = fake_image_search(["https://cdn.example/p.jpg"])

    url = await requests_service.find_poster(db, "Ruta de Cristal", 2024)

    assert url == "/api/stream/images/requests/x.webp"
    assert asked == ["Ruta de Cristal 2024 poster"]


async def test_the_year_is_not_repeated_when_the_title_already_carries_it(
    db: AsyncSession, fake_image_search: Callable[..., list[str]]
) -> None:
    asked = fake_image_search(["https://cdn.example/p.jpg"])
    await requests_service.find_poster(db, "Ruta de Cristal 2024", 2024)
    assert asked == ["Ruta de Cristal 2024 poster"]


async def test_the_first_usable_hit_wins_and_the_rest_are_not_downloaded(
    db: AsyncSession, fake_image_search: Callable[..., list[str]]
) -> None:
    fake_image_search(["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"])
    assert await requests_service.find_poster(db, "Algo") == "/api/stream/images/requests/x.webp"


async def test_an_unusable_image_is_no_picture_rather_than_an_error(
    db: AsyncSession, fake_image_search: Callable[..., list[str]]
) -> None:
    """A dead link or an HTML page must not be the reason a member cannot ask for something."""
    fake_image_search(["https://cdn.example/broken.jpg"], cached=None)
    assert await requests_service.find_poster(db, "Algo") is None


async def test_no_image_search_configured_is_simply_no_poster(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _none(_session: Any) -> Any:
        return None

    monkeypatch.setattr(requests_service, "get_image_search", _none)
    requests_service._POSTER_MEMO.clear()
    assert await requests_service.find_poster(db, "Algo") is None


async def test_the_same_title_is_only_searched_once(
    db: AsyncSession, fake_image_search: Callable[..., list[str]]
) -> None:
    """Every card on the screen asks for its own poster; a memo keeps that to one call each."""
    asked = fake_image_search(["https://cdn.example/p.jpg"])

    await requests_service.find_poster(db, "Ruta de Cristal", 2024)
    await requests_service.find_poster(db, "Ruta de Cristal", 2024)

    assert len(asked) == 1


async def test_the_poster_route_answers_null_without_failing(
    db: AsyncSession, viewer_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`{"url": null}` is an ordinary answer. Most of this library has no poster anywhere."""

    async def _none(_session: Any) -> Any:
        return None

    monkeypatch.setattr(requests_service, "get_image_search", _none)
    requests_service._POSTER_MEMO.clear()

    answer = await viewer_client.get("/api/requests/poster", params={"q": "Algo"})
    assert answer.status_code == 200
    assert answer.json() == {"url": None}


async def test_the_poster_route_is_not_reachable_anonymously(db: AsyncSession) -> None:
    """It reaches the household's own search instance, so it is signed-in-only like the rest."""
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            assert (await http.get("/api/requests/poster", params={"q": "x"})).status_code == 401
    finally:
        app.dependency_overrides.clear()


async def test_a_blank_query_asks_nobody_anything(
    db: AsyncSession, viewer_user: User, serve_tmdb: Callable[..., list[httpx.Request]]
) -> None:
    await configure_tmdb(db)
    seen = serve_tmdb(tmdb_rows([]))

    found = await requests_service.search(db, viewer_user, "   ")
    assert found.results == []
    assert seen == []


# ==========================================================================================
# Preparing the landing place
# ==========================================================================================


@pytest.mark.parametrize(
    ("title", "year", "expected"),
    [
        ("Ruta de Cristal", 2024, "Ruta de Cristal (2024)"),
        # The real one, with the colon that would otherwise be refused by some filesystems.
        ("Fruit Dimension: Orange Light Redemption", 2025,
         "Fruit Dimension Orange Light Redemption (2025)"),
        # No year is not an invented year. It parses back as `None`, which is correct.
        ("Definitely Not the Dragon God", None, "Definitely Not the Dragon God"),
        # A title cannot introduce a path separator.
        ("Love/Redemption", 2024, "Love Redemption (2024)"),
        ("../../etc/passwd", None, "etc passwd"),
        ("", 2024, "Untitled (2024)"),
    ],
)
def test_folder_name_for(title: str, year: int | None, expected: str) -> None:
    assert requests_service.folder_name_for(title, year) == expected


@pytest.mark.parametrize(
    ("title", "year"),
    [
        ("Ruta de Cristal", 2024),
        ("Fruit Dimension: Orange Light Redemption", 2025),
        ("Definitely Not the Dragon God", None),
        ("Tempest The Last Mecha", 2026),
    ],
)
def test_the_folder_parses_back_to_what_it_was_built_from(title: str, year: int | None) -> None:
    """The whole point, and the property that would fail invisibly.

    The folder is named canonically so the scan that finds the files already knows what they
    are. A name that does not survive `parse_series_folder` looks perfectly reasonable in a
    directory listing and quietly produces a series titled something else.
    """
    from app.services.scanner.parse import parse_series_folder

    name = requests_service.folder_name_for(title, year)
    parsed_title, parsed_year = parse_series_folder(name)

    assert parsed_year == year
    # Sanitizing may drop punctuation, so the comparison is against the sanitized title.
    from app.services.renamer import sanitize_filename

    assert parsed_title == sanitize_filename(title)


async def test_preparing_a_folder_creates_it_and_says_so(
    db: AsyncSession, tmp_env: Any, viewer_user: User
) -> None:
    from app.schemas.requests import RequestCreateIn

    made = await requests_service.create(
        db, viewer_user, RequestCreateIn(title="Ruta de Cristal", year=2024, ids={})
    )

    path, created = await requests_service.prepare_folder(db, made.id)

    assert path == "Ruta de Cristal (2024)"
    assert created is True
    assert (tmp_env / "library" / path).is_dir()


async def test_preparing_twice_is_not_an_error(
    db: AsyncSession, tmp_env: Any, viewer_user: User
) -> None:
    """Pressing it again means "make sure it is there", not "make a second one"."""
    from app.schemas.requests import RequestCreateIn

    made = await requests_service.create(
        db, viewer_user, RequestCreateIn(title="Ruta de Cristal", year=2024, ids={})
    )
    await requests_service.prepare_folder(db, made.id)

    path, created = await requests_service.prepare_folder(db, made.id)
    assert created is False
    assert (tmp_env / "library" / path).is_dir()


async def test_a_title_full_of_traversal_lands_inside_the_library(
    db: AsyncSession, tmp_env: Any, viewer_user: User
) -> None:
    """The title is free text a *member* typed, so it is the least trusted input here."""
    from app.schemas.requests import RequestCreateIn

    made = await requests_service.create(
        db, viewer_user, RequestCreateIn(title="../../../../tmp/evil", ids={})
    )

    path, _ = await requests_service.prepare_folder(db, made.id)

    assert ".." not in path
    assert (tmp_env / "library" / path).is_dir()
    # And nothing appeared outside the root.
    assert not (tmp_env / "tmp").exists()


async def test_only_an_admin_can_prepare_a_folder(
    db: AsyncSession, viewer_user: User
) -> None:
    """It writes to the library mount, so it lives behind the admin router like the rest."""
    from app.main import app

    try:
        async with make_client(db, viewer_user) as http:
            answer = await http.post("/api/admin/requests/whatever/folder")
    finally:
        app.dependency_overrides.clear()
    assert answer.status_code == 403


async def test_short_drama_mode_filters_by_host_rather_than_trusting_site(
    db: AsyncSession, viewer_user: User, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """The same correction the editor's search carries; this path had been left behind.

    This household's instance ignores `site:` outright, so a mode that relied on it was
    labelling unfiltered results as narrowed — Dailymotion and YouTube presented as if they
    had come from the configured platforms.
    """
    await configure_searxng(db)
    from app.services.settings import SettingsService

    await SettingsService(db).set(
        "library.providers", [{"name": "ReelShort", "domain": "reelshort.com"}]
    )

    serve_searxng(
        searxng_rows(
            [
                {"url": "https://www.dailymotion.com/video/x1", "title": "noise"},
                {"url": "https://www.reelshort.com/movie/9", "title": "the real one"},
                {"url": "https://www.imdb.com/title/tt42157994/", "title": "IMDb page"},
            ]
        )
    )

    found = await requests_service.search(db, viewer_user, "algo", short_drama=True)

    hosts = [card.source_url or "" for card in found.results]
    assert not any("dailymotion" in host for host in hosts)
    # The platform *and* IMDb survive: the reference databases ride along in this mode.
    assert any("reelshort.com" in host for host in hosts)
    assert any("imdb.com" in host for host in hosts)
