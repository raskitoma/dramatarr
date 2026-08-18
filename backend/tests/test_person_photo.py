"""Actor photos: DramaDex has them, and dramatarr threw them away.

The indexer grew `photo_url` on its person payload and a `/people/{id}/photo` route serving
WebP bytes. `dramadex.Person` had no field for it, so the parse dropped it the instant it
arrived and no picture could reach a screen no matter what the frontend did.

The second half of this file is a bug that was already live and invisible: the *title* poster
proxy was admin-only, while `_hit` stamps its URL onto every credit on the member-facing person
page and onto every wishlist card. Every non-admin has been getting 403 and a blank rectangle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services import dramadex

pytestmark = pytest.mark.anyio

PHOTO = b"RIFF\x00\x00\x00\x00WEBPVP8 fake-bytes"


def person_payload(**overrides: Any) -> dict[str, Any]:
    """A real `/people/{id}` body, including the field this file exists for.

    The id is 43 while the *request* is for 273: DramaDex de-duplicated its people, so asking
    for an alias answers with the canonical row.
    """
    return {
        "id": 43,
        "name": "Addison Bowman",
        "bio": "Addison Bowman is a Los Angeles-based actor and model.",
        "photo_url": "/people/43/photo",
        "external_ids": {"reelshort": "https://www.reelshort.com/fandom/addison-bowman-01/"},
        "titles": [],
        **overrides,
    }


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        class Fake(httpx.AsyncClient):
            def __init__(self, *a: Any, **kw: Any) -> None:
                kw["transport"] = httpx.MockTransport(wrapped)
                super().__init__(*a, **kw)

        monkeypatch.setattr(dramadex.httpx, "AsyncClient", Fake)
        return seen

    return install


def client(base: str = "http://dramadex.test:8099") -> dramadex.DramadexClient:
    return dramadex.DramadexClient(base, "k")


async def connect(db: AsyncSession) -> None:
    """Without this `require_client` raises DRAMADEX_NOT_CONFIGURED and a route test passes
    on the wrong branch."""
    from app.services.settings import SettingsService

    svc = SettingsService(db)
    await svc.set(dramadex.BASE_URL_SETTING, "http://dramadex.test:8099")
    await svc.set(dramadex.API_KEY_SETTING, "k")
    await db.commit()
    SettingsService.invalidate()


def _http(db: AsyncSession, user: User, *, admin: bool) -> AsyncClient:
    from app.api.deps import get_current_user, require_admin
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: user
    if admin:
        app.dependency_overrides[require_admin] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def viewer_http(db: AsyncSession, viewer_user: User) -> AsyncIterator[AsyncClient]:
    from app.main import app

    try:
        async with _http(db, viewer_user, admin=False) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


# --- The client ------------------------------------------------------------------------------


async def test_the_photo_path_survives_the_parse(
    serve: Callable[..., list[httpx.Request]],
) -> None:
    serve(lambda _r: httpx.Response(200, json=person_payload()))

    person = await client().person(273)

    assert person is not None
    assert person.photo_url == "/people/43/photo"


@pytest.mark.parametrize("raw", [{}, {"photo_url": ""}, {"photo_url": None}])
async def test_a_person_with_no_picture_parses_to_none(
    serve: Callable[..., list[httpx.Request]], raw: dict[str, Any]
) -> None:
    """About one credited performer in four. Absent and empty must both mean the same."""
    payload = person_payload()
    payload.pop("photo_url")
    payload.update(raw)
    serve(lambda _r: httpx.Response(200, json=payload))

    person = await client().person(43)

    assert person is not None
    assert person.photo_url is None


async def test_photo_bytes_gets_past_the_json_insistence(
    serve: Callable[..., list[httpx.Request]],
) -> None:
    """`_get` ends in `response.json()`; handing it image bytes raises DRAMADEX_BAD_RESPONSE,
    which is exactly what happened when this was first measured."""
    serve(lambda _r: httpx.Response(200, content=PHOTO, headers={"content-type": "image/webp"}))

    fetched = await client().photo_bytes(43)

    assert fetched == (PHOTO, "image/webp")


async def test_a_photoless_person_is_none_not_a_json_body(
    serve: Callable[..., list[httpx.Request]],
) -> None:
    serve(lambda _r: httpx.Response(404, json={"detail": "no photo"}))

    assert await client().photo_bytes(43) is None


async def test_an_unreachable_indexer_is_none(serve: Callable[..., list[httpx.Request]]) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    serve(boom)

    assert await client().photo_bytes(43) is None


async def test_the_path_is_built_not_taken_from_the_payload(
    serve: Callable[..., list[httpx.Request]],
) -> None:
    """DramaDex's own `photo_url` starts with a slash, so `urljoin`ing it would discard a base
    path prefix — latent today, silent the day the indexer moves behind a gateway."""
    seen = serve(
        lambda _r: httpx.Response(200, content=PHOTO, headers={"content-type": "image/webp"})
    )

    await client("http://dramadex.test:8099/api/v1/").photo_bytes(43)

    assert str(seen[0].url) == "http://dramadex.test:8099/api/v1/people/43/photo"


# --- The routes ------------------------------------------------------------------------------


async def test_a_member_can_load_an_actors_photo(
    db: AsyncSession, viewer_http: AsyncClient, serve: Callable[..., list[httpx.Request]]
) -> None:
    await connect(db)
    serve(lambda _r: httpx.Response(200, content=PHOTO, headers={"content-type": "image/webp"}))

    response = await viewer_http.get("/api/library/dramadex/people/43/photo")

    assert response.status_code == 200
    assert response.content == PHOTO
    assert response.headers["content-type"].startswith("image/webp")


async def test_a_member_can_load_a_credits_poster(
    db: AsyncSession, viewer_http: AsyncClient, serve: Callable[..., list[httpx.Request]]
) -> None:
    """The pre-existing bug. `_hit` puts this URL on every credit of the person page, which is
    open to any member — so while the route was admin-only it 403'd for everyone else, and the
    only symptom was a grey rectangle."""
    await connect(db)
    serve(lambda _r: httpx.Response(200, content=PHOTO, headers={"content-type": "image/webp"}))

    response = await viewer_http.get("/api/library/dramadex/titles/4242/poster")

    assert response.status_code == 200


async def test_a_person_with_no_picture_is_a_clean_404(
    db: AsyncSession, viewer_http: AsyncClient, serve: Callable[..., list[httpx.Request]]
) -> None:
    await connect(db)
    serve(lambda _r: httpx.Response(404, json={"detail": "no photo"}))

    response = await viewer_http.get("/api/library/dramadex/people/43/photo")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DRAMADEX_PERSON_PHOTO_NOT_FOUND"


async def test_the_person_route_hands_over_this_apps_path_not_the_indexers(
    db: AsyncSession, viewer_http: AsyncClient, serve: Callable[..., list[httpx.Request]]
) -> None:
    """Asserted on the JSON body, not the model: a field missing from `PersonCredits` is
    stripped by `response_model` while every test on the Python object still passes.

    And the id is the canonical one the indexer answered with, not the alias that was asked
    for, so two routes to the same actor share one browser cache entry.
    """
    await connect(db)
    serve(lambda _r: httpx.Response(200, json=person_payload()))

    response = await viewer_http.get("/api/library/dramadex/people/273")

    assert response.status_code == 200
    body = response.json()
    assert body["photoUrl"] == "/api/library/dramadex/people/43/photo"
    assert body["id"] == 43


async def test_no_picture_means_no_link(
    db: AsyncSession, viewer_http: AsyncClient, serve: Callable[..., list[httpx.Request]]
) -> None:
    """Null, not a path that would 404 on every render."""
    await connect(db)
    payload = person_payload()
    payload.pop("photo_url")
    serve(lambda _r: httpx.Response(200, json=payload))

    response = await viewer_http.get("/api/library/dramadex/people/43")

    assert response.json()["photoUrl"] is None
