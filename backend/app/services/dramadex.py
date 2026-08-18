"""DramaDex — the household's own indexer, and the reason the rest of this stopped mattering.

dramatarr spent months asking the wrong question. TMDB and IMDb do not catalogue vertical
short drama, so the app resorted to a metasearch engine, scoring web results, reading a
platform's name off a hostname, and asking an admin to paste a synopsis by hand. Every one of
those was a workaround for a missing index.

DramaDex *is* that index. It scrapes DramaBox and ReelShort directly, and at the time of
writing holds 7106 titles, 453k episodes and 2303 people, with a synopsis on 100% of them.
Measured against this household's library, a normalised title lookup finds 11 of 17 series
outright — against 1 for the whole SearXNG apparatus it replaces.

Three things it gives that nothing before it could:

* **Cast**, so a series can list who is in it and an actor can be followed to their other
  shorts. This existed nowhere for this genre.
* **`watch_url` per studio**, built from the platform's id rather than its slug, so a link
  survives the retitles these platforms do constantly.
* **Tags across sixteen namespaces** — `reelshort.trope`, `dramabox.category`,
  `reelshort.setting` — which is genuine structure where dramatarr had a free-text field.

**Ratings need care and this module enforces it.** DramaDex flags the platforms' own scores
`is_editorial` because they are marketing copy: DramaBox floor-clamps to 7.1–10, ReelShort's
entire catalogue sits inside 0.4 points. Only IMDb's carry real vote counts. `genuine_ratings`
is the only accessor, and it filters.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

BASE_URL_SETTING = "dramadex.base_url"
API_KEY_SETTING = "dramadex.api_key"

#: Long enough for the indexer to answer a cold search over 7k titles, short enough that a
#: dead instance does not hold an admin's request open.
TIMEOUT_SECONDS = 20.0
#: DramaDex caps `limit` at 200 and rejects more.
MAX_LIMIT = 200


# --- Matching ---------------------------------------------------------------------------
#
# Worth its own section because the naive version is wrong in a way that looks right.


def normalize_title(value: str) -> str:
    """Fold a title to the form two catalogues can agree on.

    Exact string equality matches 6 of this household's 17 series. This matches 11. The
    difference is entirely punctuation and case that nobody would call a different show:

        'Nero Made Men'            == 'Nero: Made Men'
        'Tempest The Last Mecha'   == 'Tempest：The Last Mecha'      <- fullwidth colon
        'You Fired a Tech Genius'  == 'You Fired A Tech Genius'

    NFKC is what collapses that fullwidth colon to an ASCII one; without it the strings differ
    by a character an admin cannot see. Everything that is not alphanumeric then becomes a
    single space, so separators stop mattering at all.

    This is deliberately **not** fuzzy. These titles come from a template grammar and six
    different shows are called `Fated to My {Forbidden Alpha | Swapped Alpha | …}`; a
    similarity threshold loose enough to help would confidently pick the wrong sibling.
    """
    folded = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


# --- Wire types -------------------------------------------------------------------------


@dataclass(frozen=True)
class StudioLink:
    """One platform's copy of a title, and the link to watch it there."""

    key: str
    name: str
    external_id: str
    watch_url: str

    @property
    def is_watchable(self) -> bool:
        # IMDb is a source in DramaDex but not a place to watch anything.
        return self.key != "imdb" and bool(self.watch_url)


@dataclass(frozen=True)
class Rating:
    source: str
    value: float
    votes: int | None
    scale: float
    is_editorial: bool


@dataclass(frozen=True)
class CastMember:
    person_id: int
    name: str
    role: str | None = None
    billing_order: int | None = None


