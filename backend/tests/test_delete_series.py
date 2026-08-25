"""Removing things from the catalogue.

The invariant under test is the one the whole application rests on: the library is mounted
read-only, so deleting a series deletes *rows*. The file that produced it is still on the mount
afterwards — which is exactly why the API has to admit that before the admin confirms, since
the next scan will otherwise put the series straight back and nobody will understand why.

The cascade is checked by querying for the orphans rather than by trusting the schema, and once
with `PRAGMA foreign_keys` deliberately turned off, because that pragma is the only thing
standing between an ON DELETE CASCADE and a table full of dangling watch progress.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AudioTrack,
    AuditLog,
    Episode,
    MediaFile,
    MediaStatus,
    PlayEvent,
    Series,
    SeriesTitle,
    SeriesTranslation,
    SubtitleTrack,
    User,
    WatchProgress,
)

pytestmark = pytest.mark.anyio


# --- Helpers ---------------------------------------------------------------------------------


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    """Always a round trip: the identity map must not be allowed to answer these."""
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return int((await session.execute(stmt)).scalar_one())


async def _episode_ids(session: AsyncSession, series_id: str) -> set[str]:
    return set(
        (await session.execute(select(Episode.id).where(Episode.series_id == series_id)))
        .scalars()
        .all()
    )


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


@dataclass
class Fixture:
    """One series, plus handles on the rows and files the assertions need."""

    series: Series
    present_episode: Episode
    missing_episode: Episode
    declared_episode: Episode
    on_disk: Path
    media_ids: list[str] = field(default_factory=list)
    episode_ids: list[str] = field(default_factory=list)


# --- Fixtures --------------------------------------------------------------------------------


@pytest.fixture
async def client(db: AsyncSession, admin_user: User) -> AsyncIterator[AsyncClient]:
    """The real app over ASGI, sharing the test's session.

    One session means the routes and the assertions see the same transaction, so a test can
    check what a request wrote without racing a second connection for the SQLite file.
    """
    from app.api.deps import get_current_user, require_admin
    from app.db import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def library(db: AsyncSession, tmp_env: Path, admin_user: User, viewer_user: User) -> Fixture:
    """A series covering all three states an episode can be in.

    * episode 1 — a `present` media file backed by a real file under `LIBRARY_ROOT`;
    * episode 2 — a media file the scanner marked `missing`, with nothing on disk;
    * episode 3 — declared by a metadata refresh, no media file row at all, ever.

    Both users have watch state on it, because losing one user's progress and keeping the
    other's would be the kind of half-cascade that only shows up in production.
    """
    from app.config import get_settings
    from app.services.search.index import ensure_index, reindex_series

    await ensure_index(db)

    root = get_settings().library_root
    folder = root / "Ruta de Cristal (2024)" / "Season 01"
    folder.mkdir(parents=True)
    on_disk = folder / "Ruta de Cristal - S01E01.mp4"
    on_disk.write_bytes(b"pretend this is an mp4")

    series = Series(
        title="Ruta de Cristal",
        slug="ruta-de-cristal",
        sort_title="ruta de cristal",
        library_path="Ruta de Cristal (2024)",
        year=2024,
        declared_episode_count=3,
    )
    db.add(series)
    await db.flush()

    db.add(
        SeriesTitle(
            series_id=series.id,
            language="en",
            title="Glass Route",
            kind="official",
            source="tmdb",
            normalized="glass route",
        )
    )
    db.add(SeriesTranslation(series_id=series.id, language="en", overview="A road, in glass."))

    episodes = []
    for number in (1, 2, 3):
        episode = Episode(
            series_id=series.id,
            season_number=1,
            episode_number=number,
            title=f"Episodio {number}",
            runtime_seconds=1800,
        )
        db.add(episode)
        episodes.append(episode)
    await db.flush()

    present = MediaFile(
        episode_id=episodes[0].id,
        path="Ruta de Cristal (2024)/Season 01/Ruta de Cristal - S01E01.mp4",
        size_bytes=on_disk.stat().st_size,
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        duration_seconds=1800.0,
        width=1920,
        height=1080,
        status=MediaStatus.PRESENT,
    )
    # The disk this one lived on went away; the scanner marked it and, by design, kept the row.
    gone = MediaFile(
        episode_id=episodes[1].id,
        path="Ruta de Cristal (2024)/Season 01/Ruta de Cristal - S01E02.mp4",
        size_bytes=2048,
        container="mp4",
        status=MediaStatus.MISSING,
    )
    db.add_all([present, gone])
    await db.flush()

    db.add(AudioTrack(media_file_id=present.id, stream_index=1, language="es", codec="aac"))
    db.add(SubtitleTrack(media_file_id=present.id, stream_index=2, language="es", codec="subrip"))

    for user, episode in (
        (admin_user, episodes[0]),
        (viewer_user, episodes[0]),
        (viewer_user, episodes[1]),
    ):
        db.add(
            WatchProgress(
                user_id=user.id,
                episode_id=episode.id,
                position_seconds=600.0,
                duration_seconds=1800.0,
                percent=33.3,
                play_count=1,
            )
        )
        db.add(
            PlayEvent(
                user_id=user.id,
                episode_id=episode.id,
                series_id=series.id,
                position_seconds=600.0,
            )
        )

    # Cached artwork lives under DATA_DIR. Deleting the series must take it; nothing else may.
    images = get_settings().images_dir / series.id
    images.mkdir(parents=True, exist_ok=True)
    (images / "poster.jpg").write_bytes(b"poster")

    await db.commit()
    await reindex_series(db, series.id)
    await db.commit()

    return Fixture(
        series=series,
        present_episode=episodes[0],
        missing_episode=episodes[1],
        declared_episode=episodes[2],
        on_disk=on_disk,
        media_ids=[present.id, gone.id],
        episode_ids=[episode.id for episode in episodes],
    )


@pytest.fixture
async def abandoned(db: AsyncSession, tmp_env: Path) -> Series:
    """A series whose every file is gone — the case cleanup exists for."""
    from app.services.search.index import reindex_series

    series = Series(
        title="Vientos del Sur",
        slug="vientos-del-sur",
        sort_title="vientos del sur",
        library_path="Vientos del Sur (2019)",
        year=2019,
    )
    db.add(series)
    await db.flush()

    for number in (1, 2):
        episode = Episode(series_id=series.id, season_number=1, episode_number=number)
        db.add(episode)
        await db.flush()
        db.add(
            MediaFile(
                episode_id=episode.id,
                path=f"Vientos del Sur (2019)/Vientos del Sur - S01E0{number}.mkv",
                size_bytes=4096,
                container="mkv",
                status=MediaStatus.MISSING,
            )
        )

    await db.commit()
    await reindex_series(db, series.id)
    await db.commit()
    return series


# --- Delete ------------------------------------------------------------------------------------


async def test_delete_cascades_to_every_row_that_hung_off_the_series(
    client: AsyncClient,
    db: AsyncSession,
    library: Fixture,
    admin_user: User,
    viewer_user: User,
) -> None:
    series_id = library.series.id
    episode_ids = library.episode_ids

    # Guard against a vacuous pass: both users really do have state to lose.
    for user in (admin_user, viewer_user):
        assert await _count(db, WatchProgress, WatchProgress.user_id == user.id) > 0
        assert await _count(db, PlayEvent, PlayEvent.user_id == user.id) > 0

    response = await client.delete(f"/api/library/series/{series_id}")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "deletedEpisodes": 3,
        "deletedMediaFiles": 2,
        "filesStillOnDisk": True,
    }

    assert await _count(db, Series, Series.id == series_id) == 0
    assert await _count(db, Episode, Episode.series_id == series_id) == 0
    assert await _count(db, Episode, Episode.id.in_(episode_ids)) == 0
    assert await _count(db, MediaFile, MediaFile.id.in_(library.media_ids)) == 0
    assert await _count(db, AudioTrack, AudioTrack.media_file_id.in_(library.media_ids)) == 0
    assert await _count(db, SubtitleTrack, SubtitleTrack.media_file_id.in_(library.media_ids)) == 0
    assert await _count(db, SeriesTitle, SeriesTitle.series_id == series_id) == 0
    assert await _count(db, SeriesTranslation, SeriesTranslation.series_id == series_id) == 0

    # Per user, not in aggregate: one of them surviving is the failure this guards against.
    for user in (admin_user, viewer_user):
        assert (
            await _count(
                db,
                WatchProgress,
                WatchProgress.user_id == user.id,
                WatchProgress.episode_id.in_(episode_ids),
            )
            == 0
        ), f"{user.email} kept watch progress on a deleted series"
        assert await _count(db, WatchProgress, WatchProgress.user_id == user.id) == 0
        assert await _count(db, PlayEvent, PlayEvent.user_id == user.id) == 0
    assert await _count(db, PlayEvent, PlayEvent.series_id == series_id) == 0
    assert await _count(db, PlayEvent, PlayEvent.episode_id.in_(episode_ids)) == 0


async def test_delete_leaves_the_media_file_exactly_where_it_was(
    client: AsyncClient, db: AsyncSession, library: Fixture, tmp_env: Path
) -> None:
    """The whole point. The catalogue is a projection of the mount, never its owner."""
    from app.config import get_settings

    root = get_settings().library_root
    before = _tree(root)

    response = await client.delete(f"/api/library/series/{library.series.id}")
    assert response.status_code == 200

    assert library.on_disk.is_file(), "deleting a series deleted a file off the library mount"
    assert library.on_disk.read_bytes() == b"pretend this is an mp4"
    assert _tree(root) == before, "the library mount changed shape during a catalogue delete"


async def test_delete_purges_cached_artwork_under_data_dir(
    client: AsyncClient, db: AsyncSession, library: Fixture
) -> None:
    from app.config import get_settings

    images = get_settings().images_dir / library.series.id
    assert (images / "poster.jpg").is_file()

    await client.delete(f"/api/library/series/{library.series.id}")

    assert not images.exists()
    assert get_settings().images_dir.is_dir(), "the whole image cache was removed"


async def test_delete_is_recorded_in_the_audit_trail(
    client: AsyncClient, db: AsyncSession, library: Fixture, admin_user: User
) -> None:
    series_id = library.series.id
    await client.delete(f"/api/library/series/{series_id}")

    row = (
        await db.execute(select(AuditLog).where(AuditLog.action == "series.delete"))
    ).scalar_one()
    assert row.user_id == admin_user.id
    assert row.entity_id == series_id
    assert row.detail["title"] == "Ruta de Cristal"
    assert row.detail["episodes"] == 3
    assert row.detail["filesStillOnDisk"] is True


async def test_deleting_an_unknown_id_is_not_found(client: AsyncClient) -> None:
    response = await client.delete("/api/library/series/no-such-series")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SERIES_NOT_FOUND"

    preview = await client.get("/api/library/series/no-such-series/deletion-preview")
    assert preview.status_code == 404
    assert preview.json()["error"]["code"] == "SERIES_NOT_FOUND"


async def test_cascade_is_verified_not_assumed(
    client: AsyncClient,
    db: AsyncSession,
    library: Fixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SQLite honours ON DELETE CASCADE only under `PRAGMA foreign_keys=ON`.

    With it off, the schema's cascades are inert and a plain DELETE would silently orphan
    every child row — including other people's watch progress. The delete has to notice and
    finish the job itself, so this turns the pragma off on the connection the request will use
    and demands the same end state.
    """
    series_id = library.series.id
    await db.commit()  # the pragma is a no-op inside a transaction
    await db.execute(sql("PRAGMA foreign_keys=OFF"))
    assert (await db.execute(sql("PRAGMA foreign_keys"))).scalar_one() == 0

    with caplog.at_level(logging.WARNING, logger="app.services.library"):
        response = await client.delete(f"/api/library/series/{series_id}")
    assert response.status_code == 200, response.text
    # Without this the test would pass just as well on a working cascade, proving nothing.
    assert any(
        "PRAGMA foreign_keys" in record.getMessage() for record in caplog.records
    ), "the inert cascade went unnoticed"

    assert await _count(db, Series, Series.id == series_id) == 0
    assert await _count(db, Episode, Episode.id.in_(library.episode_ids)) == 0
    assert await _count(db, MediaFile, MediaFile.id.in_(library.media_ids)) == 0
    assert await _count(db, AudioTrack, AudioTrack.media_file_id.in_(library.media_ids)) == 0
    assert await _count(db, SubtitleTrack, SubtitleTrack.media_file_id.in_(library.media_ids)) == 0
    assert await _count(db, WatchProgress, WatchProgress.episode_id.in_(library.episode_ids)) == 0
    assert await _count(db, PlayEvent, PlayEvent.series_id == series_id) == 0
    assert await _count(db, SeriesTitle, SeriesTitle.series_id == series_id) == 0
    assert await _count(db, SeriesTranslation, SeriesTranslation.series_id == series_id) == 0


