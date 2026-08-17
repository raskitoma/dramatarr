"""Poster / backdrop / still caching, avatars, and every image that arrives from outside.

TMDB is the origin for the *automatic* path; the bytes live under `DATA_DIR/images/<series_id>/`
and the models store the path *relative to DATA_DIR*, so the database survives a change of
mount point.

A metadata refresh writes the TMDB source path (`/abc.jpg`) into `poster_path` /
`backdrop_path` / `still_path`; this module turns that into a local relative path
(`images/<series_id>/poster.jpg`). `is_cached_path()` is what tells the two apart.

Three later additions share the bottom half of the file, because they share one pipeline:

* **avatars** — a member uploads a photo of themselves, stored at
  `DATA_DIR/avatars/<user_id>.<ext>`;
* **chosen artwork** — an admin picks a poster or backdrop, by URL or by upload;
* **web image search** — an optional extra source of candidates for that choice.

Every one of those accepts bytes dramatarr did not produce, so all three go through
`sniff_image_format` (magic bytes, never the declared content type or the file name) and then
`reencode`, which decodes with exactly the demuxer and decoder the magic bytes named and writes
a fresh file. **The re-encode is the point, not a formality**: it is what drops the EXIF block,
and a phone photo's EXIF carries GPS coordinates down to the building. Household members are
about to hand each other these files, so the bytes that land on disk must be the pixels and
nothing else.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote, urljoin, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError, NotFound
from app.models import Episode, Series
from app.services.settings import SettingsService
from app.services.tmdb import IMAGE_BASE
from app.utils.ids import new_id

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024

#: The only host this module will download from.
_IMAGE_HOST = (urlsplit(IMAGE_BASE).hostname or "").lower()

POSTER_SIZE = "w500"
BACKDROP_SIZE = "w1280"
STILL_SIZE = "w300"

_ALLOWED_EXT = (".jpg", ".jpeg", ".png", ".webp")
_CHUNK = 64 * 1024
_COMMIT_EVERY = 25
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


class ProgressCtx(Protocol):
    """Structural view of `app.workers.queue.JobContext`; importing it would be a cycle."""

    async def progress(self, percent: float, message: str) -> None: ...
    async def log(self, level: str, message: str) -> None: ...
    def cancelled(self) -> bool: ...


async def _shared_client() -> httpx.AsyncClient:
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=_TIMEOUT,
                limits=httpx.Limits(max_connections=6, max_keepalive_connections=3),
                follow_redirects=True,
            )
    return _client


async def close_image_client() -> None:
    """Call from the FastAPI lifespan shutdown.

    Closes the guarded client too — there are two pools in this module and the lifespan should
    not have to know that.
    """
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()
    await close_guarded_client()


# --- Paths -------------------------------------------------------------------------------


def is_cached_path(value: str | None) -> bool:
    """True when the stored value already points at a locally cached file rather than at a
    TMDB source (`/abc.jpg`) or an absolute URL."""
    if not value:
        return False
    return not value.startswith(("http://", "https://", "/"))


def _safe_component(value: str) -> str:
    cleaned = _UNSAFE.sub("", value or "")
    if not cleaned:
        raise AppError("IMAGE_PATH_INVALID", "Refusing to build an image path from that id")
    return cleaned


def _series_dir(series_id: str) -> Path:
    """Purely lexical, so it stays off the event loop: `_safe_component` has already
    stripped every path separator and dot, which is what makes the join inescapable."""
    root = get_settings().images_dir
    target = root / _safe_component(series_id)
    # Belt and braces against a future change to _safe_component. No filesystem access:
    # both operands are configured paths joined with one sanitized component.
    if not target.is_relative_to(root):
        raise AppError("IMAGE_PATH_INVALID", "Image path escapes the data directory")
    return target


def _relpath(path: Path) -> str:
    """Lexical too — every caller passes a path built from `_series_dir`, and this runs once
    per image inside the caching loop."""
    return path.relative_to(get_settings().data_dir).as_posix()


def _remote_url(value: str, size: str) -> str | None:
    """None for anything that is not a TMDB image.

    A stored value reaches this function straight from the database, so an absolute URL must
    not be fetched blindly: that would turn a metadata refresh into a server-side request to
    an arbitrary host. TMDB is the only origin this module downloads from.
    """
    if value.startswith(("http://", "https://")):
        host = urlsplit(value).hostname or ""
        if value.startswith(f"{IMAGE_BASE}/") or host.lower() == _IMAGE_HOST:
            return value
        return None
    return f"{IMAGE_BASE}/{size}{value}"


def _extension(value: str) -> str:
    suffix = Path(urlsplit(value).path).suffix.lower()
    return suffix if suffix in _ALLOWED_EXT else ".jpg"


def _find_existing_stat(directory: Path, stem: str) -> tuple[Path, os.stat_result] | None:
    """The one file stored for `stem`, whatever extension it landed on, plus its stat.

    The stat comes back with it because every caller that wants the file also wants either
    its size or its mtime, and asking twice is a second syscall for nothing.
    """
    for ext in _ALLOWED_EXT:
        candidate = directory / f"{stem}{ext}"
        try:
            st = candidate.stat()
        except OSError:
            continue
        if stat_module.S_ISREG(st.st_mode) and st.st_size > 0:
            return candidate, st
    return None


def _find_existing(directory: Path, stem: str) -> Path | None:
    found = _find_existing_stat(directory, stem)
    return None if found is None else found[0]


def _resolve_local(relative: str) -> Path | None:
    """Existing, non-empty file for a DATA_DIR-relative path."""
    data_dir = get_settings().data_dir.resolve()
    candidate = (data_dir / relative).resolve()
    if not candidate.is_relative_to(data_dir):
        return None
    try:
        return candidate if candidate.is_file() and candidate.stat().st_size > 0 else None
    except OSError:
        return None


def _write_atomic(dest: Path, data: bytes) -> None:
    """Write via a scratch name so a killed process cannot leave a truncated file in place.

    The scratch name carries a random token rather than being a flat `<dest>.part`. When this
    module only served the caching job that would have been enough — the queue runs one job at
    a time — but avatars and artwork are written straight from a request, and a member who
    double-submits the upload form has two handlers writing the same destination at once. With
    a shared scratch name their bytes interleave and the `os.replace` publishes the mixture;
    with a private one each writes its own file and the last replace simply wins.

    `_find_existing` matches `<stem><ext>` exactly, so an abandoned scratch file left by a
    crash is never served and never counted.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{new_id()[:8]}.part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _drop_stale_siblings(dest: Path, stem: str) -> None:
    """A re-download may land on a different extension; keep exactly one file per kind."""
    for ext in _ALLOWED_EXT:
        sibling = dest.parent / f"{stem}{ext}"
        if sibling != dest and sibling.exists():
            sibling.unlink(missing_ok=True)


# --- Job context helpers -----------------------------------------------------------------


async def _emit(ctx: ProgressCtx | None, percent: float, message: str) -> None:
    if ctx is not None:
        await ctx.progress(percent, message)


