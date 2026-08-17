"""Request/response models for `/api/admin`."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import Field, RootModel, field_validator, model_validator

from app.models import JobType, UserRole
from app.schemas.auth import InviteCreatedOut, InviteOut
from app.schemas.common import APIModel
from app.services.images import avatar_url

# --- Jobs & queue ---------------------------------------------------------------------------


class JobLogOut(APIModel):
    id: str
    level: str
    message: str
    at: datetime


class JobOut(APIModel):
    id: str
    type: str
    status: str
    priority: int
    payload: dict[str, Any] = Field(default_factory=dict)
    progress_percent: float = 0.0
    progress_message: str = ""
    attempts: int = 0
    max_attempts: int = 3
    error: str | None = None
    cancel_requested: bool = False
    series_id: str | None = None
    #: Display-only label for `series_id`; the queue page shows it on every row. Joined in by
    #: the list endpoints, looked up once by the single-job ones — never lazily per row.
    series_title: str | None = None
    episode_id: str | None = None
    created_by_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_attempt_at: datetime | None = None


class JobDetailOut(JobOut):
    logs: list[JobLogOut] = Field(default_factory=list)


class JobCreateIn(APIModel):
    type: JobType
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=100_000)


class JobReorderIn(APIModel):
    job_id: str
    priority: int = Field(ge=0, le=100_000)


class QueueStateOut(APIModel):
    paused: bool
    running: bool
    queued_count: int
    current: JobOut | None = None


class DeletedOut(APIModel):
    deleted: int


# --- Users ----------------------------------------------------------------------------------


class UserOut(APIModel):
    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    #: The founding account. Every mutation of this row through `/api/admin/users` is refused
    #: with `PROTECTED_ACCOUNT`, so the UI ships it to explain *why* the row's controls are
    #: disabled instead of letting an admin discover it by getting a 409.
    is_protected: bool = False
    locale: str
    theme: str
    preferred_audio_lang: str | None = None
    preferred_subtitle_lang: str | None = None
    autoplay_next: bool = True
    avatar_color: str
    #: Same derivation as `AuthUser.avatar_url` and `MeOut.avatar_url`, and for the same
    #: reason: there is no `avatar_path` column, the file's existence is the record. Without
    #: it the member list is the one surface in the application that draws initials for a
    #: member who has a photo everywhere else.
    avatar_url: str | None = None
    passkey_count: int = 0
    created_at: datetime
    last_login_at: datetime | None = None

    @model_validator(mode="after")
    def _resolve_avatar(self) -> Self:
        if self.avatar_url is None:
            self.avatar_url = avatar_url(self.id)
        return self


class UserPatchIn(APIModel):
    role: UserRole | None = None
    is_active: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("display_name", mode="before")
    @classmethod
    def _trim(cls, value: Any) -> Any:
        # Trim before the length constraint runs so a whitespace-only name fails as too
        # short. Constraint failures serialize cleanly; a raised ValueError would not.
        return value.strip() if isinstance(value, str) else value


# --- Invites --------------------------------------------------------------------------------

# `InviteOut`/`InviteCreatedOut` are defined in `app.schemas.auth` and re-exported here: the
# enrollment flow and the admin list must describe an invite with one and the same shape.

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class InviteCreateIn(APIModel):
    email: str = Field(min_length=3, max_length=320, pattern=EMAIL_PATTERN)
    role: UserRole = UserRole.VIEWER

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: Any) -> Any:
        # Lowercased so the invite matches however the recipient later types it.
        return value.strip().lower() if isinstance(value, str) else value


# --- Settings -------------------------------------------------------------------------------


class SettingsUpdateIn(RootModel[dict[str, Any]]):
    """Flat map of dotted setting keys. Unknown keys are ignored; a secret sent back as
    `"__SET__"` keeps its stored value."""

    root: dict[str, Any] = Field(default_factory=dict)


class TestResultOut(APIModel):
    ok: bool = True
    detail: str | None = None


# --- Stats ----------------------------------------------------------------------------------


class StatsOut(APIModel):
    """Counters behind the admin dashboard tiles.

    The `*Count` / `*Bytes` spellings are the ones the UI reads. The older, shorter names are
    kept so anything still reading them keeps working — they are mirrored from the canonical
    fields on every construction, so the two spellings cannot drift.
    """

    series_count: int
    episode_count: int
    available_episode_count: int
    unmatched_count: int
    user_count: int
    #: Keyed by `JobStatus` value, every status present — the queue page indexes it directly,
    #: so a missing key would render as `undefined` rather than zero.
    jobs_by_status: dict[str, int]
    #: Disk used by `DATA_DIR` (database, cached images, sprites, extracted subtitles).
    data_dir_bytes: int
    #: Total bytes of the media files themselves, summed from the scan records.
    library_bytes: int

    # Counters with no counterpart in the frontend contract; set by the caller as before.
    media_files: int = 0
    active_users: int = 0
    #: Open wishlist rows — pending only, not approved. The dashboard tile and the admin nav
    #: badge both read it, and "approved" is work already acknowledged, so counting it here
    #: would leave a badge lit for as long as the admin takes to find the files.
    pending_request_count: int = 0

    # --- Legacy spellings. Mirrored below, never passed in. ---
    series: int = 0
    episodes: int = 0
    available_episodes: int = 0
    unmatched_files: int = 0
    users: int = 0
    jobs: dict[str, int] = Field(default_factory=dict)
    data_bytes: int = 0

    @model_validator(mode="after")
    def _mirror_legacy_names(self) -> Self:
        self.series = self.series_count
        self.episodes = self.episode_count
        self.available_episodes = self.available_episode_count
        self.unmatched_files = self.unmatched_count
        self.users = self.user_count
        self.jobs = self.jobs_by_status
        self.data_bytes = self.data_dir_bytes
        return self


class ContentRatingGroupOut(APIModel):
    """One labelled cluster of ratings, rendered as an `<optgroup>`.

    Grouped rather than flat because `PG-13` and `TV-14` are not points on one scale, and a
    single list invites an admin to read them as if they were.
    """

    #: `film` | `tv` — the i18n key suffix, not a display string. The label is the UI's.
    group: str
    ratings: list[str]


class VocabularyOut(APIModel):
    """The option lists the series editor builds its two selects from.

    Neither list is closed. The editor keeps a free-text escape for both, because a rating this
    set does not carry (`R15+`, `12`) and a platform not yet listed are ordinary, and the
    server preserves whatever it is given.
    """

    content_ratings: list[ContentRatingGroupOut]
    providers: list[str]


__all__ = [
    "ContentRatingGroupOut",
    "DeletedOut",
    "InviteCreateIn",
    "InviteCreatedOut",
    "InviteOut",
    "JobCreateIn",
    "JobDetailOut",
    "JobLogOut",
    "JobOut",
    "JobReorderIn",
    "QueueStateOut",
    "SettingsUpdateIn",
    "StatsOut",
    "TestResultOut",
    "UserOut",
    "UserPatchIn",
    "VocabularyOut",
]
