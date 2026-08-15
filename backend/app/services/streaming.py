"""HTTP byte-range file serving.

`<video>` is unforgiving here: one wrong `Content-Range`, a missing `Accept-Ranges`, or a
200 where a 206 was asked for, and seeking silently stops working in a way that looks like
a broken file rather than a broken server. So range parsing is strict and every response
header is built in exactly one place.
"""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from re import compile as re_compile
from typing import NamedTuple

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.errors import AppError, NotFound

#: 256 KiB keeps per-chunk thread hand-off cheap while staying well under the socket buffer
#: pressure that smaller chunks cause on a 20 GB remux.
CHUNK_SIZE = 256 * 1024

_DIGITS = re_compile(r"[0-9]+")

_CONTENT_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mts": "video/mp2t",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".vtt": "text/vtt",
    ".srt": "application/x-subrip",
    ".ass": "text/x-ssa",
    ".ssa": "text/x-ssa",
    ".sub": "text/plain",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".gif": "image/gif",
}


class RangeError(ValueError):
    """The `Range` header names a range that cannot be satisfied — the caller answers 416.

    Distinct from a *malformed* or unknown-unit header, which RFC 9110 says to ignore:
    `parse_range_header` returns `None` for those and the caller sends the whole file.
    """


class ByteRange(NamedTuple):
    start: int
    end: int  # inclusive, as on the wire

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def content_type_for(path: Path | str) -> str:
    return _CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def safe_child_path(root: Path, *parts: str) -> Path:
    """Join user-supplied path segments under `root`, refusing anything that escapes it.

    The counterpart to `safe_media_path` for the writable `/data` tree: segments arrive from
    the URL, so `..`, separators and symlinks all have to be ruled out before we open.
    """
    for part in parts:
        if not part or part in {".", ".."} or "/" in part or "\\" in part or "\x00" in part:
            raise NotFound("File", "FILE_NOT_FOUND")
    base = root.resolve()
    candidate = base.joinpath(*parts).resolve()
    if candidate != base and not candidate.is_relative_to(base):
        raise NotFound("File", "FILE_NOT_FOUND")
    return candidate


def parse_range_header(value: str | None, size: int) -> ByteRange | None:
    """Parse `Range` into a single satisfiable byte range.

    Three outcomes, and the difference between the last two is the whole point:

    * a usable range → `ByteRange`, the caller answers 206;
    * **absent or unparseable** → `None`. RFC 9110 §14.2 says a server must ignore a
      `Range` it cannot understand — an unknown unit, a garbled spec — and serve the
      full representation, so this is a 200 and not an error;
    * **understood but unsatisfiable** (start past EOF, inverted, zero-length suffix,
      any range against an empty file) → `RangeError`, the caller answers 416 with
      `Content-Range: bytes */size`.

    Only the `bytes` unit is accepted. Multiple ranges are legal on the wire but every
    browser sends one, and a multipart/byteranges response is not worth the surface, so
    only the first spec is honoured.
    """
    if not value or not value.strip():
        return None

    unit, sep, spec = value.strip().partition("=")
    if not sep or unit.strip().lower() != "bytes":
        return None

    first = spec.split(",")[0].strip()
    start_text, dash, end_text = first.partition("-")
    if not dash:
        return None
    start_text, end_text = start_text.strip(), end_text.strip()

    if start_text and not _DIGITS.fullmatch(start_text):
        return None
    if end_text and not _DIGITS.fullmatch(end_text):
        return None

    if not start_text:
        # Suffix form `bytes=-N`: the last N bytes. N == 0 requests nothing, which RFC 9110
        # defines as unsatisfiable rather than an empty 206.
        if not end_text:
            return None  # a bare `bytes=-` is malformed, not unsatisfiable
        suffix = int(end_text)
        if suffix == 0 or size == 0:
            raise RangeError(value)
        return ByteRange(max(0, size - suffix), size - 1)

    start = int(start_text)
    if start >= size:
        raise RangeError(value)
    end = size - 1 if not end_text else min(int(end_text), size - 1)
    if end < start:
        raise RangeError(value)
    return ByteRange(start, end)


def _etag(st: os.stat_result) -> str:
    return f'"{st.st_size:x}-{st.st_mtime_ns:x}"'


