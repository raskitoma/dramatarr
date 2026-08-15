"""The job queue, its logs, the audit trail and the runtime settings store."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JobStatus, LogLevel, created_column, pk


class Job(Base):
    """One unit of background work.

    `priority` is ascending — lower runs first — so an admin can jump a scan ahead of a
    queue full of metadata refreshes without touching creation order.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = pk()
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    progress_message: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False, nullable=False)

    series_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("series.id", ondelete="SET NULL"), nullable=True
    )
    episode_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True
    )
    created_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = created_column()
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)

    logs: Mapped[list[JobLog]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobLog.at"
    )

    __table_args__ = (Index("ix_jobs_claim", "status", "priority", "created_at"),)

    @property
    def is_terminal(self) -> bool:
        return self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED}


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[str] = pk()
    job_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), default=LogLevel.INFO, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = created_column()

    job: Mapped[Job] = relationship(back_populates="logs")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = pk()
    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    at: Mapped[datetime] = created_column()

    __table_args__ = (Index("ix_audit_at", "at"),)


class Setting(Base):
    """Runtime-mutable configuration. Secrets are stored here and never returned by the API."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    updated_at: Mapped[datetime] = created_column()
    updated_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


__all__ = ["AuditLog", "Job", "JobLog", "Setting"]