@dataclass(frozen=True)
class DramadexTitle:
    """A DramaDex title. `synopsis` and below are only present on the detail endpoint."""

    id: int
    primary_title: str
    year: int | None = None
    episode_count: int | None = None
    audience_rating: str | None = None
    poster_url: str | None = None
    studios: tuple[StudioLink, ...] = ()
    synopsis: str | None = None
    completed: bool | None = None
    aliases: tuple[str, ...] = ()
    tags: tuple[tuple[str, str], ...] = ()
    cast: tuple[CastMember, ...] = ()
    ratings: tuple[Rating, ...] = ()

    @property
    def genuine_ratings(self) -> tuple[Rating, ...]:
        """The only accessor, because the editorial ones must never be shown or sorted by."""
        return tuple(r for r in self.ratings if not r.is_editorial)

    @property
    def studio_name(self) -> str | None:
        """The platform to record as this series' network."""
        return next((s.name for s in self.studios if s.is_watchable), None)

    def genres(self) -> tuple[str, ...]:
        """The tags worth showing as genres, out of sixteen namespaces of them.

        `reelshort.mood` alone holds 114 values and `dramabox.tag` 97 — dumping all of them
        into one field would bury the three that mean something under forty that do not. So
        this takes the namespaces that are actually genre-shaped and leaves the rest available
        under `tags` for anyone who wants to filter on them.
        """
        wanted = ("genre", "sub-genre", "category", "theme")
        seen: dict[str, None] = {}
        for namespace, value in self.tags:
            if namespace.rsplit(".", 1)[-1] in wanted:
                # `All` is a real value in DramaBox's category list and means nothing.
                if value.strip() and value.strip().lower() != "all":
                    seen.setdefault(value.strip(), None)
        return tuple(seen)


@dataclass
class SearchPage:
    items: list[DramadexTitle] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    bio: str | None = None
    external_ids: dict[str, str] = field(default_factory=dict)
    titles: tuple[DramadexTitle, ...] = ()
    #: DramaDex's own path (`/people/43/photo`), not a URL a browser may use — every route
    #: there needs the API key. Its presence is the only signal that a photo exists at all;
    #: measured 2026-08-18, 18 of this household's 24 credited people have one and 94.9% of
    #: the indexer's 2188 canonical people do. Never `urljoin` it: the leading slash would
    #: discard any base path prefix.
    photo_url: str | None = None


# --- Parsing ----------------------------------------------------------------------------


def _studio(raw: Any) -> StudioLink | None:
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or "").strip()
    watch = str(raw.get("watch_url") or "").strip()
    if not key:
        return None
    return StudioLink(
        key=key,
        name=str(raw.get("name") or key).strip(),
        external_id=str(raw.get("external_id") or "").strip(),
        watch_url=watch,
    )


def _title(raw: Any) -> DramadexTitle | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
        return None
    name = str(raw.get("primary_title") or "").strip()
    if not name:
        return None

    studios = tuple(s for s in (_studio(x) for x in raw.get("studios") or []) if s is not None)
    tags = tuple(
        (str(t.get("namespace") or ""), str(t.get("value") or ""))
        for t in raw.get("tags") or []
        if isinstance(t, dict)
    )
    cast = tuple(
        CastMember(
            person_id=int(p["id"]),
            name=str(p.get("name") or "").strip(),
            role=(str(p.get("role")).strip() or None) if p.get("role") else None,
            billing_order=p.get("billing_order") if isinstance(p.get("billing_order"), int) else None,
        )
        for p in raw.get("cast") or []
        if isinstance(p, dict) and isinstance(p.get("id"), int) and p.get("name")
    )
    ratings = tuple(
        Rating(
            source=str(r.get("source") or ""),
            value=float(r["value"]),
            votes=r.get("votes") if isinstance(r.get("votes"), int) else None,
            scale=float(r.get("scale") or 10.0),
            is_editorial=bool(r.get("is_editorial")),
        )
        for r in raw.get("ratings") or []
        if isinstance(r, dict) and isinstance(r.get("value"), (int, float))
    )

    return DramadexTitle(
        id=int(raw["id"]),
        primary_title=name,
        year=raw.get("year") if isinstance(raw.get("year"), int) else None,
        episode_count=(
            raw.get("episode_count") if isinstance(raw.get("episode_count"), int) else None
        ),
        audience_rating=(str(raw.get("audience_rating")).strip() or None)
        if raw.get("audience_rating")
        else None,
        poster_url=(str(raw.get("poster_url")).strip() or None) if raw.get("poster_url") else None,
        studios=studios,
        synopsis=(str(raw.get("synopsis")).strip() or None) if raw.get("synopsis") else None,
        completed=raw.get("completed") if isinstance(raw.get("completed"), bool) else None,
        aliases=tuple(str(a).strip() for a in raw.get("aliases") or [] if str(a).strip()),
        tags=tags,
        cast=cast,
        ratings=ratings,
    )


