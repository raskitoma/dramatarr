"""Protect data identity and the authenticated boundary across the three engines."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import integration
from app.api.deps import get_current_user
from app.config import get_settings
from app.db import get_session
from app.errors import AppError, install_error_handlers
from app.models import Job, JobType, Series
from app.services.acquisition import handle_download_series, publish_download, validate_source


@pytest.mark.parametrize(
    "url",
    [
        "http://dramafren.org/watch/x",
        "https://dramafren.org.evil/x",
        "https://user:password@dramafren.org/x",
        "https://127.0.0.1/x",
        "https://dramafren.org:80/x",
    ],
)
def test_refuses_unapproved_origin(url):
    with pytest.raises(AppError):
        validate_source(url)


def test_publish_preserves_library_and_partials(tmp_path):
    source, library = tmp_path / "staging" / "Title", tmp_path / "library"
    source.mkdir(parents=True)
    library.mkdir()
    (library / "existing.mp4").write_bytes(b"existing content")
    (source / "Episode_01.mp4").write_bytes(b"complete")
    (source / "Episode_02.mp4.part").write_bytes(b"partial")
    folder = publish_download(source, library, "a" * 32)
    assert (library / "existing.mp4").read_bytes() == b"existing content"
    assert (library / folder / "Episode_01.mp4").read_bytes() == b"complete"
    assert not (library / folder / "Episode_02.mp4.part").exists()
    assert (source / "Episode_02.mp4.part").read_bytes() == b"partial"
    (source / "dramadown.json").write_text('{"lastUpdatedAt":"later"}')
    assert publish_download(source, library, "a" * 32) == folder
    (library / folder / "Episode_01.mp4").write_bytes(b"changed by someone")
    with pytest.raises(FileExistsError):
        publish_download(source, library, "a" * 32)
    assert (library / folder / "Episode_01.mp4").read_bytes() == b"changed by someone"


def test_publish_refuses_symlink_destination(tmp_path):
    source, library, outside = tmp_path / "Title", tmp_path / "library", tmp_path / "outside"
    source.mkdir()
    library.mkdir()
    outside.mkdir()
    (source / "Episode_01.mp4").write_bytes(b"complete")
    (library / f"Title [download-{'a' * 32}]").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        publish_download(source, library, "a" * 32)
    assert list(outside.iterdir()) == []


def app_for(db, user=None):
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(integration.router, prefix="/api")

    async def session():
        yield db

    app.dependency_overrides[get_session] = session
    if user:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


@pytest.mark.parametrize(
    "endpoint,method",
    [
        ("/status", "GET"),
        ("/catalogue", "GET"),
        ("/console/admin", "GET"),
        ("/console/admin/sync", "POST"),
        ("/acquire", "POST"),
        ("/requests/r1", "GET"),
        ("/requests/r1/search", "POST"),
        ("/requests/r1/download", "POST"),
    ],
)
async def test_every_new_surface_requires_admin(db, viewer_user, endpoint, method):
    async with AsyncClient(
        transport=ASGITransport(app=app_for(db, viewer_user)), base_url="http://test"
    ) as client:
        result = await client.request(
            method,
            "/api/admin/integration" + endpoint,
            json={"url": "https://dramafren.org/watch/test"},
        )
    assert result.status_code == 403
    assert await db.scalar(select(Job.id)) is None


async def test_acquire_creates_one_persistent_job(db, admin_user, monkeypatch):
    monkeypatch.setattr(get_settings(), "downloader_key", "private-test-key")
    async with AsyncClient(
        transport=ASGITransport(app=app_for(db, admin_user)), base_url="http://test"
    ) as client:
        first = await client.post(
            "/api/admin/integration/acquire", json={"url": "https://dramafren.org/watch/test"}
        )
        assert first.status_code == 202
        second = await client.post(
            "/api/admin/integration/acquire", json={"url": "https://dramafren.org/watch/test"}
        )
    assert first.json()["id"] == second.json()["id"]
    await db.commit()
    job = await db.get(Job, first.json()["id"])
    assert job.type == JobType.DOWNLOAD_SERIES
    assert job.max_attempts == 1
    assert job.created_by_id == admin_user.id


async def test_complete_download_imports_scoped_and_leaves_existing_rows(
    db,
    sample_series,
    tmp_env,
    monkeypatch,
    viewer_user,
):
    from app.services.scanner import scan as scanner
    from app.services.scanner.probe import ProbeOutcome, ProbeResult, VideoStream

    settings = get_settings()
    settings.downloader_key = "test-key"
    settings.download_staging_root = tmp_env / "incoming"
    from app.models import RequestStatus, SeriesRequest
    from app.services import requests as request_service

    requested = SeriesRequest(
        user_id=viewer_user.id, title="A Show", dedupe_key="a-show", status=RequestStatus.APPROVED
    )
    db.add(requested)
    await db.flush()
    announced = []

    async def announce(session, row):
        announced.append(row.id)

    monkeypatch.setattr(request_service, "_announce_ready", announce)
    job = Job(
        type=JobType.DOWNLOAD_SERIES,
        payload={"url": "https://dramafren.org/watch/test", "requestId": requested.id},
    )
    db.add(job)
    await db.commit()
    source = settings.download_staging_root / job.id / "A Show"
    source.mkdir(parents=True)
    (source / "Episode_01.mp4").write_bytes(b"completed media")
    original_series, original_episodes = sample_series
    original_id = original_series.id
    original_episode_ids = [e.id for e in original_episodes]

    async def fake_probe(*args, **kwargs):
        return ProbeOutcome(
            result=ProbeResult(
                duration_seconds=72,
                container="mp4",
                video=[VideoStream(index=0, codec="h264", width=360, height=640)],
            )
        )

    # Match the scanner's real probe API below; an invalid fake must fail this test.
    monkeypatch.setattr(scanner, "probe", fake_probe)
    real_client = httpx.AsyncClient

    async def respond(request):
        assert request.headers["X-Worker-Key"] == "test-key"
        return httpx.Response(
            200,
            text=(
                'data: {"kind":"complete","cleanTitle":"A Show",'
                '"downloadedCount":1,"failedCount":0}\n\n'
            ),
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)
    )
    progress = []

    async def record(*args):
        progress.append(args)

    ctx = SimpleNamespace(job=job, session=db, progress=record, log=record, cancelled=lambda: False)
    await handle_download_series(ctx)
    assert await db.get(Series, original_id) is not None
    from app.models import Episode

    assert set(await db.scalars(select(Episode.id).where(Episode.series_id == original_id))) == set(
        original_episode_ids
    )
    rows = list((await db.scalars(select(Series))).all())
    assert len(rows) == 2
    assert (source / "Episode_01.mp4").exists()
    assert requested.status == RequestStatus.FULFILLED
    assert requested.series_id == job.series_id
    assert announced == [requested.id]
    assert job.payload["acquisition"]["episodesCompleted"] == 1
    assert job.payload["acquisition"]["stage"] == "complete"


@pytest.mark.parametrize(
    "value",
    [
        "再见我的家人",
        "Москва",
        "Don't look back",
        "Café",
        "Tempest：The Last Mecha",
        "Straße",
        "under_score",
    ],
)
def test_catalogue_normalization_contract(value):
    import importlib.util
    from pathlib import Path

    from app.services.dramadex import normalize_title

    path = Path(__file__).resolve().parents[2] / "catalogue" / "app" / "normalize.py"
    spec = importlib.util.spec_from_file_location("catalogue_normalization_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert normalize_title(value) == module.normalize_title(value)
    assert normalize_title(value)


async def test_invalid_language_returns_validation_error_without_queuing(db, admin_user):
    async with AsyncClient(
        transport=ASGITransport(app=app_for(db, admin_user)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/admin/integration/requests/not-real/search", json={"languages": ["../../bad"]}
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await db.scalar(select(Job.id)) is None
