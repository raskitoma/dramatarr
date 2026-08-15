"""SearXNG as the artwork picker's image-search provider.

Four things are defended here, and the first one is the reason the file exists.

**The JSON API is off by default.** SearXNG ships with `search.formats` set to `html` only, so
the very first request a freshly configured instance receives comes back as an HTML page or as
a bare 403. A client that reports that as "the provider returned an error" costs the admin an
afternoon; one that says *add "json" to search.formats in settings.yml and restart* costs them
five minutes. Both shapes of that failure are tested, and so is the text of the message.

**The two URLs in play are not the same kind of URL.** `base_url` is trusted — an admin typed
it and a self-hosted instance normally answers on `10.x` — so it must reach the network guard
never. The `img_src` values inside its results came from the open internet through a search
engine and must reach the guard always. `test_a_private_base_url_is_allowed` and
`test_a_result_pointing_inside_the_network_is_refused_at_download` are the same distinction
from its two sides; either one passing alone would mean nothing.

**Results are normalized, not passed through.** `resolution` is a free-text field engines fill
in as they please, so `"1280x720"`, a multiplication sign, `"unknown"` and a missing key all
have to land somewhere sensible.

**Unconfigured is a state, not a failure.** A provider named but not filled in leaves the
picker working with TMDB alone.

No test here reaches a real instance: every HTTP client the code under test would build is
replaced by an `httpx.MockTransport`, and the one test that resolves a name never gets past
the address check.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Series, User
from app.services import images as images_service

pytestmark = pytest.mark.anyio

#: The instance in these tests. A private address on purpose — that is where a self-hosted
#: SearXNG lives, and it is precisely what the artwork download guard refuses for *results*.
BASE_URL = "http://10.0.50.30:8080"

#: One SearXNG image result, with every field the normalizer reads.
SAMPLE_RESULT = {
    "template": "images.html",
    "url": "https://fansite.example/ruta-de-cristal",
    "title": "Ruta de Cristal (2024) poster",
    "img_src": "https://images.example/ruta-poster.jpg",
    "thumbnail_src": "/image_proxy?url=https%3A%2F%2Fimages.example%2Fruta-poster.jpg",
    "resolution": "1000x1500",
    "img_format": "jpeg",
    "source": "fansite.example",
    "engine": "bing images",
}

#: `/config` as a modern instance answers it, trimmed to the fields that are read. The
#: non-image engines are here to be filtered out, and `wikidata` carries several categories so
#: the filter cannot be a naive equality check.
CONFIG_PAYLOAD: dict[str, Any] = {
    "version": "2025.7.2+1a2b3c4",
    "brand": {"PRODUCT_NAME": "SearXNG"},
    "engines": [
        {"name": "bing images", "categories": ["images"], "enabled": True, "shortcut": "bii"},
        {"name": "google images", "categories": ["images"], "enabled": False, "shortcut": "goi"},
        {"name": "duckduckgo", "categories": ["general", "web"], "enabled": True},
        {"name": "wikidata", "categories": ["general", "images"], "shortcut": "wd"},
        {"name": "youtube", "categories": ["videos"], "enabled": True},
        "not-an-engine",
    ],
}


# --- Fixtures ---------------------------------------------------------------------------------


def mock_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> AsyncClient:
    return AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


@pytest.fixture
def serve_searxng(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    """Install a fake instance and record every request the provider makes to it.

    Patches the *shared* client, which is the one the provider uses. The guarded client — the
    one with the address checks — is deliberately left alone: nothing in this module's SearXNG
    path is supposed to touch it.
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


