"""ffprobe wrapper.

Probing is best-effort by design: a truncated download, a permission problem on the NAS or a
missing ffprobe binary must not abort a scan of ten thousand files. Every failure comes back
as `ProbeOutcome.error` so the caller can persist it in `media_files.scan_error`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from app.services.scanner.parse import VIDEO_EXTENSIONS, file_extension, normalize_language

log = logging.getLogger(__name__)

FFPROBE_BIN: str = os.environ.get("DRAMATARR_FFPROBE", "ffprobe")
PROBE_TIMEOUT_SECONDS: float = 60.0
#: How long a killed ffprobe gets to be reaped before the scan walks away from it.
REAP_GRACE_SECONDS: float = 5.0

#: Containers a browser will play back without transcoding, per the contract.
DIRECT_PLAY_CONTAINERS: frozenset[str] = frozenset({"mp4", "m4v", "webm"})
MKV_VIDEO_CODECS: frozenset[str] = frozenset({"h264", "vp8", "vp9", "av1"})
MKV_AUDIO_CODECS: frozenset[str] = frozenset({"aac", "mp3", "opus", "vorbis", "flac"})

_CONTAINER_ALIASES: dict[str, str] = {
    "matroska": "mkv",
    "matroska,webm": "mkv",
    "mov,mp4,m4a,3gp,3g2,mj2": "mp4",
    "mpegts": "ts",
    "quicktime": "mov",
    "asf": "wmv",
    "flv": "flv",
}
_VIDEO_CODEC_ALIASES: dict[str, str] = {
    "avc": "h264",
    "avc1": "h264",
    "h.264": "h264",
    "x264": "h264",
    "h.265": "hevc",
    "h265": "hevc",
    "x265": "hevc",
    "vp08": "vp8",
    "vp09": "vp9",
    "av01": "av1",
}
_AUDIO_CODEC_ALIASES: dict[str, str] = {
    "mp4a": "aac",
    "mpeg audio": "mp3",
    "mp3float": "mp3",
    "ac-3": "ac3",
    "e-ac-3": "eac3",
}


@dataclass(frozen=True, slots=True)
class VideoStream:
    index: int
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    language: str | None = None
    title: str | None = None
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class AudioStream:
    index: int
    codec: str | None = None
    language: str | None = None
    title: str | None = None
    channels: int | None = None
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class SubtitleStream:
    index: int
    codec: str | None = None
    language: str | None = None
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False


@dataclass(frozen=True, slots=True)
class ProbeResult:
    container: str | None = None
    duration_seconds: float | None = None
    bitrate: int | None = None
    video: list[VideoStream] = field(default_factory=list)
    audio: list[AudioStream] = field(default_factory=list)
    subtitles: list[SubtitleStream] = field(default_factory=list)

    @property
    def duration(self) -> float | None:
        return self.duration_seconds

    @property
    def primary_video(self) -> VideoStream | None:
        for stream in self.video:
            if stream.is_default:
                return stream
        return self.video[0] if self.video else None

    @property
    def primary_audio(self) -> AudioStream | None:
        for stream in self.audio:
            if stream.is_default:
                return stream
        return self.audio[0] if self.audio else None

    @property
    def video_codec(self) -> str | None:
        stream = self.primary_video
        return stream.codec if stream else None

    @property
    def audio_codec(self) -> str | None:
        stream = self.primary_audio
        return stream.codec if stream else None

    @property
    def width(self) -> int | None:
        stream = self.primary_video
        return stream.width if stream else None

    @property
    def height(self) -> int | None:
        stream = self.primary_video
        return stream.height if stream else None

    @property
    def direct_play_ok(self) -> bool:
        return compute_direct_play(self.container, self.video_codec, self.audio_codec)


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    result: ProbeResult | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None


def normalize_container(
    name: str | None, *, path: str | os.PathLike[str] | None = None
) -> str | None:
    """ffprobe reports ambiguous format lists (`matroska,webm`); the extension disambiguates."""
    if path is not None:
        extension = file_extension(str(path))
        if extension in VIDEO_EXTENSIONS:
            return extension
    if not name:
        return None
    key = name.strip().lower()
    if key in _CONTAINER_ALIASES:
        return _CONTAINER_ALIASES[key]
    first = key.split(",")[0].strip()
    return _CONTAINER_ALIASES.get(first, first) or None


def normalize_video_codec(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip().lower()
    return _VIDEO_CODEC_ALIASES.get(key, key) or None


def normalize_audio_codec(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip().lower()
    return _AUDIO_CODEC_ALIASES.get(key, key) or None


def compute_direct_play(
    container: str | None, video_codec: str | None, audio_codec: str | None
) -> bool:
    """mp4/m4v/webm always; mkv only with browser-friendly codecs. The UI warns on a false
    verdict rather than blocking playback — v1 has no transcoder."""
    box = (container or "").strip().lower().lstrip(".")
    if box in DIRECT_PLAY_CONTAINERS:
        return True
    if box != "mkv":
        return False
    video = normalize_video_codec(video_codec)
    audio = normalize_audio_codec(audio_codec)
    return video in MKV_VIDEO_CODECS and audio in MKV_AUDIO_CODECS


# --- ffprobe -------------------------------------------------------------------------------

#: Strong references to reapers we walked away from, so the loop does not garbage-collect a
#: pending task out from under itself.
_reapers: set[asyncio.Task[int]] = set()


def _kill(process: asyncio.subprocess.Process) -> asyncio.Task[int] | None:
    """SIGKILL the child and start reaping it in the background. Returns the reaper task."""
    if process.returncode is not None:
        return None
    with suppress(ProcessLookupError):
        process.kill()
    reaper = asyncio.ensure_future(process.wait())
    _reapers.add(reaper)
    reaper.add_done_callback(_reapers.discard)
    return reaper


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    """Kill the child, wait briefly for it, and give up rather than block the scan.

    `asyncio.Process.wait()` only resolves once *every* stdio pipe has hit EOF — not when the
    process exits. A child that cannot be reaped promptly (ffprobe wedged in uninterruptible
    I/O on a dropped NAS mount, where even SIGKILL does not land, or one that leaked its pipes
    to a grandchild) would otherwise make this await outlast the timeout it is supposed to
    enforce, by minutes or forever. With a single serialized worker that wedges the whole job
    queue, so the reaper is left running in the background and the scan moves on.
    """
    reaper = _kill(process)
    if reaper is None:
        return
    # `asyncio.wait` does not cancel on timeout, which is the point: the task stays alive and
    # completes whenever the kernel finally lets it.
    await asyncio.wait({reaper}, timeout=REAP_GRACE_SECONDS)
    if not reaper.done():
        log.warning("ffprobe pid %s did not exit after SIGKILL; abandoning it", process.pid)


async def probe(
    path: str | os.PathLike[str], *, timeout_seconds: float = PROBE_TIMEOUT_SECONDS
) -> ProbeOutcome:
    """Run ffprobe and return the parsed result or a human-readable error. Never raises."""
    target = os.fspath(path)
    try:
        process = await asyncio.create_subprocess_exec(
            FFPROBE_BIN,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return ProbeOutcome(error=f"ffprobe not found (looked for {FFPROBE_BIN!r})")
    except OSError as exc:
        return ProbeOutcome(error=f"ffprobe could not start: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        # communicate() was cancelled; reap the child so it cannot outlive the scan.
        await _kill_and_reap(process)
        return ProbeOutcome(error=f"ffprobe timed out after {timeout_seconds:.0f}s")
    except asyncio.CancelledError:
        # Already unwinding — kill and let the reaper finish on its own; awaiting it here
        # would hold up a shutdown that is trying to stop the worker.
        _kill(process)
        raise

    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        message = detail[-1] if detail else f"exit code {process.returncode}"
        return ProbeOutcome(error=f"ffprobe failed: {message}"[:500])

    try:
        payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        return ProbeOutcome(error=f"ffprobe returned invalid JSON: {exc}")

    result = parse_probe_payload(payload, path=target)
    if result.container is None and not result.video and not result.audio:
        return ProbeOutcome(error="ffprobe found no usable streams")
    return ProbeOutcome(result=result)


async def probe_file(
    path: str | os.PathLike[str], *, timeout_seconds: float = PROBE_TIMEOUT_SECONDS
) -> ProbeResult | None:
    """Convenience wrapper for callers that do not need the failure reason."""
    return (await probe(path, timeout_seconds=timeout_seconds)).result


def parse_probe_payload(
    payload: dict[str, Any], *, path: str | os.PathLike[str] | None = None
) -> ProbeResult:
    fmt = payload.get("format") or {}
    video: list[VideoStream] = []
    audio: list[AudioStream] = []
    subtitles: list[SubtitleStream] = []

    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        kind = stream.get("codec_type")
        index = _as_int(stream.get("index")) or 0
        disposition = stream.get("disposition") or {}
        tags = {str(k).lower(): v for k, v in (stream.get("tags") or {}).items()}
        language = normalize_language(_as_str(tags.get("language")))
        title = _as_str(tags.get("title"))
        is_default = bool(disposition.get("default"))

        if kind == "video":
            # Embedded cover art is a video stream; counting it would wreck the codec
            # verdict and the reported resolution.
            if disposition.get("attached_pic") or stream.get("codec_name") in {"mjpeg", "png"}:
                continue
            video.append(
                VideoStream(
                    index=index,
                    codec=normalize_video_codec(_as_str(stream.get("codec_name"))),
                    width=_as_int(stream.get("width")) or _as_int(stream.get("coded_width")),
                    height=_as_int(stream.get("height")) or _as_int(stream.get("coded_height")),
                    frame_rate=_as_rate(stream.get("avg_frame_rate"))
                    or _as_rate(stream.get("r_frame_rate")),
                    language=language,
                    title=title,
                    is_default=is_default,
                )
            )
        elif kind == "audio":
            audio.append(
                AudioStream(
                    index=index,
                    codec=normalize_audio_codec(_as_str(stream.get("codec_name"))),
                    language=language,
                    title=title,
                    channels=_as_int(stream.get("channels")),
                    is_default=is_default,
                )
            )
        elif kind == "subtitle":
            subtitles.append(
                SubtitleStream(
                    index=index,
                    codec=_as_str(stream.get("codec_name")),
                    language=language,
                    title=title,
                    is_default=is_default,
                    is_forced=bool(disposition.get("forced")),
                )
            )

    duration = _as_float(fmt.get("duration"))
    if duration is None:
        for stream in payload.get("streams") or []:
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                duration = _as_float(stream.get("duration"))
                if duration is not None:
                    break

    return ProbeResult(
        container=normalize_container(_as_str(fmt.get("format_name")), path=path),
        duration_seconds=duration,
        bitrate=_as_int(fmt.get("bit_rate")),
        video=video,
        audio=audio,
        subtitles=subtitles,
    )


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # drop NaN


def _as_rate(value: Any) -> float | None:
    """ffprobe writes frame rates as `24000/1001`."""
    if value is None:
        return None
    text = str(value).strip()
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        num = _as_float(numerator)
        den = _as_float(denominator)
        if num is None or not den:
            return None
        return num / den
    return _as_float(text)


__all__ = [
    "DIRECT_PLAY_CONTAINERS",
    "FFPROBE_BIN",
    "MKV_AUDIO_CODECS",
    "MKV_VIDEO_CODECS",
    "PROBE_TIMEOUT_SECONDS",
    "REAP_GRACE_SECONDS",
    "AudioStream",
    "ProbeOutcome",
    "ProbeResult",
    "SubtitleStream",
    "VideoStream",
    "compute_direct_play",
    "normalize_audio_codec",
    "normalize_container",
    "normalize_video_codec",
    "parse_probe_payload",
    "probe",
    "probe_file",
]
