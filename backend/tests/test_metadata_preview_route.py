"""The preview route, over HTTP — because the defect that mattered lived in the transport.

`GET …/metadata/preview` took `dramadex_id` as a bare `Query()`, with no camelCase alias. Every
other query parameter in this API is camelCase, because `APIModel` converts request *bodies*
automatically and a bare `Query()` does not. So the client's `?dramadexId=99` was discarded:
the preview showed the values of the record the series was already bound to, and the apply —
whose body *is* converted — wrote the values of the one the admin had picked.

That is the exact failure `plan_dramadex_title` and `apply_dramadex_title` were built to make
impossible, reopened one layer above them. A service-level test cannot see it. This file asks
the route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Series, User

pytestmark = pytest.mark.anyio


@pytest.fixture
async def http(db: AsyncSession, admin_user: User) -> AsyncIterator[AsyncClient]:
    from app.api.deps import get_current_user, get_session, require_admin
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def series(db: AsyncSession) -> Series:
    row = Series(
        title="You Fired A Genius Surgeon",
        slug="genius-surgeon",
        sort_title="you fired a genius surgeon",
        library_path="You Fired A Genius Surgeon",
    )
    db.add(row)
    await db.commit()
    return row


async def test_the_route_takes_its_parameter_in_the_shape_the_client_sends(
    http: AsyncClient, series: Series
) -> None:
    """The whole point. `?dramadexId=` must be the parameter, not an unknown extra.

    Asserted against the OpenAPI document rather than by watching a value arrive, because that
    is where the mistake is legible: one snake_case query parameter in an API of camelCase ones.
    """
    spec = (await http.get("/api/openapi.json")).json()
    params = spec["paths"]["/api/library/series/{series_id}/metadata/preview"]["get"]["parameters"]
    names = {param["name"] for param in params if param["in"] == "query"}

    assert "dramadexId" in names
    assert "dramadex_id" not in names


async def test_every_query_parameter_in_this_api_is_camel_case(http: AsyncClient) -> None:
    """The general rule, so the next bare `Query()` is caught rather than shipped.

    Request bodies convert themselves — `APIModel` carries `to_camel` — and query parameters do
    not. That asymmetry is invisible in the handler signature, which is why it needs a test that
    reads the whole surface instead of one route.
    """
    spec = (await http.get("/api/openapi.json")).json()

    offenders = [
        f"{method.upper()} {path} -> {param['name']}"
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        for param in operation.get("parameters", [])
        if param.get("in") == "query" and "_" in param["name"]
    ]

    assert offenders == [], offenders


async def test_an_unconfigured_indexer_is_a_flag_not_an_error(
    http: AsyncClient, series: Series
) -> None:
    """Three not-ready states, three repairs. One red box for all of them makes the admin guess."""
    response = await http.get(f"/api/library/series/{series.id}/metadata/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["rows"] == []
