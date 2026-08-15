"""Wire shapes for the wishlist — `/api/library/requests` and `/api/admin/requests`.

The viewer's and the admin's views of a request are deliberately different models rather than
one model with optional fields. A member sees their own row and the answer they were given; an
admin additionally sees *who asked*, and that is exactly the field a shared model would have to
make optional and then remember to strip. Two models, one of which extends the other, keeps
"the requester's identity never reaches another member" a property of the type rather than of a
code path someone has to get right every time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import APIModel
from app.schemas.library import DiscoveredIds

#: The four states a request can be in. Mirrors `models.requests.RequestStatus`; declared here
#: as a Literal so FastAPI validates an admin's PATCH instead of writing an unknown string.
RequestStatusName = Literal["pending", "approved", "fulfilled", "declined"]


# --- Searching for something to ask for -------------------------------------------------------


class RequestCandidate(APIModel):
    """One thing a member could ask for, as the card they choose from.

    A candidate is assembled from whatever answered — TMDB's catalogue, the household's
    SearXNG instance, or both for the same title — and the fields below are what survive that
    merge. `provider` is the host the row came from and is always shown: a member choosing
    between two spellings of a Chinese drama is really choosing between two sources, and
    hiding which is which would make the choice arbitrary.
    """

    #: Stable identity for this candidate — the same string the request row will carry. Lets
    #: the client mark a card as already-requested without guessing at title equality.
    key: str
    title: str
    year: int | None = None
    #: Absent far more often than not. Short-form drama frequently has no artwork in any
    #: source this app is allowed to read, and a card with no picture is still a usable card.
    poster_url: str | None = None
    #: The page behind the card. Opened by the *admin*, later, in their own browser.
    source_url: str | None = None
    #: `themoviedb.org`, `mydramalist.com`, `dramaboxdb.com`.
    provider: str = ""
    #: The search engine's extract, when there was one. Display only — never stored on the
    #: request, for the same reason it never becomes a series overview.
    snippet: str = ""
    ids: DiscoveredIds = Field(default_factory=DiscoveredIds)
    #: True when the library already holds this title, with the slug to link to. Answering
    #: "we have it" at the moment of asking is worth more than answering it a day later.
    in_library: bool = False
    in_library_slug: str | None = None
    #: True when *this* member already has a row for this candidate, with its status, so the
    #: card can say "you asked for this — pending" instead of offering the button again.
    requested: bool = False
    request_status: RequestStatusName | None = None


class RequestPosterOut(APIModel):
    """The answer from `GET /requests/poster`.

    Always this server's own path or null. Never a third-party URL — see the route.
    """

    url: str | None = None


class RequestSearchResults(APIModel):
    query: str = ""
    #: Which sources actually answered: `tmdb`, `searxng`, or both. Empty means neither is
    #: configured, which the UI reports as a setup gap rather than as "nothing found".
    providers: list[str] = Field(default_factory=list)
    #: False only when there is nothing to search with. A member is still able to ask for a
    #: title by name in that state — the free-text path does not depend on any provider.
    configured: bool = True
    results: list[RequestCandidate] = Field(default_factory=list)


# --- Asking ------------------------------------------------------------------------------------


class RequestIdsIn(DiscoveredIds):
    """The identifiers a candidate carried, echoed back when it is chosen.

    Same shape as what the search handed out, so the client posts the object it was given
    rather than rebuilding it field by field. Everything is optional; a request typed by hand
    carries none of it.
    """


class RequestCreateIn(APIModel):
    title: str = Field(min_length=1, max_length=300)
    year: int | None = Field(default=None, ge=1800, le=2200)
    #: Host-allowlisted server-side and dropped if it fails — see `services.requests`. A
    #: rejected picture must not cost the member their request.
    poster_url: str | None = Field(default=None, max_length=1000)
    source_url: str | None = Field(default=None, max_length=1000)
    ids: RequestIdsIn = Field(default_factory=RequestIdsIn)
    #: The member's own words about what they want. Bounded because it is rendered in a table.
    note: str | None = Field(default=None, max_length=500)

    @field_validator("title", "note")
    @classmethod
    def _collapse(cls, value: str | None) -> str | None:
        """Engines and clipboards produce newlines and runs of spaces; a title is one line."""
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None


# --- Reading -----------------------------------------------------------------------------------


class RequestSeriesRef(APIModel):
    """The series a fulfilled request became, as the two fields needed to link to it."""

    id: str
    slug: str
    title: str


class SeriesRequestOut(APIModel):
    """A member's own request, and the answer it has been given so far."""

    id: str
    title: str
    year: int | None = None
    poster_url: str | None = None
    source_url: str | None = None
    provider: str = ""
    ids: DiscoveredIds = Field(default_factory=DiscoveredIds)
    note: str | None = None
    status: RequestStatusName
    #: The admin's reply. The reason a decline is a sentence and not just a red chip.
    admin_note: str | None = None
    #: Set once the admin ties the request to a real series, so "it is here now" is a link.
    series: RequestSeriesRef | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    #: True when the POST found an identical row instead of writing a new one. Lets the client
    #: say "already on your list" rather than claiming a second request was created.
    already_existed: bool = False


class RequestRequester(APIModel):
    """Who asked. Admin-only, by construction — see the module docstring."""

    id: str
    display_name: str
    email: str
    avatar_url: str | None = None


class AdminSeriesRequestOut(SeriesRequestOut):
    requester: RequestRequester
    #: Who answered it, when somebody has. A name rather than a user object: the admin list
    #: needs to show it, not to link to it.
    resolved_by_name: str | None = None
    #: How many *other* members asked for the same thing. The one number that turns a queue
    #: into a priority order in a household — three people waiting on the same drama is a
    #: different job from one.
    also_requested_by: int = 0


class RequestPatchIn(APIModel):
    """The admin's answer. Every field optional; omitted means untouched."""

    status: RequestStatusName | None = None
    admin_note: str | None = Field(default=None, max_length=1000)
    #: Ties the request to the series it became. Validated to exist; `""` clears the link.
    series_id: str | None = Field(default=None, max_length=32)


class RequestFolderOut(APIModel):
    """Where to put the files, and whether this call is what created it."""

    #: Library-relative, which is what the admin needs to navigate to on the share.
    path: str
    #: False when the directory was already there. Not an error — see the route.
    created: bool = False


class RequestCounts(APIModel):
    """How many requests sit in each status — the admin nav badge and the page's filter tabs."""

    pending: int = 0
    approved: int = 0
    fulfilled: int = 0
    declined: int = 0
    total: int = 0


__all__ = [
    "AdminSeriesRequestOut",
    "RequestCandidate",
    "RequestCounts",
    "RequestCreateIn",
    "RequestFolderOut",
    "RequestIdsIn",
    "RequestPatchIn",
    "RequestPosterOut",
    "RequestRequester",
    "RequestSearchResults",
    "RequestSeriesRef",
    "RequestStatusName",
    "SeriesRequestOut",
]
