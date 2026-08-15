"""Async TMDB v3 client.

The API key and language are runtime settings, so a client is built per call site from
`SettingsService` while the underlying `httpx.AsyncClient` — and therefore the connection
pool — is shared process-wide.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"

DEFAULT_LANGUAGE = "es-ES"
MAX_RETRIES = 3

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)
_LIMITS = httpx.Limits(max_connections=8, max_keepalive_connections=4)

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()

#: A classic v3 key can only be sent as a query parameter, so it ends up inside every request
#: URL. httpx logs that URL at INFO — the level §3 ships as the default — which would write
#: the key into the application log on every call, and httpx error strings can carry it too.
_API_KEY_IN_TEXT = re.compile(r"(api_key=)[^&\s'\"]+", re.IGNORECASE)


def _redact(text: str) -> str:
    return _API_KEY_IN_TEXT.sub(r"\1REDACTED", text)


class _RedactApiKeyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "api_key=" in record.msg:
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _redact(str(arg)) if "api_key=" in str(arg) else arg for arg in record.args
            )
        return True


def _install_log_redaction() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactApiKeyFilter) for f in logger.filters):
        logger.addFilter(_RedactApiKeyFilter())


async def _shared_client() -> httpx.AsyncClient:
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _install_log_redaction()
            _client = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=_TIMEOUT,
                limits=_LIMITS,
                headers={"Accept": "application/json"},
                follow_redirects=True,
            )
    return _client


async def close_tmdb_client() -> None:
    """Call from the FastAPI lifespan shutdown."""
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()


def not_configured() -> AppError:
    return AppError(
        "TMDB_NOT_CONFIGURED",
        "TMDB is not configured; set an API key in settings",
        503,
    )


def image_languages(language: str) -> list[str]:
    """`es-ES` → `["es", "en", "null"]`. TMDB tags images with the bare ISO-639-1 code, so
    passing the full locale through would match nothing."""
    base = (language or "").split("-")[0].strip().lower()
    ordered = [code for code in (base, "en") if code]
    seen: list[str] = []
    for code in ordered:
        if code not in seen:
            seen.append(code)
    return [*seen, "null"]


def _retry_after_seconds(response: httpx.Response, fallback: float) -> float:
    raw = response.headers.get("retry-after", "")
    try:
        return min(max(float(raw), 0.5), 30.0)
    except ValueError:
        # The header may also be an HTTP-date; the backoff is a fine substitute.
        return fallback


class TMDBClient:
    def __init__(self, api_key: str, language: str = DEFAULT_LANGUAGE) -> None:
        self.api_key = (api_key or "").strip()
        self.language = (language or "").strip() or DEFAULT_LANGUAGE

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _auth(self) -> tuple[dict[str, str], dict[str, str]]:
        """v4 read tokens are JWTs and only work as a bearer header; classic v3 keys only
        work as a query parameter. Supporting both means an admin can paste either one."""
        if self.api_key.startswith("ey") and self.api_key.count(".") == 2:
            return {"Authorization": f"Bearer {self.api_key}"}, {}
        return {}, {"api_key": self.api_key}

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        if not self.configured:
            raise not_configured()

        headers, auth_params = self._auth()
        query: dict[str, Any] = {"language": self.language, **auth_params}
        for key, value in (params or {}).items():
            if value is not None:
                query[key] = value

        client = await _shared_client()
        backoff = 1.0

        for attempt in range(MAX_RETRIES + 1):
            last = attempt == MAX_RETRIES
            try:
                response = await client.get(path, params=query, headers=headers)
            except httpx.TimeoutException as exc:
                if last:
                    raise AppError("TMDB_UNAVAILABLE", "TMDB timed out", 503) from exc
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            except httpx.HTTPError as exc:
                if last:
                    detail = _redact(str(exc))[:300]
                    raise AppError("TMDB_UNAVAILABLE", f"TMDB unreachable: {detail}", 503) from exc
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            status = response.status_code
            if status == 429:
                if last:
                    raise AppError("TMDB_RATE_LIMITED", "TMDB rate limit exceeded", 503)
                delay = _retry_after_seconds(response, backoff)
                log.warning("TMDB rate limited on %s, sleeping %.1fs", path, delay)
                await asyncio.sleep(delay)
                backoff *= 2
                continue
            if status == 401:
                raise AppError("TMDB_UNAUTHORIZED", "TMDB rejected the API key", 502)
            if status == 404:
                if allow_404:
                    return None
                raise AppError("TMDB_NOT_FOUND", f"TMDB has no resource at {path}", 404)
            if status >= 500:
                if last:
                    raise AppError("TMDB_UNAVAILABLE", f"TMDB returned {status}", 503)
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            if status >= 400:
                raise AppError("TMDB_ERROR", f"TMDB request failed with {status}", 502)

            try:
                payload = response.json()
            except ValueError as exc:
                raise AppError("TMDB_ERROR", "TMDB returned a non-JSON body", 502) from exc
            return payload if isinstance(payload, dict) else {"results": payload}

        raise AppError("TMDB_UNAVAILABLE", "TMDB request exhausted its retries", 503)

    # --- Endpoints ------------------------------------------------------------------------

    async def search_tv(
        self,
        query: str,
        year: int | None = None,
        *,
        language: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search, optionally in a language other than the configured one.

        `language` is not cosmetic. TMDB matches the query against the titles it holds *for
        that language*, so a Korean drama the household knows as 미생 is findable under that
        name only with `language=ko`, and searching the same string under `es-ES` returns
        nothing at all. It is the difference between the match dialog working for an Asian
        drama and not.
        """
        query = (query or "").strip()
        if not query:
            return []
        params: dict[str, Any] = {"query": query, "include_adult": "false"}
        if language:
            params["language"] = language
        if year:
            params["first_air_date_year"] = year
        data = await self._get("/search/tv", params) or {}
        results = data.get("results") or []
        if not results and year:
            # The year filter is exact; a folder year that is off by one should not hide
            # the show entirely.
            retry: dict[str, Any] = {"query": query, "include_adult": "false"}
            if language:
                retry["language"] = language
            data = await self._get("/search/tv", retry) or {}
            results = data.get("results") or []
        return [item for item in results if isinstance(item, dict)]

    async def find_by_external_id(
        self, external_id: str, *, source: str = "imdb_id"
    ) -> list[dict[str, Any]]:
        """TV results for an external id — the `/find` endpoint, which is how an IMDb id is
        turned into a TMDB one. Empty list when nothing matches, never an error: an id typed
        by hand being wrong is an ordinary outcome."""
        external_id = (external_id or "").strip()
        if not external_id:
            return []
        data = (
            await self._get(
                f"/find/{external_id}",
                {"external_source": source},
                allow_404=True,
            )
            or {}
        )
        results = data.get("tv_results") or []
        return [item for item in results if isinstance(item, dict)]

    async def find_any_by_external_id(
        self, external_id: str, *, source: str = "imdb_id"
    ) -> list[dict[str, Any]]:
        """The same lookup, but keeping the **movie** results too.

        `find_by_external_id` is the matching path and is deliberately TV-only: binding a
        series to a film would be wrong. This one exists for the wishlist, where an IMDb id
        found on the web is being used to put a picture and a year on a card — and a short
        drama is filed as a film on IMDb about as often as it is filed as a series. Refusing
        the film there would mean showing nothing at all.

        Each row is tagged with the catalogue it came from, because the caller cannot tell a
        `name` from a `title` otherwise.
        """
        external_id = (external_id or "").strip()
        if not external_id:
            return []
        data = (
            await self._get(
                f"/find/{external_id}", {"external_source": source}, allow_404=True
            )
            or {}
        )
        out: list[dict[str, Any]] = []
        for key, kind in (("tv_results", "tv"), ("movie_results", "movie")):
            for item in data.get(key) or []:
                if isinstance(item, dict):
                    out.append({**item, "_kind": kind})
        return out

    async def search_multi(
        self, query: str, year: int | None = None, *, language: str | None = None
    ) -> list[dict[str, Any]]:
        """Search TV **and** film in one call, tagging each row with which one it is.

        `search_tv` alone is why a lot of this household's library finds nothing: vertical
        short-form drama is catalogued as a film on TMDB at least as often as it is catalogued
        as a series, so asking only the TV index misses it outright. `/search/multi` also
        returns people, which are filtered out here — a drama query matches its lead actor
        constantly, and a person is never something you can ask the household to add.
        """
        query = (query or "").strip()
        if not query:
            return []
        params: dict[str, Any] = {"query": query, "include_adult": "false"}
        if language:
            params["language"] = language
        if year:
            params["year"] = year
        data = await self._get("/search/multi", params) or {}
        rows = data.get("results") or []

        out: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            kind = item.get("media_type")
            if kind not in ("tv", "movie"):
                continue
            out.append({**item, "_kind": kind})
        return out

    async def get_movie(self, tmdb_id: int) -> dict[str, Any]:
        """One film. The counterpart to `get_tv` for the wishlist's card enrichment."""
        data = await self._get(f"/movie/{tmdb_id}", {"append_to_response": "external_ids"})
        return data or {}

    async def get_tv(self, tmdb_id: int) -> dict[str, Any]:
        data = await self._get(
            f"/tv/{tmdb_id}",
            {"append_to_response": "external_ids,content_ratings"},
        )
        return data or {}

    async def get_tv_images(self, tmdb_id: int) -> dict[str, list[dict[str, Any]]]:
        """Every poster and backdrop TMDB holds for a title, in the languages worth showing.

        `include_image_language` is additive to `language`, and the three values it is given
        are the ones an admin actually chooses between: the household's own language, English
        because it is what most artwork is lettered in, and `null` — the textless artwork,
        which is often the only version at a decent resolution.
        """
        include = ",".join(image_languages(self.language))
        data = (
            await self._get(
                f"/tv/{tmdb_id}/images",
                {"include_image_language": include},
                allow_404=True,
            )
            or {}
        )
        return {
            kind: [item for item in (data.get(kind) or []) if isinstance(item, dict)]
            for kind in ("posters", "backdrops")
        }

    async def get_tv_alternative_titles(self, tmdb_id: int) -> list[dict[str, Any]]:
        data = await self._get(f"/tv/{tmdb_id}/alternative_titles", allow_404=True) or {}
        results = data.get("results") or data.get("titles") or []
        return [item for item in results if isinstance(item, dict)]

    async def get_tv_translations(self, tmdb_id: int) -> list[dict[str, Any]]:
        data = await self._get(f"/tv/{tmdb_id}/translations", allow_404=True) or {}
        results = data.get("translations") or []
        return [item for item in results if isinstance(item, dict)]

    async def get_season(
        self,
        tmdb_id: int,
        season_number: int,
        *,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        """`language` overrides the configured one; used to pull native-language episode
        titles alongside the localized ones."""
        return await self._get(
            f"/tv/{tmdb_id}/season/{season_number}",
            {"language": language},
            allow_404=True,
        )

    async def validate_key(self) -> bool:
        """True when TMDB accepts the key. Network problems still raise."""
        if not self.configured:
            raise not_configured()
        try:
            await self._get("/configuration")
        except AppError as exc:
            if exc.code == "TMDB_UNAUTHORIZED":
                return False
            raise
        return True


# --- Factories ---------------------------------------------------------------------------


async def get_tmdb_client(session: AsyncSession) -> TMDBClient:
    """Raises `TMDB_NOT_CONFIGURED` when there is no key or TMDB is switched off."""
    client = await try_tmdb_client(session)
    if client is None:
        raise not_configured()
    return client


async def try_tmdb_client(session: AsyncSession) -> TMDBClient | None:
    """`None` instead of an error, for background work that should degrade quietly."""
    settings = SettingsService(session)
    if not await settings.get_bool("tmdb.enabled", True):
        return None
    api_key = await settings.get_str("tmdb.api_key")
    if not api_key.strip():
        return None
    language = await settings.get_str("tmdb.language", DEFAULT_LANGUAGE)
    return TMDBClient(api_key, language)


async def validate_api_key(session: AsyncSession, api_key: str | None = None) -> bool:
    """Validate the stored key, or a candidate one before it is saved."""
    if api_key is not None and api_key.strip():
        language = await SettingsService(session).get_str("tmdb.language", DEFAULT_LANGUAGE)
        return await TMDBClient(api_key, language).validate_key()
    return await (await get_tmdb_client(session)).validate_key()


__all__ = [
    "API_BASE",
    "DEFAULT_LANGUAGE",
    "IMAGE_BASE",
    "MAX_RETRIES",
    "TMDBClient",
    "close_tmdb_client",
    "get_tmdb_client",
    "image_languages",
    "not_configured",
    "try_tmdb_client",
    "validate_api_key",
]