# --- Client -----------------------------------------------------------------------------


class DramadexClient:
    """Thin async client. One instance per request; the caller owns the session."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.get(
                    url,
                    params={k: v for k, v in (params or {}).items() if v not in (None, "")},
                    # Header, never a query parameter: DramaDex's own auth module refuses the
                    # query form precisely because it lands in logs and Referer headers.
                    headers={"X-API-Key": self.api_key},
                )
        except httpx.HTTPError as exc:
            raise AppError(
                "DRAMADEX_UNREACHABLE",
                "Could not reach DramaDex",
                502,
                {"detail": str(exc)},
            ) from exc

        if response.status_code == 401:
            raise AppError("DRAMADEX_UNAUTHORIZED", "DramaDex rejected the API key", 400)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise AppError(
                "DRAMADEX_BAD_RESPONSE",
                f"DramaDex answered {response.status_code}",
                502,
                {"status": response.status_code},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AppError("DRAMADEX_BAD_RESPONSE", "DramaDex sent no JSON", 502) from exc

    # --- Catalogue ---------------------------------------------------------------------

    async def search(
        self,
        query: str = "",
        *,
        genre: str | None = None,
        studio: str | None = None,
        person: str | None = None,
        year: int | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> SearchPage:
        payload = await self._get(
            "titles",
            {
                "q": query.strip() or None,
                "genre": genre,
                "studio": studio,
                "person": person,
                "year": year,
                "limit": max(1, min(limit, MAX_LIMIT)),
                "cursor": cursor,
            },
        )
        if not isinstance(payload, dict):
            return SearchPage()
        items = [t for t in (_title(x) for x in payload.get("items") or []) if t is not None]
        cursor_out = payload.get("next_cursor")
        return SearchPage(items=items, next_cursor=cursor_out if isinstance(cursor_out, str) else None)

    async def title(self, title_id: int) -> DramadexTitle | None:
        return _title(await self._get(f"titles/{int(title_id)}"))

    async def person(self, person_id: int) -> Person | None:
        raw = await self._get(f"people/{int(person_id)}")
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
            return None
        return Person(
            id=int(raw["id"]),
            name=str(raw.get("name") or "").strip(),
            bio=(str(raw.get("bio")).strip() or None) if raw.get("bio") else None,
            external_ids={
                str(k): str(v) for k, v in (raw.get("external_ids") or {}).items()
            },
            titles=tuple(
                t for t in (_title(x) for x in raw.get("titles") or []) if t is not None
            ),
            photo_url=(
                (str(raw.get("photo_url")).strip() or None) if raw.get("photo_url") else None
            ),
        )

    async def genres(self) -> tuple[tuple[str, str], ...]:
        raw = await self._get("genres")
        if not isinstance(raw, list):
            return ()
        return tuple(
            (str(t.get("namespace") or ""), str(t.get("value") or ""))
            for t in raw
            if isinstance(t, dict) and t.get("value")
        )

    async def stats(self) -> dict[str, Any]:
        raw = await self._get("stats")
        return raw if isinstance(raw, dict) else {}

    async def poster_bytes(self, title_id: int) -> tuple[bytes, str] | None:
        """Fetch key art. DramaDex serves it itself rather than hotlinking, which is also why
        dramatarr can cache it without leaking a viewer's IP to a platform CDN."""
        url = urljoin(self.base_url, f"titles/{int(title_id)}/poster")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers={"X-API-Key": self.api_key})
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or not response.content:
            return None
        return response.content, response.headers.get("content-type", "image/webp")

    async def photo_bytes(self, person_id: int) -> tuple[bytes, str] | None:
        """Fetch an actor's photo, for the same reason `poster_bytes` exists: `_get` ends in
        `response.json()` and raises `DRAMADEX_BAD_RESPONSE` the moment it is handed bytes.

        The path is built here rather than taken from `Person.photo_url` — that value starts
        with a slash, and `urljoin` would throw away any base path prefix the indexer picks
        up the day it moves behind a gateway. A person with no photo answers 404 with a JSON
        body, which the status check below absorbs.
        """
        url = urljoin(self.base_url, f"people/{int(person_id)}/photo")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers={"X-API-Key": self.api_key})
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or not response.content:
            return None
        return response.content, response.headers.get("content-type", "image/webp")

    # --- Matching ----------------------------------------------------------------------

    async def best_match(self, title: str, *, year: int | None = None) -> DramadexTitle | None:
        """The one result that *is* this series, or nothing.

        Returns a match only on a normalised title equality — never the top-ranked result.
        A search for a title DramaDex has never indexed still returns plausible neighbours
        (asking for "The Call Boy I Met in Paris" offers "Call to Arms, Call to Love"), and
        binding one of those is worse than binding nothing: it writes a wrong synopsis and a
        wrong cast onto a series, which is the exact failure the TMDB era shipped.
        """
        wanted = normalize_title(title)
        if not wanted:
            return None
        page = await self.search(title, limit=10)
        matches = [t for t in page.items if normalize_title(t.primary_title) == wanted]
        if not matches:
            return None
        if year is not None:
            for candidate in matches:
                if candidate.year == year:
                    return candidate
        return matches[0]


