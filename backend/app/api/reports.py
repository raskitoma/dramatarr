"""Authenticated feedback intake and administrator-only triage."""

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert

from app.api.deps import AdminUser, CurrentUser, SessionDep
from app.errors import AppError, Forbidden, NotFound
from app.models import User
from app.models.reports import Report, ReportBlock
from app.utils.time import as_aware, utcnow

router = APIRouter()
Category = Literal["error", "feature", "suggestion"]
Priority = Literal["normal", "high", "urgent", "critical"]


class ErrorItem(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    section: str = Field(max_length=200)
    timestamp: datetime
    comment: str = Field(default="", max_length=1000)


class ReportIn(BaseModel):
    category: Category
    section: str = Field(max_length=200)
    message: str = Field(default="", max_length=2000)
    errors: list[ErrorItem] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check_content(self) -> "ReportIn":
        if (self.errors and self.category != "error") or not (self.errors or self.message.strip()):
            raise ValueError("Provide a message or select errors to report")
        return self


class ReportOut(ReportIn):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str | None
    reporter: str
    priority: Priority
    closed: bool
    notes: list[dict[str, str]]
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> datetime | None:
        return as_aware(value)


class ReportPatch(BaseModel):
    priority: Priority | None = None
    closed: bool | None = None
    note: str = Field(default="", max_length=2000)


@router.post("/reports", status_code=204)
async def submit_report(body: ReportIn, session: SessionDep, user: CurrentUser) -> None:
    if await session.get(ReportBlock, user.id):
        raise Forbidden(message="Reporting is disabled for this account")
    recent = await session.scalar(
        select(func.count())
        .select_from(Report)
        .where(Report.user_id == user.id, Report.created_at >= utcnow() - timedelta(hours=1))
    )
    if recent is not None and recent >= 10:
        raise AppError("RATE_LIMITED", "Too many reports. Try again later.", 429)
    session.add(Report(**body.model_dump(mode="json"), user_id=user.id, reporter=user.email))
    await session.flush()


@router.get("/admin/reports", response_model=list[ReportOut])
async def list_reports(
    session: SessionDep, admin: AdminUser, before: str | None = None
) -> list[Report]:
    query = select(Report).order_by(Report.created_at.desc(), Report.id.desc()).limit(50)
    if before:
        last = await session.get(Report, before)
        if last is None:
            raise NotFound("Report")
        query = query.where(
            (Report.created_at < last.created_at)
            | ((Report.created_at == last.created_at) & (Report.id < last.id))
        )
    return list(await session.scalars(query))


@router.get("/admin/reports/blocks")
async def list_blocks(session: SessionDep, admin: AdminUser) -> list[dict[str, str]]:
    rows = await session.execute(
        select(User.id, User.email)
        .join(ReportBlock, ReportBlock.user_id == User.id)
        .order_by(User.email)
    )
    return [{"id": row.id, "email": row.email} for row in rows]


@router.put("/admin/reports/blocks/{user_id}", status_code=204)
async def block_reporter(user_id: str, session: SessionDep, admin: AdminUser) -> None:
    if await session.get(User, user_id) is None:
        raise NotFound("User")
    await session.execute(insert(ReportBlock).values(user_id=user_id).on_conflict_do_nothing())


@router.delete("/admin/reports/blocks/{user_id}", status_code=204)
async def unblock_reporter(user_id: str, session: SessionDep, admin: AdminUser) -> None:
    if block := await session.get(ReportBlock, user_id):
        await session.delete(block)


@router.patch("/admin/reports/{report_id}", response_model=ReportOut)
async def update_report(
    report_id: str, body: ReportPatch, session: SessionDep, admin: AdminUser
) -> Report:
    report = await session.get(Report, report_id)
    if report is None:
        raise NotFound("Report")
    if body.priority is not None:
        report.priority = body.priority
    if body.closed is not None:
        report.closed = body.closed
    if note := body.note.strip():
        await session.flush()
        await session.execute(
            update(Report)
            .where(Report.id == report.id)
            .values(
                notes=func.json_insert(
                    Report.notes,
                    "$[#]",
                    func.json_object(
                        "author", admin.email, "message", note, "at", utcnow().isoformat()
                    ),
                )
            )
        )
        await session.refresh(report)
    return report
