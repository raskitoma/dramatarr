"""Job handlers and the dispatch table the worker runs them from.

Service imports are deliberately inside the handler bodies: the queue must stay importable
even while the scanner/metadata/image modules are being worked on, and a handler that is
never dispatched should not drag ffmpeg-adjacent code into the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.config import get_settings
from app.errors import AppError, NotFound
from app.models import Episode, JobType, LogLevel
from app.services.settings import SettingsService
from app.workers.queue import JobCanceled, JobContext

log = logging.getLogger(__name__)

JobHandler = Callable[[JobContext], Awaitable[None]]

# --- Sprite tuning ---------------------------------------------------------------------------

SPRITE_TILE_WIDTH = 160
SPRITE_COLUMNS = 10
SPRITE_MAX_TILES = 400
SPRITE_MIN_INTERVAL = 5.0
SPRITE_TIMEOUT_SECONDS = 60 * 30

# --- remote_import limits --------------------------------------------------------------------

REMOTE_IMPORT_MAX_BYTES = 20 * 1024**3
REMOTE_IMPORT_CHUNK = 1024 * 1024
REMOTE_IMPORT_TIMEOUT = 60.0

_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ()\[\]]+")


# --- library_scan ----------------------------------------------------------------------------


async def handle_library_scan(ctx: JobContext) -> None:
    from app.services.scanner.scan import scan_library

    payload = ctx.job.payload or {}
    kwargs: dict[str, Any] = {}
    if payload.get("path"):
        kwargs["path"] = str(payload["path"])
    if "full" in payload:
        kwargs["full"] = bool(payload["full"])
    if "incremental" in payload:
        kwargs["incremental"] = bool(payload["incremental"])

    await ctx.progress(0.0, "Scanning library")
    report = await scan_library(ctx.session, ctx=ctx, **kwargs)
    if report is not None:
        await ctx.log(LogLevel.INFO, str(report)[:2000])
    await ctx.progress(100.0, "Scan complete")


# --- library_rename --------------------------------------------------------------------------


async def handle_library_rename(ctx: JobContext) -> None:
    """Rename one series' files on disk to match the metadata it was matched to.

    A job rather than a request handler for two reasons. It is the only work in the
    application that **writes to the library mount**, so it has to be serialized against the
    scanner — a rename racing a scan would leave the catalogue pointing at names that moved
    while it was reading them, and the queue's concurrency of exactly one is what prevents
    that. And it is the only work whose per-item outcome an admin genuinely needs afterwards:
    which file became which name, and which were skipped and why. Job logs are already that.
    """
    from app.services.renamer import STATUS_OK, apply

    series_id = str((ctx.job.payload or {}).get("seriesId") or "")
    if not series_id:
        raise ValueError("library_rename needs a seriesId")

    tally = await apply(ctx.session, series_id, ctx=ctx)
    await ctx.log(
        LogLevel.INFO,
        ", ".join(f"{status}: {count}" for status, count in sorted(tally.items())),
    )
    if tally.get(STATUS_OK):
        # The catalogue now points at the new names, so anything holding the old ones — a
        # cached search row, an open episode page — is stale.
        await ctx.log(LogLevel.INFO, "Paths updated in the catalogue")


# --- metadata_refresh ------------------------------------------------------------------------


async def handle_metadata_refresh(ctx: JobContext) -> None:
    """Re-read a series from DramaDex.

    This job used to call TMDB. It now calls the household's own indexer, which is the only
    source that catalogues this library — and the swap is why removing TMDB did not break the
    "Refresh metadata" button that admins and the series page both use.
    """
    from app.services.dramadex import refresh_series

    payload = ctx.job.payload or {}
    series_id = _first(payload, "seriesId", "series_id") or ctx.job.series_id
    if not series_id:
        raise AppError("INVALID_PAYLOAD", "metadata_refresh requires seriesId", 400)

    await ctx.progress(0.0, "Refreshing metadata")
    filled = await refresh_series(ctx.session, str(series_id), ctx=ctx)
    await ctx.progress(100.0, f"Metadata refreshed ({', '.join(filled) or 'no changes'})")


# --- image_cache -----------------------------------------------------------------------------


async def handle_image_cache(ctx: JobContext) -> None:
    from app.services.images import cache_episode_still, cache_series_images

    payload = ctx.job.payload or {}
    series_id = _first(payload, "seriesId", "series_id") or ctx.job.series_id
    episode_id = _first(payload, "episodeId", "episode_id") or ctx.job.episode_id
    if not series_id and not episode_id:
        raise AppError("INVALID_PAYLOAD", "image_cache requires seriesId or episodeId", 400)

    if series_id:
        await ctx.progress(10.0, "Caching series images")
        await cache_series_images(ctx.session, str(series_id), ctx=ctx)
    if episode_id:
        await ctx.progress(60.0, "Caching episode still")
        await cache_episode_still(ctx.session, str(episode_id), ctx=ctx)
    await ctx.progress(100.0, "Images cached")


# --- thumbnail_sprite ------------------------------------------------------------------------


async def handle_thumbnail_sprite(ctx: JobContext) -> None:
    """Two ffmpeg passes: extract evenly spaced frames, then tile them into one sheet.

    Splitting the work is what makes progress real — the extraction pass reports the input
    timeline through `-progress`, whereas a single tiling pass only ever emits one frame —
    and it lets the cue file be written from the tiles that actually exist rather than from
    a count guessed off the declared duration.
    """
    from app.services.scanner import safe_media_path

    settings = get_settings()
    payload = ctx.job.payload or {}
    episode_id = str(_first(payload, "episodeId", "episode_id") or ctx.job.episode_id or "")
    if not episode_id:
        raise AppError("INVALID_PAYLOAD", "thumbnail_sprite requires episodeId", 400)

    episode = await ctx.session.get(Episode, episode_id)
    if episode is None:
        raise NotFound("Episode", "EPISODE_NOT_FOUND")
    media = episode.primary_file
    if media is None:
        raise AppError("EPISODE_NOT_AVAILABLE", "Episode has no present media file", 409)

    source = safe_media_path(media.path)
    duration = float(media.duration_seconds or 0.0)
    if duration <= 0:
        await ctx.progress(2.0, "Probing duration")
        duration = await _probe_duration(source)
    if duration <= 0:
        raise AppError("UNKNOWN_DURATION", "Could not determine the media duration", 422)

    interval = max(SPRITE_MIN_INTERVAL, duration / SPRITE_MAX_TILES)
    tile_height = _tile_height(media.width, media.height)

    out_dir = settings.thumbs_dir / episode_id
    frames_dir = out_dir / ".frames"
    with suppress(FileNotFoundError):
        await asyncio.to_thread(shutil.rmtree, frames_dir)
    await asyncio.to_thread(frames_dir.mkdir, parents=True, exist_ok=True)

    try:
        await ctx.progress(5.0, "Extracting frames")
        await _run_ffmpeg(
            ctx,
            [
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                f"fps=1/{interval:.6f},scale={SPRITE_TILE_WIDTH}:{tile_height}",
                "-an",
                "-sn",
                "-dn",
                "-q:v",
                "5",
                "-progress",
                "pipe:1",
                "-nostats",
                str(frames_dir / "%05d.jpg"),
            ],
            on_progress=_frame_progress(ctx, duration),
            timeout_seconds=SPRITE_TIMEOUT_SECONDS,
        )

        frames = sorted(await asyncio.to_thread(lambda: list(frames_dir.glob("*.jpg"))))
        if not frames:
            raise AppError("SPRITE_NO_FRAMES", "ffmpeg produced no frames", 422)

        columns = min(SPRITE_COLUMNS, len(frames))
        rows = math.ceil(len(frames) / columns)
        sprite_tmp = out_dir / "sprite.tmp.jpg"
        await ctx.progress(88.0, f"Tiling {len(frames)} frames")
        await _run_ffmpeg(
            ctx,
            [
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                "1",
                "-start_number",
                "1",
                "-i",
                str(frames_dir / "%05d.jpg"),
                "-vf",
                f"tile={columns}x{rows}",
                "-frames:v",
                "1",
                "-q:v",
                "4",
                str(sprite_tmp),
            ],
            timeout_seconds=SPRITE_TIMEOUT_SECONDS,
        )

        await ctx.progress(95.0, "Writing cues")
        vtt = _build_vtt(
            count=len(frames),
            interval=interval,
            duration=duration,
            columns=columns,
            tile_width=SPRITE_TILE_WIDTH,
            tile_height=tile_height,
        )
        vtt_tmp = out_dir / "thumbnails.tmp.vtt"
        await asyncio.to_thread(vtt_tmp.write_text, vtt, "utf-8")

        # Publish both files only once they are complete, so a partial sprite is never served.
        await asyncio.to_thread(sprite_tmp.replace, out_dir / "sprite.jpg")
        await asyncio.to_thread(vtt_tmp.replace, out_dir / "thumbnails.vtt")
    finally:
        with suppress(FileNotFoundError):
            await asyncio.to_thread(shutil.rmtree, frames_dir)

    await ctx.log(
        LogLevel.INFO,
        f"Sprite: {len(frames)} tiles in {columns}x{rows}, one every {interval:.1f}s",
    )
    await ctx.progress(100.0, "Thumbnails ready")


def _frame_progress(ctx: JobContext, duration: float) -> Callable[[str], Awaitable[None]]:
    async def on_line(line: str) -> None:
        if not line.startswith("out_time_us="):
            return
        raw = line.split("=", 1)[1].strip()
        if not raw or raw == "N/A":
            return
        try:
            seconds = int(raw) / 1_000_000
        except ValueError:
            return
        share = min(1.0, seconds / duration) if duration > 0 else 0.0
        await ctx.progress(5.0 + 80.0 * share, "Extracting frames")

    return on_line


def _tile_height(width: int | None, height: int | None) -> int:
    if width and height and width > 0:
        scaled = SPRITE_TILE_WIDTH * height / width
    else:
        scaled = SPRITE_TILE_WIDTH * 9 / 16
    # h264/mjpeg encoders want even dimensions.
    return max(2, round(scaled / 2) * 2)


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    millis = round((seconds - int(seconds)) * 1000)
    if millis == 1000:  # rounding carry
        millis, secs = 0, secs + 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _build_vtt(
    *,
    count: int,
    interval: float,
    duration: float,
    columns: int,
    tile_width: int,
    tile_height: int,
) -> str:
    lines = ["WEBVTT", ""]
    for index in range(count):
        start = index * interval
        end = min(duration, start + interval) if index == count - 1 else start + interval
        x = (index % columns) * tile_width
        y = (index // columns) * tile_height
        lines.append(f"{_timestamp(start)} --> {_timestamp(end)}")
        lines.append(f"sprite.jpg#xywh={x},{y},{tile_width},{tile_height}")
        lines.append("")
    return "\n".join(lines)


# --- remote_import ---------------------------------------------------------------------------


async def handle_remote_import(ctx: JobContext) -> None:
    """Download exactly one file from the origin an admin configured.

    This handler does not crawl, scrape, follow redirects, parse HTML or discover URLs. It
    takes one URL, checks it against `library.source_base_url`, and streams it to a staging
    directory. Nothing is written into `LIBRARY_ROOT` and nothing is attached to the library.
    """
    import httpx

    settings = get_settings()
    base = (await SettingsService(ctx.session).get_str("library.source_base_url")).strip()
    if not base:
        raise AppError(
            "SOURCE_NOT_CONFIGURED",
            "library.source_base_url is not configured; remote import is disabled",
            409,
        )

    payload = ctx.job.payload or {}
    url = str(payload.get("url") or "").strip()
    if not url:
        raise AppError("INVALID_PAYLOAD", "remote_import requires url", 400)
    filename = _check_source_url(url, base)

    staging = settings.staging_dir / ctx.job.id
    await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=True)
    target = staging / filename
    partial = staging / f"{filename}.part"

    await ctx.progress(0.0, f"Downloading {filename}")
    written = 0
    try:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=REMOTE_IMPORT_TIMEOUT
        ) as client:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    # Following a redirect would leave the origin the admin authorised.
                    raise AppError(
                        "REMOTE_IMPORT_REDIRECTED",
                        "The source redirected; only direct file URLs are accepted",
                        422,
                    )
                if response.status_code >= 400:
                    raise AppError(
                        "REMOTE_IMPORT_FAILED",
                        f"Source responded {response.status_code}",
                        502,
                    )

                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                if content_type in {"text/html", "application/xhtml+xml"}:
                    raise AppError(
                        "REMOTE_IMPORT_NOT_A_FILE",
                        "The URL returned a web page, not a file",
                        422,
                    )

                total = _content_length(response.headers.get("content-length"))
                if total > REMOTE_IMPORT_MAX_BYTES:
                    raise AppError("REMOTE_IMPORT_TOO_LARGE", "File exceeds the size cap", 413)

                handle = await asyncio.to_thread(partial.open, "wb")
                try:
                    async for chunk in response.aiter_bytes(REMOTE_IMPORT_CHUNK):
                        if ctx.cancelled():
                            raise JobCanceled
                        written += len(chunk)
                        if written > REMOTE_IMPORT_MAX_BYTES:
                            raise AppError(
                                "REMOTE_IMPORT_TOO_LARGE", "File exceeds the size cap", 413
                            )
                        await asyncio.to_thread(handle.write, chunk)
                        if total:
                            await ctx.progress(100.0 * written / total, f"Downloading {filename}")
                        else:
                            await ctx.progress(
                                ctx.job.progress_percent, f"Downloaded {written // 1024} KiB"
                            )
                finally:
                    await asyncio.to_thread(handle.close)
    except httpx.HTTPError as exc:
        with suppress(FileNotFoundError):
            await asyncio.to_thread(partial.unlink)
        raise AppError("REMOTE_IMPORT_FAILED", f"Download failed: {exc}", 502) from exc
    except BaseException:
        with suppress(FileNotFoundError):
            await asyncio.to_thread(partial.unlink)
        raise

    await asyncio.to_thread(partial.replace, target)
    await ctx.log(LogLevel.INFO, f"Staged {written} bytes at {target}")
    await ctx.progress(100.0, "Download complete")


def _check_source_url(url: str, base: str) -> str:
    """Accept only a direct file URL on the configured origin. Returns the safe filename."""
    parsed = urlsplit(url)
    configured = urlsplit(base if "//" in base else f"//{base}")

    if parsed.scheme not in {"http", "https"}:
        raise AppError("URL_NOT_ALLOWED", "Only http(s) URLs are accepted", 400)

    def origin(parts: Any) -> tuple[str, str, int]:
        scheme = (parts.scheme or "https").lower()
        # An unknown scheme in the configured base gets port 0, which can never equal the
        # port of an http(s) URL — a misconfigured base refuses everything instead of
        # raising KeyError out of the handler.
        return (scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme, 0))

    if origin(parsed) != origin(configured):
        raise AppError("URL_NOT_ALLOWED", "The URL is not on the configured source origin", 403)

    # The configured path is a directory prefix, not an RFC-3986 base to resolve against:
    # a base of `https://host/media` must not authorise `https://host/anything-else`.
    prefix = configured.path or "/"
    if not prefix.endswith("/"):
        prefix += "/"
    if not parsed.path.startswith(prefix):
        raise AppError("URL_NOT_ALLOWED", "The URL is not under the configured base path", 403)

    # A trailing slash or an extensionless segment is a directory or an index page, and this
    # handler downloads one file — it must never be pointed at something it would have to
    # interpret to use.
    if parsed.path.endswith("/"):
        raise AppError("URL_NOT_ALLOWED", "The URL does not point at a file", 400)
    name = _safe_filename(Path(unquote(parsed.path)).name)
    if not name or "." not in name:
        raise AppError("URL_NOT_ALLOWED", "The URL does not point at a file", 400)
    return name


def _safe_filename(raw: str) -> str:
    cleaned = _UNSAFE_NAME.sub("_", raw).strip().strip(".")
    return cleaned[:200]


def _content_length(raw: str | None) -> int:
    try:
        return max(0, int(raw or 0))
    except ValueError:
        return 0


# --- Subprocess plumbing ---------------------------------------------------------------------


async def _run_ffmpeg(
    ctx: JobContext,
    args: list[str],
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    timeout_seconds: float | None = None,
    binary: str = "ffmpeg",
) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AppError("FFMPEG_NOT_AVAILABLE", f"{binary} is not installed", 503) from exc

    stderr_tail: list[str] = []

    async def pump_stdout() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            if on_progress is not None:
                await on_progress(raw.decode("utf-8", "replace").strip())

    async def pump_stderr() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
            del stderr_tail[:-20]

    async def watch_cancel() -> None:
        while True:
            await asyncio.sleep(0.5)
            if ctx.cancelled():
                with suppress(ProcessLookupError):
                    proc.kill()
                return

    tasks = [
        asyncio.create_task(pump_stdout()),
        asyncio.create_task(pump_stderr()),
        asyncio.create_task(watch_cancel()),
    ]
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
    except TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise AppError("FFMPEG_TIMEOUT", f"{binary} exceeded its time budget", 504) from None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if ctx.cancelled():
        raise JobCanceled
    if code != 0:
        detail = f"{binary} exited {code}: {' '.join(stderr_tail)}"[:1000]
        raise AppError("FFMPEG_FAILED", detail, 500)


async def _probe_duration(path: Path) -> float:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise AppError("FFMPEG_NOT_AVAILABLE", "ffprobe is not installed", 503) from exc

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return 0.0
    if proc.returncode != 0:
        return 0.0
    try:
        data = json.loads(stdout.decode("utf-8", "replace") or "{}")
        return float(data.get("format", {}).get("duration") or 0.0)
    except (ValueError, AttributeError):
        return 0.0


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


# --- Dispatch --------------------------------------------------------------------------------

HANDLERS: dict[JobType, JobHandler] = {
    JobType.LIBRARY_SCAN: handle_library_scan,
    JobType.LIBRARY_RENAME: handle_library_rename,
    JobType.METADATA_REFRESH: handle_metadata_refresh,
    JobType.IMAGE_CACHE: handle_image_cache,
    JobType.THUMBNAIL_SPRITE: handle_thumbnail_sprite,
    JobType.REMOTE_IMPORT: handle_remote_import,
}


def get_handler(job_type: JobType | str) -> JobHandler:
    try:
        resolved = JobType(str(job_type))
    except ValueError as exc:
        raise AppError("UNKNOWN_JOB_TYPE", f"Unknown job type {job_type!r}", 400) from exc
    handler = HANDLERS.get(resolved)
    if handler is None:  # pragma: no cover - the table covers every JobType
        raise AppError("UNKNOWN_JOB_TYPE", f"No handler for {job_type!r}", 400)
    return handler


__all__ = [
    "HANDLERS",
    "JobHandler",
    "get_handler",
    "handle_image_cache",
    "handle_library_scan",
    "handle_metadata_refresh",
    "handle_remote_import",
    "handle_thumbnail_sprite",
]
