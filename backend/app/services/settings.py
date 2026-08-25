"""Runtime-mutable settings, backed by the `settings` table with a process-local cache.

Boot config (paths, secret key, RP id) lives in `app.config`. Everything an admin can change
from the UI lives here, so changing the indexer's address or the SMTP host never requires a
restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Setting

# The seed for `library.providers`. `vocab` imports nothing from here, so there is no cycle.
from app.services.vocab import DEFAULT_PROVIDERS as _SEED_PROVIDERS
from app.utils.time import utcnow

REDACTED = "__SET__"

#: Keys whose value is never returned by the API. A client sends back `REDACTED` to mean
#: "leave this alone", which is what lets the settings form round-trip without leaking.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "smtp.password",
        "oauth.google.client_secret",
        "dramadex.api_key",
    }
)

DEFAULTS: dict[str, Any] = {
    #: DramaDex — the household's own indexer of DramaBox, ReelShort, GoodShort and NetShort,
    #: and the only source that catalogues this library's genre. TMDB, OMDb and the SearXNG
    #: discovery search all predated it, all existed because it did not, and all are gone.
    #: Empty means "not connected yet": every caller treats that as a feature nobody has
    #: switched on, never as an error.
    "dramadex.base_url": "",
    "dramadex.api_key": "",
    "smtp.host": "",
    "smtp.port": 587,
    "smtp.username": "",
    "smtp.password": "",
    "smtp.from_email": "",
    "smtp.from_name": "dramatarr",
    "smtp.use_tls": True,
    "smtp.use_ssl": False,
    "site.name": "dramatarr",
    "site.default_locale": "es",
    "library.scan_cron": "0 4 * * *",
    "library.source_base_url": "",
    "queue.paused": False,
    "auth.magic_link_enabled": True,
    "auth.invite_ttl_hours": 168,
    # Sign in with Google. Off until an admin pastes a client id, so a fresh install never
    # advertises a button that cannot work.
    "oauth.google.enabled": False,
    "oauth.google.client_id": "",
    "oauth.google.client_secret": "",
    #: Whether a Google sign-in may *accept* an invitation addressed to the same verified
    #: address. It can never create an account any other way — dramatarr stays invite-only.
    "oauth.google.allow_invites": True,
}


@dataclass(slots=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    from_email: str
    from_name: str
    use_tls: bool
    use_ssl: bool

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_email)


class SettingsService:
    """Reads go through a shared cache; writes invalidate it.

    The cache is process-local, which is safe because dramatarr runs a single API process
    (see the topology section of the architecture doc).
    """

    _cache: ClassVar[dict[str, Any] | None] = None
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Cache ----------------------------------------------------------------------------

    @classmethod
    def invalidate(cls) -> None:
        cls._cache = None

    async def _load(self) -> dict[str, Any]:
        if SettingsService._cache is not None:
            return SettingsService._cache
        async with SettingsService._lock:
            if SettingsService._cache is not None:
                return SettingsService._cache
            rows = (await self.session.execute(select(Setting))).scalars().all()
            merged = dict(DEFAULTS)
            for row in rows:
                # Values are stored wrapped so that JSON columns can hold scalars portably.
                merged[row.key] = row.value.get("v") if isinstance(row.value, dict) else row.value
            SettingsService._cache = merged
            return merged

    # --- Reads ----------------------------------------------------------------------------

    async def get(self, key: str, default: Any = None) -> Any:
        data = await self._load()
        return data.get(key, DEFAULTS.get(key, default))

    async def get_str(self, key: str, default: str = "") -> str:
        value = await self.get(key, default)
        return str(value) if value is not None else default

    async def get_int(self, key: str, default: int = 0) -> int:
        value = await self.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def get_bool(self, key: str, default: bool = False) -> bool:
        value = await self.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    async def all(self, *, redact: bool = True) -> dict[str, Any]:
        data = dict(await self._load())
        if redact:
            for key in SECRET_KEYS:
                data[key] = REDACTED if data.get(key) else ""
        return data

    async def smtp(self) -> SmtpConfig:
        return SmtpConfig(
            host=await self.get_str("smtp.host"),
            port=await self.get_int("smtp.port", 587),
            username=await self.get_str("smtp.username"),
            password=await self.get_str("smtp.password"),
            from_email=await self.get_str("smtp.from_email"),
            from_name=await self.get_str("smtp.from_name", "dramatarr"),
            use_tls=await self.get_bool("smtp.use_tls", True),
            use_ssl=await self.get_bool("smtp.use_ssl", False),
        )

    # --- Writes ---------------------------------------------------------------------------

    async def set(self, key: str, value: Any, *, user_id: str | None = None) -> None:
        row = await self.session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value={"v": value}, updated_by_id=user_id)
            self.session.add(row)
        else:
            row.value = {"v": value}
            row.updated_at = utcnow()
            row.updated_by_id = user_id
        await self.session.flush()
        SettingsService.invalidate()

    async def update_many(self, values: dict[str, Any], *, user_id: str | None = None) -> None:
        """Partial update. A secret arriving as `REDACTED` keeps its stored value, and unknown
        keys are ignored so a stale client cannot inject arbitrary configuration."""
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            if key in SECRET_KEYS and value == REDACTED:
                continue
            await self.set(key, value, user_id=user_id)


__all__ = ["DEFAULTS", "REDACTED", "SECRET_KEYS", "SettingsService", "SmtpConfig"]