async def _log(ctx: ProgressCtx | None, level: str, message: str) -> None:
    log.log({"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 20), message)
    if ctx is not None:
        await ctx.log(level, message)


# --- Download ----------------------------------------------------------------------------


async def _fetch(url: str, *, ctx: ProgressCtx | None) -> bytes | None:
    """Bytes, or None when the image is unavailable or over the cap. Never raises: one bad
    image must not fail a whole series' caching job."""
    client = await _shared_client()
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                await _log(ctx, "warning", f"Image fetch failed ({response.status_code}): {url}")
                return None
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                await _log(ctx, "warning", f"Image exceeds {MAX_IMAGE_BYTES} bytes: {url}")
                return None
            buffer = bytearray()
            async for chunk in response.aiter_bytes(_CHUNK):
                buffer += chunk
                if len(buffer) > MAX_IMAGE_BYTES:
                    await _log(ctx, "warning", f"Image exceeds {MAX_IMAGE_BYTES} bytes: {url}")
                    return None
    except httpx.HTTPError as exc:
        await _log(ctx, "warning", f"Image fetch failed ({exc}): {url}")
        return None
    return bytes(buffer) if buffer else None


async def _cache_one(
    series_id: str,
    stem: str,
    value: str | None,
    size: str,
    *,
    force: bool = False,
    ctx: ProgressCtx | None = None,
) -> str | None:
    """Returns the DATA_DIR-relative path to store, or None to leave the column alone.

    An empty string means "clear the column": the stored path was local but the file is gone,
    so the next metadata refresh should repopulate it from TMDB.
    """
    if not value:
        return None

    if is_cached_path(value):
        # Already local: there is no remote source left to re-download from, so `force`
        # cannot apply. A dangling reference is cleared instead, which lets the next
        # metadata refresh write the TMDB path back in.
        if await asyncio.to_thread(_resolve_local, value) is not None:
            return value
        await _log(ctx, "warning", f"Cached image missing, clearing reference: {value}")
        return ""

    directory = _series_dir(series_id)
    if not force:
        existing = await asyncio.to_thread(_find_existing, directory, stem)
        if existing is not None:
            return _relpath(existing)

    url = _remote_url(value, size)
    if url is None:
        await _log(ctx, "warning", f"Refusing to fetch an image from outside TMDB: {value}")
        return None

    data = await _fetch(url, ctx=ctx)
    if data is None:
        return None

    dest = directory / f"{stem}{_extension(value)}"
    await asyncio.to_thread(_write_atomic, dest, data)
    await asyncio.to_thread(_drop_stale_siblings, dest, stem)
    return _relpath(dest)


def _still_stem(episode_id: str) -> str:
    return f"still-{_safe_component(episode_id)}"


# --- Public API --------------------------------------------------------------------------


async def cache_series_images(
    session: AsyncSession,
    series_id: str,
    *,
    ctx: ProgressCtx | None = None,
    force: bool = False,
    include_stills: bool = True,
) -> None:
    """Cache the poster, the backdrop and — by default — every episode still of the series.

    Stills are folded in here rather than queued per episode: one series is one job, and a
    100-episode show would otherwise flood a strictly serialized queue.
    """
    series = await session.get(Series, series_id)
    if series is None:
        raise NotFound("Series", "SERIES_NOT_FOUND")

    await _emit(ctx, 2, f"Caching images for {series.title}")

    for stem, attr, size in (
        ("poster", "poster_path", POSTER_SIZE),
        ("backdrop", "backdrop_path", BACKDROP_SIZE),
    ):
        result = await _cache_one(
            series.id, stem, getattr(series, attr), size, force=force, ctx=ctx
        )
        if result is not None:
            setattr(series, attr, result or None)
    # Commit, not flush. `ctx.progress` writes the job row through a *separate* control
    # session, i.e. a second connection; a merely flushed change would hold this one's write
    # transaction open across that write and SQLite would block the control session for the
    # full `busy_timeout` before failing it. The poster and backdrop are already on disk at
    # this point, so there is nothing here worth keeping rollback-able.
    await session.commit()
    await _emit(ctx, 10, "Poster and backdrop cached")

    if not include_stills:
        await session.commit()
        await _emit(ctx, 100, "Images cached")
        return

    episodes = list(
        (
            await session.execute(
                select(Episode)
                .where(Episode.series_id == series.id, Episode.still_path.is_not(None))
                .order_by(Episode.season_number, Episode.episode_number)
            )
        ).scalars()
    )

    total = len(episodes)
    for index, episode in enumerate(episodes, start=1):
        if ctx is not None and ctx.cancelled():
            await _log(ctx, "info", "Image caching cancelled")
            break
        result = await _cache_one(
            series.id,
            _still_stem(episode.id),
            episode.still_path,
            STILL_SIZE,
            force=force,
            ctx=ctx,
        )
        if result is not None:
            episode.still_path = result or None
        if index % _COMMIT_EVERY == 0:
            await session.commit()
        await _emit(ctx, 10 + 90 * index / total, f"Stills {index}/{total}")

    await session.commit()
    await _emit(ctx, 100, "Images cached")


async def cache_episode_still(
    session: AsyncSession,
    episode_id: str,
    *,
    ctx: ProgressCtx | None = None,
    force: bool = False,
) -> None:
    episode = await session.get(Episode, episode_id)
    if episode is None:
        raise NotFound("Episode", "EPISODE_NOT_FOUND")
    if not episode.still_path:
        await _emit(ctx, 100, "No still to cache")
        return

    result = await _cache_one(
        episode.series_id,
        _still_stem(episode.id),
        episode.still_path,
        STILL_SIZE,
        force=force,
        ctx=ctx,
    )
    if result is not None:
        episode.still_path = result or None
    await session.commit()
    await _emit(ctx, 100, "Still cached")


async def purge_series_images(series_id: str) -> None:
    """Delete every cached image for a series. The database columns are left to the caller —
    clearing them makes the next metadata refresh repopulate the TMDB source paths."""
    directory = _series_dir(series_id)
    await asyncio.to_thread(shutil.rmtree, directory, True)


# ==========================================================================================
# Images that arrive from outside
# ==========================================================================================
#
# Everything above this line fetches from one hard-coded host. Everything below accepts bytes
# chosen by a person — an upload, or a URL an admin pasted — and so has to prove three things
# before those bytes are allowed to become a file: that they really are an image, that there
# are not too many of them, and that whatever metadata rode along is gone.

MAX_AVATAR_BYTES = 5 * 1024 * 1024
MAX_ARTWORK_BYTES = 10 * 1024 * 1024

#: Avatars are square; the UI renders them at 32-96 px and a retina 2x of the largest is well
#: under this. Bigger buys nothing and costs the NAS.
AVATAR_EDGE = 512
#: Posters and backdrops keep their aspect ratio, bounded on the long edge. A TMDB `original`
#: poster is 2000 px tall, so this stores the best TMDB has without re-encoding it larger.
ARTWORK_EDGE = 2000

#: One still image, however large, decodes in well under this. A file that takes longer is a
#: decode bomb, not a photo.
STILL_TIMEOUT_SECONDS = 20.0

ImageFormat = Literal["png", "jpeg", "webp"]
ArtworkKind = Literal["poster", "backdrop"]

#: How each accepted format is *read back*. Forcing both the demuxer and the decoder means
#: ffmpeg decodes the file as exactly what the magic bytes claimed or not at all — it cannot be
#: talked into probing its way to some other demuxer (a playlist, a concat script) by a file
#: that merely starts with a PNG signature.
_FORMAT_INPUT: dict[str, tuple[str, str]] = {
    "png": ("image2", "png"),
    "jpeg": ("image2", "mjpeg"),
    "webp": ("webp_pipe", "webp"),
}
_FORMAT_SUFFIX: dict[str, str] = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}

#: Output encoders in order of preference. webp is the target the design asks for; the two
#: fallbacks exist because `libwebp` is a *build option* of ffmpeg and an install whose ffmpeg
#: was built without it must still be able to strip EXIF. Which one is in use decides the
#: stored extension, which is why avatars are addressed by stem and not by a fixed filename.
_STILL_ENCODERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("libwebp", ".webp", ("-quality", "82")),
    ("mjpeg", ".jpg", ("-q:v", "3")),
    ("png", ".png", ()),
)

