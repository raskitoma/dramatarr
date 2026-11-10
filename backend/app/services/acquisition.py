"""Download into a separate mount, then publish complete files without replacing data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.errors import AppError
from app.models import LogLevel, Series
from app.workers.queue import JobCanceled, JobContext

PROVIDERS = {
    "reelshort",
    "dramabox",
    "goodshort",
    "dramawave",
    "netshort",
    "shortmax",
    "flickreels",
    "radreel",
    "stardusttv",
}
SOURCE_HOSTS = {"dramafren.org", "www.dramafren.org"} | {f"{p}.dramafren.org" for p in PROVIDERS}


def validate_source(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise AppError("URL_NOT_ALLOWED", "Invalid watch URL", 400) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in SOURCE_HOSTS
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        raise AppError("URL_NOT_ALLOWED", "Use an HTTPS dramafren.org watch URL", 400)
    return url.strip()


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def publish_download(source: Path, library: Path, job_id: str) -> str:
    """Copy from staging across filesystems; link the completed copy atomically, never replace.

    Each job owns a new folder. Retrying that same job accepts only byte-identical files.
    Staging is retained, including resumable partials and original sidecars.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid job identifier")
    if source.is_symlink() or not source.is_dir() or not library.is_dir():
        raise ValueError("Staging and library mounts must be present")
    # Never merge an acquisition into a pre-existing series on title similarity.
    name = f"{source.name[:100]} [download-{job_id}]"
    target = library / name
    target.mkdir(exist_ok=True)
    if target.is_symlink() or target.resolve().parent != library.resolve():
        raise ValueError("Unsafe destination")
    files = sorted(source.iterdir())
    videos = [p for p in files if p.suffix.lower() in {".mp4", ".mkv", ".ts", ".webm"}]
    if not videos:
        raise ValueError("No completed video files to publish")
    for item in files:
        if item.is_symlink():
            raise ValueError("Symlinks are not accepted in downloads")
        if not item.is_file() or item.suffix.lower() not in {
            ".mp4",
            ".mkv",
            ".ts",
            ".webm",
            ".srt",
            ".vtt",
            ".ass",
        }:
            continue
        dest = target / item.name
        if dest.exists() or dest.is_symlink():
            if dest.is_symlink() or not dest.is_file() or _digest(dest) != _digest(item):
                raise FileExistsError(f"Existing file differs: {dest.name}")
            continue
        # Temporary copy lives on the destination filesystem even if staging does not.
        fd, tmp = tempfile.mkstemp(prefix=".publish-", suffix=".part", dir=target)
        try:
            with os.fdopen(fd, "wb") as out, item.open("rb") as incoming:
                shutil.copyfileobj(incoming, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(tmp, 0o644)
            os.link(tmp, dest)  # Atomic publication with O_EXCL semantics.
        finally:
            Path(tmp).unlink(missing_ok=True)
    return name


_ENGINE_FAILURES = {
    "provider_access": "Provider access was blocked or its browser challenge did not finish",
    "timeout": "Provider operation timed out",
    "no_playable_episodes": "The provider page did not expose playable episodes",
    "extraction_failed": "Could not extract episode links",
    "transfer_failed": "Could not transfer episodes",
}


def _engine_failure(event: dict[str, Any]) -> str:
    phase = {"extracting": "Extraction", "downloading": "Download"}.get(
        str(event.get("phase")), "Source operation"
    )
    reason = event.get("reason")
    status = event.get("httpStatus")
    if reason == "provider_http" and type(status) is int and 400 <= status <= 599:
        return f"{phase}: provider returned HTTP {status}"
    detail = _ENGINE_FAILURES.get(str(reason))
    return f"{phase}: {detail}" if detail else "Source operation failed; retry available"


async def engine_events(
    ctx: JobContext,
    operation: str,
    body: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    settings = get_settings()
    if not settings.downloader_key:
        raise AppError("SOURCE_NOT_CONFIGURED", "Download engine is not configured", 409)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=60)) as client:
        async with client.stream(
            "POST",
            f"{settings.downloader_url.rstrip('/')}/api/{operation}",
            headers={"X-Worker-Key": settings.downloader_key},
            json=body,
        ) as response:
            if response.status_code != 200:
                raise AppError("REMOTE_IMPORT_FAILED", "Download engine refused the operation", 502)
            async for line in response.aiter_lines():
                if ctx.cancelled():
                    raise JobCanceled
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event.get("kind") == "error":
                    raise AppError(
                        "REMOTE_IMPORT_FAILED", _engine_failure(event), 502
                    )
                yield event


