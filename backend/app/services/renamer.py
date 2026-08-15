"""Renaming media files on disk to match the metadata they were matched to.

This is the one module in dramatarr that **writes to the library mount**. Everything else
treats `LIBRARY_ROOT` as read-only, and until this existed the mount was literally `:ro` and the
import assistant emitted a shell script for the operator to run by hand. That was the right
default and it stopped being enough: once a series is matched, the files still carry the CDN
object ids they arrived with, and a library whose filenames mean nothing is a library that has
to be re-matched by hand every time it is rescanned.

So the rule is no longer "never write" — it is **write only these bytes, only this way**:

* **Rename in place, never move.** The target is always a sibling of the source, in the same
  directory. That is what "rename the files" was asked for, and it means no code path here can
  relocate a file into another series' folder, into a season folder that does not exist, or
  anywhere else. `_target_for` builds a *filename*; the parent comes from the source and is
  never recomputed.
* **Both ends are checked against the root.** Source and target both go through
  `safe_media_path`, which resolves symlinks first. A file that is a link out of the library
  cannot be used to rename something outside it.
* **Never overwrite.** A target that already exists is a collision and the file is skipped,
  reported, and left exactly as it is. There is no flag to force it. Losing an episode to a
  name clash would be the single most expensive bug this module could have.
* **The database follows the disk, one file at a time.** `media_files.path` is updated in the
  same step as the rename, so an interrupted run leaves a consistent catalogue for the files it
  got to rather than a set of rows pointing at names that no longer exist.

Planning and applying are separate calls on purpose. The plan is what the admin reads before
anything happens, and it is computed the same way the apply computes it, so what they approved
is what runs.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import AppError, Forbidden
from app.models import Episode, MediaFile, MediaStatus, Series
from app.services.scanner.scan import relative_media_path, safe_media_path

log = logging.getLogger(__name__)

#: Characters that must not reach a filename. `/` and `\` would change the target's directory,
#: which `_target_for` must never do; the rest are refused by one filesystem or another and a
#: NAS share is frequently more than one filesystem.
_UNSAFE_IN_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

#: What a run reports per file.
STATUS_OK = "ok"
STATUS_UNCHANGED = "unchanged"
STATUS_COLLISION = "collision"
STATUS_MISSING = "missing"
STATUS_FAILED = "failed"


class ProgressCtx(Protocol):
    """The slice of the job context this module uses. Mirrors `images.ProgressCtx`."""

    async def progress(self, percent: float, message: str) -> None: ...
    async def log(self, level: str, message: str) -> None: ...
    def cancelled(self) -> bool: ...


@dataclass(slots=True)
class RenameItem:
    media_file_id: str
    #: Library-relative, as stored in `media_files.path`.
    current_path: str
    #: Library-relative. Equal to `current_path` when the file is already correctly named.
    proposed_path: str
    status: str = STATUS_OK
    #: Why this row will be skipped, when it will be. Empty for the ones that will run.
    reason: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    size_bytes: int = 0


@dataclass(slots=True)
class RenamePlan:
    series_id: str
    series_title: str
    items: list[RenameItem] = field(default_factory=list)

    @property
    def renamable(self) -> list[RenameItem]:
        return [item for item in self.items if item.status == STATUS_OK]


def sanitize_filename(value: str) -> str:
    """A title reduced to something safe to put in a filename.

    Collapses runs of unsafe characters to a single space rather than deleting them, so
    `Love: Redemption` becomes `Love Redemption` and not `LoveRedemption`. Trailing dots and
    spaces are stripped because Windows silently drops them, which would make a share
    round-trip a name into a different one.
    """
    cleaned = _UNSAFE_IN_FILENAME.sub(" ", value or "")
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned


def _extension(path: str) -> str:
    """The source's own extension, kept verbatim. Renaming must never change the container."""
    suffix = Path(path).suffix
    return suffix if len(suffix) <= 10 else ""


def target_filename(title: str, season: int, episode: int, source_path: str) -> str:
    """`Tempest The Last Mecha - S01E03.mp4`.

    The same shape the import assistant's rename script proposes, so a household that already
    ran that script by hand sees "unchanged" here rather than a second round of churn.
    """
    safe = sanitize_filename(title) or "Series"
    return f"{safe} - S{season:02d}E{episode:02d}{_extension(source_path)}"


def _target_for(item_path: str, title: str, season: int, episode: int) -> str:
    """The library-relative target path: the source's own directory, a new filename.

    The parent is taken from the source and never recomputed. That is what makes it impossible
    for this module to move a file somewhere else — see the module docstring.
    """
    parent = str(Path(item_path).parent)
    name = target_filename(title, season, episode, item_path)
    return name if parent in ("", ".") else f"{parent}/{name}"