_encoder: tuple[str, str, tuple[str, ...]] | None = None
_encoder_lock = asyncio.Lock()


class Readable(Protocol):
    """Structural view of Starlette's `UploadFile`, so this module stays framework-free."""

    async def read(self, size: int = -1) -> bytes: ...


# --- Magic bytes ---------------------------------------------------------------------------


def sniff_image_format(data: bytes) -> ImageFormat | None:
    """The format of `data` according to its own first bytes, or None.

    Deliberately not `UploadFile.content_type` and deliberately not the file extension: both
    are supplied by the client, so a `.png` that is really a shell script arrives claiming
    `image/png` and named `avatar.png`. The signature is the only part of an upload the
    uploader cannot simply assert.
    """
    if len(data) < 12:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def read_capped(source: Readable, limit: int, code: str) -> bytes:
    """Read an upload, refusing anything over `limit`.

    Starlette has already spooled the request body to disk by the time a handler runs, so this
    does not save the transfer — it caps what is held in memory and what is handed to ffmpeg,
    which is the part that costs.
    """
    buffer = bytearray()
    while chunk := await source.read(64 * 1024):
        buffer += chunk
        if len(buffer) > limit:
            raise AppError(code, f"Image exceeds {limit} bytes", 413, {"maxBytes": limit})
    return bytes(buffer)


# --- Re-encoding ---------------------------------------------------------------------------