def _etag_matches(header: str, etag: str) -> bool:
    """If-None-Match uses weak comparison, so a `W/` prefix on the client's tag is ignored."""
    for raw in header.split(","):
        candidate = raw.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _if_range_matches(header: str, etag: str, mtime: float) -> bool:
    """True when the client still holds the representation it is asking to continue.

    A mismatch is not an error — the range is dropped and the whole file is sent instead.
    """
    value = header.strip()
    if not value:
        return False
    if value.startswith('"'):
        return value == etag
    if value.startswith("W/"):
        return False  # weak validators may not be used with If-Range
    try:
        supplied = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return False
    if supplied.tzinfo is None:
        supplied = supplied.replace(tzinfo=UTC)
    # HTTP dates carry whole seconds; Last-Modified was truncated the same way.
    return supplied == datetime.fromtimestamp(int(mtime), UTC)


async def _iter_file(path: Path, start: int, end: int) -> AsyncIterator[bytes]:
    """Yield `[start, end]` inclusive, reading in a worker thread so the loop stays free."""
    remaining = end - start + 1
    if remaining <= 0:
        return
    handle = await asyncio.to_thread(open, path, "rb")
    try:
        if start:
            await asyncio.to_thread(handle.seek, start)
        while remaining > 0:
            chunk = await asyncio.to_thread(handle.read, min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        # Sync close: this also runs on client disconnect, where awaiting inside a cancelled
        # generator's finally is not reliable.
        handle.close()


async def build_range_response(
    path: Path | str,
    request: Request,
    *,
    content_type: str | None = None,
    cache_control: str | None = None,
) -> Response:
    """Serve `path`, honouring `Range`, `If-Range`, `If-None-Match` and `HEAD`.

    `path` must already have been validated against its root — `safe_media_path` for the
    library mount, `safe_child_path` for the `/data` tree.
    """
    target = Path(path)
    try:
        st = await asyncio.to_thread(os.stat, target)
    except OSError as exc:
        raise NotFound("File", "FILE_NOT_FOUND") from exc
    if not stat_module.S_ISREG(st.st_mode):
        raise NotFound("File", "FILE_NOT_FOUND")

    size = st.st_size
    etag = _etag(st)
    media_type = content_type or content_type_for(target)
    headers = {
        "accept-ranges": "bytes",
        "etag": etag,
        "last-modified": formatdate(st.st_mtime, usegmt=True),
    }
    if cache_control:
        headers["cache-control"] = cache_control

    # Preconditions are evaluated before Range, per RFC 9110.
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and _etag_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    range_header = request.headers.get("range")
    if range_header is not None:
        if_range = request.headers.get("if-range")
        if if_range is not None and not _if_range_matches(if_range, etag, st.st_mtime):
            # The client is holding a stale copy; ignore its range and resend the whole file.
            range_header = None

    is_head = request.method.upper() == "HEAD"

    try:
        wanted = parse_range_header(range_header, size)
    except RangeError:
        error = AppError(
            "RANGE_NOT_SATISFIABLE",
            "Requested range cannot be satisfied",
            status.HTTP_416_RANGE_NOT_SATISFIABLE,
            {"size": size},
        )
        # Returned rather than raised: 416 is only useful with `Content-Range: bytes */size`,
        # and AppError cannot carry a header through the shared handler. Same wire shape.
        return JSONResponse(
            status_code=error.status_code,
            content=error.to_payload(),
            headers={**headers, "content-range": f"bytes */{size}"},
        )

    if wanted is None:
        headers["content-length"] = str(size)
        if is_head:
            return Response(status_code=status.HTTP_200_OK, headers=headers, media_type=media_type)
        return StreamingResponse(
            _iter_file(target, 0, size - 1),
            status_code=status.HTTP_200_OK,
            headers=headers,
            media_type=media_type,
        )

    headers["content-range"] = f"bytes {wanted.start}-{wanted.end}/{size}"
    headers["content-length"] = str(wanted.length)
    if is_head:
        return Response(
            status_code=status.HTTP_206_PARTIAL_CONTENT, headers=headers, media_type=media_type
        )
    return StreamingResponse(
        _iter_file(target, wanted.start, wanted.end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        headers=headers,
        media_type=media_type,
    )


__all__ = [
    "CHUNK_SIZE",
    "ByteRange",
    "RangeError",
    "build_range_response",
    "content_type_for",
    "parse_range_header",
    "safe_child_path",
]
