"""On-demand subtitle conversion to WebVTT.

`<track>` only understands WebVTT, while the library holds srt/ass/ssa sidecars and streams
embedded in the container. Conversion happens on the first request for a track and the
result is cached under `/data/subs`, so the cost is paid once per track rather than per play.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError, NotFound
from app.models import MediaFile, SubtitleTrack
from app.services.scanner import safe_media_path
from app.utils.ids import new_id

#: A 40-minute episode's subtitle stream converts in well under a second; anything that runs
#: this long is a codec ffmpeg cannot handle, not a slow disk.
FFMPEG_TIMEOUT_SECONDS = 120.0

_locks: dict[str, asyncio.Lock] = {}
_lock_waiters: dict[str, int] = {}
_locks_guard = asyncio.Lock()


@asynccontextmanager
async def _track_lock(track_id: str) -> AsyncIterator[None]:
    """Serialize conversions of one track without leaking a lock per track forever.

    A player opening two <track> elements, or a reload mid-conversion, otherwise runs two
    ffmpeg processes writing the same destination.
    """
    async with _locks_guard:
        lock = _locks.setdefault(track_id, asyncio.Lock())
        _lock_waiters[track_id] = _lock_waiters.get(track_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        async with _locks_guard:
            _lock_waiters[track_id] -= 1
            if _lock_waiters[track_id] <= 0:
                _lock_waiters.pop(track_id, None)
                _locks.pop(track_id, None)


def _extract_failed(detail: str) -> AppError:
    return AppError(
        "SUBTITLE_EXTRACT_FAILED",
        f"Subtitle conversion failed: {detail}",
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


async def _stat(path: Path) -> os.stat_result | None:
    try:
        return await asyncio.to_thread(os.stat, path)
    except OSError:
        return None


async def _resolve_source(session: AsyncSession, track: SubtitleTrack) -> Path:
    # `safe_media_path` walks the NAS mount to resolve symlinks, so it runs in a thread.
    if track.sidecar_path:
        return await asyncio.to_thread(safe_media_path, track.sidecar_path)
    if track.stream_index is None:
        raise NotFound("Subtitle track", "SUBTITLE_NOT_AVAILABLE")
    media = await session.get(MediaFile, track.media_file_id)
    if media is None:
        raise NotFound("Media file", "SUBTITLE_NOT_AVAILABLE")
    return await asyncio.to_thread(safe_media_path, media.path)


def _target_for(track: SubtitleTrack) -> Path:
    return get_settings().subs_dir / f"{track.id}.vtt"


def _record(track: SubtitleTrack, target: Path) -> None:
    """Store the cache location relative to DATA_DIR, matching poster/backdrop paths."""
    relative = str(target.relative_to(get_settings().data_dir))
    if track.extracted_path != relative:
        track.extracted_path = relative


async def _convert(source: Path, target: Path, stream_index: int | None) -> None:
    settings = get_settings()
    await asyncio.to_thread(settings.subs_dir.mkdir, parents=True, exist_ok=True)
    # Convert to a scratch name and rename: a killed ffmpeg must not leave a truncated file
    # sitting at the cache path looking perfectly fresh.
    tmp = target.with_name(f"{target.name}.{new_id()[:8]}.part")

    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
    ]
    if stream_index is not None:
        args += ["-map", f"0:{stream_index}"]
    args += ["-f", "webvtt", str(tmp)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise _extract_failed("ffmpeg is not available") from exc

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
            await proc.wait()
        await _unlink(tmp)
        raise _extract_failed("timed out") from exc

    if proc.returncode != 0:
        await _unlink(tmp)
        message = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise _extract_failed(message[-1] if message else f"ffmpeg exited {proc.returncode}")

    produced = await _stat(tmp)
    if produced is None or produced.st_size == 0:
        await _unlink(tmp)
        raise _extract_failed("produced an empty track")

    await asyncio.to_thread(os.replace, tmp, target)


async def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        await asyncio.to_thread(os.unlink, path)


async def ensure_webvtt(session: AsyncSession, track: SubtitleTrack) -> Path:
    """Return an on-disk WebVTT file for `track`, converting it if needed.

    A sidecar that is already WebVTT is served straight from the library mount; everything
    else lands in `/data/subs/<track_id>.vtt` and is reused while it stays newer than its
    source.
    """
    source = await _resolve_source(session, track)
    source_stat = await _stat(source)
    if source_stat is None:
        raise NotFound("Subtitle source", "SUBTITLE_SOURCE_MISSING")

    if track.sidecar_path and source.suffix.lower() == ".vtt":
        return source

    target = _target_for(track)
    if await _is_fresh(target, source_stat.st_mtime):
        _record(track, target)
        await session.flush()
        return target

    stream_index = None if track.sidecar_path else track.stream_index

    async with _track_lock(track.id):
        # Another request may have finished the conversion while we waited for the lock.
        if not await _is_fresh(target, source_stat.st_mtime):
            await _convert(source, target, stream_index)
        _record(track, target)
        await session.flush()
        return target


async def _is_fresh(target: Path, source_mtime: float) -> bool:
    cached = await _stat(target)
    return cached is not None and cached.st_size > 0 and cached.st_mtime >= source_mtime


__all__ = ["FFMPEG_TIMEOUT_SECONDS", "ensure_webvtt"]