# --- Configuration ------------------------------------------------------------------------


def _valid_base_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AppError(
            "DRAMADEX_BAD_URL", "The DramaDex URL must be http(s) with a host", 400
        )
    return value


async def try_client(session: AsyncSession) -> DramadexClient | None:
    """A client, or None when the household has not connected DramaDex yet.

    Absence is not an error: dramatarr runs without it, the same way it ran before it existed.
    Every caller treats `None` as "this feature is not switched on".
    """
    settings = SettingsService(session)
    base_url = str(await settings.get(BASE_URL_SETTING, "") or "").strip()
    api_key = str(await settings.get(API_KEY_SETTING, "") or "").strip()
    if not base_url or not api_key:
        return None
    return DramadexClient(_valid_base_url(base_url), api_key)


async def require_client(session: AsyncSession) -> DramadexClient:
    client = await try_client(session)
    if client is None:
        raise AppError(
            "DRAMADEX_NOT_CONFIGURED",
            "Connect DramaDex in settings first",
            400,
        )
    return client


__all__ = [
    "API_KEY_SETTING",
    "BASE_URL_SETTING",
    "CastMember",
    "DramadexClient",
    "DramadexTitle",
    "Person",
    "Rating",
    "SearchPage",
    "StudioLink",
    "normalize_title",
    "require_client",
    "try_client",
]


# --- Presentation ---------------------------------------------------------------------------


def hit_from(
    found: DramadexTitle,
    *,
    wanted: str = "",
    in_library: bool = False,
    library_slug: str | None = None,
) -> dict[str, Any]:
    """Flatten a title into the shape the UI chooses between.

    `matches` is computed here rather than in the client so that one rule serves the admin
    editor, the wishlist and the actor screen — a row that "is" this series in one of them
    must be the same row in the others.
    """
    watch = next((s for s in found.studios if s.is_watchable), None)
    return {
        "id": found.id,
        "title": found.primary_title,
        "year": found.year,
        "episode_count": found.episode_count,
        "poster_url": None,  # filled by the route, which knows the proxy path
        "synopsis": found.synopsis,
        "studio": watch.name if watch else None,
        "watch_url": watch.watch_url if watch else None,
        "genres": list(found.genres()),
        "cast": [member.name for member in found.cast][:8],
        "matches": bool(wanted) and normalize_title(found.primary_title) == normalize_title(wanted),
        "in_library": in_library,
        "library_slug": library_slug,
    }