async def _ffmpeg_encoders() -> frozenset[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-encoders",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AppError("FFMPEG_NOT_AVAILABLE", "ffmpeg is not installed", 503) from exc
    stdout, _ = await proc.communicate()
    return _parse_encoders(stdout.decode("utf-8", "replace"))


#: ` V....D libwebp   libwebp WebP image` — six flag characters, then the encoder name. The
#: legend above the list has the same first column (` V..... = Video`), which is what the
#: `=` check is for.
_ENCODER_LINE = re.compile(r"^\s*[VAS][0-9A-Z.]{5}\s+(?P<name>[A-Za-z0-9_.-]+)\s+\S")


def _parse_encoders(listing: str) -> frozenset[str]:
    return frozenset(
        match.group("name")
        for line in listing.splitlines()
        if (match := _ENCODER_LINE.match(line)) and match.group("name") != "="
    )


async def _still_encoder() -> tuple[str, str, tuple[str, ...]]:
    """The encoder this ffmpeg build can actually use, resolved once per process."""
    global _encoder
    if _encoder is not None:
        return _encoder
    async with _encoder_lock:
        if _encoder is not None:
            return _encoder
        available = await _ffmpeg_encoders()
        for name, suffix, options in _STILL_ENCODERS:
            if name in available:
                if name != _STILL_ENCODERS[0][0]:
                    log.warning(
                        "ffmpeg was built without %s; re-encoded images will be stored as %s",
                        _STILL_ENCODERS[0][0],
                        suffix,
                    )
                _encoder = (name, suffix, options)
                return _encoder
        raise AppError(
            "FFMPEG_NOT_AVAILABLE", "ffmpeg has no usable still-image encoder", 503
        )


def _scratch_dir() -> Path:
    return get_settings().staging_dir / "images"


#: Centre-crop to a square, then bound it — `min(EDGE, iw)` rather than a flat `EDGE` so a
#: small photo is never upscaled into a blurry one.
def _avatar_filter() -> str:
    return (
        "crop=w='min(iw,ih)':h='min(iw,ih)',"
        f"scale=w='min({AVATAR_EDGE},iw)':h='min({AVATAR_EDGE},ih)':flags=lanczos"
    )


#: Keep the aspect ratio, fit inside a box bounded by the long edge. `decrease` means an image
#: already smaller than the box passes through at its own size.
def _artwork_filter() -> str:
    return (
        f"scale=w='min({ARTWORK_EDGE},iw)':h='min({ARTWORK_EDGE},ih)'"
        ":force_original_aspect_ratio=decrease:flags=lanczos"
    )


async def _run_still_ffmpeg(args: list[str], *, code: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise AppError("FFMPEG_NOT_AVAILABLE", "ffmpeg is not installed", 503) from exc

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=STILL_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise AppError(code, "That image took too long to decode", 400) from exc

    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        detail = tail[-1] if tail else f"ffmpeg exited {proc.returncode}"
        # The caller's bytes are the suspect here, not the server: a file ffmpeg cannot decode
        # is a bad upload, so this is a 400 with the caller's own error code.
        raise AppError(code, f"That image could not be decoded: {detail}"[:300], 400)


async def reencode(data: bytes, *, vfilter: str, code: str) -> tuple[bytes, str]:
    """Decode `data`, apply `vfilter`, encode a fresh single frame. Returns (bytes, suffix).

    This is where EXIF dies. Nothing is copied from the input container: `-map_metadata -1`
    drops the metadata streams, `-frames:v 1` takes one frame from what may be an animation,
    and the encoder writes a file built only from decoded pixels. An orientation tag is applied
    by the decoder before the filters run, so the picture still comes out the right way up.

    Raises `code` (a 400) for anything ffmpeg refuses — the caller names the code so an avatar
    failure and an artwork failure stay distinguishable on the wire.
    """
    fmt = sniff_image_format(data)
    if fmt is None:
        raise AppError(code, "That file is not a PNG, JPEG or WebP image", 400)

    demuxer, decoder = _FORMAT_INPUT[fmt]
    encoder, suffix, options = await _still_encoder()

    scratch = _scratch_dir()
    await asyncio.to_thread(scratch.mkdir, parents=True, exist_ok=True)
    token = new_id()[:12]
    source = scratch / f"{token}{_FORMAT_SUFFIX[fmt]}"
    target = scratch / f"{token}.out{suffix}"

    try:
        await asyncio.to_thread(source.write_bytes, data)
        await _run_still_ffmpeg(
            [
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                demuxer,
                "-c:v",
                decoder,
                "-i",
                str(source),
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                vfilter,
                "-frames:v",
                "1",
                "-c:v",
                encoder,
                *options,
                str(target),
            ],
            code=code,
        )
        produced = await asyncio.to_thread(_read_if_nonempty, target)
    finally:
        await asyncio.to_thread(_unlink, source)
        await asyncio.to_thread(_unlink, target)

    if produced is None:
        raise AppError(code, "That image produced no output", 400)
    return produced, suffix


def _read_if_nonempty(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# --- Avatars -------------------------------------------------------------------------------
#
# There is no `users.avatar_path` column. The file's own existence is the record: an avatar is
# `DATA_DIR/avatars/<user_id>.<ext>` and `avatar_url()` is a stat on a path derived from the id.
# That decision buys two things. It needs no migration, and it cannot drift — a column saying
# "there is an avatar" next to a directory that disagrees is a bug that only shows up as a
# broken image weeks later, and the alternative here has nowhere for that disagreement to live.
# The cost is one stat per profile response, on a local file in the page cache.

AVATAR_ROUTE = "/api/stream/avatars"


def _avatars_dir() -> Path:
    return get_settings().data_dir / "avatars"


def avatar_file(user_id: str) -> Path | None:
    """The stored avatar, or None when the member has never uploaded one."""
    try:
        stem = _safe_component(user_id)
    except AppError:
        return None
    return _find_existing(_avatars_dir(), stem)


def avatar_url(user_id: str) -> str | None:
    """Address for a member's avatar, or None — in which case the UI draws initials.

    The `?v=` token is the same size+mtime pair the served response uses as its ETag. It is
    what lets the response be cached hard: replacing an avatar changes the token, so the
    browser fetches a different URL rather than waiting out a max-age on the old one.
    """
    try:
        stem = _safe_component(user_id)
    except AppError:
        return None
    found = _find_existing_stat(_avatars_dir(), stem)
    if found is None:
        return None
    _, st = found
    return f"{AVATAR_ROUTE}/{quote(user_id, safe='')}?v={st.st_size:x}-{st.st_mtime_ns:x}"


async def store_avatar(user_id: str, data: bytes) -> str:
    """Validate, re-encode and store an uploaded avatar. Returns its DATA_DIR-relative path."""
    encoded, suffix = await reencode(data, vfilter=_avatar_filter(), code="AVATAR_INVALID")
    stem = _safe_component(user_id)
    dest = _avatars_dir() / f"{stem}{suffix}"
    await asyncio.to_thread(_write_atomic, dest, encoded)
    # A member who replaces a JPEG with a WebP must not be left with both, or `_find_existing`
    # would keep serving whichever extension it checks first — the old one.
    await asyncio.to_thread(_drop_stale_siblings, dest, stem)
    return _relpath(dest)


async def delete_avatar(user_id: str) -> bool:
    """Remove every stored avatar for a member. True when there was one."""
    try:
        stem = _safe_component(user_id)
    except AppError:
        return False
    directory = _avatars_dir()
    removed = False
    for ext in _ALLOWED_EXT:
        candidate = directory / f"{stem}{ext}"
        if await asyncio.to_thread(candidate.exists):
            await asyncio.to_thread(_unlink, candidate)
            removed = True
    return removed


# --- Fetching an admin-supplied URL ----------------------------------------------------------
#
# An admin pasting a URL is asking the *server* to make a request, and the server sits inside
# the household network with the NAS, the router's admin page and whatever else runs on the
# box. Left unguarded that is a port scanner with a login form. So: only http(s), every hop
# resolved and checked before it is fetched, and redirects followed by hand rather than by
# httpx, because a public URL that 302s to 169.254.169.254 is the whole attack.

_guarded: httpx.AsyncClient | None = None
_guarded_lock = asyncio.Lock()

MAX_REDIRECTS = 4


async def _guarded_client() -> httpx.AsyncClient:
    """Separate from `_shared_client` for one reason: `follow_redirects=False`.

    The shared client follows redirects itself, which is right for TMDB's CDN and exactly
    wrong here — a followed redirect is a request that was never checked.
    """
    global _guarded
    if _guarded is not None and not _guarded.is_closed:
        return _guarded
    async with _guarded_lock:
        if _guarded is None or _guarded.is_closed:
            _guarded = httpx.AsyncClient(
                timeout=_TIMEOUT,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                follow_redirects=False,
            )
    return _guarded


async def close_guarded_client() -> None:
    global _guarded
    client, _guarded = _guarded, None
    if client is not None and not client.is_closed:
        await client.aclose()


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything that is not a plain, routable, public address.

    The two unwrappings matter more than the flag list: `::ffff:127.0.0.1` is an IPv6 address
    whose `is_loopback` is False, and `2002:7f00:1::` is a 6to4 address wrapping 127.0.0.1.
    Both reach loopback and both would sail past a naive check.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        ip = sixtofour
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_host(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def assert_public_url(url: str) -> None:
    """Refuse a URL that is not http(s), or that points anywhere inside this network.

    Every address the name resolves to is checked, not just the first: a host with one public
    A record and one 127.0.0.1 A record is a bypass, and which one connect() picks is not ours
    to decide. What this cannot close on its own is a rebind between here and the connection —
    the name is resolved twice — so it is a strong filter rather than a proof, and the size cap
    and the format check downstream are what make that acceptable.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise AppError(
            "ARTWORK_URL_INVALID", "Only http(s) image URLs are accepted", 400, {"url": url[:200]}
        )
    host = parts.hostname
    if not host:
        raise AppError("ARTWORK_URL_INVALID", "That URL has no host", 400, {"url": url[:200]})

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_address(literal):
            raise AppError(
                "ARTWORK_URL_BLOCKED",
                "That URL points inside this network",
                400,
                {"host": host},
            )
        return

    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(_resolve_host, host, port)
    except OSError as exc:
        raise AppError(
            "ARTWORK_FETCH_FAILED", f"Could not resolve {host}", 502, {"host": host}
        ) from exc
    if not addresses:
        raise AppError(
            "ARTWORK_FETCH_FAILED", f"Could not resolve {host}", 502, {"host": host}
        )
    for raw in addresses:
        try:
            candidate = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if _is_blocked_address(candidate):
            raise AppError(
                "ARTWORK_URL_BLOCKED",
                "That URL resolves to an address inside this network",
                400,
                {"host": host},
            )


async def fetch_remote_image(url: str, *, max_bytes: int = MAX_ARTWORK_BYTES) -> bytes:
    """Download an admin-supplied image URL, checking every hop before requesting it."""
    client = await _guarded_client()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        await assert_public_url(current)
        try:
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise AppError(
                            "ARTWORK_FETCH_FAILED", "That URL redirected to nowhere", 502
                        )
                    current = str(response.url.join(location))
                    continue
                if response.status_code >= 400:
                    raise AppError(
                        "ARTWORK_FETCH_FAILED",
                        f"That URL returned {response.status_code}",
                        502,
                        {"status": response.status_code},
                    )
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise AppError(
                        "ARTWORK_TOO_LARGE",
                        f"That image exceeds {max_bytes} bytes",
                        413,
                        {"maxBytes": max_bytes},
                    )
                buffer = bytearray()
                async for chunk in response.aiter_bytes(_CHUNK):
                    buffer += chunk
                    if len(buffer) > max_bytes:
                        raise AppError(
                            "ARTWORK_TOO_LARGE",
                            f"That image exceeds {max_bytes} bytes",
                            413,
                            {"maxBytes": max_bytes},
                        )
                if not buffer:
                    raise AppError("ARTWORK_FETCH_FAILED", "That URL returned an empty body", 502)
                return bytes(buffer)
        except httpx.HTTPError as exc:
            raise AppError(
                "ARTWORK_FETCH_FAILED", f"Could not fetch that image: {exc}"[:300], 502
            ) from exc
    raise AppError("ARTWORK_FETCH_FAILED", "That URL redirected too many times", 502)


# --- Chosen artwork --------------------------------------------------------------------------


async def store_series_artwork(series_id: str, kind: ArtworkKind, data: bytes) -> str:
    """Re-encode a chosen poster/backdrop into the series' image directory.

    Written under the same stem the caching job uses, so the file the admin picked simply *is*
    the series' poster from here on — `is_cached_path` reports it as local, and `image_cache`
    leaves a local file alone.
    """
    encoded, suffix = await reencode(data, vfilter=_artwork_filter(), code="ARTWORK_INVALID")
    dest = _series_dir(series_id) / f"{kind}{suffix}"
    await asyncio.to_thread(_write_atomic, dest, encoded)
    await asyncio.to_thread(_drop_stale_siblings, dest, kind)
    return _relpath(dest)


# --- Wishlist posters ------------------------------------------------------------------------
#
# A request card wants a picture, and for this library the picture is almost never TMDB's:
# vertical short-form drama frequently has no TMDB entry at all, so the only source left is the
# household's own image search. That raises a problem the artwork picker never had, because the
# artwork picker is used by an admin sitting on the same network as the SearXNG instance.
#
# A request card is rendered by **every member**, including from outside the house. So the
# image cannot be an `img_src` from a search engine (that hotlinks a third party and tells them
# who is browsing) and it cannot be SearXNG's own proxied thumbnail (a private LAN address that
# resolves to nothing off the network). It has to be served from here.
#
# So the bytes are fetched once, through the same guarded path an admin-supplied artwork URL
# goes through — public hosts only, redirects re-checked, size capped, re-encoded by ffmpeg so
# nothing but pixels survives — and written into `DATA_DIR/images/requests/`, which the existing
# `/api/stream/images/{kind}/{name}` route already serves to signed-in members.

#: The §5 route these files are served from. Spelled out rather than imported: it also lives
#: in `app.services.library`, which imports *this* module, so reaching for it there would be an
#: import cycle. It has to stay in step with `app.main`'s prefix and with `app/api/stream.py`.
_IMAGES_ROUTE = "/api/stream/images"

#: Directory component under `images_dir`, and therefore the `{kind}` segment of the URL.
REQUEST_POSTER_KIND = "requests"

#: Bound on a wishlist poster. Smaller than `MAX_ARTWORK_BYTES` because these arrive from an
#: open-web image search rather than from a catalogue, and a card thumbnail needs no more.
MAX_REQUEST_POSTER_BYTES = 6 * 1024 * 1024


def _requests_dir() -> Path:
    return get_settings().images_dir / REQUEST_POSTER_KIND


def request_poster_url(name: str) -> str:
    """The served path for a cached wishlist poster."""
    return f"{_IMAGES_ROUTE}/{REQUEST_POSTER_KIND}/{quote(name, safe='')}"


def _poster_stem(source_url: str) -> str:
    """A stable filename for a source URL.

    Content-addressed by URL rather than by the bytes, so the same picture asked for twice is
    one download and one file. sha256 truncated to 32 hex characters — this is a cache key, not
    a security boundary, and the full digest makes for needlessly long filenames.
    """
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:32]


async def cache_request_poster(source_url: str) -> str | None:
    """Download, re-encode and store one wishlist poster. Returns its served URL, or None.

    None on every failure, deliberately: a card with no picture is a perfectly good card, and
    an image search that returned a dead link, an HTML page or a 40 MB TIFF must not turn a
    working search into an error. Failures are logged, not raised.
    """
    url = (source_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None

    stem = _poster_stem(url)
    directory = _requests_dir()
    existing = await asyncio.to_thread(_find_existing, directory, stem)
    if existing is not None:
        return request_poster_url(existing.name)

    try:
        raw = await fetch_remote_image(url, max_bytes=MAX_REQUEST_POSTER_BYTES)
        encoded, suffix = await reencode(
            raw, vfilter=_artwork_filter(), code="REQUEST_POSTER_INVALID"
        )
    except AppError as exc:
        log.info("wishlist poster skipped (%s): %s", exc.code, url[:120])
        return None
    except Exception:  # pragma: no cover - defensive; a card must never fail on artwork
        log.warning("wishlist poster failed unexpectedly: %s", url[:120], exc_info=True)
        return None

    dest = directory / f"{stem}{suffix}"
    await asyncio.to_thread(_write_atomic, dest, encoded)
    return request_poster_url(dest.name)


def is_request_poster(value: str | None) -> bool:
    """True for a URL this module produced, i.e. one that is served from this origin."""
    return bool(value) and str(value).startswith(f"{_IMAGES_ROUTE}/{REQUEST_POSTER_KIND}/")


# --- Web image search ------------------------------------------------------------------------
#
# Off unless an admin configures it, and a *supplement* to TMDB rather than a replacement:
# TMDB's gallery needs no configuration and is already the best source for a linked title. This
# exists for the show TMDB has one bad poster for.


@dataclass(slots=True, frozen=True)
class ImageSearchResult:
    url: str
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None
    title: str = ""
    context_url: str | None = None
    #: Which back-end engine produced the hit, when the provider says. SearXNG does; Google
    #: does not. Shown in the picker so an admin can tell *which* of their engines is worth
    #: keeping in `images.search.engines`.
    engine: str = ""


class ImageSearchProvider(Protocol):
    """Deliberately one method wide, so a second provider is a class and nothing else."""

    name: str

    async def search(self, query: str, *, limit: int = 10) -> list[ImageSearchResult]: ...


GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"


class GoogleCseImageSearch:
    """Google's Custom Search JSON API in image mode.

    Needs both a key and a search-engine id; a CSE configured for the whole web is what makes
    it useful for poster hunting. `num` is capped at 10 by the API itself.
    """

    name = "google_cse"

    def __init__(self, api_key: str, cse_id: str) -> None:
        self.api_key = api_key
        self.cse_id = cse_id

    async def search(self, query: str, *, limit: int = 10) -> list[ImageSearchResult]:
        query = (query or "").strip()
        if not query:
            return []
        client = await _shared_client()
        params = {
            "key": self.api_key,
            "cx": self.cse_id,
            "q": query,
            "searchType": "image",
            "num": str(max(1, min(limit, 10))),
            "safe": "active",
        }
        response = await client.get(GOOGLE_CSE_URL, params=params)
        if response.status_code >= 400:
            raise AppError(
                "IMAGE_SEARCH_FAILED",
                f"The image search provider returned {response.status_code}",
                502,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AppError(
                "IMAGE_SEARCH_FAILED", "The image search provider returned a non-JSON body", 502
            ) from exc

        results: list[ImageSearchResult] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            link = item.get("link")
            if not isinstance(link, str) or not link.startswith(("http://", "https://")):
                continue
            raw_image = item.get("image")
            image: dict[str, Any] = raw_image if isinstance(raw_image, dict) else {}
            results.append(
                ImageSearchResult(
                    url=link,
                    thumbnail_url=_as_str(image.get("thumbnailLink")),
                    width=_as_int(image.get("width")),
                    height=_as_int(image.get("height")),
                    title=str(item.get("title") or ""),
                    context_url=_as_str(image.get("contextLink")),
                )
            )
        return results


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_http_url(value: str | None) -> bool:
    return bool(value) and str(value).startswith(("http://", "https://"))


# --- SearXNG ---------------------------------------------------------------------------------
#
# The household's own metasearch instance. No third-party key, no quota, and the query never
# leaves the network — which is the whole reason it is the recommended provider.
#
# ┌── SSRF: the two kinds of URL in this section are NOT the same, and the difference decides
# │   which guard applies. Getting this backwards either breaks the feature or opens a hole,
# │   so it is stated here rather than left to be inferred.
# │
# │ * `base_url` is **trusted**. An admin typed it into the settings form and a self-hosted
# │   SearXNG normally answers on a private address — `http://10.0.50.30:8080`,
# │   `http://searx.local`. Requests to it therefore go through the plain shared client and
# │   deliberately do **not** pass through `assert_public_url`: that guard exists to stop this
# │   server being aimed at the household network, and here the household network *is* the
# │   intended destination.
# │
# │ * `img_src` inside a **result** is **not trusted**. It is an arbitrary URL from the open
# │   internet that a search engine handed us, and it arrives here having been chosen by
# │   nobody. Downloading one keeps going through `fetch_remote_image` → `assert_public_url`
# │   exactly as a pasted URL does: http(s) only, private/loopback/link-local refused, every
# │   redirect hop re-checked, size capped, bytes re-encoded through ffmpeg.
# │
# └── The obvious refactor is to apply one rule to both. Don't.

#: Plain and honest. Some instances filter on User-Agent and an empty one looks like a bot.
SEARXNG_USER_AGENT = "dramatarr/1.0 (self-hosted media library)"

SEARXNG_SEARCH_PATH = "/search"
SEARXNG_CONFIG_PATH = "/config"

#: What `POST /settings/test-search` searches for. Fixed, harmless, and non-empty — the point
#: is to prove the round trip works, not to find anything.
SEARXNG_SAMPLE_QUERY = "poster"
SEARXNG_SAMPLE_LIMIT = 10

#: `"1280x720"`, and the U+00D7 multiplication sign some engines report instead of an `x`.
_RESOLUTION = re.compile(r"(\d{1,6})\s*[x\u00d7]\s*(\d{1,6})")

#: What the fix actually is, in the admin's own settings.yml. Repeated into every message and
#: detail block that names it, because this string is the point of the whole error path.
SEARXNG_JSON_FIX = (
    'add "json" to search.formats in the instance\'s settings.yml and restart it'
)

#: SearXNG's `safesearch` levels, low to high — the index *is* the value the API takes.
SAFE_SEARCH_LEVELS: tuple[str, ...] = ("off", "moderate", "strict")

#: Moderate, not off. This searches the open web for a picture of a television show, and a
#: level nobody could read must never end up searching wider than the admin asked for.
SAFE_SEARCH_DEFAULT = 1


def parse_safe_search(value: Any) -> int:
    """`"strict"` → 2, `2` → 2, `"1"` → 1; anything unreadable → moderate.

    Both spellings genuinely occur and neither is wrong. The settings screen stores the *name*,
    because a row that reads `moderate` says what it does, while `DEFAULTS` and a row edited by
    hand hold SearXNG's own `0`/`1`/`2`. SearXNG's API only speaks the numbers, so the
    translation has to happen somewhere — and if it does not happen here, choosing "strict" in
    the UI is a silent no-op: the stored value reads back correctly, the screen shows exactly
    what the admin picked, and every query still goes out at moderate.
    """
    if isinstance(value, bool):  # `True` is an int; it is not a level.
        return SAFE_SEARCH_DEFAULT
    if isinstance(value, int):
        return max(0, min(2, value))
    text = str(value or "").strip().lower()
    if not text:
        return SAFE_SEARCH_DEFAULT
    if text in SAFE_SEARCH_LEVELS:
        return SAFE_SEARCH_LEVELS.index(text)
    try:
        return max(0, min(2, int(text)))
    except ValueError:
        return SAFE_SEARCH_DEFAULT


@dataclass(slots=True, frozen=True)
class SearxngConfig:
    """Everything the provider needs, already validated and normalized."""

    base_url: str
    engines: tuple[str, ...] = ()
    safe_search: int = SAFE_SEARCH_DEFAULT
    language: str = ""
    timeout: float = 15.0
    api_key: str = ""


@dataclass(slots=True, frozen=True)
class SearxngEngine:
    """One image engine the instance knows about, and whether it is switched on."""

    name: str
    enabled: bool = True
    shortcut: str = ""


@dataclass(slots=True, frozen=True)
class SearxngEngineList:
    base_url: str
    engines: tuple[SearxngEngine, ...] = ()
    version: str | None = None


@dataclass(slots=True, frozen=True)
class SearchDiagnostics:
    """What `POST /api/admin/settings/test-search` reports on success."""

    provider: str
    base_url: str = ""
    #: Only if the instance offers one; `/config` is not guaranteed to be reachable.
    version: str | None = None
    image_engine_count: int = 0
    sample_query: str = ""
    sample_result_count: int = 0


def normalize_base_url(value: str) -> str:
    """`10.0.50.30:8080` → `http://10.0.50.30:8080`, and `.../search` → the origin.

    Both corrections are for what admins actually type. A bare host:port is the address they
    read off their own docker-compose, and a pasted `/search` URL is what the browser's address
    bar shows once they have used the instance — neither is worth an error message.
    """
    base = (value or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    if base.endswith(SEARXNG_SEARCH_PATH):
        base = base[: -len(SEARXNG_SEARCH_PATH)]
    return base.rstrip("/")


def parse_engines(value: str) -> tuple[str, ...]:
    """Comma-separated engine names → a deduped tuple, order preserved."""
    seen: dict[str, None] = {}
    for part in (value or "").split(","):
        name = part.strip()
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


async def searxng_config(settings: SettingsService) -> SearxngConfig | None:
    """The stored SearXNG configuration, or None when there is no base URL to talk to."""
    base_url = normalize_base_url(await settings.get_str("images.search.base_url"))
    if not base_url:
        return None
    return SearxngConfig(
        base_url=base_url,
        engines=parse_engines(await settings.get_str("images.search.engines")),
        safe_search=parse_safe_search(
            await settings.get("images.search.safe_search", SAFE_SEARCH_DEFAULT)
        ),
        language=(await settings.get_str("images.search.language")).strip(),
        # Clamped rather than trusted: a `0` here would mean "no timeout at all", and this
        # request is made from inside a web request.
        timeout=float(max(1, min(120, await settings.get_int("images.search.timeout", 15)))),
        api_key=(await settings.get_str("images.search.api_key")).strip(),
    )


def _searxng_headers(config: SearxngConfig) -> dict[str, str]:
    headers = {"User-Agent": SEARXNG_USER_AGENT, "Accept": "application/json"}
    if config.api_key:
        # Optional for SearXNG itself; this is for the instances that sit behind an
        # authenticating reverse proxy. Sent only when there is something to send, so an
        # ordinary instance never sees an empty bearer header.
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def _searxng_payload(response: httpx.Response, *, path: str) -> dict[str, Any]:
    """Decode a SearXNG response, naming the one failure that is worth naming.

    **SearXNG ships with the JSON API disabled.** An instance in that state answers
    `format=json` with its ordinary HTML page or with a flat 403, and a client that only says
    "the provider returned an error" leaves the admin to discover `search.formats` on their
    own. So a non-JSON answer to a search is not reported as a generic failure: it is reported
    as `SEARXNG_JSON_DISABLED`, with the exact edit in the message.
    """
    body = response.text or ""
    head = body.lstrip()[:1]
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    looks_json = "json" in content_type or head in {"{", "["}

    if response.status_code == 404:
        # Not the disabled-format case: SearXNG serves this path whatever `search.formats`
        # says. A 404 means the base URL is pointing somewhere else — a subpath the reverse
        # proxy does not map, or another service entirely. Saying "enable json" here would be
        # a confident wrong answer.
        raise AppError(
            "SEARXNG_BAD_RESPONSE",
            f"{path} answered HTTP 404. Check that the base URL points at SearXNG itself.",
            502,
            {"status": 404, "path": path},
        )

    if response.status_code == 403 or head == "<" or not looks_json:
        if path == SEARXNG_SEARCH_PATH:
            raise AppError(
                "SEARXNG_JSON_DISABLED",
                f"SearXNG answered format=json with HTTP {response.status_code} "
                f"{content_type or 'and no content type'} instead of JSON. "
                f"SearXNG ships with the JSON API off: {SEARXNG_JSON_FIX}.",
                502,
                {"status": response.status_code, "contentType": content_type,
                 "fix": SEARXNG_JSON_FIX},
            )
        raise AppError(
            "SEARXNG_BAD_RESPONSE",
            f"{path} answered HTTP {response.status_code} without JSON. Check that the base "
            "URL points at SearXNG itself.",
            502,
            {"status": response.status_code, "path": path},
        )

    if response.status_code >= 400:
        raise AppError(
            "SEARXNG_BAD_RESPONSE",
            f"SearXNG returned HTTP {response.status_code}",
            502,
            {"status": response.status_code, "path": path},
        )

    try:
        payload = response.json()
    except ValueError as exc:
        # Declared JSON, did not parse — a truncated body or a proxy that mangled it. Not the
        # disabled-format case, and saying so would send the admin to edit a working file.
        raise AppError(
            "SEARXNG_BAD_RESPONSE",
            "SearXNG declared a JSON response that could not be parsed",
            502,
            {"path": path},
        ) from exc
    if not isinstance(payload, dict):
        raise AppError(
            "SEARXNG_BAD_RESPONSE",
            "SearXNG returned JSON that is not an object",
            502,
            {"path": path},
        )
    return payload


async def _searxng_get(
    config: SearxngConfig, path: str, params: dict[str, str] | None = None
) -> dict[str, Any]:
    """One GET against the *trusted* base URL. See the SSRF note above: no address guard here,
    on purpose — a private address is the normal case for a self-hosted instance."""
    client = await _shared_client()
    try:
        response = await client.get(
            f"{config.base_url}{path}",
            params=params,
            headers=_searxng_headers(config),
            timeout=config.timeout,
        )
    except httpx.HTTPError as exc:
        raise AppError(
            "SEARXNG_UNREACHABLE",
            f"Could not reach SearXNG at {config.base_url}: {exc}"[:300],
            502,
            {"baseUrl": config.base_url},
        ) from exc
    return _searxng_payload(response, path=path)


def _parse_resolution(value: str | None) -> tuple[int | None, int | None]:
    """`"1280x720"` → `(1280, 720)`. Anything else → `(None, None)`.

    Engines report this field freely: with a multiplication sign, as `"unknown"`, or not at
    all. None is a perfectly good answer — the picker shows dimensions when it has them.
    """
    if not value:
        return None, None
    match = _RESOLUTION.search(value)
    if match is None:
        return None, None
    return int(match.group(1)) or None, int(match.group(2)) or None


def _searxng_result(item: Any, config: SearxngConfig) -> ImageSearchResult | None:
    if not isinstance(item, dict):
        return None
    img_src = _as_str(item.get("img_src"))
    if img_src is None or not _is_http_url(img_src):
        # No usable image. A site-relative `/image_proxy?...` lands on the instance itself, and
        # the artwork download guard refuses a private address by design (see the SSRF note),
        # so offering it would be offering a candidate that cannot be applied.
        return None

    thumbnail = _as_str(item.get("thumbnail_src"))
    if thumbnail and not _is_http_url(thumbnail):
        # SearXNG proxies thumbnails through itself and reports them site-relative. This URL is
        # rendered by the *admin's browser*, which is on the same network as the instance, so
        # resolving it against base_url is what makes the picker grid show pictures at all.
        thumbnail = urljoin(f"{config.base_url}/", thumbnail.lstrip("/"))

    width, height = _parse_resolution(_as_str(item.get("resolution")))
    context = _as_str(item.get("url"))
    return ImageSearchResult(
        url=img_src,
        thumbnail_url=thumbnail,
        width=width,
        height=height,
        title=str(item.get("title") or item.get("source") or ""),
        context_url=context if _is_http_url(context) else None,
        engine=_as_str(item.get("engine")) or "",
    )


class SearxngImageSearch:
    """A self-hosted SearXNG instance's JSON API, in image mode.

    `GET {base_url}/search?q=…&format=json&categories=images` plus the configured engines,
    safe-search level and language. Results are normalized into exactly the shape the TMDB
    gallery already returns, so the picker renders both without special-casing either.
    """

    name = "searxng"

    def __init__(self, config: SearxngConfig) -> None:
        self.config = config

    def _params(self, query: str) -> dict[str, str]:
        params = {
            "q": query,
            "format": "json",
            "categories": "images",
            "safesearch": str(self.config.safe_search),
        }
        # Both omitted rather than sent empty: an empty `engines` means "no engines at all" to
        # SearXNG, and an empty `language` overrides the instance's own default with nothing.
        if self.config.engines:
            params["engines"] = ",".join(self.config.engines)
        if self.config.language:
            params["language"] = self.config.language
        return params

    async def search(self, query: str, *, limit: int = 10) -> list[ImageSearchResult]:
        query = (query or "").strip()
        if not query:
            return []
        payload = await _searxng_get(self.config, SEARXNG_SEARCH_PATH, self._params(query))
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise AppError(
                "SEARXNG_BAD_RESPONSE",
                "SearXNG's answer had no results list",
                502,
                {"keys": sorted(payload)[:10]},
            )
        out: list[ImageSearchResult] = []
        for item in rows:
            candidate = _searxng_result(item, self.config)
            if candidate is None:
                continue
            out.append(candidate)
            if len(out) >= limit:
                break
        return out


def _image_engines(payload: dict[str, Any]) -> list[SearxngEngine]:
    """The image engines named in a `/config` document, enabled ones first.

    This is what lets an admin *pick* engines from a real list instead of typing names they
    have to guess — every instance has a different set, and a wrong name is a silent no-op.
    """
    rows = payload.get("engines")
    if not isinstance(rows, list):
        raise AppError(
            "SEARXNG_BAD_RESPONSE", "SearXNG's /config had no engine list", 502
        )
    out: list[SearxngEngine] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        name = _as_str(entry.get("name"))
        categories = entry.get("categories")
        if not name or not isinstance(categories, list):
            continue
        if "images" not in {str(category) for category in categories}:
            continue
        out.append(
            SearxngEngine(
                name=name,
                # Absent means enabled: `/config` omits the flag on engines that are simply on.
                enabled=bool(entry.get("enabled", True)),
                shortcut=_as_str(entry.get("shortcut")) or "",
            )
        )
    out.sort(key=lambda engine: (not engine.enabled, engine.name))
    return out


# --- Provider selection and diagnostics --------------------------------------------------------

#: Provider name → label for the settings screen. Adding one is a line here and a class above.
IMAGE_SEARCH_PROVIDERS: dict[str, str] = {
    "searxng": "SearXNG",
    "google_cse": "Google Custom Search",
}


async def get_image_search(session: AsyncSession) -> ImageSearchProvider | None:
    """The configured provider, or None when the feature is switched off or half-configured.

    None rather than an error: an unconfigured web search is the *default* state of a fresh
    install, and the artwork endpoint has TMDB to fall back on. A provider named but not yet
    filled in is the same thing — an admin halfway through the settings form must not turn the
    artwork picker into a 502.
    """
    settings = SettingsService(session)
    provider = (await settings.get_str("images.search.provider")).strip().lower()

    if provider == "searxng":
        config = await searxng_config(settings)
        return SearxngImageSearch(config) if config is not None else None

    if provider == "google_cse":
        api_key = (await settings.get_str("images.search.api_key")).strip()
        cse_id = (await settings.get_str("images.search.cse_id")).strip()
        if not api_key or not cse_id:
            return None
        return GoogleCseImageSearch(api_key, cse_id)

    return None


async def _configured_searxng(session: AsyncSession) -> SearxngConfig:
    """The SearXNG configuration, or `SEARCH_NOT_CONFIGURED` saying which half is missing."""
    settings = SettingsService(session)
    provider = (await settings.get_str("images.search.provider")).strip().lower()
    if provider != "searxng":
        raise AppError(
            "SEARCH_NOT_CONFIGURED",
            "Set the image search provider to SearXNG first",
            400,
            {"provider": provider},
        )
    config = await searxng_config(settings)
    if config is None:
        raise AppError(
            "SEARCH_NOT_CONFIGURED",
            "SearXNG needs the address of your instance in images.search.base_url",
            400,
            {"provider": provider},
        )
    return config


async def list_search_engines(session: AsyncSession) -> SearxngEngineList:
    """Every image engine the configured instance knows about."""
    config = await _configured_searxng(session)
    payload = await _searxng_get(config, SEARXNG_CONFIG_PATH)
    return SearxngEngineList(
        base_url=config.base_url,
        engines=tuple(_image_engines(payload)),
        version=_as_str(payload.get("version")),
    )


async def diagnose_search(session: AsyncSession) -> SearchDiagnostics:
    """Prove the configured provider actually answers, or raise the code that says why not."""
    settings = SettingsService(session)
    provider = (await settings.get_str("images.search.provider")).strip().lower()

    if provider == "searxng":
        config = await _configured_searxng(session)
        # The search comes first and on its own: `SEARXNG_JSON_DISABLED` is the failure this
        # endpoint exists to name, and it is only visible on a `format=json` request.
        results = await SearxngImageSearch(config).search(
            SEARXNG_SAMPLE_QUERY, limit=SEARXNG_SAMPLE_LIMIT
        )
        version: str | None = None
        engine_count = 0
        try:
            payload = await _searxng_get(config, SEARXNG_CONFIG_PATH)
        except AppError as exc:
            # Search works, `/config` does not — an older instance, or a proxy exposing only
            # `/search`. That is a worse diagnostic, not a failed one.
            log.info("SearXNG /config unavailable (%s); reporting without it", exc.code)
        else:
            version = _as_str(payload.get("version"))
            engine_count = len(_image_engines(payload))
        return SearchDiagnostics(
            provider="searxng",
            base_url=config.base_url,
            version=version,
            image_engine_count=engine_count,
            sample_query=SEARXNG_SAMPLE_QUERY,
            sample_result_count=len(results),
        )

    if provider == "google_cse":
        google = await get_image_search(session)
        if google is None:
            raise AppError(
                "SEARCH_NOT_CONFIGURED",
                "Google Custom Search needs both an API key and a search-engine id",
                400,
                {"provider": provider},
            )
        results = await google.search(SEARXNG_SAMPLE_QUERY, limit=SEARXNG_SAMPLE_LIMIT)
        return SearchDiagnostics(
            provider=google.name,
            sample_query=SEARXNG_SAMPLE_QUERY,
            sample_result_count=len(results),
        )

    raise AppError(
        "SEARCH_NOT_CONFIGURED",
        "No image search provider is configured",
        400,
        {"provider": provider},
    )


__all__ = [
    "ARTWORK_EDGE",
    "AVATAR_EDGE",
    "AVATAR_ROUTE",
    "BACKDROP_SIZE",
    "GOOGLE_CSE_URL",
    "IMAGE_SEARCH_PROVIDERS",
    "MAX_ARTWORK_BYTES",
    "MAX_AVATAR_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_REDIRECTS",
    "POSTER_SIZE",
    "SAFE_SEARCH_DEFAULT",
    "SAFE_SEARCH_LEVELS",
    "SEARXNG_CONFIG_PATH",
    "SEARXNG_JSON_FIX",
    "SEARXNG_SAMPLE_QUERY",
    "SEARXNG_SEARCH_PATH",
    "SEARXNG_USER_AGENT",
    "STILL_SIZE",
    "ArtworkKind",
    "GoogleCseImageSearch",
    "ImageFormat",
    "ImageSearchProvider",
    "ImageSearchResult",
    "ProgressCtx",
    "Readable",
    "SearchDiagnostics",
    "SearxngConfig",
    "SearxngEngine",
    "SearxngEngineList",
    "SearxngImageSearch",
    "assert_public_url",
    "avatar_file",
    "avatar_url",
    "cache_episode_still",
    "cache_series_images",
    "close_guarded_client",
    "close_image_client",
    "delete_avatar",
    "diagnose_search",
    "fetch_remote_image",
    "get_image_search",
    "is_cached_path",
    "list_search_engines",
    "normalize_base_url",
    "parse_engines",
    "parse_safe_search",
    "purge_series_images",
    "read_capped",
    "reencode",
    "searxng_config",
    "sniff_image_format",
    "store_avatar",
    "MAX_REQUEST_POSTER_BYTES",
    "REQUEST_POSTER_KIND",
    "cache_request_poster",
    "is_request_poster",
    "request_poster_url",
    "store_series_artwork",
]


async def cache_dramadex_poster(raw: bytes, *, title_id: int) -> str | None:
    """Store key art fetched from DramaDex. Returns its served URL, or None.

    A separate entry point from `cache_request_poster` because DramaDex's poster route needs
    the API key in a header — the bytes are already in hand by the time they get here, so this
    takes them rather than a URL it could not fetch.

    Keyed by title id rather than by URL: DramaDex serves every poster from the same path per
    title and re-encodes in place when a source rotates its art, so the URL is not a content
    key and hashing it would pin the first version forever.

    None on every failure, like its sibling: a card with no picture is still a card, and a
    poster that will not decode must not turn a working metadata fill into an error.
    """
    if not raw:
        return None

    stem = _poster_stem(f"dramadex:{int(title_id)}")
    directory = _requests_dir()
    existing = await asyncio.to_thread(_find_existing, directory, stem)
    if existing is not None:
        return request_poster_url(existing.name)

    try:
        encoded, suffix = await reencode(
            raw, vfilter=_artwork_filter(), code="DRAMADEX_POSTER_INVALID"
        )
    except AppError as exc:
        log.info("dramadex poster skipped (%s) for title %s", exc.code, title_id)
        return None
    except Exception:  # pragma: no cover - defensive; artwork must never fail a fill
        log.warning("dramadex poster failed for title %s", title_id, exc_info=True)
        return None

    dest = directory / f"{stem}{suffix}"
    await asyncio.to_thread(_write_atomic, dest, encoded)
    return request_poster_url(dest.name)
