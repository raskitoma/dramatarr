from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.errors import AppError
from app.models import Job, JobStatus, JobType, RequestStatus, SeriesRequest
from app.schemas.requests import RequestPatchIn
from app.services import request_acquisition as service
from app.services.requests import update


@pytest.fixture
async def series_request(db, viewer_user):
    row = SeriesRequest(
        user_id=viewer_user.id,
        title="The Dragon King",
        dedupe_key="test-dragon",
        source_url="https://www.reelshort.com/movie/abc",
    )
    db.add(row)
    await db.commit()
    return row


async def test_approval_queues_provider_search_with_language_and_review(
    db, admin_user, series_request
):
    await update(
        db, admin_user, series_request.id, RequestPatchIn(status="approved", languages=["fr", "es"])
    )
    job = await service.latest_job(db, series_request.id)
    assert job.type == JobType.FIND_SOURCE
    assert job.payload["languages"] == ["fr", "es"]
    assert job.payload["reviewBeforeDownload"] is True
    assert job.payload["sourceUrl"] == series_request.source_url
    await update(db, admin_user, series_request.id, RequestPatchIn(admin_note="Note"))
    assert len(list(await db.scalars(select(Job)))) == 1


async def test_declining_cancels_queued_acquisition(db, admin_user, series_request):
    await update(db, admin_user, series_request.id, RequestPatchIn(status="approved"))
    await update(db, admin_user, series_request.id, RequestPatchIn(status="declined"))
    job = await service.latest_job(db, series_request.id)
    assert job.status == JobStatus.CANCELED
    assert job.cancel_requested


@pytest.mark.parametrize(
    "review,id_match,expected", [(True, True, False), (False, True, True), (False, False, False)]
)
async def test_discovery_requires_review_unless_exact_published_id_matches(
    db,
    series_request,
    monkeypatch,
    review,
    id_match,
    expected,
):
    series_request.status = RequestStatus.APPROVED
    job = await service.queue_search(
        db, series_request, created_by=None, languages=["es", "en"], review=review
    )
    await db.commit()
    candidate = {
        "title": "El Rey Dragón",
        "url": "https://reelshort.dramafren.org/index.php?page=watch&id=abc&lang=es",
        "provider": "reelshort",
        "language": "es",
        "idMatch": id_match,
    }

    async def events(ctx, operation, body):
        assert operation == "discover"
        assert body["sourceUrl"] == series_request.source_url
        yield {"kind": "complete", "candidates": [candidate], "errors": []}

    monkeypatch.setattr(service, "engine_events", events)

    async def progress(*args):
        await db.commit()

    ctx = SimpleNamespace(job=job, session=db, progress=progress, cancelled=lambda: False)
    await service.handle_find_source(ctx)
    jobs = list(await db.scalars(select(Job).where(Job.type == JobType.DOWNLOAD_SERIES)))
    assert bool(jobs) == expected
    assert job.payload["acquisition"]["stage"] == "awaiting_review"
    if expected:
        assert jobs[0].payload["language"] == "es"


async def test_confirmation_must_use_a_saved_result_and_preserves_selected_language(
    db, series_request
):
    series_request.status = RequestStatus.APPROVED
    job = await service.queue_search(db, series_request, created_by=None)
    job.status = JobStatus.SUCCEEDED
    url = "https://reelshort.dramafren.org/index.php?page=watch&id=abc&lang=pt"
    job.payload = {
        **job.payload,
        "candidates": [{"url": url, "language": "pt", "provider": "reelshort"}],
    }
    await db.commit()
    with pytest.raises(AppError):
        await service.confirm_candidate(
            db,
            series_request,
            job,
            "https://reelshort.dramafren.org/index.php?page=watch&id=other",
            None,
        )
    download = await service.confirm_candidate(db, series_request, job, url, None)
    await db.commit()
    assert download.payload["language"] == "pt"
    again = await service.confirm_candidate(db, series_request, job, url, None)
    assert again.id == download.id


async def test_source_failure_does_not_report_an_empty_success(db, series_request, monkeypatch):
    series_request.status = RequestStatus.APPROVED
    job = await service.queue_search(db, series_request, created_by=None)
    await db.commit()

    async def events(*args):
        yield {
            "kind": "complete",
            "candidates": [],
            "errors": [{"provider": "reelshort", "error": "HTTP 500"}],
        }

    monkeypatch.setattr(service, "engine_events", events)

    async def progress(*args):
        await db.commit()

    ctx = SimpleNamespace(job=job, session=db, progress=progress, cancelled=lambda: False)
    with pytest.raises(AppError):
        await service.handle_find_source(ctx)
    assert job.payload["acquisition"]["stage"] == "search_failed"


async def test_retry_keeps_search_preferences_and_rejects_superseded_jobs(
    db, admin_user, series_request
):
    from app.api.admin import retry_job

    series_request.status = RequestStatus.APPROVED
    job = await service.queue_search(
        db, series_request, created_by=admin_user.id, languages=["pt", "es"], review=False
    )
    job.status = JobStatus.FAILED
    job.error = "Navigation timeout"
    job.payload = {**job.payload, "searchErrors": [{"error": job.error}], "candidates": []}
    await db.commit()

    result = await retry_job(job.id, db, admin_user)
    assert result.status == JobStatus.QUEUED
    assert result.error is None
    assert result.payload["languages"] == ["pt", "es"]
    assert result.payload["reviewBeforeDownload"] is False
    assert result.payload["acquisition"] == {"stage": "searching"}
    assert "searchErrors" not in result.payload

    job.status = JobStatus.FAILED
    await db.flush()
    await service.queue_search(db, series_request, created_by=admin_user.id)
    await db.commit()
    with pytest.raises(AppError, match="newer acquisition"):
        await retry_job(job.id, db, admin_user)


async def test_cached_search_survives_failed_download_until_cleanup(db, admin_user, series_request):
    series_request.status = RequestStatus.APPROVED
    search = await service.queue_search(db, series_request, created_by=admin_user.id)
    search.status = JobStatus.SUCCEEDED
    url = "https://reelshort.dramafren.org/index.php?page=watch&id=abc&lang=es"
    search.payload = {**search.payload, "candidates": [{"url": url, "language": "es"}]}
    await db.commit()

    download = await service.confirm_candidate(db, series_request, search, url, admin_user.id)
    download.status = JobStatus.FAILED
    await db.commit()

    assert (await service.cached_search(db, series_request)).id == search.id
    assert (
        await service.queue_search(db, series_request, created_by=admin_user.id)
    ).id == search.id
    fresh = await service.queue_search(db, series_request, created_by=admin_user.id, refresh=True)
    assert fresh.id != search.id
    assert (await service.cached_search(db, series_request)).id == search.id
    await service.clear_cached_searches(db, series_request.id)
    await db.commit()
    assert await service.cached_search(db, series_request) is None
    assert "candidates" not in search.payload
