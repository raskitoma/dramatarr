"""Users, passkeys, invites, sessions, magic links and federated identities."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    ChallengeKind,
    OAuthProvider,
    OAuthStateKind,
    Theme,
    UserRole,
    created_column,
    pk,
)

if TYPE_CHECKING:
    from app.models.watch import WatchProgress


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=UserRole.VIEWER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: The founding account — the one the household was bootstrapped with, and its recovery
    #: path of last resort. While this is set, `/api/admin/users` refuses every mutation of
    #: the row (see `_assert_not_protected`), so no admin can demote, deactivate, rename or
    #: delete it from the web UI, whether by accident or with a stolen admin session. It is
    #: deliberately not exposed as anything the API can write: the flag moves only from a
    #: shell, with `python -m app.cli protect|unprotect`, which is the same authority that
    #: already mints sign-in links. Self-service through `/api/me` is unaffected.
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    locale: Mapped[str] = mapped_column(String(16), default="es", nullable=False)

    @property
    def sees_restricted(self) -> bool:
        """Whether this member reaches series an admin marked restricted.

        The coercion is the point. `role` is a **string column**, so a row read back from the
        database carries `"viewer_r"`, not `UserRole.VIEWER_R` — and `user.role.sees_restricted`
        would raise `AttributeError` in production while passing in every test, because the
        fixtures construct the enum in memory and the session does not expire it on commit.
        Asking the user, never the raw column, is what makes that impossible to get wrong.
        """
        try:
            return UserRole(self.role).sees_restricted
        except ValueError:
            # An unrecognised value in the column is not a licence. Fail closed.
            return False
    theme: Mapped[str] = mapped_column(String(8), default=Theme.AUTO, nullable=False)
    preferred_audio_lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    preferred_subtitle_lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    autoplay_next: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    avatar_color: Mapped[str] = mapped_column(String(9), default="#6c8cff", nullable=False)

    created_at: Mapped[datetime] = created_column()
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)

    credentials: Mapped[list[Credential]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    # Eager like `credentials`, and for the same reason: every guard that asks "would this
    # leave the account with no way in?" reads both, and a lazy load on an async session
    # raises rather than emitting a second query.
    oauth_identities: Mapped[list[OAuthIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    progress: Mapped[list[WatchProgress]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


class Credential(Base):
    """A registered WebAuthn passkey."""

    __tablename__ = "credentials"

    id: Mapped[str] = pk()
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    credential_id: Mapped[bytes] = mapped_column(
        LargeBinary, unique=True, index=True, nullable=False
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transports: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    aaguid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nickname: Mapped[str] = mapped_column(String(120), default="Passkey", nullable=False)
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = created_column()
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped[User] = relationship(back_populates="credentials")


class Invite(Base):
    """A single-use enrollment token. The raw token is mailed and never stored."""

    __tablename__ = "invites"

    id: Mapped[str] = pk()
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=UserRole.VIEWER, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    created_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    accepted_user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_column()

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id], lazy="selectin")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = pk()
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = created_column()
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[datetime] = created_column()
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions", lazy="selectin")


class MagicLink(Base):
    """Fallback sign-in for a device with no passkey yet."""

    __tablename__ = "magic_links"

    id: Mapped[str] = pk()
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_column()


class WebAuthnChallenge(Base):
    """Server-side, single-use challenge store.

    Keeping challenges in the DB rather than a cookie means an assertion cannot be replayed
    and the login flow works across the two-request begin/complete split without session state.
    """

    __tablename__ = "webauthn_challenges"

    id: Mapped[str] = pk()
    challenge: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    invite_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("invites.id", ondelete="CASCADE"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_column()

    __table_args__ = (Index("ix_challenge_kind_expires", "kind", "expires_at"),)


class OAuthIdentity(Base):
    """A federated account bound to a local one.

    The two unique constraints are the whole point of the table. `(provider, provider_sub)`
    stops one Google account from reaching two local accounts, and `(provider, user_id)` stops
    one local account from collecting several Google accounts — without the second, unlinking
    "the" Google identity would be ambiguous and the last-way-in guard would be unsound.

    `email` is what the provider asserted when the link was made. It is a record of that
    assertion, not an identifier: resolution keys off `provider_sub`, which Google promises is
    stable even when the user changes their address.
    """

    __tablename__ = "oauth_identities"

    id: Mapped[str] = pk()
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(
        String(32), default=OAuthProvider.GOOGLE, nullable=False
    )
    provider_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)

    created_at: Mapped[datetime] = created_column()
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped[User] = relationship(back_populates="oauth_identities")

    __table_args__ = (
        UniqueConstraint("provider", "provider_sub", name="uq_oauth_identity_provider_sub"),
        UniqueConstraint("provider", "user_id", name="uq_oauth_identity_provider_user"),
        Index("ix_oauth_identity_provider_sub", "provider", "provider_sub"),
    )


class OAuthState(Base):
    """Short-lived, single-use state for one authorization round trip.

    Same shape and the same reasoning as `WebAuthnChallenge`: the browser leaves for Google
    and comes back on a different request, so everything that must survive the round trip —
    the PKCE verifier, the nonce, which flow this is, where to land afterwards — is kept
    server-side and keyed by the opaque `state` the provider echoes back. Nothing sensitive
    rides in the URL, and the row is burned on first use so a replayed callback is refused.
    """

    __tablename__ = "oauth_states"

    id: Mapped[str] = pk()
    state: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: sha256 of the value in the browser's short-lived OAuth cookie, which is what makes this
    #: row redeemable *only* by the browser that started the round trip. Without it the state
    #: is a bearer token: an attacker who authorizes at Google themselves and then feeds the
    #: resulting `code`/`state` to someone else's browser as a plain link silently signs that
    #: person into the attacker's account (RFC 6819 §4.4.1.8). Nullable only so the column can
    #: be added to an existing table; a NULL is refused rather than treated as "any browser".
    browser_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Set for a link, absent for a login — a login has no account yet by definition.
    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    invite_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("invites.id", ondelete="CASCADE"), nullable=True
    )
    next_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_column()

    __table_args__ = (Index("ix_oauth_state_kind_expires", "kind", "expires_at"),)


__all__ = [
    "AuthSession",
    "ChallengeKind",
    "Credential",
    "Invite",
    "MagicLink",
    "OAuthIdentity",
    "OAuthProvider",
    "OAuthState",
    "OAuthStateKind",
    "User",
    "WebAuthnChallenge",
]
