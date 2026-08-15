"""Admin API — queue control, users, invites, settings, stats and the SSE event stream.

Every route here is gated on `require_admin` at the router level; the `AdminUser` parameter
on individual routes is the (dependency-cached) caller, needed for the audit trail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import AdminUser, SessionDep, hash_token, require_admin
from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import AppError, Conflict, MailNotConfigured, NotFound
from app.models import (
    Episode,
    Invite,
    Job,
    JobStatus,
    JobType,
    MediaFile,
    MediaStatus,
    RequestStatus,
    Series,
    SeriesRequest,
    User,
    UserRole,
)
from app.schemas.admin import (
    ContentRatingGroupOut,
    DeletedOut,
    InviteCreatedOut,
    InviteCreateIn,
    InviteOut,
    JobCreateIn,
    JobDetailOut,
    JobOut,
    JobReorderIn,
    QueueStateOut,
    SearchEngineOut,
    SearchEnginesOut,
    SearchTestOut,
    SettingsUpdateIn,
    StatsOut,
    TestResultOut,
    TmdbTestIn,
    UserOut,
    UserPatchIn,
    VocabularyOut,
)
from app.schemas.common import Ok, Page
from app.schemas.requests import (
    AdminSeriesRequestOut,
    RequestCounts,
    RequestFolderOut,
    RequestPatchIn,
    RequestStatusName,
)
from app.services import audit
from app.services import oauth as oauth_service
from app.services import requests as requests_service
from app.services.images import diagnose_search, list_search_engines
from app.services.mail import send_mail
from app.services.settings import REDACTED, SettingsService
from app.services.vocab import CONTENT_RATINGS, PROVIDERS
from app.utils.ids import new_token
from app.utils.time import in_minutes, is_expired, utcnow
from app.workers.events import publish, subscribe
from app.workers.queue import (
    enqueue,
    job_event_payload,
    publish_queue_state,
    queue_snapshot,
    request_cancel,
    series_title_for,
    wake_worker,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

TERMINAL_STATUSES: tuple[str, ...] = (
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELED,
)
HEARTBEAT_SECONDS = 25.0
TMDB_BASE = "https://api.themoviedb.org/3"


# --- Shared helpers -------------------------------------------------------------------------


async def _get_job(session: AsyncSession, job_id: str, *, with_logs: bool = False) -> Job:
    stmt = select(Job).where(Job.id == job_id)
    if with_logs:
        stmt = stmt.options(selectinload(Job.logs))
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise NotFound("Job", "JOB_NOT_FOUND")
    return job


#: Display label for a job's series. Defined in the queue module because the SSE payloads
#: built there need exactly the same lookup; two copies would be two things to keep in step.
_series_title = series_title_for


async def _job_out(session: AsyncSession, job: Job) -> JobOut:
    out = JobOut.model_validate(job)
    out.series_title = await _series_title(session, job.series_id)
    return out


async def _job_detail_out(session: AsyncSession, job: Job) -> JobDetailOut:
    out = JobDetailOut.model_validate(job)
    out.series_title = await _series_title(session, job.series_id)
    return out


async def _queue_state(session: AsyncSession) -> QueueStateOut:
    # `queue_snapshot` is the queue module's own `queue.state` payload; reusing it keeps the
    # REST view and the SSE event from drifting into two shapes the UI has to reconcile.
    # `current` already carries `seriesTitle` — the worker resolves it once when it claims
    # the job — so there is nothing to patch in here.
    return QueueStateOut.model_validate(await queue_snapshot(session))


async def _assert_not_last_admin(session: AsyncSession, target: User, *, action: str) -> None:
    """Refuse any change that would leave the household with no way back in.

    Used by the demote, deactivate and delete paths alike so the three cannot drift apart.
    """
    if target.role != UserRole.ADMIN or not target.is_active:
        return
    remaining = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.role == UserRole.ADMIN,
            User.is_active.is_(True),
            User.id != target.id,
        )
    )
    if not remaining:
        raise Conflict("LAST_ADMIN", f"Cannot {action} the last active administrator")


def _assert_not_protected(target: User, *, action: str) -> None:
    """Refuse any admin-surface mutation of the protected founding account.

    `_assert_not_last_admin` answers "is anyone left?"; this answers "is this *the* account?".
    The founding account is the household's way back in when everything else has gone wrong,
    and it is the one loss with no remedy short of shell access — so the management surface
    cannot touch it at all: not its role, not its active flag, not even its display name.

    Deliberately unconditional, and deliberately not "unless it is you". An attacker holding
    an admin session is precisely the case this exists for, and any self-exception would hand
    them the account they compromised. The protected person still edits their own profile
    through `/api/me`, which is self-service and never reaches this router, and the flag
    itself is moved only from a shell: `python -m app.cli protect|unprotect`.

    Called by every path here that mutates a user, the way `_assert_not_last_admin` is, so a
    route added later inherits the guard by reaching for the same helper.
    """
    if target.is_protected:
        raise Conflict("PROTECTED_ACCOUNT", f"Cannot {action} the protected founding account")


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        is_protected=user.is_protected,
        locale=user.locale,
        theme=user.theme,
        preferred_audio_lang=user.preferred_audio_lang,
        preferred_subtitle_lang=user.preferred_subtitle_lang,
        autoplay_next=user.autoplay_next,
        avatar_color=user.avatar_color,
        passkey_count=len(user.credentials),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _invite_status(invite: Invite) -> str:
    if invite.accepted_at is not None:
        return "accepted"
    if invite.revoked_at is not None:
        return "revoked"
    if is_expired(invite.expires_at):
        return "expired"
    return "pending"


def _creator_name(invite: Invite) -> str | None:
    # A just-constructed Invite has no loaded `created_by`, and touching it would fire a
    # lazy load outside the greenlet; callers that know the creator pass the name instead.
    if "created_by" in sa_inspect(invite).unloaded:
        return None
    creator = invite.created_by
    return creator.display_name if creator is not None else None


def _invite_out(invite: Invite, *, created_by_name: str | None = None) -> InviteOut:
    if created_by_name is None:
        created_by_name = _creator_name(invite)
    return InviteOut(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        status=_invite_status(invite),
        expires_at=invite.expires_at,
        accepted_at=invite.accepted_at,
        revoked_at=invite.revoked_at,
        created_at=invite.created_at,
        created_by_id=invite.created_by_id,
        created_by_name=created_by_name,
        accepted_user_id=invite.accepted_user_id,
    )


def _invite_url(raw_token: str) -> str:
    return f"{get_settings().public_url.rstrip('/')}/invite/{raw_token}"


def _invite_created(invite: InviteOut, url: str, *, email_sent: bool) -> InviteCreatedOut:
    """Flat: the invite's own fields at the top level, plus the one-time enrollment link."""
    return InviteCreatedOut(**invite.model_dump(), enrollment_url=url, email_sent=email_sent)


