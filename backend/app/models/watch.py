"""Per-user watch state.

Progress is deliberately per (user, episode) with no shared state: the whole point of the
household model is that two people watching the same series never disturb each other.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, created_column, pk

if TYPE_CHECKING:
    from app.models.identity import User
    from app.models.library import Episode


class WatchProgress(Base):
    __tablename__ = "watch_progress"

    id: Mapped[str] = pk()
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )

    position_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    play_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    first_played_at: Mapped[datetime] = created_column()
    last_played_at: Mapped[datetime] = created_column()

    user: Mapped[User] = relationship(back_populates="progress")
    episode: Mapped[Episode] = relationship(back_populates="progress")

    __table_args__ = (
        UniqueConstraint("user_id", "episode_id", name="uq_watch_user_episode"),
        Index("ix_watch_user_last_played", "user_id", "last_played_at"),
    )


class PlayEvent(Base):
    """Append-only trail used only to order "continue watching"."""

    __tablename__ = "play_events"

    id: Mapped[str] = pk()
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    series_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    at: Mapped[datetime] = created_column()

    __table_args__ = (Index("ix_play_events_user_at", "user_id", "at"),)


__all__ = ["PlayEvent", "WatchProgress"]
