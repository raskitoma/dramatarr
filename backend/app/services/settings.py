"""Runtime-mutable settings, backed by the `settings` table with a process-local cache.

Boot config (paths, secret key, RP id) lives in `app.config`. Everything an admin can change
from the UI lives here, so changing the TMDB key or SMTP host never requires a restart.
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
        "tmdb.api_key",
        "smtp.password",
        "oauth.google.client_secret",
        "images.search.api_key",
        "omdb.api_key",
    }
)

DEFAULTS: dict[str, Any] = {
    "tmdb.api_key": "",
    "tmdb.language": "es-ES",
    "tmdb.enabled": True,
    #: OMDb, a public API over IMDb's data. The **fallback** for an IMDb id TMDB does not
    #: carry — which for this library's vertical short drama is most of them. Empty disables
    #: the feature entirely; nothing else changes.
    "omdb.api_key": "",
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
    #: The household's own list of vertical-drama platforms, as
    #: `[{"name": "DramaBox", "domain": "dramabox.com"}]`. Editable in the admin UI because it
    #: changes faster than this code does. `domain` may be empty — it is only used to narrow a
    #: web search, and a guessed one would return nothing silently.
    #:
    #: The default **is the seed**, not `[]`. With `[]` a fresh install showed an empty editor
    #: while the search quietly used the seed behind it, and the two disagreeing is worse than
    #: either answer on its own. It also makes "the admin deleted every row" mean exactly that.
    "library.providers": [
        {"name": provider.name, "domain": provider.domain} for provider in _SEED_PROVIDERS
    ],
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
    # Web image search for the artwork picker. Off by default: TMDB's own gallery is the
    # primary source and needs no configuration, so this is the fallback for the show TMDB has
    # one bad poster for, not a requirement. `""` means disabled.
    #
    # Two providers. `"searxng"` is the recommended one — the household's own SearXNG, no
    # third-party key, no quota, and the query never leaves the network; it needs
    # `base_url` and nothing else. `"google_cse"` needs `api_key` + `cse_id`.
    #
    # The keys below `provider` are deliberately provider-neutral, because "which engines,
    # how safe, what language, how long to wait" are questions any search backend can answer;
    # only `cse_id` is specific to Google.
    "images.search.provider": "",
    #: Optional for SearXNG (some instances sit behind a proxy that wants a token), required
    #: for Google. Secret either way — see SECRET_KEYS.
    "images.search.api_key": "",
    #: SearXNG only. The instance's origin, e.g. `http://10.0.50.30:8080`. A private address
    #: here is normal and is *not* filtered by the artwork download guard — see the SSRF note
    #: in `app.services.images`.
    "images.search.base_url": "",
    #: SearXNG only. Comma-separated engine names, e.g. `bing images,duckduckgo images`.
    #: Empty means "whatever the instance has enabled for the images category".
    "images.search.engines": "",
    #: SearXNG's own scale: 0 off, 1 moderate, 2 strict. The settings screen writes the *name*
    #: (`off` / `moderate` / `strict`) because a row that reads `moderate` says what it does;
    #: `images.parse_safe_search` accepts either spelling, and both end up as the number
    #: SearXNG's API takes.
    "images.search.safe_search": 1,
    #: BCP-47 or a SearXNG language tag; `""` uses the instance's default.
    "images.search.language": "",
    #: Seconds. A SearXNG query fans out to several engines, so it is slower than a plain API.
    "images.search.timeout": 15,
    #: Google Custom Search only: the search-engine id.
    "images.search.cse_id": "",
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