# --- Deletion preview ---------------------------------------------------------------------------


async def test_preview_counts_the_rows_and_admits_the_files_are_still_there(
    client: AsyncClient, library: Fixture
) -> None:
    response = await client.get(f"/api/library/series/{library.series.id}/deletion-preview")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "seriesTitle": "Ruta de Cristal",
        "episodeCount": 3,
        "mediaFileCount": 2,
        "presentFileCount": 1,
        "missingFileCount": 1,
        "usersWithProgress": 2,
        "filesStillOnDisk": True,
    }


async def test_preview_checks_the_filesystem_rather_than_the_stored_status(
    client: AsyncClient, db: AsyncSession, library: Fixture
) -> None:
    """The other direction, and the reason the check cannot be a database query.

    The row still says `present` — that is the scanner's opinion from its last pass. The file
    is gone, so the delete will stick, and the admin must be told that and not the opposite.
    """
    library.on_disk.unlink()
    status = (
        await db.execute(select(MediaFile.status).where(MediaFile.id == library.media_ids[0]))
    ).scalar_one()
    assert status == MediaStatus.PRESENT

    body = (await client.get(f"/api/library/series/{library.series.id}/deletion-preview")).json()
    assert body["filesStillOnDisk"] is False
    assert body["presentFileCount"] == 1, "the stored counts still report what the scanner saw"

    # And the delete itself re-measures rather than reusing the preview's answer.
    deleted = (await client.delete(f"/api/library/series/{library.series.id}")).json()
    assert deleted["filesStillOnDisk"] is False


