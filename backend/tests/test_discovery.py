"""Web discovery: reading a linkable identifier out of a search result.

The feature exists because TMDB's title search cannot find this household's library. A web
search can — it returns the show's TMDB, IMDb, Wikipedia or MyDramaList *page* — so the value
sits in the URL, and `extract_ids` is the function that decides whether the feature works at
all. Almost everything below is about that one pure function, because a pattern that is
slightly wrong fails silently: the admin sees a page they recognize with no button on it.

Three properties are defended here beyond the patterns themselves.

**The near-misses matter as much as the matches.** `themoviedb.org/person/1234` is a real
result for a drama query and it is not the drama; a lookalike domain must never contribute an
id to a series; and a URL with `tv/` in its *query string* is a search-results page, which is
what a web search returns most of. Matching the URL as a flat string would accept all three.

**A general search is not an image search.** The instance and its settings are shared with the
artwork picker, but the category is `general` and the configured engine list — image engines,
chosen on the artwork screen — must not be sent, or SearXNG filters every result away.

**No result page is fetched.** The only host this module talks to is the configured instance.
That is asserted by the mock transport itself: every request the code makes lands in a list.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Series, User
from app.schemas.library import DiscoveredIds
from app.services import images as images_service
from app.services.discovery import (
    discover,
    discover_scoped,
    extract_ids,
    rank,
    search_query,
)

pytestmark = pytest.mark.anyio

BASE_URL = "http://10.0.50.30:8080"


# ==========================================================================================
# extract_ids
# ==========================================================================================
#
# One table, one assertion helper. Every row is a URL a search engine actually produces.


def ids(**fields: Any) -> DiscoveredIds:
    return DiscoveredIds(**fields)


TMDB_CASES: list[tuple[str, DiscoveredIds | None]] = [
    # --- The shapes TMDB links arrive in ---
    ("https://www.themoviedb.org/tv/1396", ids(tmdb_id=1396, tmdb_kind="tv")),
    ("https://themoviedb.org/tv/1396", ids(tmdb_id=1396, tmdb_kind="tv")),
    ("http://www.themoviedb.org/tv/1396", ids(tmdb_id=1396, tmdb_kind="tv")),
    ("HTTPS://WWW.THEMOVIEDB.ORG/TV/1396", ids(tmdb_id=1396, tmdb_kind="tv")),
    ("https://www.themoviedb.org/tv/1396/", ids(tmdb_id=1396, tmdb_kind="tv")),
    # The slugged form is what the site itself links to.
    ("https://www.themoviedb.org/tv/1396-breaking-bad", ids(tmdb_id=1396, tmdb_kind="tv")),
    ("https://www.themoviedb.org/tv/1396-breaking-bad/", ids(tmdb_id=1396, tmdb_kind="tv")),
    # A language prefix, and a language query string.
    ("https://www.themoviedb.org/es/tv/1396", ids(tmdb_id=1396, tmdb_kind="tv")),
    ("https://www.themoviedb.org/pt-br/tv/1396", ids(tmdb_id=1396, tmdb_kind="tv")),
    ("https://www.themoviedb.org/tv/1396?language=es-ES", ids(tmdb_id=1396, tmdb_kind="tv")),
    ("https://www.themoviedb.org/tv/1396#seasons", ids(tmdb_id=1396, tmdb_kind="tv")),
    # A deep link into the same series is still that series.
    ("https://www.themoviedb.org/tv/1396/season/2", ids(tmdb_id=1396, tmdb_kind="tv")),
    # Movies are the other catalogue, and the kind has to travel with the number.
    ("https://www.themoviedb.org/movie/550", ids(tmdb_id=550, tmdb_kind="movie")),
    (
        "https://www.themoviedb.org/movie/550-fight-club?language=ko",
        ids(tmdb_id=550, tmdb_kind="movie"),
    ),
    # --- Near misses ---
    # A person page is a very common result for a drama query.
    ("https://www.themoviedb.org/person/1234-lee-min-ho", None),
    ("https://www.themoviedb.org/collection/1241", None),
    ("https://www.themoviedb.org/tv", None),
    ("https://www.themoviedb.org/", None),
    ("https://www.themoviedb.org/tv/abc", None),
    ("https://www.themoviedb.org/tv/1396x", None),
    # `tv/` only in the query string: a search-results page, not a title.
    ("https://www.themoviedb.org/search?query=tv/1396", None),
    # Lookalike domains.
    ("https://notthemoviedb.org/tv/1396", None),
    ("https://themoviedb.org.evil.example/tv/1396", None),
    ("https://example.com/tv/1396", None),
]

IMDB_CASES: list[tuple[str, DiscoveredIds | None]] = [
    ("https://www.imdb.com/title/tt0903747/", ids(imdb_id="tt0903747")),
    ("https://imdb.com/title/tt0903747", ids(imdb_id="tt0903747")),
    ("https://m.imdb.com/title/tt0903747/", ids(imdb_id="tt0903747")),
    # IMDb's regional prefixes, which is how the site links itself in a Spanish locale.
    ("https://www.imdb.com/es/title/tt0903747/", ids(imdb_id="tt0903747")),
    ("https://www.imdb.com/pt-br/title/tt0903747/", ids(imdb_id="tt0903747")),
    # A sub-page of the title, plus the tracking query every IMDb result carries.
    ("https://www.imdb.com/title/tt0903747/episodes?season=1", ids(imdb_id="tt0903747")),
    ("https://www.imdb.com/title/tt0903747/?ref_=nv_sr_1", ids(imdb_id="tt0903747")),
    # Normalized, so it can be handed straight to `/tmdb/lookup`.
    ("https://www.IMDb.com/title/TT0903747/", ids(imdb_id="tt0903747")),
    ("https://www.imdb.com/title/tt12345678901/", ids(imdb_id="tt12345678901")),
    # --- Near misses ---
    ("https://www.imdb.com/name/nm0000123/", None),
    ("https://www.imdb.com/list/ls012345678/", None),
    ("https://www.imdb.com/title/", None),
    ("https://www.imdb.com/title/xx0903747/", None),
    ("https://www.imdb.com/title/tt99/", None),
    ("https://www.imdb.com/search/title/?title_type=tv_series", None),
]

WIKIPEDIA_CASES: list[tuple[str, DiscoveredIds | None]] = [
    (
        "https://en.wikipedia.org/wiki/Squid_Game",
        ids(wikipedia_lang="en", wikipedia_title="Squid Game"),
    ),
    (
        "https://es.wikipedia.org/wiki/El_juego_del_calamar",
        ids(wikipedia_lang="es", wikipedia_title="El juego del calamar"),
    ),
    # Mobile hosts are what a phone-sourced result looks like.
    (
        "https://en.m.wikipedia.org/wiki/Squid_Game",
        ids(wikipedia_lang="en", wikipedia_title="Squid Game"),
    ),
    # Percent-encoded Korean: unreadable in a picker unless it is decoded.
    (
        "https://ko.wikipedia.org/wiki/%EC%98%A4%EC%A7%95%EC%96%B4_%EA%B2%8C%EC%9E%84",
        ids(wikipedia_lang="ko", wikipedia_title="오징어 게임"),
    ),
    # Language codes that are not two letters.
    (
        "https://simple.wikipedia.org/wiki/Squid_Game",
        ids(wikipedia_lang="simple", wikipedia_title="Squid Game"),
    ),
    (
        "https://zh-yue.wikipedia.org/wiki/Squid_Game",
        ids(wikipedia_lang="zh-yue", wikipedia_title="Squid Game"),
    ),
    (
        "https://en.wikipedia.org/wiki/Squid_Game?action=history",
        ids(wikipedia_lang="en", wikipedia_title="Squid Game"),
    ),
    (
        "https://en.wikipedia.org/wiki/Squid_Game#Cast",
        ids(wikipedia_lang="en", wikipedia_title="Squid Game"),
    ),
    (
        "https://en.wikipedia.org/wiki/Squid_Game/",
        ids(wikipedia_lang="en", wikipedia_title="Squid Game"),
    ),
    # A colon in an article title is ordinary and must not be read as a namespace.
    (
        "https://en.wikipedia.org/wiki/Mission:_Impossible",
        ids(wikipedia_lang="en", wikipedia_title="Mission: Impossible"),
    ),
    # Parentheses are how Wikipedia disambiguates, so they are in half these URLs.
    (
        "https://en.wikipedia.org/wiki/Squid_Game_(TV_series)",
        ids(wikipedia_lang="en", wikipedia_title="Squid Game (TV series)"),
    ),
    # The `/wiki/` prefix is recognized whatever its case; the title keeps the case it had,
    # because `Squid_Game` and `SQUID_GAME` are different strings to Wikipedia.
    (
        "HTTPS://EN.WIKIPEDIA.ORG/WIKI/SQUID_GAME",
        ids(wikipedia_lang="en", wikipedia_title="SQUID GAME"),
    ),
    # --- Near misses ---
    ("https://wikipedia.org/", None),
    ("https://www.wikipedia.org/", None),
    ("https://en.wikipedia.org/", None),
    ("https://en.wikipedia.org/wiki/", None),
    ("https://en.wikipedia.org/wiki/Main_Page", None),
    ("https://en.wikipedia.org/wiki/Special:Random", None),
    ("https://en.wikipedia.org/wiki/Category:South_Korean_television_series", None),
    ("https://en.wikipedia.org/wiki/File:Squid_Game_logo.png", None),
    # The old query-string form addresses an article, but not in the path this reads.
    ("https://en.wikipedia.org/w/index.php?title=Squid_Game", None),
    # A different project on the same family of domains.
    ("https://en.wikinews.org/wiki/Squid_Game", None),
]

WIKIDATA_CASES: list[tuple[str, DiscoveredIds | None]] = [
    ("https://www.wikidata.org/wiki/Q108808429", ids(wikidata_id="Q108808429")),
    ("https://wikidata.org/wiki/Q108808429", ids(wikidata_id="Q108808429")),
    ("https://www.wikidata.org/wiki/q108808429", ids(wikidata_id="Q108808429")),
    ("https://www.wikidata.org/entity/Q108808429", ids(wikidata_id="Q108808429")),
    ("https://m.wikidata.org/wiki/Q108808429?uselang=es", ids(wikidata_id="Q108808429")),
    # --- Near misses ---
    ("https://www.wikidata.org/wiki/Property:P31", None),
    ("https://www.wikidata.org/wiki/Q", None),
    ("https://www.wikidata.org/", None),
]

MYDRAMALIST_CASES: list[tuple[str, DiscoveredIds | None]] = [
    ("https://mydramalist.com/686327-crash-landing-on-you", ids(mydramalist_id="686327")),
    ("https://www.mydramalist.com/686327-crash-landing-on-you/", ids(mydramalist_id="686327")),
    (
        "https://mydramalist.com/686327-crash-landing-on-you/episodes",
        ids(mydramalist_id="686327"),
    ),
    ("https://mydramalist.com/686327-crash-landing?ref=search", ids(mydramalist_id="686327")),
    # --- Near misses ---
    ("https://mydramalist.com/shows/top", None),
    ("https://mydramalist.com/people/1234-someone", None),
    ("https://mydramalist.com/", None),
    ("https://mydramalist.com/686327", None),
]

MALFORMED_CASES: list[tuple[str, DiscoveredIds | None]] = [
    ("", None),
    ("   ", None),
    ("not a url at all", None),
    ("themoviedb.org/tv/1396", None),
    ("javascript:alert(1)//themoviedb.org/tv/1396", None),
    ("ftp://www.themoviedb.org/tv/1396", None),
    ("file:///etc/passwd", None),
    ("https:///tv/1396", None),
]

ALL_CASES = (
    TMDB_CASES
    + IMDB_CASES
    + WIKIPEDIA_CASES
    + WIKIDATA_CASES
    + MYDRAMALIST_CASES
    + MALFORMED_CASES
)


@pytest.mark.parametrize(
    ("url", "expected"), ALL_CASES, ids=[case[0] or "empty" for case in ALL_CASES]
)
def test_extract_ids(url: str, expected: DiscoveredIds | None) -> None:
    """The whole feature rests on this table: a URL in, an identifier or nothing out."""
    assert extract_ids(url) == expected


def test_the_case_table_is_exhaustive_enough_to_be_worth_trusting() -> None:
    """A guard on the guard: patterns are only as good as the number of shapes tried."""
    assert len(ALL_CASES) >= 30
    assert sum(1 for _, expected in ALL_CASES if expected is None) >= 15, (
        "near misses are half the point; a pattern that matches everything passes the rest"
    )


def test_a_tv_and_a_movie_with_the_same_number_are_different_titles() -> None:
    """The kind is not decoration — TMDB's two catalogues number independently."""
    tv = extract_ids("https://www.themoviedb.org/tv/550")
    movie = extract_ids("https://www.themoviedb.org/movie/550")
    assert tv is not None and movie is not None
    assert tv.tmdb_id == movie.tmdb_id == 550
    assert tv != movie


