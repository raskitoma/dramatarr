"""Shared test fixtures.

Each test gets its own SQLite file and its own data/library directories, so tests never
interfere and a failure leaves an inspectable database behind in the tmp dir.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest

# Must be set before app.config is imported anywhere, since get_settings() is lru_cached.
os.environ.setdefault("DRAMATARR_ENVIRONMENT", "dev")
os.environ.setdefault("DRAMATARR_SECRET_KEY", "test-secret-key-not-for-production")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Point the app at throwaway library/data dirs and reset the settings cache."""
    from app.config import get_settings

    library = tmp_path / "library"
    data = tmp_path / "data"
    library.mkdir()
    data.mkdir()

    monkeypatch.setenv("DRAMATARR_LIBRARY_ROOT", str(library))
    monkeypatch.setenv("DRAMATARR_DATA_DIR", str(data))
    monkeypatch.setenv("DRAMATARR_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("DRAMATARR_ENVIRONMENT", "dev")

    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()

    yield tmp_path

    get_settings.cache_clear()


@pytest.fixture
async def db(tmp_env: Path) -> AsyncGenerator:
    """A live session against a freshly created schema."""
    from app.db import create_all, dispose_engine, get_sessionmaker
    from app.services.settings import SettingsService

    await dispose_engine()  # drop any engine bound to a previous test's URL
    await create_all()
    SettingsService.invalidate()

    async with get_sessionmaker()() as session:
        yield session

    await dispose_engine()
    SettingsService.invalidate()


@pytest.fixture
async def admin_user(db):
    from app.models import User, UserRole

    user = User(
        email="admin@example.com",
        display_name="Admin",
        # `.value`, because the column is a string and a fixture holding an enum would let
        # `UserRole(self.role)` pass here and fail against a real row.
        role=UserRole.ADMIN.value,
        locale="es",
    )
    db.add(user)
    await db.commit()
    return user


@pytest.fixture
async def viewer_user(db):
    from app.models import User, UserRole

    user = User(
        email="viewer@example.com",
        display_name="Viewer",
        role=UserRole.VIEWER.value,
        locale="es",
    )
    db.add(user)
    await db.commit()
    return user


@pytest.fixture
async def sample_series(db):
    """A series with three episodes, the middle one carrying a media file."""
    from app.models import Episode, MediaFile, Series

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

    media = MediaFile(
        episode_id=episodes[0].id,
        path="Ruta de Cristal (2024)/Season 01/Ruta de Cristal - S01E01.mp4",
        size_bytes=1024,
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        duration_seconds=1800.0,
        width=1920,
        height=1080,
    )
    db.add(media)
    await db.commit()

    return series, episodes


@pytest.fixture
async def viewer_r_user(db):
    """A member who may reach restricted series. "Espectador-R"."""
    from app.models import User, UserRole

    user = User(
        email="viewer-r@example.com",
        display_name="Viewer R",
        role=UserRole.VIEWER_R.value,
        locale="es",
    )
    db.add(user)
    await db.commit()
    return user
