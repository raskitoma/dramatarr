"""Job queue semantics.

Strict serialization is the one property the whole design rests on: the worker exists as a
single in-process task precisely so that "one job at a time" is enforced by the runtime.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models import JobStatus, JobType

pytestmark = pytest.mark.anyio


async def test_enqueue_creates_queued_job(db, admin_user) -> None:
    from app.workers.queue import enqueue

    job = await enqueue(db, JobType.LIBRARY_SCAN, {"full": False}, created_by_id=admin_user.id)
    await db.commit()

    assert job.status == JobStatus.QUEUED
    assert job.type == JobType.LIBRARY_SCAN
    assert job.progress_percent == 0.0
    assert job.attempts == 0


async def test_jobs_claimed_in_priority_then_creation_order(db, admin_user) -> None:
    from app.workers.queue import claim_next_job, enqueue

    first = await enqueue(db, JobType.LIBRARY_SCAN, {}, priority=100)
    urgent = await enqueue(db, JobType.METADATA_REFRESH, {}, priority=10)
    await enqueue(db, JobType.IMAGE_CACHE, {}, priority=100)
    await db.commit()

    claimed = await claim_next_job(db)
    assert claimed is not None
    assert claimed.id == urgent.id, "lower priority number must run first"

    claimed.status = JobStatus.SUCCEEDED
    await db.commit()

    second = await claim_next_job(db)
    assert second is not None
    assert second.id == first.id, "same priority falls back to creation order"


async def test_only_one_job_runs_at_a_time(db, admin_user) -> None:
    """The property the whole design rests on: a claimed job is never handed out twice."""
    from app.workers.queue import claim_next_job, enqueue

    await enqueue(db, JobType.LIBRARY_SCAN, {})
    await db.commit()

    first = await claim_next_job(db)
    second = await claim_next_job(db)

    assert first is not None
    assert second is None, "a job already RUNNING must not be claimable again"


async def test_running_job_is_not_reclaimed(db) -> None:
    from app.workers.queue import claim_next_job, enqueue

    job = await enqueue(db, JobType.LIBRARY_SCAN, {})
    await db.commit()

    claimed = await claim_next_job(db)
    assert claimed is not None and claimed.status == JobStatus.RUNNING
    assert claimed.id == job.id
    assert await claim_next_job(db) is None


async def test_cancel_requested_is_visible_to_context(db) -> None:
    """Cancellation is cooperative — a long scan polls this between files.

    The flag lives on the running context rather than being re-read from the row, which is
    what makes a cancel take effect without the handler touching the database.
    """
    from app.workers.queue import JobContext, enqueue

    job = await enqueue(db, JobType.LIBRARY_SCAN, {})
    await db.commit()

    ctx = JobContext(job=job, session=db)
    assert ctx.cancelled() is False

    ctx.mark_cancelled()
    assert ctx.cancelled() is True
    assert ctx.cancel_requested is True


async def test_progress_updates_job(db) -> None:
    from app.workers.queue import JobContext, enqueue

    job = await enqueue(db, JobType.LIBRARY_SCAN, {})
    await db.commit()

    ctx = JobContext(job=job, session=db)
    await ctx.progress(42.5, "Scanning Ruta de Cristal")
    # progress() throttles DB writes, so give the in-memory object the assertion.
    assert job.progress_percent == pytest.approx(42.5)
    assert "Ruta de Cristal" in job.progress_message


async def test_events_are_published_to_subscribers() -> None:
    from app.workers.events import publish, subscribe

    received: list[tuple[str, dict]] = []

    async def consume() -> None:
        async for event, data in subscribe():
            received.append((event, data))
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # let the subscriber register before publishing
    await publish("job.started", {"id": "abc"})
    await asyncio.wait_for(task, timeout=2.0)

    assert received == [("job.started", {"id": "abc"})]