# --- The refresh job ------------------------------------------------------------------------


def _keeps_its_own_artwork(series: Any) -> bool:
    """True when this series' picture is somebody's explicit choice, not the indexer's.

    Reads `artwork_locked`, which exists precisely so that this question and "has an admin
    edited this record" stop sharing one answer. They used to: the picker set
    `metadata_locked`, so choosing a poster opted the series out of every future metadata
    update — a decision nobody made by uploading a picture.
    """
    return bool(series.artwork_locked)


async def refresh_series(session: AsyncSession, series_id: str, *, ctx: Any = None) -> list[str]:
    """Re-read a bound series from the indexer and write what changed.

    This replaces the TMDB refresh job rather than sitting beside it, and the replacement is
    what made deleting TMDB safe instead of lossy: "Refresh metadata" is a button members and
    admins already use, and leaving it pointed at a service the app no longer talks to would
    have turned a working control into a silent no-op.

    An unbound series is not an error. It simply has nothing to refresh from — the same shape
    as a household that has not connected DramaDex at all — so it returns an empty list and
    the job succeeds.
    """
    from app.models import Series
    from app.services.library import apply_dramadex_title, get_series_or_404

    series = await get_series_or_404(session, series_id)
    if series.dramadex_id is None:
        log.info("dramadex refresh: %s is not bound to a title", series_id)
        return []

    client = await try_client(session)
    if client is None:
        log.info("dramadex refresh: not configured")
        return []

    if ctx is not None:
        await ctx.progress(30.0, "Reading DramaDex")
    found = await client.title(series.dramadex_id)
    if found is None:
        # The indexer dropped or merged the title. Left bound rather than silently unbound:
        # an admin who sees a stale id can re-bind, where a cleared one looks like it was
        # never matched.
        log.warning("dramadex refresh: title %s is gone", series.dramadex_id)
        return []

    if ctx is not None:
        await ctx.progress(70.0, "Writing metadata")
    # `overwrite=True` because this is somebody pressing "Refresh": re-reading the one index
    # that catalogues this library is the whole point, and a blanks-only pass over a row that
    # is already full does nothing and says so, which is exactly how this button came to look
    # broken. A synopsis a person wrote is still protected — by its provenance, inside
    # `apply_dramadex_title`, not by this flag.
    filled = await apply_dramadex_title(session, series, found, overwrite=True)

    # Artwork last, and never fatally.
    #
    # **Re-fetched every time**, not only when the poster looks missing. This function runs for
    # exactly one reason — somebody pressed "Actualizar metadata" — and the whole point of that
    # button is to go and ask again. The previous rule fetched only when the stored picture was
    # unloadable or was a wishlist thumbnail, so a title already carrying the indexer's art
    # could never pick up a newer version, and the owner reported precisely that: the refresh
    # did not replace the poster.
    #
    # It is cheap to be right here. `cache_dramadex_poster` names the file after a digest of the
    # encoded bytes, so unchanged art resolves to the file already on disk and writes nothing —
    # and *changed* art gets a new name, which is what makes the browser show it instead of the
    # copy it already had.
    #
    # The one exception is artwork an admin chose, which is theirs.
    if not _keeps_its_own_artwork(series):
        fetched = await client.poster_bytes(found.id)
        if fetched is not None:
            from app.services.images import cache_dramadex_poster

            stored = await cache_dramadex_poster(fetched[0], title_id=found.id)
            if stored and stored != series.poster_path:
                series.poster_path = stored
                await session.commit()
                filled.append("poster_path")

    _ = Series  # imported for the type it documents above
    return filled
