"""Playback endpoints: the media file itself, its subtitles, scrubbing cues and cached art.

Everything here is signed-in-only and read-only. Library paths are resolved through
`safe_media_path`; `/data` paths through `safe_child_path`. Neither is optional — the
episode id and image name arrive straight from the URL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SessionDep, get_current_user
from app.config import get_settings
from app.errors import NotFound
from app.models import Episode, MediaFile, SubtitleTrack
from app.services.images import avatar_file
from app.services.scanner import safe_media_path
from app.services.streaming import build_range_response, safe_child_path
from app.services.subtitles import ensure_webvtt

router = APIRouter(prefix="/stream", tags=["stream"], dependencies=[Depends(get_current_user)])

#: Cached art is written under a content-derived name, so it can be pinned indefinitely.
#: `private` rather than `public`: these responses sit behind the session cookie.
IMMUTABLE_CACHE = "private, max-age=31536000, immutable"
#: The media file is large and its ETag is cheap to check, so revalidate rather than guess.
MEDIA_CACHE = "private, max-age=0, must-revalidate"
#: Subtitles and scrubbing cues live at a stable URL and can be regenerated, so they get a
#: short TTL instead of `immutable`.
DERIVED_CACHE = "private, max-age=3600"
#: Avatars appear beside every row of a watch history, so they want a real cache — but the URL
#: is stable and the file behind it can be replaced, so `immutable` would be a lie. The `?v=`
#: token `avatar_url()` appends is what makes a replacement visible at once: the new avatar is
#: simply a different URL. The ETag (size+mtime, from `build_range_response`) then keeps a
#: revalidation of the *old* URL down to a 304 for the client that kept it.
AVATAR_CACHE = "private, max-age=86400, must-revalidate"

#: The `thumbnail_sprite` job writes both files into `DATA_DIR/thumbs/<episode_id>/`; these
#: names are that handler's, and the two routes below have to agree with it exactly.
THUMBNAILS_NAME = "thumbnails.vtt"
SPRITE_NAME = "sprite.jpg"


def playable(path: str) -> Callable[[Any], Any]:
    """Register one handler for both `GET` and `HEAD`.

    `HEAD` is not decoration: a `<video>` element, and every proxy in front of it, probes
    the resource before it starts fetching ranges.

    Two separate routes rather than one `api_route(methods=["GET", "HEAD"])`, because
    FastAPI derives a single operation id per *route object* and then emits it once per
    method — a two-method route therefore produces a duplicate operation id and an invalid
    OpenAPI document. `HEAD` stays out of the schema; it is the same operation with no body.
    """

    def register(endpoint: Any) -> Any:
        router.add_api_route(path, endpoint, methods=["GET"])
        router.add_api_route(
            path,
            endpoint,
            methods=["HEAD"],
            name=f"{endpoint.__name__}_head",
            include_in_schema=False,
        )
        return endpoint

    return register


async def _primary_file(session: AsyncSession, episode_id: str) -> MediaFile:
    episode = await session.get(Episode, episode_id)
    if episode is None:
        raise NotFound("Episode", "EPISODE_NOT_FOUND")
    media = episode.primary_file
    if media is None:
        raise NotFound("Episode", "EPISODE_NOT_AVAILABLE")
    return media


# Declared first so a `kind` of "subtitles" cannot be swallowed by the episode routes below.
@playable("/images/{kind}/{name}")
async def stream_image(
    kind: str,
    name: str,
    request: Request,
) -> Response:
    path = await asyncio.to_thread(safe_child_path, get_settings().images_dir, kind, name)
    return await build_range_response(path, request, cache_control=IMMUTABLE_CACHE)


@playable("/avatars/{user_id}")
async def stream_avatar(user_id: str, request: Request) -> Response:
    """A household member's profile picture.

    Readable by any signed-in member, not just its owner: it is drawn next to their name in
    the member list and in shared watch history, so a picture only its owner could load would
    be a picture nobody ever sees.

    No database lookup — the id is only ever used to build a filename, and `avatar_file`
    sanitizes it to `[A-Za-z0-9_-]` before joining. A user id that does not exist and one
    whose owner never uploaded anything are the same 404, which also means this route cannot
    be used to enumerate accounts.
    """
    path = await asyncio.to_thread(avatar_file, user_id)
    if path is None:
        raise NotFound("Avatar", "AVATAR_NOT_FOUND")
    return await build_range_response(path, request, cache_control=AVATAR_CACHE)


@playable("/{episode_id}/subtitles/{track_id}.vtt")
async def stream_subtitles(
    episode_id: str,
    track_id: str,
    request: Request,
    session: SessionDep,
) -> Response:
    track = await session.get(SubtitleTrack, track_id)
    if track is None:
        raise NotFound("Subtitle track", "SUBTITLE_TRACK_NOT_FOUND")
    media = await session.get(MediaFile, track.media_file_id)
    if media is None or media.episode_id != episode_id:
        raise NotFound("Subtitle track", "SUBTITLE_TRACK_NOT_FOUND")

    path = await ensure_webvtt(session, track)
    return await build_range_response(
        path, request, content_type="text/vtt", cache_control=DERIVED_CACHE
    )


@playable("/{episode_id}/thumbnails.vtt")
async def stream_thumbnails(
    episode_id: str,
    request: Request,
) -> Response:
    path = await asyncio.to_thread(
        safe_child_path, get_settings().thumbs_dir, episode_id, THUMBNAILS_NAME
    )
    if not await asyncio.to_thread(path.is_file):
        raise NotFound("Thumbnails", "THUMBNAILS_NOT_GENERATED")
    return await build_range_response(
        path, request, content_type="text/vtt", cache_control=DERIVED_CACHE
    )


@playable("/{episode_id}/sprite.jpg")
async def stream_sprite(
    episode_id: str,
    request: Request,
) -> Response:
    """The image the cues above point at.

    Not in the contract's route table, but the cues are useless without it: every cue line
    written by `thumbnail_sprite` is the bare relative URL `sprite.jpg`, which the browser
    resolves against the `.vtt` URL — i.e. exactly here. The alternative would be to move
    the sprite into `/data/images` and rewrite the cues, which changes a second module.
    """
    path = await asyncio.to_thread(
        safe_child_path, get_settings().thumbs_dir, episode_id, SPRITE_NAME
    )
    if not await asyncio.to_thread(path.is_file):
        raise NotFound("Thumbnails", "THUMBNAILS_NOT_GENERATED")
    return await build_range_response(
        path, request, content_type="image/jpeg", cache_control=DERIVED_CACHE
    )


@playable("/{episode_id}")
async def stream_episode(
    episode_id: str,
    request: Request,
    session: SessionDep,
) -> Response:
    media = await _primary_file(session, episode_id)
    # `safe_media_path` resolves every component against the NAS mount; that is a syscall
    # per level over the network, so it does not belong on the event loop.
    path = await asyncio.to_thread(safe_media_path, media.path)
    return await build_range_response(path, request, cache_control=MEDIA_CACHE)


__all__ = ["router"]
