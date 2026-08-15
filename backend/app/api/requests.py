"""Wishlist routes — `/api/requests`.

Every route here is a member's own: they search, they ask, they see what they asked for, they
withdraw it. Nothing is admin-gated, and nothing needs to be — the one route that could leak
across members (`GET /`) is scoped to the caller in SQL rather than filtered afterwards, and
the two write routes take the row's owner from the session rather than from the request.

The admin's side of the same table lives in `app.api.admin`, deliberately: `/api/admin/*` is
gated at the router, so an admin-only route placed here would be one `require_admin` away from
being an accident. Splitting by audience rather than by table is what makes "another member can
never see who asked for what" true by construction.

This router reaches TMDB and the household's SearXNG instance on a member's behalf, which is
the one thing worth noticing about it: those are the same two providers the admin screens
already use, called through the same services, with the keys never leaving the server. It
fetches no video, queues no job, and writes nothing outside `series_requests`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.schemas.common import Ok
from app.schemas.requests import (
    RequestCreateIn,
    RequestPosterOut,
    RequestSearchResults,
    SeriesRequestOut,
)
from app.services import requests as requests_service

router = APIRouter(prefix="/requests", tags=["requests"])


@router.get("/search", response_model=RequestSearchResults)
async def search_requests(
    session: SessionDep,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=requests_service.MAX_SEARCH_LIMIT)] = (
        requests_service.DEFAULT_SEARCH_LIMIT
    ),
    short_drama: Annotated[bool, Query(alias="shortDrama")] = False,
) -> RequestSearchResults:
    """Find something to ask for.

    Answers from TMDB and SearXNG together, marking the cards the library already holds and
    the ones this member has already requested — both questions are cheaper to answer here
    than to discover after clicking.

    An unconfigured provider is not an error: the response says which sources answered, and a
    member can still ask for a title by name when none did.

    `shortDrama=true` narrows the search to the household's configured platforms and skips
    TMDB entirely. TMDB's catalogue is conventional television, so for a vertical-drama query
    the closest thing it has is the ordinary series that most resembles the title — which is
    exactly the noise this mode removes.
    """
    return await requests_service.search(session, user, q, limit=limit, short_drama=short_drama)


@router.get("/poster", response_model=RequestPosterOut)
async def find_poster(
    session: SessionDep,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    year: Annotated[int | None, Query(ge=1800, le=2200)] = None,
) -> RequestPosterOut:
    """A poster for a card that came back without one.

    Separate from `/search`, and fetched per card, because the alternative is worse in both
    directions: folding it into the search would make the whole result list wait on the
    slowest of a dozen image queries, and one engine having a bad moment would empty the list
    instead of one thumbnail.

    The URL that comes back is **this server's**. The bytes were fetched through the guarded
    path (public hosts only, redirects re-checked, size capped) and re-encoded by ffmpeg, so
    nothing a member's browser loads here comes from a third party — which matters because a
    request card is rendered by everyone in the house, including from outside it.

    `{"url": null}` is an ordinary answer, not an error: most of this library has no poster
    anywhere, and a card without a picture is still a card.
    """
    return RequestPosterOut(url=await requests_service.find_poster(session, q, year))


@router.get("", response_model=list[SeriesRequestOut])
@router.get("/", response_model=list[SeriesRequestOut], include_in_schema=False)
async def my_requests(session: SessionDep, user: CurrentUser) -> list[SeriesRequestOut]:
    """This member's own list and the answers on it. Never anybody else's."""
    return await requests_service.list_for_user(session, user)


@router.post("", response_model=SeriesRequestOut, status_code=201)
@router.post("/", response_model=SeriesRequestOut, status_code=201, include_in_schema=False)
async def create_request(
    session: SessionDep, user: CurrentUser, body: RequestCreateIn
) -> SeriesRequestOut:
    """Ask for something.

    Takes a card from the search, or a title typed by hand — `ids` may be empty, and a request
    with nothing but a title is a perfectly good request. Asking for something the library
    already has is refused with `REQUEST_ALREADY_IN_LIBRARY`; asking twice for the same thing
    returns the row that already exists rather than a second one.
    """
    return await requests_service.create(session, user, body)


@router.delete("/{request_id}", response_model=Ok)
async def delete_request(session: SessionDep, user: CurrentUser, request_id: str) -> Ok:
    """Withdraw one of your own. Another member's id answers `REQUEST_NOT_FOUND`, not 403 —
    whether a request exists is itself something only its owner should learn."""
    await requests_service.delete_own(session, user, request_id)
    return Ok()


__all__ = ["router"]
