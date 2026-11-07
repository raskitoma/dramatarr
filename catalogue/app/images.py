"""Poster and avatar storage.

Content-addressed by sha256, so identical art reused across titles is stored
once and a re-run costs nothing. Downscaled on ingest -- warehousing originals
buys nothing and drifts closer to redistributing someone's image library.

Kept private by design: locally caching third-party key art for a single-user
instance is defensible; re-serving that cache publicly is not, and no source
here licenses it.
"""

from __future__ import annotations

import hashlib
import logging
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Image, ImageKind, Source, TitleImage

log = logging.getLogger(__name__)

#: Long edge, in pixels. Vertical drama key art is 2:3 / 9:16, so this keeps a
#: usable poster without storing a print-resolution file.
MAX_EDGE = 720
#: Headshots are rendered small everywhere they appear, and there are thousands
#: of them. Storing avatars at poster size would multiply the image store for
#: pixels nothing displays.
AVATAR_EDGE = 320
WEBP_QUALITY = 80


def _phash(img: PILImage.Image, size: int = 8) -> str:
    """A small difference hash.

    Free here because the image is already decoded for downscaling. Used as a
    cross-source match signal and to notice key-art replacement.
    """
    grey = img.convert("L").resize((size + 1, size), PILImage.Resampling.LANCZOS)
    pixels = list(grey.getdata())
    bits = 0
    index = 0
    for row in range(size):
        offset = row * (size + 1)
        for col in range(size):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            if left > right:
                bits |= 1 << index
            index += 1
    return f"{bits:016x}"


def store_image(
    session: Session,
    data: bytes,
    *,
    root: Path,
    kind: str,
    source_id: int | None,
    source_url: str | None,
    max_edge: int = MAX_EDGE,
    license_tag: str = "third-party promotional art; cached for personal use",
) -> Image | None:
    """Normalise, hash and persist one image. Returns the Image row."""
    try:
        img = PILImage.open(BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 - a bad image is not a fatal error
        log.warning("undecodable image from %s: %s", source_url, exc)
        return None

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    phash = _phash(img)

    img.thumbnail((max_edge, max_edge), PILImage.Resampling.LANCZOS)
    buffer = BytesIO()
    img.save(buffer, format="WEBP", quality=WEBP_QUALITY)
    payload = buffer.getvalue()

    sha = hashlib.sha256(payload).hexdigest()
    existing = session.scalar(select(Image).where(Image.sha256 == sha))
    if existing is not None:
        return existing

    path = Path(kind + "s") / sha[:2] / f"{sha}.webp"
    absolute = root / path
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(payload)

    row = Image(
        kind=kind,
        local_path=str(path),
        sha256=sha,
        phash=phash,
        width=img.width,
        height=img.height,
        bytes=len(payload),
        source_id=source_id,
        source_url=source_url,
        license_tag=license_tag,
    )
    session.add(row)
    session.flush()
    return row


def attach_poster(
    session: Session, title_id: int, image: Image, *, primary: bool = True
) -> None:
    exists = session.scalar(
        select(TitleImage).where(
            TitleImage.title_id == title_id,
            TitleImage.image_id == image.id,
            TitleImage.kind == ImageKind.POSTER,
        )
    )
    if exists is None:
        session.add(
            TitleImage(
                title_id=title_id,
                image_id=image.id,
                kind=ImageKind.POSTER,
                is_primary=primary,
            )
        )


def fetch_posters(
    session: Session,
    fetcher,
    root: Path,
    *,
    source_key: str | None = None,
    limit: int = 0,
    progress=None,
) -> dict:
    """Download posters for titles that do not have one yet."""
    from app.models import Title, TitleSource

    stats = {"considered": 0, "fetched": 0, "reused": 0, "failed": 0, "skipped": 0}

    have = {
        row[0]
        for row in session.execute(select(TitleImage.title_id).distinct()).all()
    }
    stmt = (
        select(Title.id, TitleSource.source_id, TitleSource.poster_url)
        .join(TitleSource, TitleSource.title_id == Title.id)
        .where(TitleSource.poster_url.isnot(None))
    )
    if source_key:
        stmt = stmt.join(Source, TitleSource.source_id == Source.id).where(
            Source.key == source_key
        )

    rows = [r for r in session.execute(stmt).all() if r[0] not in have]
    if limit:
        rows = rows[:limit]

    for index, (title_id, source_id, url) in enumerate(rows, start=1):
        stats["considered"] += 1
        if not url:
            stats["skipped"] += 1
            continue
        result = fetcher.get(url)
        if not result.ok:
            stats["failed"] += 1
            continue
        image = store_image(
            session,
            result.doc.body,
            root=root,
            kind=ImageKind.POSTER,
            source_id=source_id,
            source_url=url,
        )
        if image is None:
            stats["failed"] += 1
            continue
        attach_poster(session, title_id, image)
        stats["fetched"] += 1
        # Same reason as the catalogue job: the image download sits inside this
        # loop, so batching would hold the write lock across network calls.
        session.commit()
        if progress:
            progress(index, len(rows), "posters")

    session.commit()
    return stats


def attach_avatar(session: Session, link, image: Image) -> None:
    """Point a person's source link at its stored headshot.

    On PersonSource rather than Person on purpose: each source publishes its own
    photo of the same actor, and `person` is the deduplicated identity. Storing
    it per-link keeps the provenance and lets a source be revoked cleanly.
    """
    link.avatar_image_id = image.id


def fetch_avatars(
    session: Session,
    fetcher,
    root: Path,
    *,
    source_key: str | None = None,
    limit: int = 0,
    progress=None,
) -> dict:
    """Download headshots for people who do not have one stored yet.

    Walks `person_source`, not `person`: the URL is a per-source fact, and a
    person known to two platforms can legitimately end up with two photos.
    Content addressing means the duplicate costs one row, not one file.
    """
    from app.models import PersonSource

    stats = {"considered": 0, "fetched": 0, "reused": 0, "failed": 0, "skipped": 0}

    stmt = select(
        PersonSource.id, PersonSource.source_id, PersonSource.avatar_url
    ).where(
        PersonSource.avatar_url.isnot(None),
        PersonSource.avatar_image_id.is_(None),
    )
    if source_key:
        stmt = stmt.join(Source, PersonSource.source_id == Source.id).where(
            Source.key == source_key
        )

    rows = session.execute(stmt).all()
    if limit:
        rows = rows[:limit]

    for index, (link_id, source_id, url) in enumerate(rows, start=1):
        stats["considered"] += 1
        if not url:
            stats["skipped"] += 1
            continue
        result = fetcher.get(url)
        if not result.ok:
            stats["failed"] += 1
            continue
        before = session.scalar(select(Image).where(Image.source_url == url))
        image = store_image(
            session,
            result.doc.body,
            root=root,
            kind=ImageKind.AVATAR,
            source_id=source_id,
            source_url=url,
            max_edge=AVATAR_EDGE,
        )
        if image is None:
            stats["failed"] += 1
            continue
        link = session.get(PersonSource, link_id)
        if link is not None:
            attach_avatar(session, link, image)
        stats["reused" if before is not None else "fetched"] += 1
        # Commit per image: the download sits inside this loop, so batching
        # would hold the write lock across network calls.
        session.commit()
        if progress:
            progress(index, len(rows), "avatars")

    session.commit()
    return stats
