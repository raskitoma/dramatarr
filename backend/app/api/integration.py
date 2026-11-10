"""The single authenticated product boundary for catalogue and download engines."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field, field_validator
from sqlalchemy import select

from app.api.deps import AdminUser, SessionDep, require_admin
from app.config import get_settings
from app.errors import AppError
from app.models import Job, JobStatus, JobType
from app.schemas.admin import JobOut
from app.schemas.common import APIModel
from app.services import audit, dramadex
from app.services.acquisition import validate_source
from app.workers.queue import enqueue

router = APIRouter(prefix="/admin/integration", dependencies=[Depends(require_admin)])


class AcquireIn(APIModel):
    url: str = Field(min_length=1, max_length=2000)
    title_id: int | None = Field(default=None, gt=0)


@router.post("/acquire", response_model=JobOut, status_code=202)
async def acquire(body: AcquireIn, session: SessionDep, user: AdminUser) -> Job:
    url = validate_source(body.url)
    settings = get_settings()
    if not settings.downloader_key:
        raise AppError("SOURCE_NOT_CONFIGURED", "Download engine is not configured", 409)
    if body.title_id is not None:
        client = await dramadex.try_client(session)
        if client is None or await client.title(body.title_id) is None:
            raise AppError("NOT_FOUND", "Catalogue title was not found", 404)
    # Repeated clicks must not launch two writers for the same selection.
    existing = (
        await session.execute(
            select(Job).where(
                Job.type == JobType.DOWNLOAD_SERIES,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
    ).scalars()
    for job in existing:
        if (job.payload or {}).get("url") == url:
            return job
    job = await enqueue(
        session,
        JobType.DOWNLOAD_SERIES,
        {"url": url, "titleId": body.title_id},
        created_by_id=user.id,
    )
    job.max_attempts = 1  # Manual retry re-extracts links and reuses the same staging folder.
    await audit.record(session, user.id, "download.create", "job", job.id, {"url": url})
    return job


@router.get("/catalogue")
async def catalogue(
    session: SessionDep,
    q: str = "",
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    client = await dramadex.try_client(session)
    if client is None:
        raise AppError("SOURCE_NOT_CONFIGURED", "Catalogue is not configured", 409)
    page = await client.search(q, limit=limit, cursor=cursor)
    page = await client.detailed(page)
    return {
        "items": [
            {
                "id": t.id,
                "title": t.primary_title,
                "year": t.year,
                "synopsis": t.synopsis,
                "episodeCount": t.episode_count,
            }
            for t in page.items
        ],
        "nextCursor": page.next_cursor,
    }


@router.get("/status")
async def integration_status(session: SessionDep) -> dict[str, Any]:
    settings = get_settings()
    catalogue_ok = False
    client = await dramadex.try_client(session)
    if client is not None:
        try:
            catalogue_ok = bool(await client.stats())
        except AppError:
            pass
    downloader_ok = False
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            response = await http.get(f"{settings.downloader_url.rstrip('/')}/api/health")
            downloader_ok = response.status_code == 200 and response.json().get("status") == "ok"
    except (httpx.HTTPError, ValueError):
        pass
    return {
        "catalogue": catalogue_ok,
        "downloader": downloader_ok,
        "libraryMounted": settings.library_root.is_dir(),
        "stagingMounted": settings.download_staging_root.is_dir(),
    }


@router.api_route("/console/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def catalogue_console(
    path: str,
    request: Request,
    session: SessionDep,
    user: AdminUser,
) -> Response:
    """Keep existing operator tools behind the household admin session, with no shared cookies."""
    import re

    base = "/api/admin/integration/console/"
    if not (
        re.fullmatch(r"admin(?:/[a-z0-9/-]+)?", path)
        or re.fullmatch(r"static/[a-zA-Z0-9_.-]+", path)
        or re.fullmatch(r"titles/[0-9]+/poster", path)
    ) or path in {"admin/login", "admin/logout"}:
        raise AppError("NOT_FOUND", "Unknown catalogue console route", 404)
    client = await dramadex.require_client(session)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
            result = await http.request(
                request.method,
                client.base_url + path,
                params=list(request.query_params.multi_items()),
                content=await request.body(),
                headers={
                    "X-API-Key": client.api_key,
                    "Content-Type": request.headers.get("content-type", ""),
                },
            )
    except httpx.HTTPError as exc:
        raise AppError("DRAMADEX_UNREACHABLE", "Catalogue is unreachable", 502) from exc
    headers = {"Cache-Control": "no-store"}
    if result.is_redirect:
        from urllib.parse import urlsplit

        location = urlsplit(result.headers.get("location", ""))
        if location.scheme or location.netloc or not location.path.startswith("/admin"):
            raise AppError("DRAMADEX_BAD_RESPONSE", "Unexpected catalogue redirect", 502)
        headers["Location"] = base + location.path.lstrip("/")
        if location.query:
            headers["Location"] += "?" + location.query
        if request.method == "POST":
            await audit.record(session, user.id, "catalogue.action", detail={"path": path})
        return Response(status_code=result.status_code, headers=headers)
    content = result.content
    media_type = result.headers.get("content-type", "application/octet-stream").split(";")[0]
    if media_type == "text/html":
        html = result.text
        html = re.sub(
            r'((?:href|src|action)=["\'])/(admin|static|titles)(?=[/"\'])',
            lambda m: m[1] + base + m[2],
            html,
        )
        html = html.replace('href="/docs"', 'href="/api/docs"')
        html = html.replace("Dramadex", "Dramatarr Catalogue")
        content = html.encode()
    return Response(content, status_code=result.status_code, media_type=media_type, headers=headers)


class SearchRequestIn(APIModel):
    languages: list[str] = Field(default_factory=lambda: ["es", "en"], min_length=1, max_length=5)
    review_before_download: bool = True
    refresh: bool = False

    @field_validator("languages")
    @classmethod
    def valid_languages(cls, values: list[str]) -> list[str]:
        from app.schemas.requests import RequestPatchIn

        return RequestPatchIn.validate_languages(values)


class ConfirmSourceIn(APIModel):
    search_job_id: str = Field(min_length=32, max_length=32)
    url: str = Field(min_length=1, max_length=2000)


@router.get("/requests/{request_id}", response_model=JobOut | None)
async def request_acquisition(request_id: str, session: SessionDep) -> Job | None:
    from app.services.request_acquisition import latest_job
    from app.services.requests import get_or_404

    await get_or_404(session, request_id)
    return await latest_job(session, request_id)


@router.get("/requests/{request_id}/results", response_model=JobOut | None)
async def request_search_results(request_id: str, session: SessionDep) -> Job | None:
    from app.services.request_acquisition import cached_search
    from app.services.requests import get_or_404

    return await cached_search(session, await get_or_404(session, request_id))


@router.post("/requests/{request_id}/search", response_model=JobOut, status_code=202)
async def search_request(
    request_id: str,
    body: SearchRequestIn,
    session: SessionDep,
    user: AdminUser,
) -> Job:
    from app.services.request_acquisition import queue_search
    from app.services.requests import get_or_404

    row = await get_or_404(session, request_id)
    return await queue_search(
        session,
        row,
        created_by=user.id,
        languages=body.languages,
        review=body.review_before_download,
        refresh=body.refresh,
    )


@router.post("/requests/{request_id}/download", response_model=JobOut, status_code=202)
async def download_request(
    request_id: str,
    body: ConfirmSourceIn,
    session: SessionDep,
    user: AdminUser,
) -> Job:
    from app.services.request_acquisition import confirm_candidate
    from app.services.requests import get_or_404

    row = await get_or_404(session, request_id)
    job = await session.get(Job, body.search_job_id)
    if job is None:
        raise AppError("NOT_FOUND", "Search was not found", 404)
    return await confirm_candidate(session, row, job, body.url, user.id)
