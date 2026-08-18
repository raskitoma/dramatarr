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


def is_people(request: httpx.Request) -> bool:
    return urlsplit(str(request.url)).path.rstrip("/").endswith("/people")


def people(*names: str) -> httpx.Response:
    """`GET /people?q=` answers a bare list of shallow rows — id and name, no titles."""
    return httpx.Response(200, json=[{"id": 5 + n, "name": name} for n, name in enumerate(names)])


async def test_the_indexer_is_asked_by_person_as_well_as_by_title(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if is_people(request):
            return people("Adam Daniel")
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


async def test_a_card_names_the_actor_and_not_the_search_box(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    """The defect this resolves: `?person=` takes a partial name and never says which person
    it matched, so echoing the query printed "Reparto: «herr»" on twelve cards whose actual
    actor was Marc Herrmann every time."""

    def handler(request: httpx.Request) -> httpx.Response:
        if is_people(request):
            return people("Marc Herrmann")
        if "person" in params_of(request):
            return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})
        return httpx.Response(200, json={"items": []})

    serve(handler)
    user = await a_member(db, "p2@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "herr")
    assert found.results[0].matched_person == "Marc Herrmann"
    assert found.results[0].matched_by_cast is True


async def test_an_ambiguous_name_marks_the_card_without_naming_anybody(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    """`?person=` unions across everyone it matched and never says which title was whose, so
    picking one of several would be a guess. The card still says why it is here."""

    def handler(request: httpx.Request) -> httpx.Response:
        if is_people(request):
            return people("Samantha Drews", "Roman Chsherbakov")
        if "person" in params_of(request):
            return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})
        return httpx.Response(200, json={"items": []})

    serve(handler)
    user = await a_member(db, "p7@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "an")
    assert found.results[0].matched_person == ""
    assert found.results[0].matched_by_cast is True


async def test_the_people_lookup_failing_does_not_lose_the_cards(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if is_people(request):
            return httpx.Response(500, json={"detail": "boom"})
        if "person" in params_of(request):
            return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})
        return httpx.Response(200, json={"items": []})

    serve(handler)
    user = await a_member(db, "p8@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "herr")
    assert [card.title for card in found.results] == ["Feast of Power"]
    assert found.results[0].matched_by_cast is True
    assert found.results[0].matched_person == ""


async def test_a_title_both_searches_return_appears_once(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if is_people(request):
            return people("Feast Somebody")
        return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})

    serve(handler)
    user = await a_member(db, "p3@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Feast")
    assert [card.key for card in found.results] == ["dramadex:7"]
    # One card, and it still says the cast confirmed it — the title search reaching it first
    # is not a reason to hide that the query also names somebody in it.
    assert found.results[0].matched_person == "Feast Somebody"


async def test_cast_matches_are_not_pushed_off_a_full_page(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    """A page full of title hits must still leave room, or the second search is invisible."""

    def handler(request: httpx.Request) -> httpx.Response:
        if is_people(request):
            return people("Adam Daniel")
        if "person" in params_of(request):
            return httpx.Response(200, json={"items": [row(900 + n, f"Cast {n}") for n in range(6)]})
        return httpx.Response(200, json={"items": [row(n, f"Title {n}") for n in range(12)]})

    serve(handler)
    user = await a_member(db, "p4@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Adam", limit=12)
    by_cast = [card for card in found.results if card.matched_by_cast]
    assert len(found.results) == 12
    assert len(by_cast) == 6


async def test_one_search_failing_does_not_take_the_other_down(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if is_people(request):
            return people()
        if "person" in params_of(request):
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})

    serve(handler)
    user = await a_member(db, "p5@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Feast")
    assert [card.title for card in found.results] == ["Feast of Power"]


async def test_a_card_only_the_title_search_found_says_nothing_about_a_cast(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if is_people(request):
            return people()
        if "person" in params_of(request):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [row(7, "Feast of Power")]})

    serve(handler)
    user = await a_member(db, "p6@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Feast")
    assert found.results[0].matched_person == ""
    assert found.results[0].matched_by_cast is False


# --- "we could not ask" is not "there is nothing" ---------------------------------------------


async def test_an_unreachable_indexer_does_not_report_itself_as_a_provider(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    """The screen blamed the member's spelling for an outage.

    `_dramadex_page` swallowed every AppError into `[]`, and `configured` came from
    `try_client`, which reads two settings rows and never opens a socket — so a dead container,
    a rotated key and a 5xx all rendered as "no encontramos nada, prueba con otra grafía", for
    titles the household already owns.
    """

    def dead(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    serve(dead)
    user = await a_member(db, "p9@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "Her Man Is the Son of Death")

    assert found.results == []
    # Still configured — an admin pasted a URL and a key. But nobody answered.
    assert found.configured is True
    assert found.providers == []


async def test_a_rejected_key_is_an_outage_too(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    serve(lambda _r: httpx.Response(401, json={"detail": "bad key"}))
    user = await a_member(db, "p10@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "cualquier cosa")

    assert found.providers == []


async def test_a_genuine_miss_still_says_the_indexer_answered(
    db: AsyncSession, serve: Callable[..., list[httpx.Request]]
) -> None:
    """The other half: an empty answer from a live indexer must stay an honest empty answer."""
    serve(lambda r: people() if is_people(r) else httpx.Response(200, json={"items": []}))
    user = await a_member(db, "p11@example.com")

    from app.services import requests as requests_service

    found = await requests_service.search(db, user, "zzzz no existe")

    assert found.results == []
    assert found.providers == ["dramadex"]
