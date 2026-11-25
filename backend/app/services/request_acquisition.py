"""Approval -> provider/language discovery -> optional review -> persistent download job."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import Job, JobStatus, JobType, RequestStatus, SeriesRequest
from app.services.acquisition import engine_events, validate_source
from app.workers.queue import JobCanceled, JobContext, enqueue


async def latest_job(session: AsyncSession, request_id: str) -> Job | None:
    return await session.scalar(
        select(Job)
        .where(
            Job.payload["requestId"].as_string() == request_id,
            Job.type.in_([JobType.FIND_SOURCE, JobType.DOWNLOAD_SERIES]),
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    )


def _request_searches(request_id: str) -> Select[Job]:
    return select(Job).where(
        Job.type == JobType.FIND_SOURCE,
        Job.payload["requestId"].as_string() == request_id,
    )


async def cached_search(session: AsyncSession, row: SeriesRequest) -> Job | None:
    if row.status != RequestStatus.APPROVED:
        return None
    searches = await session.scalars(
        _request_searches(row.id)
        .where(Job.status == JobStatus.SUCCEEDED)
        .order_by(Job.created_at.desc(), Job.id.desc())
    )
    return next(
        (
            job
            for job in searches
            if job.payload.get("candidates") and job.payload.get("sourceUrl") == row.source_url
        ),
        None,
    )


async def clear_cached_searches(session: AsyncSession, request_id: str) -> None:
    for search in await session.scalars(_request_searches(request_id)):
        if "candidates" in search.payload:
            search.payload = {k: v for k, v in search.payload.items() if k != "candidates"}


async def queue_search(
    session: AsyncSession,
    row: SeriesRequest,
    *,
    created_by: str | None,
    languages: list[str] | None = None,
    review: bool = True,
    refresh: bool = False,
) -> Job:
    if row.status != RequestStatus.APPROVED:
        raise AppError("INVALID_PAYLOAD", "The request must be approved", 409)
    current = await latest_job(session, row.id)
    if current is not None and current.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
        return current
    preferred = languages or ["es", "en"]
    if not refresh:
        cached = await cached_search(session, row)
        if (
            cached
            and cached.payload.get("languages") == preferred
            and (cached.payload.get("reviewBeforeDownload", True) == review)
        ):
            return cached
    job = await enqueue(
        session,
        JobType.FIND_SOURCE,
        {
            "requestId": row.id,
            "title": row.title,
            "sourceUrl": row.source_url,
            "languages": preferred,
            "reviewBeforeDownload": review,
            "acquisition": {"stage": "searching"},
        },
        created_by_id=created_by,
    )
    job.max_attempts = 1
    return job


async def confirm_candidate(
    session: AsyncSession,
    row: SeriesRequest,
    search: Job,
    url: str,
    created_by: str | None,
) -> Job:
    if row.status != RequestStatus.APPROVED or search.payload.get("requestId") != row.id:
        raise AppError("INVALID_PAYLOAD", "Request is not approved for this search", 409)
    current = await latest_job(session, row.id)
    if (
        current
        and current.type == JobType.DOWNLOAD_SERIES
        and current.status
        in {
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.SUCCEEDED,
        }
    ):
        return current
    if (
        search.type != JobType.FIND_SOURCE
        or search.status != JobStatus.SUCCEEDED
        or search.payload.get("sourceUrl") != row.source_url
    ):
        raise AppError("INVALID_PAYLOAD", "Search has not completed", 409)
    candidate = next((c for c in search.payload.get("candidates", []) if c.get("url") == url), None)
    if candidate is None:
        raise AppError("URL_NOT_ALLOWED", "Select a result from this search", 400)
    job = await enqueue(
        session,
        JobType.DOWNLOAD_SERIES,
        {
            "requestId": row.id,
            "title": row.title,
            "url": validate_source(url),
            "language": candidate.get("language"),
            "provider": candidate.get("provider"),
            "acquisition": {"stage": "queued"},
        },
        created_by_id=created_by,
    )
    job.max_attempts = 1
    return job


async def handle_find_source(ctx: JobContext) -> None:
    payload = ctx.job.payload or {}
    row = await ctx.session.get(SeriesRequest, payload.get("requestId"))
    if row is None or row.status != RequestStatus.APPROVED:
        raise JobCanceled
    # Release the read transaction before progress writes use the separate control session.
    await ctx.session.commit()
    result: dict[str, Any] | None = None
    await ctx.progress(1, "Searching Dramafren")
    async for event in engine_events(
        ctx,
        "discover",
        {
            "query": row.title,
            "sourceUrl": row.source_url,
            "languages": payload.get("languages"),
        },
    ):
        if event.get("kind") == "complete":
            result = event
        else:
            ctx.job.payload = {
                **ctx.job.payload,
                "acquisition": {
                    "stage": "searching",
                    "provider": event.get("provider"),
                    "language": event.get("language"),
                },
            }
            await ctx.progress(10, str(event.get("message") or "Searching Dramafren"))
    if result is None:
        raise AppError("REMOTE_IMPORT_FAILED", "Search ended without confirmation", 502)
    candidates = result.get("candidates") or []
    stage = "awaiting_review" if candidates else "no_match"
    if result.get("errors") and not candidates:
        stage = "search_failed"
    ctx.job.payload = {
        **ctx.job.payload,
        "candidates": candidates,
        "searchErrors": result.get("errors", []),
        "availableLanguages": result.get("availableLanguages", {}),
        "perLanguageLimit": result.get("perLanguageLimit", 10),
        "acquisition": {"stage": stage, "candidatesCount": len(candidates)},
    }
    if stage == "search_failed":
        errors = result.get("errors") or []
        first = errors[0]
        detail = (
            f"{first.get('provider', '?')} / {first.get('language', '?')}: {first.get('error', '')}"
        )
        await ctx.progress(100, "Dramafren search failed")
        raise AppError("REMOTE_IMPORT_FAILED", f"Dramafren search failed ({detail[:350]})", 502)
    await ctx.progress(100, "Review source results" if candidates else "No verified source found")
    # Never infer identity from a translated title. Automatic mode requires exactly one
    # matching provider-published id in the first available preferred language.
    if not payload.get("reviewBeforeDownload", True):
        for language in payload.get("languages", ["es", "en"]):
            exact = [
                c
                for c in candidates
                if c.get("idMatch")
                and (
                    c.get("language", "").lower() == language.lower()
                    or (
                        "-" not in language
                        and c.get("language", "").lower().split("-")[0] == language.lower()
                    )
                )
            ]
            if len(exact) == 1:
                row = await ctx.session.get(SeriesRequest, row.id, populate_existing=True)
                if row is None or row.status != RequestStatus.APPROVED:
                    raise JobCanceled
                job = await enqueue(
                    ctx.session,
                    JobType.DOWNLOAD_SERIES,
                    {
                        "requestId": row.id,
                        "title": row.title,
                        "url": validate_source(exact[0]["url"]),
                        "provider": exact[0].get("provider"),
                        "language": exact[0].get("language"),
                        "acquisition": {"stage": "queued"},
                    },
                    created_by_id=ctx.job.created_by_id,
                )
                job.max_attempts = 1
                await ctx.session.commit()
                break
