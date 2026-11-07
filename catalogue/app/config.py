"""Runtime configuration, read from the environment."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": .env also carries compose-level variables (DRAMADEX_PORT)
    # that are not application settings. Rejecting them would make the app
    # refuse to start over a value it does not need.
    model_config = SettingsConfigDict(
        env_prefix="DRAMADEX_", env_file=".env", extra="ignore"
    )

    db_url: str = "sqlite:///./data/dramadex.db"
    data_dir: Path = Path("./data")
    api_key: str = ""
    tz: str = "UTC"

    # Identify ourselves honestly: an operator who objects should be able to
    # reach us rather than having to block us.
    user_agent: str = "DramadexBot/0.1 (self-hosted metadata indexer)"

    # Cap on items ingested by a full_catalog run. 0 means uncapped.
    max_items: int = 50

    # Raw response archive: how many historical payloads to keep per URL.
    archive_keep_versions: int = 1

    # Run the in-process scheduler. Turn it off to drive syncs entirely by hand.
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 300
    # Earliest local hour a scheduled sync may start. None means "any time".
    sync_hour: int | None = 3

    # How many verified backups to keep.
    backup_keep: int = 7

    # Whether the `dedup` job MERGES or only reports. Off by default and meant to
    # stay off until a dry run has been read: a merge rewrites eleven tables and
    # retires ids the downstream app already stores, which is not something a
    # scheduled run should start doing because a container restarted.
    dedup_apply: bool = False

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def sqlite_path(self) -> Path | None:
        """Filesystem path of the SQLite database, if that is the engine."""
        if not self.db_url.startswith("sqlite"):
            return None
        return Path(self.db_url.split("sqlite:///", 1)[-1])

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "images"

    def today(self) -> date:
        """The current run_date, in the configured timezone.

        Scheduling is capped at one run per source per job type per day, so the
        day boundary needs to be explicit rather than whatever the host thinks.
        """
        return datetime.now(ZoneInfo(self.tz)).date()


settings = Settings()