async def test_preview_reports_a_series_whose_media_is_entirely_gone(
    client: AsyncClient, abandoned: Series
) -> None:
    body = (await client.get(f"/api/library/series/{abandoned.id}/deletion-preview")).json()
    assert body == {
        "seriesTitle": "Vientos del Sur",
        "episodeCount": 2,
        "mediaFileCount": 2,
        "presentFileCount": 0,
        "missingFileCount": 2,
        "usersWithProgress": 0,
        "filesStillOnDisk": False,
    }


# --- Search -------------------------------------------------------------------------------------


async def test_deleted_series_disappears_from_search(
    client: AsyncClient, db: AsyncSession, library: Fixture
) -> None:
    from app.services.search.service import search_library

    before = await search_library(db, "Ruta de Cristal", sees_restricted=True)
    assert library.series.id in {hit.series.id for hit in before.series}

    await client.delete(f"/api/library/series/{library.series.id}")

    after = await search_library(db, "Ruta de Cristal", sees_restricted=True)
    assert library.series.id not in {hit.series.id for hit in after.series}
    assert not after.series
    # The episode titles went with it; a hit here would 404 the moment it was clicked.
    assert not after.episodes


# --- Cleanup --------------------------------------------------------------------------------------


async def test_cleanup_dry_run_changes_nothing_and_reports_the_real_numbers(
    client: AsyncClient, db: AsyncSession, library: Fixture, abandoned: Series
) -> None:
    """Same plan, applied or not — otherwise the confirmation dialog is a guess."""
    series_before = await _count(db, Series)
    episodes_before = await _count(db, Episode)
    files_before = await _count(db, MediaFile)

    dry = await client.post("/api/library/cleanup", json={"dryRun": True})
    assert dry.status_code == 200, dry.text
    assert dry.json() == {
        # Only the series that loses its last episode. "Ruta de Cristal" loses one episode
        # and keeps the other two, so it is not on this list.
        "removedSeries": ["Vientos del Sur"],
        "removedEpisodes": 3,
        "removedMediaFiles": 3,
        "dryRun": True,
    }

    assert await _count(db, Series) == series_before
    assert await _count(db, Episode) == episodes_before
    assert await _count(db, MediaFile) == files_before
    assert await _count(db, AuditLog, AuditLog.action == "library.cleanup") == 0

    real = await client.post("/api/library/cleanup", json={})
    assert real.status_code == 200, real.text
    assert real.json() == {**dry.json(), "dryRun": False}

    assert await _count(db, Series, Series.id == abandoned.id) == 0
    assert await _count(db, Episode, Episode.series_id == abandoned.id) == 0
    assert await _count(db, Series, Series.id == library.series.id) == 1
    assert await _count(db, Episode) == episodes_before - 3
    assert await _count(db, MediaFile) == files_before - 3
    assert await _count(db, AuditLog, AuditLog.action == "library.cleanup") == 1


