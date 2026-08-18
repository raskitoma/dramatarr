"""The wishlist search asks DramaDex twice — once by title and once by person.

Short drama is retitled between platforms and often between weeks; the cast is the part that
holds still. DramaDex answers `?person=` on a partial, case-insensitive name over the ~2300
titles whose credits it has extracted, so this costs one extra call and reaches shows the
household does not own and could not otherwise name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import dramadex

pytestmark = pytest.mark.anyio


def row(title_id: int, name: str) -> dict[str, Any]:
    return {
        "id": title_id,
        "primary_title": name,
        "year": 2025,
        "synopsis": f"{name}, a synopsis.",
        "studios": [
            {
                "key": "reelshort",
                "name": "ReelShort",
                "external_id": str(title_id),
                "watch_url": f"https://www.reelshort.com/movie/{title_id}",
            }
        ],
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


async def a_member(db: AsyncSession, email: str):
    from app.models import User, UserRole
    from app.services.settings import SettingsService

    user = User(email=email, display_name="V", role=UserRole.VIEWER, locale="es")
    db.add(user)
    svc = SettingsService(db)
    await svc.set(dramadex.BASE_URL_SETTING, "http://dramadex.test:8099")
    await svc.set(dramadex.API_KEY_SETTING, "k")
    await db.commit()
    SettingsService.invalidate()
    return user


def params_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


async def test_the_indexer_is_asked_by_person_as_well_as_by_title(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        asked = params_of(request)
        if "person" in asked:
            return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})
        return httpx.Response(200, json={"items": []})

    seen = serve(handler)
    user = await a_member(db, "p1@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Adam Daniel")

    asked_by = {tuple(sorted(params_of(r))) for r in seen}
    assert any("person" in key for key in asked_by), "never asked by person"
    assert any("q" in key for key in asked_by), "stopped asking by title"
    assert [card.title for card in found.results] == ["Feast of Power"]


async def test_a_card_found_by_the_cast_says_whose_name_it_was(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    """A row that looks nothing like what was typed is noise unless it explains itself."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "person" in params_of(request):
            return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})
        return httpx.Response(200, json={"items": []})

    serve(handler)
    user = await a_member(db, "p2@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Adam Daniel")
    assert found.results[0].matched_person == "Adam Daniel"


async def test_a_title_both_searches_return_appears_once(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})

    serve(handler)
    user = await a_member(db, "p3@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Feast")
    assert [card.key for card in found.results] == ["dramadex:7"]
    # Reported as a title match, because that is the search that reached it first.
    assert found.results[0].matched_person == ""


async def test_cast_matches_are_not_pushed_off_a_full_page(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    """A page full of title hits must still leave room, or the second search is invisible."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "person" in params_of(request):
            return httpx.Response(200, json={"items": [row(900 + n, f"Cast {n}") for n in range(6)]})
        return httpx.Response(200, json={"items": [row(n, f"Title {n}") for n in range(12)]})

    serve(handler)
    user = await a_member(db, "p4@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Adam", limit=12)
    by_cast = [card for card in found.results if card.matched_person]
    assert len(found.results) == 12
    assert len(by_cast) == 6


async def test_one_search_failing_does_not_take_the_other_down(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "person" in params_of(request):
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})

    serve(handler)
    user = await a_member(db, "p5@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Feast")
    assert [card.title for card in found.results] == ["Feast of Power"]
