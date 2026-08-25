"""Declarative base, shared column types and enums."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.ids import new_id
from app.utils.time import utcnow


class UTCDateTime(TypeDecorator):
    """SQLite drops tzinfo on the way in and returns naive datetimes on the way out.

    Without this, every comparison against `utcnow()` raises TypeError at runtime, in the
    expiry checks that guard invites, sessions and magic links — so it is normalized here
    rather than at each call site.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            from datetime import UTC

            return value.replace(tzinfo=UTC)
        from datetime import UTC

        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            from datetime import UTC

            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    # SQLAlchemy reads this as a plain mutable class attribute; annotating it ClassVar to
    # satisfy RUF012 would fight the base class's own declaration.
    type_annotation_map = {  # noqa: RUF012
        dict[str, Any]: JSON,
        list[str]: JSON,
        datetime: UTCDateTime,
    }


def pk() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def created_column() -> Mapped[datetime]:
    return mapped_column(UTCDateTime, default=utcnow, nullable=False)


def updated_column() -> Mapped[datetime]:
    return mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --- Enums --------------------------------------------------------------------------------


class UserRole(enum.StrEnum):
    """Who somebody is, which here is also what they may see.

    Three values, not a boolean plus a flag, because the household's question is genuinely one
    of three answers. `VIEWER_R` — "Espectador-R" — is an ordinary member who may also reach
    series an admin has marked restricted.

    Stored as `String(16)`, so a new member costs no migration. **Nothing may assume that
    "not admin" means `VIEWER`**: that inference was true for the app's whole life until this
    value existed, and it is the shape of bug that ships without a sound.
    """

    ADMIN = "admin"
    VIEWER = "viewer"
    VIEWER_R = "viewer_r"

    @property
    def sees_restricted(self) -> bool:
        """Whether this role reaches series marked restricted. Asked rather than compared, so
        adding a fourth role later is one edit here instead of a search across the codebase."""
        return self in (UserRole.ADMIN, UserRole.VIEWER_R)


class Theme(enum.StrEnum):
    AUTO = "auto"
    LIGHT = "light"
    DARK = "dark"


class SeriesStatus(enum.StrEnum):
    ONGOING = "ongoing"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class TitleKind(enum.StrEnum):
    OFFICIAL = "official"
    NATIVE = "native"
    ROMANIZATION = "romanization"
    ALTERNATE = "alternate"
    ACRONYM = "acronym"


class TitleSource(enum.StrEnum):
    TMDB = "tmdb"
    MANUAL = "manual"
    FILENAME = "filename"


class MediaStatus(enum.StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    ERROR = "error"


class JobType(enum.StrEnum):
    LIBRARY_SCAN = "library_scan"
    METADATA_REFRESH = "metadata_refresh"
    IMAGE_CACHE = "image_cache"
    THUMBNAIL_SPRITE = "thumbnail_sprite"
    REMOTE_IMPORT = "remote_import"
    #: The only job that writes to the library mount. Runs through the queue like everything
    #: else so it is serialized against the scanner — a rename racing a scan would produce a
    #: catalogue full of files that moved out from under it.
    LIBRARY_RENAME = "library_rename"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class LogLevel(enum.StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ChallengeKind(enum.StrEnum):
    REGISTRATION = "registration"
    AUTHENTICATION = "authentication"


class OAuthProvider(enum.StrEnum):
    GOOGLE = "google"


class OAuthStateKind(enum.StrEnum):
    """Which flow a stored `state` belongs to.

    A state minted to sign in must never be redeemable as a link (it carries no account) and
    a link state must never be redeemable as a login (it would bypass the account resolution
    rules), so the kind is checked when the row is burned.
    """

    LOGIN = "login"
    LINK = "link"