async def test_cleanup_keeps_an_episode_metadata_declared_but_no_file_ever_backed(
    client: AsyncClient, db: AsyncSession, library: Fixture
) -> None:
    """`12 of 24 available` depends on those rows; sweeping them would erase the "of 24"."""
    response = await client.post("/api/library/cleanup", json={})
    assert response.status_code == 200, response.text

    remaining = await _episode_ids(db, library.series.id)
    assert library.declared_episode.id in remaining, "a declared episode was swept away"
    assert library.present_episode.id in remaining
    assert library.missing_episode.id not in remaining
    assert await _count(db, Series, Series.id == library.series.id) == 1


async def test_cleanup_spares_an_episode_whose_missing_file_came_back(
    client: AsyncClient, db: AsyncSession, library: Fixture, tmp_env: Path
) -> None:
    """`missing` is the last scan's opinion. A remounted NAS makes it wrong, and cleanup is
    irreversible, so the plan is checked against the filesystem before anything is condemned."""
    from app.config import get_settings

    path = (
        await db.execute(select(MediaFile.path).where(MediaFile.id == library.media_ids[1]))
    ).scalar_one()
    resurfaced = get_settings().library_root / path
    resurfaced.parent.mkdir(parents=True, exist_ok=True)
    resurfaced.write_bytes(b"back from the dead")

    response = await client.post("/api/library/cleanup", json={})
    assert response.json() == {
        "removedSeries": [],
        "removedEpisodes": 0,
        "removedMediaFiles": 0,
        "dryRun": False,
    }
    assert library.missing_episode.id in await _episode_ids(db, library.series.id)