def json_response(payload: Any, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def searxng_routes(
    *,
    results: list[Any] | None = None,
    config: dict[str, Any] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that answers `/search` and `/config` the way a working instance does."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return json_response({"query": "x", "results": results if results is not None else []})
        if request.url.path == "/config":
            return json_response(config if config is not None else CONFIG_PAYLOAD)
        return httpx.Response(404, text="not found")

    return handler


@pytest.fixture
async def series(db: AsyncSession) -> Series:
    row = Series(
        title="Ruta de Cristal",
        slug="ruta-de-cristal",
        sort_title="ruta de cristal",
        library_path="Ruta de Cristal (2024)",
        year=2024,
    )
    db.add(row)
    await db.commit()
    return row


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
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def configure(
    db: AsyncSession,
    *,
    provider: str = "searxng",
    base_url: str = BASE_URL,
    **extra: Any,
) -> None:
    from app.services.settings import SettingsService

    settings = SettingsService(db)
    await settings.set("images.search.provider", provider)
    await settings.set("images.search.base_url", base_url)
    for key, value in extra.items():
        await settings.set(f"images.search.{key}", value)


def error_code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


def error_message(response: httpx.Response) -> str:
    return str(response.json()["error"]["message"])


def web_source(body: dict[str, Any]) -> dict[str, Any]:
    return {item["name"]: item for item in body["sources"]}["web"]


# --- Settings -----------------------------------------------------------------------------------


def test_settings_defaults_are_provider_neutral() -> None:
    """The keys a SearXNG install needs exist, and the Google-only one is still there."""
    from app.services.settings import DEFAULTS, SECRET_KEYS

    assert DEFAULTS["images.search.provider"] == ""
    assert DEFAULTS["images.search.base_url"] == ""
    assert DEFAULTS["images.search.engines"] == ""
    assert DEFAULTS["images.search.safe_search"] == 1
    assert DEFAULTS["images.search.language"] == ""
    assert DEFAULTS["images.search.timeout"] == 15
    # Google keeps working; the provider is a setting, not a rewrite.
    assert DEFAULTS["images.search.cse_id"] == ""
    # Optional for SearXNG, required for Google, secret either way.
    assert DEFAULTS["images.search.api_key"] == ""
    assert "images.search.api_key" in SECRET_KEYS


def test_base_url_is_normalized_the_way_admins_type_it() -> None:
    from app.services.images import normalize_base_url

    assert normalize_base_url("http://10.0.50.30:8080") == BASE_URL
    assert normalize_base_url("  http://10.0.50.30:8080/  ") == BASE_URL
    # Read off a docker-compose, with no scheme.
    assert normalize_base_url("10.0.50.30:8080") == BASE_URL
    # Copied out of the browser's address bar, where the path is /search.
    assert normalize_base_url("http://10.0.50.30:8080/search") == BASE_URL
    assert normalize_base_url("https://searx.example/") == "https://searx.example"
    assert normalize_base_url("   ") == ""


def test_engine_lists_are_parsed_forgivingly() -> None:
    from app.services.images import parse_engines

    assert parse_engines("bing images, google images") == ("bing images", "google images")
    assert parse_engines(" bing images ,, ,bing images ") == ("bing images",)
    assert parse_engines("") == ()


async def test_provider_is_unavailable_rather_than_broken_when_unconfigured(
    db: AsyncSession, client: AsyncClient, series: Series
) -> None:
    """A half-filled settings form must not turn the artwork picker into a 502.

    The picker is the screen an admin is *on* while configuring this. Failing it closed would
    hide TMDB's gallery behind a provider that is not set up yet.
    """
    from app.services.images import get_image_search
    from app.services.settings import SettingsService

    assert await get_image_search(db) is None

    await SettingsService(db).set("images.search.provider", "searxng")
    assert await get_image_search(db) is None, "a provider with no base URL is not configured"

    response = await client.get(f"/api/library/series/{series.id}/artwork")
    assert response.status_code == 200, response.text
    source = web_source(response.json())
    assert source["available"] is False
    assert source["reason"] == "NOT_CONFIGURED"

    await SettingsService(db).set("images.search.base_url", BASE_URL)
    provider = await get_image_search(db)
    assert provider is not None
    assert provider.name == "searxng"


async def test_google_cse_is_still_selectable(db: AsyncSession) -> None:
    """Leaving the old provider in place costs nothing; removing it would strand an install."""
    from app.services.images import GoogleCseImageSearch, get_image_search

    await configure(db, provider="google_cse", base_url="", api_key="key", cse_id="cx")
    provider = await get_image_search(db)
    assert isinstance(provider, GoogleCseImageSearch)
    assert provider.name == "google_cse"


async def test_switching_provider_forgets_the_other_providers_key(
    db: AsyncSession, client: AsyncClient, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """A Google API key must never be handed to the household's SearXNG instance.

    One settings slot serves both providers, so without this the key an admin pasted for
    `googleapis.com` leaves as `Authorization: Bearer …` to a host that never needed it — a live
    third-party credential disclosed by a dropdown change. The non-secret fields stay: `cse_id`
    is not a credential and switching back should not mean retyping everything.
    """
    from app.services.settings import SettingsService

    await configure(db, provider="google_cse", base_url="", api_key="google-key", cse_id="cx")

    response = await client.put(
        "/api/admin/settings",
        json={"images.search.provider": "searxng", "images.search.base_url": BASE_URL},
    )
    assert response.status_code == 200, response.text
    assert response.json()["images.search.api_key"] == ""
    assert response.json()["images.search.cse_id"] == "cx"
    assert await SettingsService(db).get_str("images.search.api_key") == ""

    seen = serve_searxng(searxng_routes(results=[]))
    provider = await images_service.get_image_search(db)
    assert provider is not None
    await provider.search("ruta")
    assert "authorization" not in seen[0].headers


async def test_switching_provider_keeps_a_key_supplied_in_the_same_save(
    db: AsyncSession, client: AsyncClient
) -> None:
    """The clear is about the *old* provider's secret, not about the save that replaces it."""
    from app.services.settings import SettingsService

    await configure(db, provider="google_cse", base_url="", api_key="google-key", cse_id="cx")

    response = await client.put(
        "/api/admin/settings",
        json={
            "images.search.provider": "searxng",
            "images.search.base_url": BASE_URL,
            "images.search.api_key": "searxng-proxy-token",
        },
    )
    assert response.status_code == 200, response.text
    assert await SettingsService(db).get_str("images.search.api_key") == "searxng-proxy-token"


async def test_saving_without_changing_provider_keeps_the_key(
    db: AsyncSession, client: AsyncClient
) -> None:
    """Every other settings save must leave the stored secret exactly where it is."""
    from app.services.settings import REDACTED, SettingsService

    await configure(db, api_key="searxng-proxy-token")

    response = await client.put(
        "/api/admin/settings",
        json={
            "images.search.provider": "searxng",
            "images.search.language": "es",
            "images.search.api_key": REDACTED,
        },
    )
    assert response.status_code == 200, response.text
    assert await SettingsService(db).get_str("images.search.api_key") == "searxng-proxy-token"


# --- The request ---------------------------------------------------------------------------------


async def test_the_query_is_an_images_search_in_json(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    from app.services.images import get_image_search

    await configure(db)
    seen = serve_searxng(searxng_routes(results=[SAMPLE_RESULT]))

    provider = await get_image_search(db)
    assert provider is not None
    await provider.search("ruta de cristal poster")

    request = seen[0]
    assert str(request.url).startswith(f"{BASE_URL}/search")
    assert request.url.params["q"] == "ruta de cristal poster"
    assert request.url.params["format"] == "json"
    assert request.url.params["categories"] == "images"
    assert request.url.params["safesearch"] == "1"
    # Omitted, not sent empty: an empty `engines` means "no engines at all" to SearXNG, and an
    # empty `language` would override the instance's own default with nothing.
    assert "engines" not in request.url.params
    assert "language" not in request.url.params
    assert request.headers["user-agent"] == images_service.SEARXNG_USER_AGENT
    assert "authorization" not in request.headers


async def test_configured_engines_language_and_safe_search_are_sent(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """Whichever of the instance's engines the admin chose, and nothing else."""
    from app.services.images import get_image_search

    await configure(
        db,
        engines="bing images, duckduckgo images",
        language="es",
        safe_search=2,
    )
    seen = serve_searxng(searxng_routes(results=[]))

    provider = await get_image_search(db)
    assert provider is not None
    await provider.search("ruta")

    request = seen[0]
    assert request.url.params["engines"] == "bing images,duckduckgo images"
    assert request.url.params["language"] == "es"
    assert request.url.params["safesearch"] == "2"


@pytest.mark.parametrize("api_key", ["", "proxy-token"])
async def test_the_api_key_rides_as_a_bearer_header_only_when_set(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]], api_key: str
) -> None:
    """SearXNG itself needs no key; the ones behind an authenticating proxy do.

    An empty bearer header is worse than none — some proxies reject it outright — so an
    ordinary instance must never see the header at all.
    """
    from app.services.images import get_image_search

    await configure(db, api_key=api_key)
    seen = serve_searxng(searxng_routes(results=[]))

    provider = await get_image_search(db)
    assert provider is not None
    await provider.search("ruta")

    if api_key:
        assert seen[0].headers["authorization"] == "Bearer proxy-token"
    else:
        assert "authorization" not in seen[0].headers


async def test_an_empty_query_never_leaves_the_process(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    from app.services.images import get_image_search

    await configure(db)
    seen = serve_searxng(searxng_routes(results=[SAMPLE_RESULT]))

    provider = await get_image_search(db)
    assert provider is not None
    assert await provider.search("   ") == []
    assert not seen


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # What the settings screen writes.
        ("off", "0"),
        ("moderate", "1"),
        ("strict", "2"),
        ("STRICT", "2"),
        # What `DEFAULTS` holds and what a hand-edited row looks like.
        (0, "0"),
        (2, "2"),
        ("2", "2"),
        # Unreadable: moderate, never wider than the admin asked for.
        ("", "1"),
        ("aggressive", "1"),
        (True, "1"),
        (9, "2"),
        (-3, "0"),
    ],
)
async def test_the_stored_safe_search_level_reaches_the_instance(
    db: AsyncSession,
    serve_searxng: Callable[..., list[httpx.Request]],
    stored: Any,
    expected: str,
) -> None:
    """The UI stores a *name*; SearXNG's API only takes 0/1/2.

    Without the translation this is the worst kind of bug: the row reads back exactly as the
    admin set it, the screen shows `strict`, and every query still goes out at moderate. So both
    spellings are asserted end to end, on the outgoing request rather than on the config object.
    """
    from app.services.images import get_image_search

    await configure(db, safe_search=stored)
    seen = serve_searxng(searxng_routes(results=[]))

    provider = await get_image_search(db)
    assert provider is not None
    await provider.search("ruta")

    assert seen[0].url.params["safesearch"] == expected


# --- Normalization -------------------------------------------------------------------------------


async def test_results_become_the_same_candidates_tmdb_produces(
    db: AsyncSession,
    client: AsyncClient,
    series: Series,
    serve_searxng: Callable[..., list[httpx.Request]],
) -> None:
    """One shape on the wire, so the picker renders both sources without special-casing."""
    await configure(db)
    serve_searxng(
        searxng_routes(
            results=[
                SAMPLE_RESULT,
                # No resolution at all, and no thumbnail either.
                {
                    "img_src": "https://images.example/plain.png",
                    "title": "Plain",
                    "engine": "google images",
                },
                # A resolution no parser should guess at, and one written with U+00D7.
                {"img_src": "https://images.example/odd.jpg", "resolution": "unknown"},
                {"img_src": "https://images.example/times.jpg", "resolution": "1280 \u00d7 720"},
                # Skipped: no image to download.
                {"title": "no image here", "thumbnail_src": "/image_proxy?url=x"},
                {"img_src": "", "title": "empty"},
                # Skipped: a proxied path resolves to the instance itself, and the download
                # guard refuses a private address by design.
                {"img_src": "/image_proxy?url=whatever", "title": "relative"},
                "not-a-dict",
            ]
        )
    )

    response = await client.get(f"/api/library/series/{series.id}/artwork?q=ruta")
    assert response.status_code == 200, response.text
    body = response.json()
    web = body["web"]

    assert [item["url"] for item in web] == [
        "https://images.example/ruta-poster.jpg",
        "https://images.example/plain.png",
        "https://images.example/odd.jpg",
        "https://images.example/times.jpg",
    ], "results without a usable img_src are not candidates"

    first = web[0]
    assert first["source"] == "searxng"
    assert first["engine"] == "bing images"
    assert first["title"] == "Ruta de Cristal (2024) poster"
    assert first["contextUrl"] == "https://fansite.example/ruta-de-cristal"
    assert (first["width"], first["height"]) == (1000, 1500)
    # SearXNG reports its proxied thumbnail site-relative. The *browser* loads this one, and
    # the admin's browser is on the same network as the instance, so it is resolved against
    # the base URL rather than dropped.
    assert first["thumbnailUrl"] == (
        f"{BASE_URL}/image_proxy?url=https%3A%2F%2Fimages.example%2Fruta-poster.jpg"
    )
    # No declared kind: the provider does not know a poster from a backdrop, the admin does.
    assert first["kind"] == ""

    plain = web[1]
    assert plain["width"] is None and plain["height"] is None
    # A result with no thumbnail falls back to the image itself rather than to null.
    assert plain["thumbnailUrl"] == "https://images.example/plain.png"
    assert plain["contextUrl"] is None

    assert (web[2]["width"], web[2]["height"]) == (None, None), "'unknown' is not a resolution"
    assert (web[3]["width"], web[3]["height"]) == (1280, 720)

    source = web_source(body)
    assert source["available"] is True
    assert source["provider"] == "searxng"
    assert source["count"] == 4


async def test_the_result_limit_is_honoured(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    from app.services.images import get_image_search

    await configure(db)
    serve_searxng(
        searxng_routes(
            results=[
                dict(SAMPLE_RESULT, img_src=f"https://images.example/{n}.jpg") for n in range(30)
            ]
        )
    )
    provider = await get_image_search(db)
    assert provider is not None
    assert len(await provider.search("ruta", limit=4)) == 4


# --- The failure that must be diagnosed, not guessed at ----------------------------------------


async def test_json_disabled_is_recognized_from_an_html_body(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """The default state of a fresh SearXNG: it answers format=json with its own search page.

    HTTP 200, an HTML body, and nothing that reads like an error — which is exactly why a
    naive client reports something useless here.
    """
    from app.errors import AppError
    from app.services.images import get_image_search

    await configure(db)
    serve_searxng(
        lambda request: httpx.Response(
            200,
            html="<!DOCTYPE html><html><body><form id='search'></form></body></html>",
        )
    )

    provider = await get_image_search(db)
    assert provider is not None
    with pytest.raises(AppError) as raised:
        await provider.search("ruta")

    assert raised.value.code == "SEARXNG_JSON_DISABLED"
    message = raised.value.message
    assert "search.formats" in message, "the message has to name the setting"
    assert "settings.yml" in message, "and the file it lives in"
    assert "restart" in message.lower()
    assert raised.value.details["fix"]


async def test_json_disabled_is_recognized_from_a_403(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """The other shape of the same misconfiguration, from a differently built instance."""
    from app.errors import AppError
    from app.services.images import get_image_search

    await configure(db)
    serve_searxng(lambda request: httpx.Response(403, text="Forbidden"))

    provider = await get_image_search(db)
    assert provider is not None
    with pytest.raises(AppError) as raised:
        await provider.search("ruta")

    assert raised.value.code == "SEARXNG_JSON_DISABLED"
    assert "search.formats" in raised.value.message


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("the instance never answered"),
    ],
    ids=["refused", "connect-timeout", "read-timeout"],
)
async def test_an_unreachable_instance_says_so(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]], failure: Exception
) -> None:
    from app.errors import AppError
    from app.services.images import get_image_search

    await configure(db)

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    serve_searxng(handler)

    provider = await get_image_search(db)
    assert provider is not None
    with pytest.raises(AppError) as raised:
        await provider.search("ruta")

    assert raised.value.code == "SEARXNG_UNREACHABLE"
    assert BASE_URL in raised.value.message


async def test_malformed_json_is_not_blamed_on_the_format_setting(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """A body that declares JSON and then does not parse is a different bug.

    Reporting it as SEARXNG_JSON_DISABLED would send the admin to edit a settings.yml that is
    already correct, which is the failure mode this whole error path exists to avoid.
    """
    from app.errors import AppError
    from app.services.images import get_image_search

    await configure(db)
    serve_searxng(
        lambda request: httpx.Response(
            200,
            content=b'{"results": [{"img_src": "https://images.example/a.jpg"',
            headers={"content-type": "application/json"},
        )
    )

    provider = await get_image_search(db)
    assert provider is not None
    with pytest.raises(AppError) as raised:
        await provider.search("ruta")
    assert raised.value.code == "SEARXNG_BAD_RESPONSE"


async def test_a_wrong_path_is_not_blamed_on_the_format_setting_either(
    db: AsyncSession, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """`/search` exists whatever `search.formats` says, so a 404 means the base URL is wrong."""
    from app.errors import AppError
    from app.services.images import get_image_search

    await configure(db, base_url="http://10.0.50.30:8080/searxng")
    serve_searxng(lambda request: httpx.Response(404, html="<html>404</html>"))

    provider = await get_image_search(db)
    assert provider is not None
    with pytest.raises(AppError) as raised:
        await provider.search("ruta")
    assert raised.value.code == "SEARXNG_BAD_RESPONSE"
    assert "base URL" in raised.value.message


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
    from app.services.images import get_image_search

    await configure(db)
    serve_searxng(lambda request: json_response(payload))

    provider = await get_image_search(db)
    assert provider is not None
    with pytest.raises(AppError) as raised:
        await provider.search("ruta")
    assert raised.value.code == "SEARXNG_BAD_RESPONSE"


async def test_a_provider_failure_degrades_the_picker_instead_of_breaking_it(
    db: AsyncSession,
    client: AsyncClient,
    series: Series,
    serve_searxng: Callable[..., list[httpx.Request]],
) -> None:
    """On the artwork screen the diagnosis belongs to test-search, not to a 502 in the grid."""
    await configure(db)
    serve_searxng(lambda request: httpx.Response(200, html="<html></html>"))

    response = await client.get(f"/api/library/series/{series.id}/artwork")
    assert response.status_code == 200, response.text
    source = web_source(response.json())
    assert source["available"] is False
    assert source["reason"] == "PROVIDER_ERROR"
    assert source["provider"] == "searxng"


# --- SSRF: one rule for the instance, another for what it finds --------------------------------


async def test_a_private_base_url_is_allowed(
    db: AsyncSession,
    client: AsyncClient,
    series: Series,
    serve_searxng: Callable[..., list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured instance is trusted, and it lives on 10.x. That is the normal case.

    Two assertions, and the pair is the point: the same address that the artwork download guard
    refuses outright must be reachable here. Applying one rule to both URLs is the obvious
    refactor and it would break every self-hosted setup.
    """
    from app.errors import AppError

    await configure(db, base_url=BASE_URL)

    def never(url: str) -> None:
        raise AssertionError(f"the address guard must not run for the configured instance: {url}")

    monkeypatch.setattr(images_service, "assert_public_url", never)
    serve_searxng(searxng_routes(results=[SAMPLE_RESULT]))

    response = await client.get(f"/api/library/series/{series.id}/artwork")
    assert response.status_code == 200, response.text
    assert web_source(response.json())["available"] is True
    assert len(response.json()["web"]) == 1

    # And the other side of the split: as a *download* target, that same host is refused.
    monkeypatch.undo()
    with pytest.raises(AppError) as raised:
        await images_service.assert_public_url(f"{BASE_URL}/image_proxy")
    assert raised.value.code == "ARTWORK_URL_BLOCKED"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/poster.jpg",
        "http://10.0.50.30:8080/image_proxy?url=x",
        "http://169.254.169.254/latest/meta-data/poster.jpg",
        "http://192.168.1.1/poster.jpg",
        "http://[::1]/poster.jpg",
    ],
)
async def test_a_result_pointing_inside_the_network_is_refused_at_download(
    db: AsyncSession, client: AsyncClient, series: Series, url: str
) -> None:
    """A search result is not trusted just because a trusted instance handed it over.

    `img_src` is whatever an engine crawled off the open internet. Applying a chosen candidate
    goes through the same guard a pasted link does — including for the instance's own address,
    which is why the second case here is the SearXNG host itself.
    """
    await configure(db)
    response = await client.post(
        f"/api/library/series/{series.id}/artwork", json={"kind": "poster", "url": url}
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "ARTWORK_URL_BLOCKED"


async def test_a_result_on_a_non_http_scheme_is_refused_at_download(
    db: AsyncSession, client: AsyncClient, series: Series
) -> None:
    await configure(db)
    response = await client.post(
        f"/api/library/series/{series.id}/artwork",
        json={"kind": "poster", "url": "file:///etc/passwd"},
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "ARTWORK_URL_INVALID"


# --- The diagnostic ----------------------------------------------------------------------------


async def test_test_search_reports_what_the_instance_is(
    db: AsyncSession, client: AsyncClient, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    await configure(db)
    seen = serve_searxng(searxng_routes(results=[SAMPLE_RESULT, SAMPLE_RESULT]))

    response = await client.post("/api/admin/settings/test-search")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["ok"] is True
    assert body["provider"] == "searxng"
    assert body["baseUrl"] == BASE_URL
    assert body["version"] == "2025.7.2+1a2b3c4"
    # bing images, google images and wikidata — not duckduckgo, not youtube.
    assert body["imageEngineCount"] == 3
    assert body["sampleQuery"] == images_service.SEARXNG_SAMPLE_QUERY
    assert body["sampleResultCount"] == 2

    paths = [request.url.path for request in seen]
    assert paths[0] == "/search", "the JSON check comes first; it is what this endpoint is for"
    assert "/config" in paths


async def test_test_search_still_answers_when_config_is_unavailable(
    db: AsyncSession, client: AsyncClient, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """Search works, `/config` does not — a worse diagnostic, not a failed one."""
    await configure(db)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return json_response({"results": [SAMPLE_RESULT]})
        return httpx.Response(404, html="<html>nope</html>")

    serve_searxng(handler)

    response = await client.post("/api/admin/settings/test-search")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["version"] is None
    assert body["imageEngineCount"] == 0
    assert body["sampleResultCount"] == 1


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (lambda request: httpx.Response(200, html="<html></html>"), "SEARXNG_JSON_DISABLED"),
        (lambda request: httpx.Response(403, text="Forbidden"), "SEARXNG_JSON_DISABLED"),
        (lambda request: json_response({"nope": True}), "SEARXNG_BAD_RESPONSE"),
        (lambda request: httpx.Response(500, json={"error": "boom"}), "SEARXNG_BAD_RESPONSE"),
    ],
    ids=["html", "403", "wrong-shape", "server-error"],
)
async def test_test_search_distinguishes_its_failures(
    db: AsyncSession,
    client: AsyncClient,
    serve_searxng: Callable[..., list[httpx.Request]],
    handler: Callable[[httpx.Request], httpx.Response],
    expected: str,
) -> None:
    """Four failures, four repairs. One flat "test failed" would make the admin guess."""
    await configure(db)
    serve_searxng(handler)

    response = await client.post("/api/admin/settings/test-search")
    assert response.status_code == 502, response.text
    assert error_code(response) == expected


async def test_test_search_reports_an_unreachable_instance(
    db: AsyncSession, client: AsyncClient, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    await configure(db)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    serve_searxng(handler)

    response = await client.post("/api/admin/settings/test-search")
    assert response.status_code == 502, response.text
    assert error_code(response) == "SEARXNG_UNREACHABLE"


async def test_test_search_needs_a_provider_and_a_base_url(
    db: AsyncSession, client: AsyncClient
) -> None:
    from app.services.settings import SettingsService

    response = await client.post("/api/admin/settings/test-search")
    assert response.status_code == 400, response.text
    assert error_code(response) == "SEARCH_NOT_CONFIGURED"

    await SettingsService(db).set("images.search.provider", "searxng")
    response = await client.post("/api/admin/settings/test-search")
    assert response.status_code == 400, response.text
    assert error_code(response) == "SEARCH_NOT_CONFIGURED"
    assert "base_url" in error_message(response)


# --- The engine list ---------------------------------------------------------------------------


async def test_engines_lists_only_image_engines_with_their_state(
    db: AsyncSession, client: AsyncClient, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """What lets an admin pick engine names instead of guessing them.

    A mistyped engine name is a silent no-op in SearXNG — the search simply returns nothing —
    so the list has to come from the instance, and it has to say which ones are switched off.
    """
    await configure(db)
    seen = serve_searxng(searxng_routes())

    response = await client.get("/api/admin/search/engines")
    assert response.status_code == 200, response.text
    body = response.json()

    assert seen[0].url.path == "/config"
    assert body["provider"] == "searxng"
    assert body["baseUrl"] == BASE_URL
    assert body["version"] == "2025.7.2+1a2b3c4"

    names = [engine["name"] for engine in body["engines"]]
    assert names == ["bing images", "wikidata", "google images"], "enabled first, then by name"
    assert "duckduckgo" not in names, "a general-category engine is not an image engine"
    assert "youtube" not in names

    by_name = {engine["name"]: engine for engine in body["engines"]}
    assert by_name["bing images"]["enabled"] is True
    assert by_name["bing images"]["shortcut"] == "bii"
    assert by_name["google images"]["enabled"] is False
    # An entry with no `enabled` key is one the instance has on.
    assert by_name["wikidata"]["enabled"] is True


async def test_engines_needs_searxng_to_be_the_provider(
    db: AsyncSession, client: AsyncClient
) -> None:
    await configure(db, provider="google_cse", base_url="", api_key="key", cse_id="cx")
    response = await client.get("/api/admin/search/engines")
    assert response.status_code == 400, response.text
    assert error_code(response) == "SEARCH_NOT_CONFIGURED"


async def test_engines_reports_a_json_disabled_instance_as_a_bad_response(
    db: AsyncSession, client: AsyncClient, serve_searxng: Callable[..., list[httpx.Request]]
) -> None:
    """`/config` is not gated by `search.formats`, so HTML here means a different problem.

    Pointing at the wrong service, most likely. Saying "enable json in settings.yml" would be
    a confident wrong answer, so this path deliberately does not.
    """
    await configure(db)
    serve_searxng(lambda request: httpx.Response(200, html="<html>a login page</html>"))

    response = await client.get("/api/admin/search/engines")
    assert response.status_code == 502, response.text
    assert error_code(response) == "SEARXNG_BAD_RESPONSE"