def test_extract_ids_is_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network, ever. The identifier is in the URL; the page behind it is nobody's business.

    Enforced by breaking the HTTP client the rest of this codebase would reach for.
    """

    async def never() -> None:
        raise AssertionError("extract_ids must not open a connection")

    monkeypatch.setattr(images_service, "_shared_client", never)
    monkeypatch.setattr(images_service, "_guarded_client", never)
    assert extract_ids("https://www.themoviedb.org/tv/1396") == ids(tmdb_id=1396, tmdb_kind="tv")


# ==========================================================================================
# The query
# ==========================================================================================


@pytest.mark.parametrize(
    ("query", "year", "expected"),
    [
        ("Ruta de Cristal", 2024, "Ruta de Cristal 2024"),
        ("Ruta de Cristal", None, "Ruta de Cristal"),
        # Already there: a title carrying its own year must not say it twice.
        ("Ruta de Cristal 2024", 2024, "Ruta de Cristal 2024"),
        ("  Ruta   de  Cristal ", None, "Ruta de Cristal"),
        ("", 2024, "2024"),
        ("", None, ""),
    ],
)
def test_the_year_is_folded_into_the_query(query: str, year: int | None, expected: str) -> None:
    assert search_query(query, year) == expected


# ==========================================================================================
# The search
# ==========================================================================================


def mock_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> AsyncClient:
    return AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


@pytest.fixture
def serve_searxng(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    """Install a fake instance and record every request that leaves the process.

    The returned list is also the assertion that nothing scrapes a result page: if a request
    ever went anywhere but the configured instance it would be in here.
    """

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


def serve(results: list[Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(200, json={"query": "x", "results": results})
        return httpx.Response(404, text="not found")

    return handler


def row(url: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "title": "A result", "content": "Some words", "engine": "duckduckgo",
            **extra}


@pytest.fixture
async def series(db: AsyncSession) -> Series:
    row_ = Series(
        title="Ruta de Cristal",
        slug="ruta-de-cristal",
        sort_title="ruta de cristal",
        library_path="Ruta de Cristal (2024)",
        year=2024,
    )
    db.add(row_)
    await db.commit()
    return row_


@pytest.fixture
async def client(db: AsyncSession, admin_user: User) -> AsyncIterator[AsyncClient]:
    from app.api.deps import get_current_user, require_admin
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def configure(db: AsyncSession, *, provider: str = "searxng", base_url: str = BASE_URL,
                    **extra: Any) -> None:
    from app.services.settings import SettingsService

    settings = SettingsService(db)
    await settings.set("images.search.provider", provider)
    await settings.set("images.search.base_url", base_url)
    for key, value in extra.items():
        await settings.set(f"images.search.{key}", value)


def error_code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


async def test_the_query_is_a_general_search_not_an_image_search(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """Same instance, same settings, different category — and no engine list.

    `images.search.engines` holds *image* engines, picked on the artwork screen. Sending them
    with `categories=general` asks SearXNG for a general engine named `bing images`, which does
    not exist, and every result is filtered away. The bug would look like "discovery returns
    nothing" on exactly the installs that configured the picker most carefully.
    """
    await configure(db, engines="bing images, google images", language="es", safe_search=2)
    seen = serve_searxng(serve([]))

    await discover(db, "ruta de cristal")

    request = seen[0]
    assert str(request.url).startswith(f"{BASE_URL}/search")
    assert request.url.params["q"] == "ruta de cristal"
    assert request.url.params["format"] == "json"
    assert request.url.params["categories"] == "general"
    assert request.url.params["safesearch"] == "2"
    assert request.url.params["language"] == "es"
    assert "engines" not in request.url.params
    assert request.headers["user-agent"] == images_service.SEARXNG_USER_AGENT


async def test_results_carry_their_ids_their_source_and_their_snippet(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """What the picker renders: where it came from, what it says, and what it identifies."""
    await configure(db)
    serve_searxng(
        serve(
            [
                row(
                    "https://www.themoviedb.org/tv/1396-ruta-de-cristal",
                    title="Ruta de Cristal (2024) — The Movie Database",
                    content="  Ruta de Cristal is a\n2024 South Korean\ttelevision series …  ",
                    engine="brave",
                )
            ]
        )
    )

    results = await discover(db, "ruta de cristal")
    assert results.provider == "searxng"
    assert results.query == "ruta de cristal"
    assert len(results.results) == 1

    found = results.results[0]
    assert found.ids.tmdb_id == 1396
    assert found.ids.tmdb_kind == "tv"
    assert found.resolvable is True
    assert found.source_domain == "themoviedb.org"
    assert found.engine == "brave"
    # Whitespace collapsed, and kept as *display* text: nothing writes this to an overview.
    assert found.snippet == "Ruta de Cristal is a 2024 South Korean television series …"


async def test_id_bearing_results_come_first_and_the_rest_keep_engine_order(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """Two tiers, and the second one is left exactly as the instance ranked it.

    A blog post about the drama is not useless — it is how an admin confirms they have the
    right show — so it stays. It just cannot outrank the row with the button on it.
    """
    await configure(db)
    serve_searxng(
        serve(
            [
                row("https://fansite.example/reviews/ruta", title="A review"),
                row("https://en.wikipedia.org/wiki/Ruta_de_Cristal", title="Wikipedia"),
                row("https://blog.example/ruta", title="A blog"),
                row("https://www.imdb.com/title/tt0903747/", title="IMDb"),
                row("https://mydramalist.com/686327-ruta-de-cristal", title="MDL"),
                row("https://www.themoviedb.org/tv/1396", title="TMDB"),
            ]
        )
    )

    results = await discover(db, "ruta de cristal")
    titles = [result.title for result in results.results]

    assert titles[:2] == ["IMDb", "TMDB"], "resolvable first, in the order the engines gave them"
    assert titles[2:] == ["A review", "Wikipedia", "A blog", "MDL"]
    assert results.resolvable_count == 2
    # Recorded and shown, but nothing here resolves them.
    assert results.results[3].ids.wikipedia_title == "Ruta de Cristal"
    assert results.results[3].resolvable is False
    assert results.results[5].ids.mydramalist_id == "686327"


async def test_five_links_to_one_title_collapse_to_one_row(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """Aggregators quote each other, so one answer arrives five times in five spellings."""
    await configure(db)
    serve_searxng(
        serve(
            [
                row("https://www.themoviedb.org/tv/1396", engine="brave"),
                row("https://www.themoviedb.org/tv/1396-ruta-de-cristal", engine="duckduckgo"),
                row("https://themoviedb.org/es/tv/1396", engine="google"),
                row("https://www.themoviedb.org/tv/1396?language=ko", engine="startpage"),
                row("https://www.themoviedb.org/tv/1396/season/1", engine="mojeek"),
                # A different title on the same site is not a duplicate.
                row("https://www.themoviedb.org/tv/1397", engine="brave"),
                # Neither is the same number in the other catalogue.
                row("https://www.themoviedb.org/movie/1396", engine="brave"),
                # No id at all: deduplicated by URL instead, so three engines reporting one
                # page produce one row.
                row("https://fansite.example/ruta", engine="brave"),
                row("https://fansite.example/ruta/", engine="duckduckgo"),
                row("https://fansite.example/ruta", engine="google"),
            ]
        )
    )

    results = await discover(db, "ruta de cristal")
    assert [result.url for result in results.results] == [
        "https://www.themoviedb.org/tv/1396",
        "https://www.themoviedb.org/tv/1397",
        "https://www.themoviedb.org/movie/1396",
        "https://fansite.example/ruta",
    ]
    assert [result.engine for result in results.results[:1]] == ["brave"], "the first one wins"


def test_ranking_and_dedup_are_testable_without_a_search() -> None:
    """`rank` is pure, which is what lets the ordering rule be read at a glance."""
    from app.schemas.library import DiscoveryResult

    def result(url: str, **fields: Any) -> DiscoveryResult:
        from app.services.discovery import is_resolvable

        found = extract_ids(url) or DiscoveredIds()
        return DiscoveryResult(url=url, ids=found, resolvable=is_resolvable(found), **fields)

    ranked = rank(
        [
            result("https://blog.example/a"),
            result("https://www.imdb.com/title/tt0903747/"),
            result("https://blog.example/b"),
            result("https://m.imdb.com/title/tt0903747/"),
        ]
    )
    assert [item.url for item in ranked] == [
        "https://www.imdb.com/title/tt0903747/",
        "https://blog.example/a",
        "https://blog.example/b",
    ]


async def test_results_that_point_nowhere_usable_are_dropped(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    await configure(db)
    serve_searxng(
        serve(
            [
                row("https://www.themoviedb.org/tv/1396"),
                {"title": "no url at all", "content": "..."},
                row("ftp://files.example/ruta.zip"),
                row("/relative/path"),
                {"url": 12345},
                "not-a-dict",
            ]
        )
    )

    results = await discover(db, "ruta")
    assert [result.url for result in results.results] == ["https://www.themoviedb.org/tv/1396"]


async def test_the_limit_is_honoured_and_bounded(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    await configure(db)
    serve_searxng(serve([row(f"https://blog.example/{n}") for n in range(80)]))

    assert len((await discover(db, "ruta", limit=5)).results) == 5
    assert len((await discover(db, "ruta")).results) == 20
    # Asking for more than the cap gets the cap, not an error.
    assert len((await discover(db, "ruta", limit=999)).results) == 50


async def test_a_long_snippet_is_bounded(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """An engine that returns a page's worth of text must not become the response."""
    from app.services.discovery import SNIPPET_MAX_CHARS

    await configure(db)
    serve_searxng(serve([row("https://blog.example/a", content="palabra " * 500)]))

    snippet = (await discover(db, "ruta")).results[0].snippet
    assert len(snippet) <= SNIPPET_MAX_CHARS + 1
    assert snippet.endswith("…")


