"""All ORM models.

Import this module (not the submodules) wherever the full metadata is needed — Alembic and
`create_all` rely on every mapper being registered before they run.
"""

from app.models.base import (
    Base,
    ChallengeKind,
    JobStatus,
    JobType,
    LogLevel,
    MediaStatus,
    OAuthProvider,
    OAuthStateKind,
    SeriesStatus,
    Theme,
    TitleKind,
    TitleSource,
    UserRole,
)
from app.models.identity import (
    AuthSession,
    Credential,
    Invite,
    MagicLink,
    OAuthIdentity,
    OAuthState,
    User,
    WebAuthnChallenge,
)
from app.models.jobs import AuditLog, Job, JobLog, Setting
from app.models.library import (
    AudioTrack,
    Episode,
    EpisodeTitle,
    MediaFile,
    Series,
    SeriesCast,
    SeriesLink,
    SeriesTitle,
    SeriesTranslation,
    SubtitleTrack,
)
from app.models.requests import OPEN_STATUSES, RequestStatus, SeriesRequest
from app.models.watch import PlayEvent, WatchProgress

__all__ = [
    "OPEN_STATUSES",
    "AudioTrack",
    "AuditLog",
    "AuthSession",
    "Base",
    "ChallengeKind",
    "Credential",
    "Episode",
    "EpisodeTitle",
    "Invite",
    "Job",
    "JobLog",
    "JobStatus",
    "JobType",
    "LogLevel",
    "MagicLink",
    "MediaFile",
    "MediaStatus",
    "OAuthIdentity",
    "OAuthProvider",
    "OAuthState",
    "OAuthStateKind",
    "PlayEvent",
    "RequestStatus",
    "Series",
    "SeriesCast",
    "SeriesLink",
    "SeriesRequest",
    "SeriesStatus",
    "SeriesTitle",
    "SeriesTranslation",
    "Setting",
    "SubtitleTrack",
    "Theme",
    "TitleKind",
    "TitleSource",
    "User",
    "UserRole",
    "WatchProgress",
    "WebAuthnChallenge",
]