async def handle_download_series(ctx: JobContext) -> None:
    settings = get_settings()
    if not settings.downloader_key:
        raise AppError("SOURCE_NOT_CONFIGURED", "Download engine is not configured", 409)
    payload = ctx.job.payload or {}
    url = validate_source(str(payload.get("url") or ""))
    # Never trust a client-supplied filesystem path or the engine's absolute folder path.
    job_id = ctx.job.id
    await ctx.progress(1, "Extracting episodes")
    done: dict[str, Any] | None = None
    async for event in engine_events(ctx, "acquire", {"url": url, "jobId": job_id}):
        if event.get("kind") == "complete":
            done = event
        else:
            previous = ctx.job.payload.get("acquisition", {})
            details = {
                **previous,
                **{k: v for k, v in event.items() if k not in {"kind", "message"}},
            }
            details["stage"] = "downloading" if event.get("episodesTotal") else "extracting"
            ctx.job.payload = {**ctx.job.payload, "acquisition": details}
            total = max(1, int(details.get("episodesTotal") or 1))
            completed = int(details.get("episodesCompleted") or 0)
            fraction = (
                float(details.get("currentEpisodePercent") or 0) / 100
                if event.get("stage") not in {"episode_done", "episode_skipped", "all_done"}
                else 0
            )
            await ctx.progress(
                min(85, 5 + 80 * (completed + min(fraction, 0.99)) / total),
                str(event.get("message") or "Downloading episodes")[:500],
            )
    if done is None or not done.get("cleanTitle"):
        raise AppError("REMOTE_IMPORT_FAILED", "Download ended without confirmation", 502)
    title = str(done["cleanTitle"])
    if title in {".", ".."} or Path(title).name != title or "\\" in title:
        raise ValueError("Unsafe download folder")
    if int(done.get("failedCount") or 0) or not int(done.get("downloadedCount") or 0):
        raise AppError("REMOTE_IMPORT_FAILED", "Some episodes failed; retry to resume", 502)
    source = settings.download_staging_root / job_id / title
    root = settings.download_staging_root.resolve()
    if not source.resolve().is_relative_to(root) or (root / job_id).is_symlink():
        raise ValueError("Staged folder escapes its mount")
    if ctx.cancelled():
        raise JobCanceled
    request = None
    if payload.get("requestId"):
        from app.models import RequestStatus, SeriesRequest

        request = await ctx.session.get(SeriesRequest, payload["requestId"], populate_existing=True)
        if request is None or request.status != RequestStatus.APPROVED:
            raise JobCanceled
        await ctx.session.commit()
    ctx.job.payload = {
        **ctx.job.payload,
        "acquisition": {
            **ctx.job.payload.get("acquisition", {}),
            "stage": "importing",
            "episodesCompleted": int(done.get("downloadedCount") or 0),
            "episodesTotal": int(done.get("episodesTotal") or done.get("downloadedCount") or 0),
        },
    }
    await ctx.progress(88, "Publishing completed files")
    folder = await asyncio.to_thread(publish_download, source, settings.library_root, job_id)
    from app.services.scanner.scan import scan_library

    report = await scan_library(ctx.session, path=folder, incremental=False)
    if report.errors:
        raise AppError("REMOTE_IMPORT_FAILED", "Files published; library scan needs retry", 502)
    series = await ctx.session.scalar(select(Series).where(Series.library_path == folder))
    if series is None:
        raise AppError("REMOTE_IMPORT_FAILED", "Files published but no series indexed", 502)
    metadata_id = payload.get("titleId")
    if metadata_id is None and request is not None:
        from app.services import dramadex

        # Bind only by the original platform-published id, never by title similarity.
        original = urlsplit(request.source_url or "")
        provider = next(
            (
                p
                for p in PROVIDERS
                if original.hostname in {f"{p}.com", f"www.{p}.com", f"{p}.dramafren.org"}
            ),
            None,
        )
        params = parse_qs(original.query)
        external_id = next(
            (params[k][0] for k in ("id", "bookId", "book_id") if params.get(k)), None
        )
        external_id = external_id or original.path.rstrip("/").rsplit("/", 1)[-1]
        client = await dramadex.try_client(ctx.session)
        await ctx.session.commit()
        if client and provider and external_id:
            try:
                page = await client.search(request.title, limit=20)
                exact = {
                    t.id
                    for t in page.items
                    if any(s.key == provider and s.external_id == external_id for s in t.studios)
                }
                if len(exact) == 1:
                    metadata_id = exact.pop()
            except AppError:
                metadata_id = None
    if metadata_id is not None:
        from app.services.dramadex import refresh_series

        series.dramadex_id = int(metadata_id)
        await ctx.session.commit()
        await refresh_series(ctx.session, series.id)
    await ctx.session.commit()
    await ctx.log(LogLevel.INFO, f"Imported {folder}; staging retained for recovery")
    if payload.get("requestId"):
        from app.services.requests import _announce_ready
        from app.utils.time import utcnow

        request = await ctx.session.get(SeriesRequest, payload["requestId"], populate_existing=True)
        if request is not None and request.status == RequestStatus.APPROVED:
            from sqlalchemy import update

            changed = await ctx.session.scalar(
                update(SeriesRequest)
                .where(
                    SeriesRequest.id == request.id,
                    SeriesRequest.status == RequestStatus.APPROVED,
                )
                .values(
                    series_id=series.id,
                    status=RequestStatus.FULFILLED,
                    resolved_at=utcnow(),
                    resolved_by_user_id=ctx.job.created_by_id,
                    seen_at=None,
                )
                .returning(SeriesRequest.id)
            )
            await ctx.session.commit()
            if changed:
                await ctx.session.refresh(request)
                await _announce_ready(ctx.session, request)
    ctx.job.series_id = series.id
    ctx.job.payload = {
        **ctx.job.payload,
        "acquisition": {
            **ctx.job.payload.get("acquisition", {}),
            "stage": "complete",
        },
    }
    await ctx.progress(100, "Download imported into library")
