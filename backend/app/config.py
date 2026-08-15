"""Boot-time configuration.

Only things needed *before* the database is reachable live here. Everything an admin can
change at runtime (TMDB key, SMTP, scan schedule, source base URL) lives in the `settings`
table and is reached through `app.services.settings.SettingsService`.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DRAMATARR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core -----------------------------------------------------------------------------
    secret_key: str = Field(default="")
    environment: str = "production"
    log_level: str = "INFO"

    # --- Storage --------------------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:////data/dramatarr.db"
    library_root: Path = Path("/library")
    data_dir: Path = Path("/data")

    # --- Public identity ------------------------------------------------------------------
    public_url: str = "http://localhost:8080"

    # --- WebAuthn -------------------------------------------------------------------------
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "dramatarr"
    webauthn_origin: str = "http://localhost:8080"

    # --- Sessions -------------------------------------------------------------------------
    session_ttl_days: int = 30
    session_cookie_name: str = "dramatarr_session"
    cookie_secure: bool | None = None  # None → inferred from public_url scheme

    # --- Bootstrap ------------------------------------------------------------------------
    default_locale: str = "es"
    bootstrap_admin_email: str | None = None

    # --- CORS (dev only; in prod nginx proxies same-origin) --------------------------------
    cors_origins: str = ""

    @field_validator("library_root", "data_dir", mode="before")
    @classmethod
    def _as_path(cls, value: object) -> object:
        return Path(str(value)) if value is not None else value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def webauthn_origins(self) -> list[str]:
        return [o.strip() for o in self.webauthn_origin.split(",") if o.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def use_secure_cookie(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.public_url.lower().startswith("https://")

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def subs_dir(self) -> Path:
        return self.data_dir / "subs"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    def ensure_dirs(self) -> None:
        """Create the writable directories. `library_root` is never created — it is a mount."""
        for path in (self.data_dir, self.images_dir, self.thumbs_dir, self.subs_dir, self.staging_dir):
            path.mkdir(parents=True, exist_ok=True)

    def resolve_secret_key(self) -> str:
        """Refuse to run in production without an explicit key; autogenerate in dev.

        An ephemeral key in production would silently invalidate every outstanding invite
        and magic link on each restart, which is far worse than failing loudly at boot.
        """
        if self.secret_key:
            return self.secret_key
        if self.is_production:
            raise RuntimeError(
                "DRAMATARR_SECRET_KEY must be set in production. "
                "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        self.secret_key = secrets.token_urlsafe(48)
        return self.secret_key


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.resolve_secret_key()
    return settings