async def _mail_invite(
    session: AsyncSession, invite: Invite, url: str, *, inviter_name: str | None = None
) -> bool:
    """Best-effort. A missing or broken SMTP config must never fail invite creation — the
    admin still gets the enrollment URL back and can pass it along by hand."""
    service = SettingsService(session)
    locale = await service.get_str("site.default_locale", get_settings().default_locale)
    context = {
        "url": url,
        "invite_url": url,
        "email": invite.email,
        "role": invite.role,
        "site_name": await service.get_str("site.name", "dramatarr"),
        "expires_at": invite.expires_at,
        # The shipped templates greet with "<name> has invited you" and fall back to the
        # impersonal phrasing when this is empty, so it must be passed, not omitted.
        "inviter_name": inviter_name or "",
    }
    try:
        await send_mail(session, invite.email, "invite", locale, context)
    except MailNotConfigured:
        return False
    except Exception:
        log.warning("Invite mail to %s failed", invite.email, exc_info=True)
        return False
    return True


# --- Jobs -----------------------------------------------------------------------------------


@router.get("/jobs", response_model=Page[JobOut])
async def list_jobs(
    session: SessionDep,
    job_status: JobStatus | None = Query(None, alias="status"),
    job_type: JobType | None = Query(None, alias="type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200, alias="pageSize"),
) -> Page[JobOut]:
    filters = []
    if job_status is not None:
        filters.append(Job.status == job_status)
    if job_type is not None:
        filters.append(Job.type == job_type)

    total = await session.scalar(select(func.count()).select_from(Job).where(*filters))
    # The series title rides along on the same statement: the queue page labels every row
    # with it, and a per-row lookup would be one query per job on every poll.
    stmt = select(Job, Series.title).outerjoin(Series, Series.id == Job.series_id).where(*filters)
    if job_status == JobStatus.QUEUED:
        # Pending work is only meaningful in the order the worker will claim it.
        stmt = stmt.order_by(Job.priority.asc(), Job.created_at.asc())
    else:
        stmt = stmt.order_by(Job.created_at.desc())
    rows = (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()

    items: list[JobOut] = []
    for job, series_title in rows:
        out = JobOut.model_validate(job)
        out.series_title = series_title
        items.append(out)

    return Page[JobOut](
        items=items,
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


@router.post("/jobs/reorder", response_model=JobOut)
async def reorder_job(payload: JobReorderIn, session: SessionDep) -> JobOut:
    job = await _get_job(session, payload.job_id)
    if job.status != JobStatus.QUEUED:
        raise Conflict("JOB_NOT_QUEUED", "Only a queued job can be reordered")
    job.priority = payload.priority
    await session.flush()
    await publish_queue_state(session)
    return await _job_out(session, job)


@router.delete("/jobs/completed", response_model=DeletedOut)
async def clear_completed_jobs(session: SessionDep, admin: AdminUser) -> DeletedOut:
    result = await session.execute(delete(Job).where(Job.status.in_(TERMINAL_STATUSES)))
    deleted = int(cast("CursorResult[Any]", result).rowcount or 0)
    await audit.record(session, admin.id, "job.clear_completed", "job", None, {"deleted": deleted})
    await publish_queue_state(session)
    return DeletedOut(deleted=deleted)


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
async def get_job(job_id: str, session: SessionDep) -> JobDetailOut:
    job = await _get_job(session, job_id, with_logs=True)
    return await _job_detail_out(session, job)


async def _display_ref(session: AsyncSession, model: Any, *candidates: Any) -> str | None:
    """Resolve a payload id to a *display* foreign key, or None.

    `Job.series_id`/`Job.episode_id` are real foreign keys, so handing them an id that is
    not in the table fails the insert and surfaces as a 500. The payload itself is opaque
    and stays untouched — the handler is the one that gets to complain about a bad id.
    """
    for value in candidates:
        if isinstance(value, str) and value and await session.get(model, value) is not None:
            return value
    return None


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreateIn, session: SessionDep, admin: AdminUser) -> JobOut:
    data = payload.payload
    job = await enqueue(
        session,
        payload.type,
        data,
        priority=payload.priority,
        created_by_id=admin.id,
        series_id=await _display_ref(session, Series, data.get("seriesId"), data.get("series_id")),
        episode_id=await _display_ref(
            session, Episode, data.get("episodeId"), data.get("episode_id")
        ),
    )
    return await _job_out(session, job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str, session: SessionDep, admin: AdminUser) -> JobOut:
    job = await _get_job(session, job_id)
    if job.is_terminal:
        raise Conflict("JOB_NOT_CANCELABLE", "Job has already finished")

    job.cancel_requested = True
    if job.status == JobStatus.QUEUED:
        # Nothing is holding it, so it never has to start.
        job.status = JobStatus.CANCELED
        job.finished_at = utcnow()
        job.progress_message = "canceled"
        await session.flush()
        await publish(
            "job.finished", job_event_payload(job, await _series_title(session, job.series_id))
        )
        await publish_queue_state(session)
    else:
        # Running: the DB flag is the durable signal (it survives a restart); the in-process
        # nudge just saves the handler a poll interval.
        await session.flush()
        request_cancel(job.id)
        await publish(
            "job.progress", job_event_payload(job, await _series_title(session, job.series_id))
        )
    await audit.record(session, admin.id, "job.cancel", "job", job.id, {"type": job.type})
    return await _job_out(session, job)


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str, session: SessionDep, admin: AdminUser) -> JobOut:
    job = await _get_job(session, job_id)
    if job.status not in {JobStatus.FAILED, JobStatus.CANCELED}:
        raise Conflict("JOB_NOT_RETRYABLE", "Only a failed or canceled job can be retried")

    job.status = JobStatus.QUEUED
    job.attempts = 0
    job.error = None
    job.cancel_requested = False
    job.progress_percent = 0.0
    job.progress_message = ""
    job.started_at = None
    job.finished_at = None
    job.next_attempt_at = None
    await session.flush()
    await publish(
        "job.created", job_event_payload(job, await _series_title(session, job.series_id))
    )
    await publish_queue_state(session)
    wake_worker()
    await audit.record(session, admin.id, "job.retry", "job", job.id, {"type": job.type})
    return await _job_out(session, job)


# --- Queue ----------------------------------------------------------------------------------


@router.get("/queue", response_model=QueueStateOut)
async def get_queue(session: SessionDep) -> QueueStateOut:
    return await _queue_state(session)


@router.post("/queue/pause", response_model=QueueStateOut)
async def pause_queue(session: SessionDep, admin: AdminUser) -> QueueStateOut:
    await SettingsService(session).set("queue.paused", True, user_id=admin.id)
    await audit.record(session, admin.id, "queue.pause", "queue", None, {})
    await publish_queue_state(session)
    return await _queue_state(session)


@router.post("/queue/resume", response_model=QueueStateOut)
async def resume_queue(session: SessionDep, admin: AdminUser) -> QueueStateOut:
    await SettingsService(session).set("queue.paused", False, user_id=admin.id)
    await audit.record(session, admin.id, "queue.resume", "queue", None, {})
    await publish_queue_state(session)
    wake_worker()  # a resumed queue should start claiming now, not after the idle sleep
    return await _queue_state(session)


# --- SSE ------------------------------------------------------------------------------------


def _sse(event: str, data: Any) -> str:
    body = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


async def _event_stream(request: Request, initial: str) -> AsyncGenerator[str, None]:
    """Bridge the process-wide pub/sub onto one HTTP response.

    Events are pumped into a local queue by a child task rather than iterated directly, so
    the heartbeat timeout cancels a `Queue.get()` instead of cancelling the shared
    subscriber generator mid-await.
    """
    outbox: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(maxsize=512)
    source = subscribe()

    async def pump() -> None:
        try:
            async for item in source:
                await outbox.put(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Admin SSE source failed")
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                outbox.put_nowait(None)

    task = asyncio.create_task(pump())
    try:
        yield ": open\n\n"
        yield initial
        while True:
            try:
                item = await asyncio.wait_for(outbox.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                if await request.is_disconnected():
                    break
                yield ": ping\n\n"  # keeps proxies from reaping an idle connection
                continue
            if item is None:
                break
            event, data = item
            yield _sse(event, data)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    # A snapshot first: a client that connects mid-job is correct immediately instead of
    # waiting for the next progress tick.
    #
    # Deliberately *not* a `SessionDep`: FastAPI keeps a request-scoped dependency alive
    # until the response has finished sending, and this response never finishes. A handful
    # of open admin tabs would each pin a pooled connection for hours and starve every
    # other request. A session of our own, closed here, costs one query per connect.
    async with get_sessionmaker()() as session:
        initial = _sse("queue.state", await queue_snapshot(session))
    return StreamingResponse(
        _event_stream(request, initial),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- Users ----------------------------------------------------------------------------------


@router.get("/users", response_model=list[UserOut])
async def list_users(session: SessionDep) -> list[UserOut]:
    rows = (await session.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
    return [_user_out(row) for row in rows]


@router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: str,
    payload: UserPatchIn,
    session: SessionDep,
    admin: AdminUser,
) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("User", "USER_NOT_FOUND")

    # Any field this schema carries is, by construction, a change to *someone else's*
    # account — that is what `UserPatchIn` is — so naming one at all is refused rather than
    # naming one whose value differs. Two things follow, both deliberate: a no-op PATCH is
    # still a 409, because "it happened to change nothing" is the subtlety a later refactor
    # turns into a hole; and a field added to the schema tomorrow inherits the guard instead
    # of having to be remembered here. An empty patch reads the row back, as before.
    #
    # Checked before the last-admin guard so the stricter rule gets to explain itself: on a
    # one-admin household both would fire, and "this account cannot be changed from here" is
    # the answer that tells the caller something they did not already know.
    if payload.model_fields_set:
        _assert_not_protected(user, action="change")

    demoting = payload.role is not None and payload.role != UserRole.ADMIN
    deactivating = payload.is_active is False
    if demoting or deactivating:
        await _assert_not_last_admin(session, user, action="demote" if demoting else "deactivate")

    changes: dict[str, Any] = {}
    if payload.role is not None and payload.role != user.role:
        changes["role"] = {"from": user.role, "to": str(payload.role)}
        user.role = payload.role
    if payload.is_active is not None and payload.is_active != user.is_active:
        changes["isActive"] = {"from": user.is_active, "to": payload.is_active}
        user.is_active = payload.is_active
    if payload.display_name is not None and payload.display_name != user.display_name:
        changes["displayName"] = {"from": user.display_name, "to": payload.display_name}
        user.display_name = payload.display_name

    await session.flush()
    if changes:
        await audit.record(session, admin.id, "user.update", "user", user.id, changes)
    return _user_out(user)


@router.delete("/users/{user_id}", response_model=Ok)
async def delete_user(user_id: str, session: SessionDep, admin: AdminUser) -> Ok:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("User", "USER_NOT_FOUND")
    _assert_not_protected(user, action="delete")
    await _assert_not_last_admin(session, user, action="delete")

    await audit.record(
        session,
        admin.id,
        "user.delete",
        "user",
        user.id,
        {"email": user.email, "role": user.role},
    )
    await session.delete(user)
    await session.flush()
    return Ok()


# --- Invites --------------------------------------------------------------------------------


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(session: SessionDep) -> list[InviteOut]:
    rows = (
        (await session.execute(select(Invite).order_by(Invite.created_at.desc()))).scalars().all()
    )
    return [_invite_out(row) for row in rows]


@router.post("/invites", response_model=InviteCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_invite(
    payload: InviteCreateIn,
    session: SessionDep,
    admin: AdminUser,
) -> InviteCreatedOut:
    existing = await session.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise Conflict("EMAIL_IN_USE", "A user with that email already exists")

    service = SettingsService(session)
    ttl_hours = await service.get_int("auth.invite_ttl_hours", 168)
    raw = new_token(32)
    invite = Invite(
        email=payload.email,
        role=payload.role,
        token_hash=hash_token(raw),
        created_by_id=admin.id,
        expires_at=in_minutes(ttl_hours * 60),
    )
    session.add(invite)
    await session.flush()

    url = _invite_url(raw)
    email_sent = await _mail_invite(session, invite, url, inviter_name=admin.display_name)
    await audit.record(
        session,
        admin.id,
        "invite.create",
        "invite",
        invite.id,
        {"email": invite.email, "role": invite.role, "emailSent": email_sent},
    )
    return _invite_created(
        _invite_out(invite, created_by_name=admin.display_name), url, email_sent=email_sent
    )


@router.delete("/invites/{invite_id}", response_model=Ok)
async def revoke_invite(invite_id: str, session: SessionDep, admin: AdminUser) -> Ok:
    invite = await session.get(Invite, invite_id)
    if invite is None:
        raise NotFound("Invite", "INVITE_NOT_FOUND")
    if invite.accepted_at is not None:
        raise Conflict("INVITE_ALREADY_ACCEPTED", "An accepted invite cannot be revoked")
    if invite.revoked_at is None:
        invite.revoked_at = utcnow()
        await session.flush()
        await audit.record(
            session, admin.id, "invite.revoke", "invite", invite.id, {"email": invite.email}
        )
    return Ok()


@router.post("/invites/{invite_id}/resend", response_model=InviteCreatedOut)
async def resend_invite(invite_id: str, session: SessionDep, admin: AdminUser) -> InviteCreatedOut:
    invite = await session.get(Invite, invite_id)
    if invite is None:
        raise NotFound("Invite", "INVITE_NOT_FOUND")
    if invite.accepted_at is not None:
        raise Conflict("INVITE_ALREADY_ACCEPTED", "That invite has already been used")

    # Only the hash is stored, so resending necessarily mints a fresh token; the previous
    # link stops working, which is also the desired behaviour for a leaked one.
    raw = new_token(32)
    ttl_hours = await SettingsService(session).get_int("auth.invite_ttl_hours", 168)
    invite.token_hash = hash_token(raw)
    invite.expires_at = in_minutes(ttl_hours * 60)
    invite.revoked_at = None
    await session.flush()

    # The mail still speaks for whoever originally invited them, not whoever clicked resend.
    creator_name = _creator_name(invite) or admin.display_name
    url = _invite_url(raw)
    email_sent = await _mail_invite(session, invite, url, inviter_name=creator_name)
    await audit.record(
        session,
        admin.id,
        "invite.resend",
        "invite",
        invite.id,
        {"email": invite.email, "emailSent": email_sent},
    )
    return _invite_created(
        _invite_out(invite, created_by_name=creator_name), url, email_sent=email_sent
    )


# --- Settings -------------------------------------------------------------------------------


def _with_derived(values: dict[str, Any]) -> dict[str, Any]:
    """Add the read-only values the settings screen must *show* but must never guess.

    The two Google Cloud Console entries have to be exactly what this server will send, and
    that comes from `public_url` — not from whatever origin the admin's browser happens to be
    on. Those agree on a plain localhost install and diverge on every real one: reach the box
    by LAN address or through a reverse proxy and a browser-derived value tells the admin to
    register a URI the server will never use, after which Google answers `redirect_uri_mismatch`
    with nothing in the UI to explain why.

    `update_many` ignores keys that are not in `DEFAULTS`, so these ride back on a save
    harmlessly and can never be written.
    """
    return values | {
        "derived.google.redirect_uri": oauth_service.redirect_uri(),
        "derived.google.js_origin": get_settings().public_url.rstrip("/"),
    }


@router.get("/settings", response_model=dict[str, Any])
async def get_admin_settings(session: SessionDep) -> dict[str, Any]:
    return _with_derived(await SettingsService(session).all(redact=True))


async def _forget_stale_search_key(
    service: SettingsService, values: dict[str, Any]
) -> dict[str, Any]:
    """Drop a stored image-search secret when the provider it was typed for is being replaced.

    `images.search.api_key` is **one slot serving two providers** — Google's required API key,
    and the optional token a SearXNG behind an authenticating proxy wants. That is harmless
    while the provider stays put and wrong the instant it changes: the key an admin pasted for
    `googleapis.com` would otherwise go out as `Authorization: Bearer …` to their own SearXNG
    instance, which is a live Google credential handed to a different host that never needed it.

    So a provider change forgets the key, unless the same save supplies a new one — `REDACTED`
    does not count, since that means "keep what is stored" and what is stored belongs to the
    old provider. The other provider's *non-secret* fields (`cse_id`, `base_url`, engines) are
    deliberately left alone: switching back should not mean typing everything in again, and
    those are not credentials.
    """
    incoming = values.get("images.search.provider")
    if incoming is None:
        return values
    stored = (await service.get_str("images.search.provider")).strip().lower()
    if str(incoming).strip().lower() == stored:
        return values
    supplied = values.get("images.search.api_key")
    if isinstance(supplied, str) and supplied not in {"", REDACTED}:
        return values
    return values | {"images.search.api_key": ""}


@router.put("/settings", response_model=dict[str, Any])
async def update_admin_settings(
    payload: SettingsUpdateIn,
    session: SessionDep,
    admin: AdminUser,
) -> dict[str, Any]:
    service = SettingsService(session)
    values = await _forget_stale_search_key(service, payload.root)
    await service.update_many(values, user_id=admin.id)
    await audit.record(
        session, admin.id, "settings.update", "settings", None, audit.scrub(values)
    )
    return _with_derived(await service.all(redact=True))


@router.post("/settings/test-smtp", response_model=TestResultOut)
async def test_smtp(session: SessionDep, admin: AdminUser) -> TestResultOut:
    service = SettingsService(session)
    smtp = await service.smtp()
    if not smtp.configured:
        raise MailNotConfigured()

    context = {
        "email": admin.email,
        "site_name": await service.get_str("site.name", "dramatarr"),
        "url": get_settings().public_url,
    }
    try:
        await send_mail(session, admin.email, "test", admin.locale, context)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            "SMTP_SEND_FAILED", f"SMTP send failed: {exc}", status.HTTP_502_BAD_GATEWAY
        ) from exc
    return TestResultOut(ok=True, detail=admin.email)


@router.post("/settings/test-tmdb", response_model=TestResultOut)
async def test_tmdb(
    session: SessionDep,
    admin: AdminUser,
    payload: TmdbTestIn | None = None,
) -> TestResultOut:
    service = SettingsService(session)
    candidate = (payload.api_key if payload else None) or ""
    # An unchanged form field arrives redacted; fall back to what is stored.
    if not candidate or candidate == REDACTED:
        candidate = await service.get_str("tmdb.api_key")
    if not candidate:
        raise AppError("TMDB_KEY_MISSING", "No TMDB API key configured")

    detail = await _probe_tmdb(candidate)
    return TestResultOut(ok=True, detail=detail)


async def _probe_tmdb(api_key: str) -> str:
    """Accepts either a v3 key (query param) or a v4 read token (bearer)."""
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if api_key.count(".") == 2:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        params["api_key"] = api_key

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{TMDB_BASE}/configuration", params=params, headers=headers
            )
    except httpx.HTTPError as exc:
        raise AppError(
            "TMDB_UNREACHABLE", f"Could not reach TMDB: {exc}", status.HTTP_502_BAD_GATEWAY
        ) from exc

    if response.status_code in (401, 403):
        raise AppError("TMDB_INVALID_KEY", "TMDB rejected the API key")
    if response.status_code >= 400:
        raise AppError(
            "TMDB_ERROR",
            f"TMDB returned HTTP {response.status_code}",
            status.HTTP_502_BAD_GATEWAY,
        )

    try:
        base_url = str(response.json().get("images", {}).get("secure_base_url", ""))
    except ValueError:
        base_url = ""
    return base_url or "ok"


# --- Image search diagnostics ------------------------------------------------------------------
#
# Two routes, and between them they are the difference between a five-minute setup and an
# afternoon. SearXNG ships with its JSON API **disabled**, so the very first thing a new
# configuration does is fail — and it fails as HTML, or as a bare 403, which a naive client
# reports as "the provider returned an error". `test-search` names that case exactly and says
# which line of settings.yml to change. `search/engines` then hands over the instance's real
# image-engine list, because every instance has a different set and a mistyped engine name is
# a silent no-op rather than an error.


@router.post("/settings/test-search", response_model=SearchTestOut)
async def test_search(session: SessionDep, admin: AdminUser) -> SearchTestOut:
    """Reach the configured image-search provider and report what came back.

    Failures are raised, not flattened into `ok: false`: `SEARXNG_JSON_DISABLED`,
    `SEARXNG_UNREACHABLE`, `SEARXNG_BAD_RESPONSE` and `SEARCH_NOT_CONFIGURED` mean four
    different repairs, and a single "test failed" would make the admin guess which.
    """
    result = await diagnose_search(session)
    return SearchTestOut(
        ok=True,
        detail=result.base_url or result.provider,
        provider=result.provider,
        base_url=result.base_url,
        version=result.version,
        image_engine_count=result.image_engine_count,
        sample_query=result.sample_query,
        sample_result_count=result.sample_result_count,
    )


@router.get("/search/engines", response_model=SearchEnginesOut)
async def search_engines(session: SessionDep) -> SearchEnginesOut:
    """The instance's own image engines, so the settings form offers a list to pick from."""
    listing = await list_search_engines(session)
    return SearchEnginesOut(
        provider="searxng",
        base_url=listing.base_url,
        version=listing.version,
        engines=[
            SearchEngineOut(name=engine.name, enabled=engine.enabled, shortcut=engine.shortcut)
            for engine in listing.engines
        ],
    )


# --- Vocabulary -----------------------------------------------------------------------------


@router.get("/vocabulary", response_model=VocabularyOut)
async def get_vocabulary() -> VocabularyOut:
    """The option lists the series editor's two selects are built from.

    Served rather than mirrored in the frontend on purpose. These lists are also what
    `app.services.vocab` canonicalizes *against*, so a copy in TypeScript would be a second
    source of truth that drifts the first time a provider is added — and the failure is silent:
    the select would offer a spelling the server then rewrites to something else.
    """
    return VocabularyOut(
        content_ratings=[
            ContentRatingGroupOut(group=group, ratings=list(ratings))
            for group, ratings in CONTENT_RATINGS
        ],
        providers=list(PROVIDERS),
    )


# --- Requests -------------------------------------------------------------------------------
#
# The admin's half of the wishlist. The member's half is `/api/requests`, and the split is by
# audience rather than by table on purpose: only these routes ever say *who* asked, and only
# these routes can answer. `require_admin` is on the router, so that property does not depend
# on anyone remembering a decorator.
#
# Nothing here starts any work. Marking a request fulfilled records that the admin already put
# the files on the mount; it does not go and get them.


@router.get("/requests", response_model=Page[AdminSeriesRequestOut])
async def list_requests(
    session: SessionDep,
    status_filter: Annotated[RequestStatusName | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, alias="pageSize")] = 25,
) -> Page[AdminSeriesRequestOut]:
    """The queue, open requests first. `also_requested_by` is what turns it into a priority."""
    items, total = await requests_service.list_all(
        session, status=status_filter, page=page, page_size=page_size
    )
    return Page(items=items, total=total, page=page, page_size=page_size)


@router.get("/requests/counts", response_model=RequestCounts)
async def request_counts(session: SessionDep) -> RequestCounts:
    """Per-status totals — the nav badge and the page's filter tabs read this."""
    return await requests_service.counts(session)


@router.patch("/requests/{request_id}", response_model=AdminSeriesRequestOut)
async def patch_request(
    session: SessionDep, admin: AdminUser, request_id: str, body: RequestPatchIn
) -> AdminSeriesRequestOut:
    """Answer one: change its status, leave the requester a note, or tie it to the series it
    became. Any subset — an admin who only wants to say "working on it" sends only a status."""
    return await requests_service.update(session, admin, request_id, body)


@router.post("/requests/{request_id}/folder", response_model=RequestFolderOut)
async def prepare_request_folder(session: SessionDep, request_id: str) -> RequestFolderOut:
    """Create the destination directory for a request, named the way the scanner reads it.

    This is the one wishlist route that touches the library mount, and all it does is `mkdir`.
    Its value is that the folder is named canonically *before* anything lands in it: a scan of
    `Fruit Dimension Orange Light Redemption (2025)` produces a series with that title and that
    year, so there is nothing to correct in the editor and nothing for the renamer to fix
    afterwards. A folder typed by hand almost never round-trips that cleanly.

    Idempotent — running it twice reports the directory rather than failing.
    """
    path, created = await requests_service.prepare_folder(session, request_id)
    return RequestFolderOut(path=path, created=created)


@router.delete("/requests/{request_id}", response_model=Ok)
async def delete_request(session: SessionDep, request_id: str) -> Ok:
    """Remove a row entirely.

    Declining is the answer that keeps the record and tells the requester why; this is for
    rows that should never have existed. The member is not told, which is the reason to prefer
    declining in almost every case.
    """
    await requests_service.delete_any(session, request_id)
    return Ok()


# --- Stats ----------------------------------------------------------------------------------


def _directory_size(root: Path) -> int:
    """Blocking; always call through `asyncio.to_thread`."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue  # racing with the cache writers is expected, not an error
    return total


async def _count(session: AsyncSession, model: Any, *filters: Any) -> int:
    value = await session.scalar(select(func.count()).select_from(model).where(*filters))
    return int(value or 0)


@router.get("/stats", response_model=StatsOut)
async def get_stats(session: SessionDep) -> StatsOut:
    available = await session.scalar(
        select(func.count(func.distinct(MediaFile.episode_id))).where(
            MediaFile.episode_id.is_not(None),
            MediaFile.status == MediaStatus.PRESENT,
        )
    )
    rows: Iterable[Any] = (
        await session.execute(select(Job.status, func.count()).group_by(Job.status))
    ).all()
    jobs: dict[str, int] = {str(value): 0 for value in JobStatus}
    for job_status, count in rows:
        jobs[str(job_status)] = int(count)

    # Summed in SQL from the scan records — the library lives on a NAS mount and walking it
    # for a dashboard tile would cost minutes and hammer the share.
    library_bytes = await session.scalar(
        select(func.coalesce(func.sum(MediaFile.size_bytes), 0)).where(
            MediaFile.status != MediaStatus.MISSING
        )
    )
    data_bytes = await asyncio.to_thread(_directory_size, get_settings().data_dir)

    return StatsOut(
        series_count=await _count(session, Series),
        episode_count=await _count(session, Episode),
        available_episode_count=int(available or 0),
        unmatched_count=await _count(session, MediaFile, MediaFile.episode_id.is_(None)),
        user_count=await _count(session, User),
        jobs_by_status=jobs,
        data_dir_bytes=data_bytes,
        library_bytes=int(library_bytes or 0),
        media_files=await _count(session, MediaFile),
        active_users=await _count(session, User, User.is_active.is_(True)),
        pending_request_count=await _count(
            session, SeriesRequest, SeriesRequest.status == RequestStatus.PENDING
        ),
    )


__all__ = ["router"]