async def test_cleanup_refuses_to_run_while_the_library_mount_is_unreadable(
    client: AsyncClient, db: AsyncSession, abandoned: Series, tmp_env: Path, monkeypatch: Any
) -> None:
    """With the mount down every file looks missing, and this would wipe the library."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "library_root", tmp_env / "not-mounted")

    response = await client.post("/api/library/cleanup", json={})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LIBRARY_UNAVAILABLE"
    assert await _count(db, Series, Series.id == abandoned.id) == 1
    assert await _count(db, Episode, Episode.series_id == abandoned.id) == 2


async def test_cleanup_repairs_the_search_index_of_a_surviving_series(
    client: AsyncClient, db: AsyncSession, library: Fixture
) -> None:
    """The series stays, so nothing else drops its slice of the index — but the episode that
    went is still named in it, and a hit on it would 404 the moment it was clicked."""
    from app.services.search.service import search_library

    def episode_ids(results: Any) -> set[str]:
        found = {hit.episode.id for hit in results.episodes}
        for series_hit in results.series:
            found |= {hit.episode.id for hit in series_hit.episodes}
        return found

    before = episode_ids(await search_library(db, "Episodio", sees_restricted=True))
    assert {library.present_episode.id, library.missing_episode.id} <= before

    await client.post("/api/library/cleanup", json={})

    after = episode_ids(await search_library(db, "Episodio", sees_restricted=True))
    assert library.missing_episode.id not in after
    assert library.present_episode.id in after


async def test_delete_does_not_disturb_another_series(
    client: AsyncClient, db: AsyncSession, library: Fixture, abandoned: Series
) -> None:
    await client.delete(f"/api/library/series/{library.series.id}")

    assert await _count(db, Series, Series.id == abandoned.id) == 1
    assert await _count(db, Episode, Episode.series_id == abandoned.id) == 2
    assert await _count(db, MediaFile) == 2, "another series' media files went with the delete"