async def plan(session: AsyncSession, series_id: str) -> RenamePlan:
    """What a rename would do, without doing any of it.

    Every present media file attached to an episode of this series gets a row, including the
    ones that will be skipped: an admin deciding whether to run this needs to see the
    collisions and the already-correct files, not just the work.
    """
    series = await session.get(Series, series_id)
    if series is None:
        raise AppError("SERIES_NOT_FOUND", "Series not found", 404)

    episodes = (
        (
            await session.execute(
                select(Episode)
                .where(Episode.series_id == series_id)
                .options(selectinload(Episode.media_files))
                .order_by(Episode.season_number, Episode.episode_number)
                # `populate_existing` is load-bearing, not defensive. Without it SQLAlchemy
                # hands back identity-mapped episodes whose `media_files` collection was
                # loaded earlier in this session, so a file attached since — which is exactly
                # what the import assistant just did, right before somebody asks to rename —
                # is invisible to the plan. The failure is silent: the file is simply not in
                # the list, and nobody wonders why.
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )

    result = RenamePlan(series_id=series.id, series_title=series.title)
    # Every target this run will produce, so two episodes that would land on the same name are
    # caught here rather than by the second one failing to write.
    claimed: dict[str, str] = {}

    for episode in episodes:
        for media in episode.media_files:
            if media.status != MediaStatus.PRESENT:
                continue

            item = RenameItem(
                media_file_id=media.id,
                current_path=media.path,
                proposed_path=media.path,
                season_number=episode.season_number,
                episode_number=episode.episode_number,
                size_bytes=media.size_bytes or 0,
            )

            try:
                target = _target_for(
                    media.path, series.title, episode.season_number, episode.episode_number
                )
            except Exception:  # pragma: no cover - defensive
                item.status = STATUS_FAILED
                item.reason = "Could not build a target name"
                result.items.append(item)
                continue

            item.proposed_path = target

            if target == media.path:
                item.status = STATUS_UNCHANGED
                result.items.append(item)
                continue

            previous = claimed.get(target)
            if previous is not None:
                item.status = STATUS_COLLISION
                item.reason = f"Two files would both become {target}"
                result.items.append(item)
                continue

            # Resolved rather than trusted: `media.path` comes from the database, and the
            # check has to be against what it actually points at today.
            try:
                source_abs = safe_media_path(media.path)
                target_abs = safe_media_path(target)
            except Forbidden:
                item.status = STATUS_FAILED
                item.reason = "Path resolves outside the library"
                result.items.append(item)
                continue

            if not source_abs.exists():
                item.status = STATUS_MISSING
                item.reason = "The file is not on the mount"
                result.items.append(item)
                continue

            if target_abs.exists():
                item.status = STATUS_COLLISION
                item.reason = "A file with that name is already there"
                result.items.append(item)
                continue

            claimed[target] = media.id
            result.items.append(item)

    return result


async def apply(
    session: AsyncSession, series_id: str, *, ctx: ProgressCtx | None = None
) -> dict[str, int]:
    """Perform the renames the plan approves of. Returns a tally by status.

    Re-planned here rather than taking the plan as an argument: the admin's approval may be
    minutes old, and the filesystem is the authority on what is safe to do right now. A file
    that gained a collision in the meantime is skipped, not overwritten.
    """
    current = await plan(session, series_id)
    todo = current.renamable
    tally = {STATUS_OK: 0, STATUS_COLLISION: 0, STATUS_MISSING: 0, STATUS_FAILED: 0}
    tally[STATUS_UNCHANGED] = sum(
        1 for item in current.items if item.status == STATUS_UNCHANGED
    )
    for item in current.items:
        if item.status in (STATUS_COLLISION, STATUS_MISSING, STATUS_FAILED):
            tally[item.status] += 1
            if ctx is not None:
                await ctx.log("warning", f"Skipped {item.current_path}: {item.reason}")

    total = len(todo)
    if ctx is not None:
        await ctx.progress(0.0, f"{total} file(s) to rename")

    for index, item in enumerate(todo, start=1):
        if ctx is not None and ctx.cancelled():
            await ctx.log("info", "Cancelled; files already renamed are left as they are")
            break

        try:
            source = safe_media_path(item.current_path)
            target = safe_media_path(item.proposed_path)
        except Forbidden:
            tally[STATUS_FAILED] += 1
            continue

        # Re-checked immediately before the write. There is a window between this and the
        # rename, but it is the same window every file manager has, and the alternative —
        # an unconditional rename — would silently destroy whatever arrived in it.
        if target.exists():
            tally[STATUS_COLLISION] += 1
            if ctx is not None:
                await ctx.log("warning", f"Skipped {item.current_path}: target now exists")
            continue

        try:
            await _rename(source, target)
        except OSError as exc:
            tally[STATUS_FAILED] += 1
            log.warning("rename failed: %s -> %s (%s)", source, target, exc)
            if ctx is not None:
                await ctx.log("error", f"Could not rename {item.current_path}: {exc}")
            continue

        media = await session.get(MediaFile, item.media_file_id)
        if media is not None:
            media.path = relative_media_path(target)
        # Committed per file rather than at the end: an interrupted run must leave the
        # catalogue agreeing with the disk for everything it managed to do.
        await session.commit()

        tally[STATUS_OK] += 1
        if ctx is not None:
            await ctx.log("info", f"{item.current_path} → {item.proposed_path}")
            await ctx.progress(index / total * 100 if total else 100, item.proposed_path)

    if ctx is not None:
        await ctx.progress(100.0, f"Renamed {tally[STATUS_OK]} file(s)")
    return tally


async def _rename(source: Path, target: Path) -> None:
    """The write itself, off the event loop.

    `os.rename`, not `shutil.move`: within one directory it is atomic and it cannot silently
    fall back to a copy. It also refuses to cross a filesystem, which is a property worth
    keeping — a rename that turned into a multi-gigabyte copy would be a very different
    operation from the one the admin approved.
    """
    import asyncio

    await asyncio.to_thread(os.rename, source, target)


__all__ = [
    "STATUS_COLLISION",
    "STATUS_FAILED",
    "STATUS_MISSING",
    "STATUS_OK",
    "STATUS_UNCHANGED",
    "RenameItem",
    "RenamePlan",
    "apply",
    "plan",
    "sanitize_filename",
    "target_filename",
]
