"""Private feedback and reporting restrictions."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_column, pk


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = pk()
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reporter: Mapped[str] = mapped_column(String(320))
    category: Mapped[str] = mapped_column(String(16))
    section: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    closed: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = created_column()


class ReportBlock(Base):
    __tablename__ = "report_blocks"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = created_column()
