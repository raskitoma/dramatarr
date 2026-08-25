"""Restricted series, and the member who may not see them.

The household asked for an age filter expressed as a role: "Espectador" and "Espectador-R".
Before this, `require_admin` was the **only** role gate in the whole backend — outside it no
code path consulted `role` at all, so every member reached every series, poster and byte
identically.

**The first test in this file is the one that matters.** A filter that hides a series from the
grid while `GET /api/stream/<id>` still serves the file is not a filter, it is false confidence.
Measured against the tree before this work: as `role=viewer`, `HEAD` on the restricted series'
episode returned 200 with `content-length: 31322554`, and a ranged `GET` returned real mp4.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Episode, MediaFile, Series, SeriesTitle, User

pytestmark = pytest.mark.anyio


def _client(db: AsyncSession, user: User) -> AsyncClient:
    """The real app on the test's session. Only `get_current_user` is overridden, so every
    other dependency — including the new gate — runs exactly as in production."""
    from app.api.deps import get_current_user
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def restricted(db: AsyncSession, tmp_env: Path) -> str:
    """A restricted series with one real, playable file on disk."""
    from app.config import get_settings

    root = Path(get_settings().library_root)
    folder = root / "Adults Only (2026)" / "Season 01"
    folder.mkdir(parents=True, exist_ok=True)
    media_path = "Adults Only (2026)/Season 01/Adults Only - S01E01.mp4"
    (root / media_path).write_bytes(b"\x00" * 4096)

    series = Series(
        title="Adults Only",
        slug="adults-only",
        sort_title="adults only",
        library_path="Adults Only (2026)",
        year=2026,
        content_rating="NC-17",
        restricted=True,
    )
    db.add(series)
    await db.flush()
    # The searchable title index. The wishlist's "already in the library" lookup joins it, so
    # without a row here that lookup finds nothing whatever the gate says — and the test for
    # the gate would pass while proving nothing.
    db.add(
        SeriesTitle(
            series_id=series.id, title="Adults Only", normalized="adults only", kind="primary"
        )
    )
    episode = Episode(series_id=series.id, season_number=1, episode_number=1, title="Uno")
    db.add(episode)
    await db.flush()
    db.add(MediaFile(episode_id=episode.id, path=media_path, size_bytes=4096, container="mp4"))
    await db.commit()
    # Loaded here, inside the async context, so the route finds a populated collection. Without
    # it `session.get` returns this very instance with its `selectin` relationship unloaded and
    # the lazy load fires outside the greenlet — a harness artefact that looks exactly like a
    # defect in the code under test.
    await db.refresh(episode, ["media_files"])
    return episode.id


# --- The bytes ---------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_a_plain_viewer_cannot_fetch_the_bytes(
    db: AsyncSession, viewer_user: User, restricted: str, method: str
) -> None:
    """The whole point. `HEAD` is in here because a `<video>` probes with it, and because it
    leaks the file's size on its own even when the body is refused."""
    from app.main import app

    try:
        async with _client(db, viewer_user) as http:
            response = await http.request(method, f"/api/stream/{restricted}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


async def test_a_range_request_cannot_get_them_either(
    db: AsyncSession, viewer_user: User, restricted: str
) -> None:
    """A ranged GET is a different code path in `build_range_response`, and it is the one a
    player actually uses."""
    from app.main import app

    try:
        async with _client(db, viewer_user) as http:
            response = await http.get(f"/api/stream/{restricted}", headers={"Range": "bytes=0-63"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    # The body is the JSON refusal, not a slice of the file.
    assert response.json()["error"]["code"] == "EPISODE_NOT_FOUND"
    assert "video" not in (response.headers.get("content-type") or "")


async def test_the_permitted_roles_still_get_them(
    db: AsyncSession, restricted: str, viewer_r_user: User, admin_user: User
) -> None:
    """Otherwise the test above would pass on a gate that refuses everybody."""
    from app.main import app

    for user in (viewer_r_user, admin_user):
        try:
            async with _client(db, user) as http:
                response = await http.get(f"/api/stream/{restricted}")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200, f"{user.role} was refused"
        assert len(response.content) == 4096


async def test_an_unrestricted_series_is_untouched(
    db: AsyncSession, viewer_user: User, sample_series, tmp_env: Path
) -> None:
    """The gate must not become a wall. `sample_series` carries one playable file."""
    from app.config import get_settings
    from app.main import app

    _series, episodes = sample_series
    root = Path(get_settings().library_root)
    target = root / "Ruta de Cristal (2024)/Season 01/Ruta de Cristal - S01E01.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00" * 1024)
    # Same reason as the fixture above: load the collection here, in the async context, rather
    # than letting the route trip over a half-built instance in the identity map.
    await db.refresh(episodes[0], ["media_files"])

    try:
        async with _client(db, viewer_user) as http:
            response = await http.get(f"/api/stream/{episodes[0].id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


async def test_a_missing_episode_looks_the_same_as_a_refused_one(
    db: AsyncSession, viewer_user: User, restricted: str
) -> None:
    """404 and not 403, with the same code, so the response cannot be used to confirm that a
    restricted series exists."""
    from app.main import app

    try:
        async with _client(db, viewer_user) as http:
            refused = await http.get(f"/api/stream/{restricted}")
            absent = await http.get("/api/stream/does-not-exist")
    finally:
        app.dependency_overrides.clear()

    assert refused.status_code == absent.status_code == 404
    assert refused.json()["error"]["code"] == absent.json()["error"]["code"]


# --- The role itself ---------------------------------------------------------------------------


def test_the_gate_reads_a_plain_string_role() -> None:
    """The trap this closes: `role` is a string column, so a row read back from the database
    carries `"viewer_r"` and not the enum. `user.role.sees_restricted` would have raised
    `AttributeError` in production while passing in every test, because the fixtures build the
    enum in memory and the session does not expire it on commit."""
    assert User(role="viewer_r").sees_restricted is True
    assert User(role="viewer").sees_restricted is False
    assert User(role="admin").sees_restricted is True


def test_an_unrecognised_role_sees_nothing_restricted() -> None:
    """Fail closed. A typo in the column is not a licence."""
    assert User(role="espectador").sees_restricted is False
    assert User(role="").sees_restricted is False


# --- The lists, the counts and the facets -------------------------------------------------------


async def _visible(db: AsyncSession, user: User) -> list[str]:
    from app.services import library as library_service

    page = await library_service.list_series(
        db, user_id=user.id, sees_restricted=user.sees_restricted
    )
    return [row.slug for row in page.items]


async def test_a_restricted_series_is_absent_from_the_grid(
    db: AsyncSession, viewer_user: User, viewer_r_user: User, restricted: str, sample_series
) -> None:
    assert "adults-only" not in await _visible(db, viewer_user)
    assert "adults-only" in await _visible(db, viewer_r_user)


async def test_the_total_agrees_with_the_rows(
    db: AsyncSession, viewer_user: User, restricted: str, sample_series
) -> None:
    """The counter class of bug: hiding the row while the pager still counts it is two true
    numbers saying something false."""
    from app.services import library as library_service

    page = await library_service.list_series(db, user_id=viewer_user.id, sees_restricted=False)

    assert page.total == len(page.items)
    assert all(slug != "adults-only" for slug in (row.slug for row in page.items))


async def test_a_genre_only_the_hidden_series_has_does_not_appear(
    db: AsyncSession, viewer_user: User, restricted: str
) -> None:
    """A chip reading "Suspense (1)" that returns nothing announces the row more loudly than
    showing it would."""
    from sqlalchemy import select as sa_select

    from app.models import Series
    from app.services import library as library_service

    series = (await db.execute(sa_select(Series).where(Series.slug == "adults-only"))).scalar_one()
    series.genres = ["Solo Aqui"]
    await db.commit()

    hidden = [
        facet.genre for facet in await library_service.genre_facets(db, sees_restricted=False)
    ]
    shown = [facet.genre for facet in await library_service.genre_facets(db, sees_restricted=True)]

    assert "Solo Aqui" not in hidden
    assert "Solo Aqui" in shown


async def test_a_year_only_the_hidden_series_has_does_not_appear(
    db: AsyncSession, viewer_user: User, restricted: str
) -> None:
    from app.services import library as library_service

    hidden = [facet.year for facet in await library_service.year_facets(db, sees_restricted=False)]
    shown = [facet.year for facet in await library_service.year_facets(db, sees_restricted=True)]

    assert 2026 not in hidden
    assert 2026 in shown


async def test_the_arrows_step_over_it_rather_than_dead_ending(db: AsyncSession) -> None:
    """Filtering after `LIMIT 1` would return nothing and strand the walk at a title it should
    have kept going past. The predicate belongs inside the WHERE."""
    from app.models import Series
    from app.services import library as library_service

    for slug, title, restricted_flag in [
        ("aaa", "Aaa", False),
        ("mmm", "Mmm", True),
        ("zzz", "Zzz", False),
    ]:
        db.add(
            Series(
                title=title,
                slug=slug,
                sort_title=title.lower(),
                library_path=title,
                restricted=restricted_flag,
            )
        )
    await db.commit()
    first = (
        await db.execute(__import__("sqlalchemy").select(Series).where(Series.slug == "aaa"))
    ).scalar_one()

    _, next_hidden = await library_service.series_neighbours(db, first, sees_restricted=False)
    _, next_shown = await library_service.series_neighbours(db, first, sees_restricted=True)

    assert next_hidden is not None and next_hidden[0] == "zzz"
    assert next_shown is not None and next_shown[0] == "mmm"


async def test_search_does_not_surface_it(db: AsyncSession, restricted: str) -> None:
    from sqlalchemy import select as sa_select

    from app.models import Series
    from app.services.search.index import reindex_series
    from app.services.search.service import search_library

    series = (await db.execute(sa_select(Series).where(Series.slug == "adults-only"))).scalar_one()
    await reindex_series(db, series.id)
    await db.commit()

    hidden = await search_library(db, "Adults Only", sees_restricted=False)
    shown = await search_library(db, "Adults Only", sees_restricted=True)

    assert hidden.series == []
    assert hidden.total == 0
    assert [hit.series.slug for hit in shown.series] == ["adults-only"]


async def test_the_detail_route_refuses_it_the_same_way_a_missing_one_does(
    db: AsyncSession, viewer_user: User, restricted: str
) -> None:
    from app.main import app

    try:
        async with _client(db, viewer_user) as http:
            refused = await http.get("/api/library/series/adults-only")
            absent = await http.get("/api/library/series/no-such-thing")
    finally:
        app.dependency_overrides.clear()

    assert refused.status_code == absent.status_code == 404
    assert refused.json()["error"]["code"] == absent.json()["error"]["code"]


# --- The wishlist ------------------------------------------------------------------------------
#
# Three separate leaks live here and none of them is a list of series: the "already in the
# library" conflict quotes the title back, the fulfilled row carries a slug, and the mail
# carries both into somebody's inbox where no gate can reach them.


async def _requester(db: AsyncSession, email: str, role: str) -> User:
    user = User(email=email, display_name="R", role=role, locale="es")
    db.add(user)
    await db.commit()
    return user


async def test_asking_for_a_hidden_series_is_not_answered_with_its_title(
    db: AsyncSession, restricted: str
) -> None:
    """`REQUEST_ALREADY_IN_LIBRARY` reads "Adults Only is already in the library". Unfiltered,
    the one member forbidden the title is told it by the error that refuses them."""
    from app.models import UserRole
    from app.schemas.requests import RequestCreateIn
    from app.services import requests as requests_service

    user = await _requester(db, "asks@example.com", UserRole.VIEWER)
    payload = RequestCreateIn(title="Adults Only")

    # No conflict: it reads as a title the household does not have, so the request is filed and
    # the admin answers it like any other.
    out = await requests_service.create(db, user, payload)
    assert out.title == "Adults Only"


async def test_a_member_who_may_see_it_still_gets_the_conflict(
    db: AsyncSession, restricted: str
) -> None:
    from app.errors import Conflict
    from app.models import UserRole
    from app.schemas.requests import RequestCreateIn
    from app.services import requests as requests_service

    user = await _requester(db, "asks-r@example.com", UserRole.VIEWER_R)

    with pytest.raises(Conflict) as raised:
        await requests_service.create(db, user, RequestCreateIn(title="Adults Only"))
    assert raised.value.code == "REQUEST_ALREADY_IN_LIBRARY"


async def test_a_fulfilled_row_carries_no_link_to_a_series_they_cannot_open(
    db: AsyncSession, restricted: str
) -> None:
    """The same shape as a request an admin marked fulfilled without binding anything: status
    fulfilled, no link. The title stays the one the member typed."""
    from app.models import RequestStatus, SeriesRequest, UserRole
    from app.services import requests as requests_service

    episode = await db.get(Episode, restricted)
    assert episode is not None

    plain = await _requester(db, "plain@example.com", UserRole.VIEWER)
    allowed = await _requester(db, "allowed@example.com", UserRole.VIEWER_R)
    for user in (plain, allowed):
        db.add(
            SeriesRequest(
                user_id=user.id,
                title="Something They Typed",
                status=RequestStatus.FULFILLED,
                dedupe_key=f"restricted:{user.id}",
                series_id=episode.series_id,
            )
        )
    await db.commit()

    hidden = await requests_service.list_for_user(db, plain)
    assert hidden[0].series is None
    assert hidden[0].title == "Something They Typed"

    shown = await requests_service.list_for_user(db, allowed)
    assert shown[0].series is not None
    assert shown[0].series.slug == "adults-only"


async def test_the_admin_still_sees_the_binding(db: AsyncSession, restricted: str) -> None:
    """The person who classifies the row must not be the one person who cannot see what they
    classified — `admin_request_out` is deliberately unfiltered."""
    from app.models import RequestStatus, SeriesRequest, UserRole
    from app.services import requests as requests_service

    episode = await db.get(Episode, restricted)
    assert episode is not None
    plain = await _requester(db, "seen-by-admin@example.com", UserRole.VIEWER)
    row = SeriesRequest(
        user_id=plain.id,
        title="Something They Typed",
        status=RequestStatus.FULFILLED,
        dedupe_key="restricted:admin-view",
        series_id=episode.series_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row, ["series", "user"])

    assert requests_service.admin_request_out(row).series is not None


async def test_the_mail_sends_them_to_their_wishlist_not_to_a_404(
    db: AsyncSession, restricted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half no gate can reach: once it is in an inbox, the subject line has already been
    read. So the title and the link are decided *before* it is sent, not after."""
    from app.models import RequestStatus, SeriesRequest, UserRole
    from app.schemas.requests import RequestPatchIn
    from app.services import requests as requests_service

    sent: list[dict[str, object]] = []

    async def fake_send(_session, _to, _template, _locale, context):  # type: ignore[no-untyped-def]
        sent.append(context)

    monkeypatch.setattr(requests_service, "send_mail", fake_send)

    episode = await db.get(Episode, restricted)
    assert episode is not None
    admin = User(email="admin-mail@example.com", display_name="A", role=UserRole.ADMIN, locale="es")
    db.add(admin)
    plain = await _requester(db, "mailed@example.com", UserRole.VIEWER)
    row = SeriesRequest(
        user_id=plain.id,
        title="Something They Typed",
        status=RequestStatus.PENDING,
        dedupe_key="restricted:mail",
    )
    db.add(row)
    await db.commit()

    await requests_service.update(
        db,
        admin,
        row.id,
        RequestPatchIn(status=RequestStatus.FULFILLED, seriesId=episode.series_id),
    )

    assert len(sent) == 1
    assert sent[0]["title"] == "Something They Typed"
    assert str(sent[0]["series_url"]).endswith("/requests")


async def test_the_same_mail_to_a_permitted_member_carries_the_series(
    db: AsyncSession, restricted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models import RequestStatus, SeriesRequest, UserRole
    from app.schemas.requests import RequestPatchIn
    from app.services import requests as requests_service

    sent: list[dict[str, object]] = []

    async def fake_send(_session, _to, _template, _locale, context):  # type: ignore[no-untyped-def]
        sent.append(context)

    monkeypatch.setattr(requests_service, "send_mail", fake_send)

    episode = await db.get(Episode, restricted)
    assert episode is not None
    admin = User(
        email="admin-mail2@example.com", display_name="A", role=UserRole.ADMIN, locale="es"
    )
    db.add(admin)
    allowed = await _requester(db, "mailed-r@example.com", UserRole.VIEWER_R)
    row = SeriesRequest(
        user_id=allowed.id,
        title="Something They Typed",
        status=RequestStatus.PENDING,
        dedupe_key="restricted:mail-r",
    )
    db.add(row)
    await db.commit()

    await requests_service.update(
        db,
        admin,
        row.id,
        RequestPatchIn(status=RequestStatus.FULFILLED, seriesId=episode.series_id),
    )

    assert sent[0]["title"] == "Adults Only"
    assert str(sent[0]["series_url"]).endswith("/series/adults-only")


# --- Classifying it ----------------------------------------------------------------------------


async def test_the_admin_can_mark_a_series_restricted_without_freezing_it(
    db: AsyncSession,
) -> None:
    """`restricted` is kept out of `SERIES_METADATA_FIELDS` on purpose: deciding who may see a
    row is a household decision, not curation of its metadata. If it counted as curation, one
    click would also stop the nightly refresh ever touching the row again."""
    from app.schemas.library import SeriesPatch
    from app.services.library import apply_series_patch

    series = Series(
        title="Unclassified",
        slug="unclassified",
        sort_title="unclassified",
        library_path="Unclassified",
    )
    db.add(series)
    await db.commit()

    patch = SeriesPatch(restricted=True)
    await apply_series_patch(db, series, patch.model_dump(exclude_unset=True))
    await db.commit()

    assert series.restricted is True
    assert series.metadata_locked is False


# --- The sixteenth path -------------------------------------------------------------------------


def test_every_member_facing_route_has_been_reviewed() -> None:
    """A census, because there is no single query every read passes through.

    The filter lives in roughly fifteen places, and a plan with the filter in fifteen places is
    a plan that leaks on the sixteenth. This makes the sixteenth impossible to add silently: a
    new route reachable without `require_admin` fails here until somebody puts it in the list
    below, which is a decision rather than an oversight.

    Being in the list is **not** a claim that a route is filtered — several correctly are not,
    because they carry no library data. It is a claim that somebody looked.

    The floor at the bottom is load-bearing. The first version of this walked `app.routes`
    expecting `APIRoute` objects and found **zero**, because included routers nest rather than
    flatten — so it passed while examining nothing, including when a route was deleted from the
    list to test it. A census that cannot prove it looked is worse than no census.
    """
    from fastapi.routing import APIRoute

    from app.api.deps import require_admin
    from app.main import create_app

    reviewed = {
        # --- Auth and identity. No series data reaches any of these. ---
        "/auth/session",
        "/auth/logout",
        "/auth/methods",
        "/config",
        "/health",
        "/auth/magic-link/request",
        "/auth/magic-link/consume",
        "/auth/passkey/login/begin",
        "/auth/passkey/login/complete",
        "/auth/passkey/register/begin",
        "/auth/passkey/register/complete",
        "/auth/passkeys",
        "/auth/passkeys/{credential_id}",
        "/auth/invite/{token}",
        "/auth/invite/{token}/begin",
        "/auth/invite/{token}/complete",
        "/auth/google/start",
        "/auth/google/callback",
        "/auth/google/link",
        "/auth/google/link/start",
        # --- This member's own account. ---
        "/me",
        "/me/",
        "/me/avatar",
        "/me/sessions",
        "/me/sessions/{session_id}",
        # --- Filtered: watch state, joined to Series and restricted, rows *and* counts. ---
        "/me/continue-watching",
        "/me/history",
        # --- Filtered library reads. ---
        "/library/series",
        "/library/series/{id_or_slug}",
        "/library/series/{series_id}/episodes",
        "/library/episodes/{episode_id}",
        "/library/search",
        "/library/genres",
        "/library/years",
        # --- Filtered: the indexer's people, whose credits carry local slugs, and its poster
        #     proxy, which is addressed by a small guessable integer. ---
        "/library/dramadex/people/{person_id}",
        "/library/dramadex/people/{person_id}/photo",
        "/library/dramadex/titles/{title_id}/poster",
        # --- Gated by `assert_episode_visible` / `assert_image_visible`, attached in
        #     `playable` so a future route under that decorator inherits the check. ---
        "/stream/{episode_id}",
        "/stream/{episode_id}/subtitles/{track_id}.vtt",
        "/stream/{episode_id}/thumbnails.vtt",
        "/stream/{episode_id}/sprite.jpg",
        "/stream/images/{kind}/{name}",
        # An avatar is a member's own photograph; no library state.
        "/stream/avatars/{user_id}",
        # --- Watch writes, gated through `get_episode_or_404` / `get_series_or_404`. ---
        "/watch/{episode_id}/progress",
        "/watch/{episode_id}/complete",
        "/watch/series/{series_id}/watched",
        # --- The wishlist. A match against a restricted series reads as no match. ---
        "/requests",
        "/requests/",
        "/requests/search",
        "/requests/acknowledge",
        "/requests/{request_id}",
    }

    def walk(routes: object) -> list[APIRoute]:
        """Included routers nest rather than flatten, and the wrapper keeps the real router on
        `original_router` — not on `routes`, which is what the first version looked for."""
        found: list[APIRoute] = []
        for route in routes or []:  # type: ignore[union-attr]
            if isinstance(route, APIRoute):
                found.append(route)
                continue
            inner = getattr(route, "original_router", None) or getattr(route, "routes", None)
            nested = getattr(inner, "routes", None) if inner is not None else None
            found.extend(walk(nested if nested is not None else inner))
        return found

    every = walk(create_app().routes)
    assert len(every) > 40, (
        f"the census only found {len(every)} routes, which means it is not looking at the "
        "application any more — fix the walk before trusting anything below it"
    )

    def normalise(path: str) -> str:
        """Nested routers report paths relative to their mount, so `/api` may or may not be
        there depending on how the walk reached them. Compare on the same footing."""
        return path[4:] if path.startswith("/api/") else path

    unreviewed = {
        normalise(route.path)
        for route in every
        if not normalise(route.path).startswith("/admin")
        and not any(
            getattr(d.call, "__name__", "") == require_admin.__name__
            for d in route.dependant.dependencies
        )
        and normalise(route.path) not in {normalise(p) for p in reviewed}
    }

    assert not unreviewed, (
        "These member-facing routes have not been reviewed against the content restriction. "
        "Read each one, decide whether it can disclose a restricted series, then add it above: "
        f"{sorted(unreviewed)}"
    )
