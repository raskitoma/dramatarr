"""Wire models for `/api/auth`.

WebAuthn payloads are passed through as opaque dicts: the browser's
`navigator.credentials.create()/get()` speak the spec's own JSON shape, and re-modelling it
in Pydantic would only add a second place for it to drift. The envelopes around it are typed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import Field, model_validator

from app.schemas.common import APIModel
from app.services.images import avatar_url

#: Deliberately permissive — `email-validator` is not a dependency and a strict RFC 5322
#: pattern would reject addresses real mailservers accept. Delivery is the real validator.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AuthUser(APIModel):
    """The identity payload every auth endpoint returns once a session exists."""

    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    locale: str
    theme: str
    preferred_audio_lang: str | None = None
    preferred_subtitle_lang: str | None = None
    autoplay_next: bool = True
    avatar_color: str
    #: Null when the member has never uploaded a photo, which is the signal for the UI to keep
    #: drawing initials on `avatarColor`. Filled in below rather than read off the User row:
    #: there is no `avatar_path` column, the file's existence is the record.
    avatar_url: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None

    @model_validator(mode="after")
    def _resolve_avatar(self) -> Self:
        """One stat of a local file, on a response that has already touched SQLite.

        Done here rather than at each call site because `AuthUser.model_validate(user)` is
        built from an ORM row in four places across the auth flow, and a field that four
        callers have to remember to populate is a field that will be null in one of them.
        """
        if self.avatar_url is None:
            self.avatar_url = avatar_url(self.id)
        return self


class SessionOut(APIModel):
    user: AuthUser | None = None


class PublicKeyOptionsOut(APIModel):
    """Shaped so the client can hand the body straight to `navigator.credentials.*`."""

    public_key: dict[str, Any]


class PasskeyOut(APIModel):
    id: str
    nickname: str
    transports: list[str] = Field(default_factory=list)
    aaguid: str | None = None
    backed_up: bool = False
    created_at: datetime
    last_used_at: datetime | None = None


class PasskeyRenameIn(APIModel):
    nickname: str = Field(min_length=1, max_length=120)


class PasskeyRegisterCompleteIn(APIModel):
    credential: dict[str, Any]
    nickname: str | None = Field(default=None, max_length=120)


class PasskeyLoginCompleteIn(APIModel):
    credential: dict[str, Any]


class InviteInfoOut(APIModel):
    email: str
    role: str
    site_name: str
    expires_at: datetime


class InviteCompleteIn(APIModel):
    credential: dict[str, Any]
    display_name: str | None = Field(default=None, max_length=120)
    locale: str | None = Field(default=None, max_length=16)
    nickname: str | None = Field(default=None, max_length=120)


class MagicLinkRequestIn(APIModel):
    email: str = Field(min_length=3, max_length=320, pattern=EMAIL_PATTERN)


class MagicLinkConsumeIn(APIModel):
    token: str = Field(min_length=8, max_length=512)


class GoogleAuthUrlOut(APIModel):
    """Where to send the browser. The state, nonce and PKCE challenge are already in it, and
    the matching secrets stay in `oauth_states` — nothing here is worth stealing."""

    authorization_url: str


class SignInMethodsOut(APIModel):
    """What this account can currently sign in with, for the account page's method list."""

    passkeys: int
    google_linked: bool
    google_email: str | None = None
    magic_link_enabled: bool


class InviteOut(APIModel):
    """Shared with `/api/admin/invites` so both surfaces describe an invite identically.

    `app.schemas.admin` re-exports this rather than declaring a second invite shape — the two
    used to disagree about `createdByName`, which is exactly the drift the sharing prevents.
    """

    id: str
    email: str
    role: str
    status: str  # pending | accepted | revoked | expired
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    created_by_id: str | None = None
    created_by_name: str | None = None
    accepted_user_id: str | None = None


class InviteCreatedOut(InviteOut):
    """A freshly minted invite, flat: every `InviteOut` field plus the enrollment link.

    The raw token is never stored, so `enrollmentUrl` is the one chance to capture it. It is
    returned whether or not the mail went out, which is what makes dramatarr usable before
    SMTP is configured.
    """

    enrollment_url: str
    email_sent: bool
    #: Legacy spelling of `enrollmentUrl`, kept so older callers keep working. Mirrored, never
    #: passed in.
    url: str = ""

    @model_validator(mode="after")
    def _mirror_legacy_url(self) -> Self:
        self.url = self.enrollment_url
        return self


__all__ = [
    "AuthUser",
    "GoogleAuthUrlOut",
    "InviteCompleteIn",
    "InviteCreatedOut",
    "InviteInfoOut",
    "InviteOut",
    "MagicLinkConsumeIn",
    "MagicLinkRequestIn",
    "PasskeyLoginCompleteIn",
    "PasskeyOut",
    "PasskeyRegisterCompleteIn",
    "PasskeyRenameIn",
    "PublicKeyOptionsOut",
    "SessionOut",
    "SignInMethodsOut",
]