async def test_no_result_page_is_ever_fetched(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """The identifier is in the URL. Following the link would put this server's network
    position behind whatever a search engine crawled, for no gain at all."""
    await configure(db)
    seen = serve_searxng(
        serve(
            [
                row("https://www.themoviedb.org/tv/1396"),
                row("https://en.wikipedia.org/wiki/Ruta_de_Cristal"),
                row("http://127.0.0.1:8080/admin"),
            ]
        )
    )

    await discover(db, "ruta")
    assert [str(request.url.host) for request in seen] == ["10.0.50.30"]
    assert len(seen) == 1


# --- Configuration and failure ------------------------------------------------------------------


async def test_an_unconfigured_provider_raises_the_code_that_already_exists(
    db: AsyncSession,
) -> None:
    """One feature, one error code. The admin has a single thing to go and fix."""
    from app.errors import AppError
    from app.services.settings import SettingsService

    with pytest.raises(AppError) as raised:
        await discover(db, "ruta")
    assert raised.value.code == "SEARCH_NOT_CONFIGURED"

    # Named but not filled in is the same state.
    await SettingsService(db).set("images.search.provider", "searxng")
    with pytest.raises(AppError) as raised:
        await discover(db, "ruta")
    assert raised.value.code == "SEARCH_NOT_CONFIGURED"
    assert "base_url" in raised.value.message


async def test_google_cse_cannot_answer_a_web_search(db: AsyncSession) -> None:
    """It is an image API in this codebase; saying so is better than an empty result list."""
    from app.errors import AppError

    await configure(db, provider="google_cse", base_url="", api_key="key", cse_id="cx")
    with pytest.raises(AppError) as raised:
        await discover(db, "ruta")
    assert raised.value.code == "SEARCH_NOT_CONFIGURED"


async def test_a_json_disabled_instance_is_still_diagnosed_here(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """The shared HTTP handling is the point of reusing it: one instance, one diagnosis."""
    from app.errors import AppError

    await configure(db)
    serve_searxng(lambda request: httpx.Response(200, html="<html><body>search</body></html>"))

    with pytest.raises(AppError) as raised:
        await discover(db, "ruta")
    assert raised.value.code == "SEARXNG_JSON_DISABLED"
    assert "search.formats" in raised.value.message


@pytest.mark.parametrize(
    "payload",
    [{"query": "ruta"}, {"results": {"one": 1}}, {"results": "nope"}],
    ids=["no-results-key", "object-instead-of-list", "string-instead-of-list"],
)
async def test_json_of_the_wrong_shape_is_a_bad_response(
    db: AsyncSession,
    serve_searxng: Callable[..., list[httpx.Request]],
    payload: dict[str, Any],
) -> None:
    from app.errors import AppError

    await configure(db)
    serve_searxng(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(AppError) as raised:
        await discover(db, "ruta")
    assert raised.value.code == "SEARXNG_BAD_RESPONSE"


# --- The routes ----------------------------------------------------------------------------------


async def test_the_free_form_route_answers_in_camel_case(
    db: AsyncSession, client: AsyncClient, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    await configure(db)
    seen = serve_searxng(serve([row("https://www.themoviedb.org/tv/1396")]))

    response = await client.get("/api/library/discover", params={"q": "ruta", "year": 2024})
    assert response.status_code == 200, response.text
    body = response.json()

    assert seen[0].url.params["q"] == "ruta 2024"
    assert body["query"] == "ruta 2024"
    assert body["seriesId"] is None
    assert body["resolvableCount"] == 1
    found = body["results"][0]
    assert found["sourceDomain"] == "themoviedb.org"
    assert found["ids"]["tmdbId"] == 1396
    assert found["ids"]["tmdbKind"] == "tv"
    assert found["ids"]["imdbId"] is None


async def test_the_series_route_builds_the_query_from_title_and_year(
    db: AsyncSession,
    client: AsyncClient,
    series: Series,
    serve_searxng: Callable[..., list[httpx.Request]],
) -> None:
    """The common case is one click: the admin is already looking at the title and the year."""
    await configure(db)
    seen = serve_searxng(serve([row("https://www.imdb.com/title/tt0903747/")]))

    response = await client.get(f"/api/library/series/{series.id}/discover")
    assert response.status_code == 200, response.text
    body = response.json()

    assert seen[0].url.params["q"] == "Ruta de Cristal 2024"
    assert body["query"] == "Ruta de Cristal 2024"
    assert body["seriesId"] == series.id
    assert body["results"][0]["ids"]["imdbId"] == "tt0903747"


async def test_the_series_route_takes_an_override(
    db: AsyncSession,
    client: AsyncClient,
    series: Series,
    serve_searxng: Callable[..., list[httpx.Request]],
) -> None:
    """The stored title is sometimes exactly the string that finds nothing."""
    await configure(db)
    seen = serve_searxng(serve([]))

    response = await client.get(
        f"/api/library/series/{series.id}/discover",
        params={"q": "미생", "year": 2014},
    )
    assert response.status_code == 200, response.text
    assert seen[0].url.params["q"] == "미생 2014"
    assert response.json()["seriesId"] == series.id


async def test_a_series_without_a_year_searches_on_the_title_alone(
    db: AsyncSession, client: AsyncClient, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    await configure(db)
    seen = serve_searxng(serve([]))

    row_ = Series(
        title="Sin Año",
        slug="sin-ano",
        sort_title="sin ano",
        library_path="Sin Ano",
    )
    db.add(row_)
    await db.commit()

    response = await client.get(f"/api/library/series/{row_.id}/discover")
    assert response.status_code == 200, response.text
    assert seen[0].url.params["q"] == "Sin Año"


async def test_the_series_route_404s_for_a_series_that_is_not_there(
    db: AsyncSession, client: AsyncClient
) -> None:
    await configure(db)
    response = await client.get("/api/library/series/nope/discover")
    assert response.status_code == 404, response.text
    assert error_code(response) == "SERIES_NOT_FOUND"


async def test_the_routes_report_an_unconfigured_provider_as_400(
    db: AsyncSession, client: AsyncClient, series: Series
) -> None:
    """Not a 502: nothing is broken, something is simply not set up yet."""
    for path in ("/api/library/discover?q=ruta", f"/api/library/series/{series.id}/discover"):
        response = await client.get(path)
        assert response.status_code == 400, response.text
        assert error_code(response) == "SEARCH_NOT_CONFIGURED"


async def test_the_free_form_route_needs_something_to_search_for(
    db: AsyncSession, client: AsyncClient
) -> None:
    await configure(db)
    response = await client.get("/api/library/discover", params={"q": ""})
    assert response.status_code == 422, response.text


async def test_discovery_is_admin_only(db: AsyncSession, viewer_user: User) -> None:
    """A viewer has no match dialog, so they have no reason to reach the household's SearXNG."""
    from app.api.deps import get_current_user
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    await configure(db)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: viewer_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get("/api/library/discover", params={"q": "ruta"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403, response.text


# --- DramaBoxDB ------------------------------------------------------------------------------
#
# Recorded as a lead only. The site publishes no API and its robots.txt closes /search, so
# dramatarr links to it and never fetches it — the same treatment MyDramaList gets, and for the
# same reason: it is often the only place an obscure short drama has a page at all.


@pytest.mark.parametrize(
    ("url", "expected_id", "expected_slug"),
    [
        ("https://www.dramaboxdb.com/movie/12345/tempest-the-last-mecha", "12345",
         "tempest-the-last-mecha"),
        ("https://dramaboxdb.com/movie/7/a-slug", "7", "a-slug"),
        # Every language prefix the site publishes, including its irregular one.
        ("https://www.dramaboxdb.com/es/movie/999/deny-me-dragon-king", "999",
         "deny-me-dragon-king"),
        ("https://www.dramaboxdb.com/ko/movie/1/x", "1", "x"),
        ("https://www.dramaboxdb.com/zhHans/movie/2/y", "2", "y"),
        # Trailing slash, query string and fragment are decoration.
        ("https://www.dramaboxdb.com/movie/3/z/", "3", "z"),
        ("https://www.dramaboxdb.com/movie/4/w?utm_source=x#top", "4", "w"),
        # A bare id with no slug is still a title page.
        ("https://www.dramaboxdb.com/movie/5", "5", None),
        ("HTTPS://WWW.DRAMABOXDB.COM/MOVIE/6/CAPS", "6", "CAPS"),
    ],
)
def test_dramaboxdb_titles_are_recognized(
    url: str, expected_id: str, expected_slug: str | None
) -> None:
    ids = extract_ids(url)
    assert ids is not None
    assert ids.dramaboxdb_id == expected_id
    assert ids.dramaboxdb_slug == expected_slug


@pytest.mark.parametrize(
    "url",
    [
        "https://www.dramaboxdb.com/",
        "https://www.dramaboxdb.com/genres/7",
        "https://www.dramaboxdb.com/search?q=tempest",
        "https://www.dramaboxdb.com/downloadapp?x=1",
        "https://www.dramaboxdb.com/movie/",
        "https://www.dramaboxdb.com/movie/notanumber/x",
        # A domain that merely contains the name is a different site.
        "https://notdramaboxdb.com/movie/1/x",
        "https://dramaboxdb.com.evil.example/movie/1/x",
    ],
)
def test_dramaboxdb_non_titles_are_ignored(url: str) -> None:
    ids = extract_ids(url)
    assert ids is None or ids.dramaboxdb_id is None


def test_dramaboxdb_is_a_lead_not_a_resolvable_match() -> None:
    """It must never rank as actionable: dramatarr cannot turn that id into metadata."""
    ids = extract_ids("https://www.dramaboxdb.com/movie/12345/tempest-the-last-mecha")
    assert ids is not None
    assert ids.tmdb_id is None
    assert ids.imdb_id is None


# ==========================================================================================
# Platform-scoped search
# ==========================================================================================
#
# This exists because of a defect that shipped: the narrowed search used to widen itself
# whenever the platform filter came back empty. It sounded protective and was the opposite —
# a household whose configured domains are thinly indexed hits the empty case on nearly every
# query, so the fallback fired nearly every time and quietly answered a platform-scoped
# request with a generic web search. The admin saw a synopsis finder returning pages about
# anything at all, with no sign their filter had been dropped.


async def test_the_scoped_query_names_every_configured_domain(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    await configure(db)
    seen = serve_searxng(serve([]))

    await discover_scoped(
        db, "ruta de cristal", domains=("dramabox.com", "reelshort.com")
    )

    sent = seen[0].url.params["q"]
    assert sent == "ruta de cristal (site:dramabox.com OR site:reelshort.com)"


async def test_an_empty_platform_search_stays_empty(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """The regression this file's newest section exists for.

    One request, not two: finding nothing on the household's platforms must not silently turn
    into a search of the whole web. `narrowed` is how the screen tells the admin which of the
    two questions was actually answered.
    """
    await configure(db)
    seen = serve_searxng(serve([]))

    found = await discover_scoped(db, "ruta de cristal", domains=("dramabox.com",))

    assert found.results == []
    assert found.narrowed is True
    assert len(seen) == 1, "a second, unscoped request means the fallback came back"


async def test_without_domains_the_search_is_plain_and_says_so(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """Widening is the caller's decision, and the answer admits which one it was."""
    await configure(db)
    seen = serve_searxng(serve([row("https://www.themoviedb.org/tv/1396")]))

    found = await discover_scoped(db, "ruta de cristal")

    assert seen[0].url.params["q"] == "ruta de cristal"
    assert found.narrowed is False
    assert len(found.results) == 1


async def test_the_filter_does_not_trust_the_engine_to_honour_site(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """Measured, not assumed: this household's instance ignores `site:` outright.

    Asked for `site:dramabox.com`, its engines drop the operator and return Dailymotion and
    YouTube. Results were being labelled `narrowed` while entirely unfiltered — a true-sounding
    label on a false claim, which is worse than not filtering. So the hosts that actually came
    back are what decide.
    """
    await configure(db)
    serve_searxng(
        serve(
            [
                row("https://www.dailymotion.com/video/xaxkhqm"),
                row("https://m.youtube.com/watch?v=r9OISqXE3gc"),
                row("https://www.dramabox.com/movie/123"),
                row("https://es.dramabox.com/movie/456"),
                row("https://notdramabox.com/movie/789"),
            ]
        )
    )

    found = await discover_scoped(db, "algo", domains=("dramabox.com",))

    assert found.narrowed is True
    # The platform's own host and its language subdomain survive; the lookalike does not.
    assert [r.url for r in found.results] == [
        "https://www.dramabox.com/movie/123",
        "https://es.dramabox.com/movie/456",
    ]


async def test_nothing_from_the_platforms_is_reported_as_nothing(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """The household's real case: the engines index none of these platforms, so the honest
    answer is an empty list that says it was narrowed — not a page of unrelated video sites."""
    await configure(db)
    serve_searxng(serve([row("https://www.dailymotion.com/video/xaxkhqm")]))

    found = await discover_scoped(db, "algo", domains=("dramabox.dramafren.org",))

    assert found.results == []
    assert found.narrowed is True


async def test_failing_engines_travel_with_the_results(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """An empty list means nothing until you know whether anything was listening.

    This household ran for days on an instance where, of fourteen general engines, only two
    answered — and read the resulting emptiness as "this content is not on the web". The
    instance knew; the app was throwing the answer away.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": "x",
                "results": [],
                "unresponsive_engines": [
                    ["duckduckgo", "CAPTCHA"],
                    ["brave", "HTTP error"],
                    ["yep", "access denied"],
                ],
            },
        )

    await configure(db)
    serve_searxng(handler)

    found = await discover(db, "ruta de cristal")

    assert found.results == []
    assert found.unresponsive_engines == ["brave", "duckduckgo", "yep"]


async def test_a_healthy_instance_reports_no_failures(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    await configure(db)
    serve_searxng(serve([row("https://www.themoviedb.org/tv/1396")]))
    assert (await discover(db, "algo")).unresponsive_engines == []


# ==========================================================================================
# Which platform published it
# ==========================================================================================
#
# Measured on the live library: of eleven series, five had no network at all — the emptiest
# field in the catalogue, and the one no metadata source fills, because TMDB and IMDb do not
# record which vertical-drama app a show came from.
#
# The search already knew. A hit on `reelshort.com` names the platform with no inference at
# all; the host *is* the answer. It was being rendered as grey provenance text and thrown
# away. This is deliberately the only thing read off a platform page — the snippet beside it
# is still someone else's prose, still cut mid-sentence, and still never stored.


async def test_a_platform_page_names_its_platform(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    await configure(db)
    serve_searxng(
        serve(
            [
                row("https://www.dramabox.com/drama/123"),
                row("https://es.reelshort.com/shorts/456"),
            ]
        )
    )

    found = await discover_scoped(db, "algo")

    # Spelled the way the platform spells itself, not the way the domain does — the same
    # canonical names the network picker offers, so the two never disagree.
    assert [r.network for r in found.results] == ["DramaBox", "ReelShort"]


async def test_the_wide_search_labels_platforms_too(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """Who published a page has nothing to do with whether the search was narrowed.

    Making the label depend on the platforms toggle would mean the same result carries a
    network in one search and not in the other, for no reason the admin could see.
    """
    await configure(db)
    serve_searxng(serve([row("https://www.netshort.com/detail/9")]))

    found = await discover_scoped(db, "algo")  # no domains: the whole web

    assert found.narrowed is False
    assert found.results[0].network == "NetShort"


async def test_a_page_that_is_not_a_platform_claims_nothing(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """IMDb is a reference database, not a network. Guessing here would write a wrong value
    into a field an admin would then have to notice and undo."""
    await configure(db)
    serve_searxng(
        serve([row("https://www.imdb.com/title/tt1234567/"), row("https://example.com/x")])
    )

    found = await discover_scoped(db, "algo")

    assert [r.network for r in found.results] == [None, None]


async def test_a_lookalike_domain_is_not_the_platform(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """`notdramabox.com` contains the name and is not the platform. The same hole the host
    filter closes, closed again here — this one writes to a column."""
    await configure(db)
    serve_searxng(serve([row("https://notdramabox.com/drama/1")]))

    assert (await discover_scoped(db, "algo")).results[0].network is None


async def test_the_households_own_list_decides(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """A configured list replaces the built-in table rather than extending it.

    That is what makes the settings screen mean something: an admin who removed a platform
    should stop seeing it proposed, and one who added their own should see theirs.
    """
    from app.services.vocab import Provider

    await configure(db)
    serve_searxng(
        serve([row("https://mi-plataforma.com/x"), row("https://www.dramabox.com/drama/1")])
    )

    found = await discover_scoped(
        db, "algo", providers=[Provider(name="Mi Plataforma", domain="mi-plataforma.com")]
    )

    assert [r.network for r in found.results] == ["Mi Plataforma", None]


async def test_the_route_carries_the_network_to_the_client(
    db: AsyncSession, client: AsyncClient, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """End to end, because the label is resolved in the API layer: the provider list reads
    settings, and settings must be read off the session before any network call starts."""
    await configure(db)
    serve_searxng(serve([row("https://www.dramabox.com/drama/123")]))

    response = await client.get("/api/library/discover", params={"q": "algo"})

    assert response.status_code == 200
    assert response.json()["results"][0]["network"] == "DramaBox"
