"""Startup and shutdown.

Order matters: directories, then schema, then the FTS index, then bootstrap, and only then
the background tasks — the worker claims jobs the moment it starts and must never see a
half-migrated database.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config
from fastapi import FastAPI

from alembic import command
from app.bootstrap import bootstrap
from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.services.images import close_image_client
from app.services.search.index import ensure_index
from app.services.tmdb import close_tmdb_client
from app.workers.queue import start_worker, stop_worker
from app.workers.scheduler import start_scheduler, stop_scheduler

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_DIR = BACKEND_DIR / "alembic"


def alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI)) if ALEMBIC_INI.is_file() else Config()
    # Absolute paths and an explicit URL: the process may not be running from the backend dir.
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    # env.py's fileConfig() would tear down the handlers uvicorn just installed.
    config.attributes["configure_logger"] = False
    return config


async def run_migrations() -> None:
    """Alembic is synchronous and its env.py calls `asyncio.run`, which cannot nest inside a
    running loop — hence the thread."""
    await asyncio.to_thread(command.upgrade, alembic_config(), "head")
    log.info("database schema is at head")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()

    await run_migrations()

    async with get_sessionmaker()() as session:
        await ensure_index(session)
        await bootstrap(session)

    await start_worker(app)
    await start_scheduler(app)
    log.info(
        "dramatarr ready (environment=%s, library=%s)", settings.environment, settings.library_root
    )

    try:
        yield
    finally:
        await stop_scheduler(app)
        await stop_worker(app)
        # The three outbound pools, closed after the worker that uses them has stopped.
        await close_tmdb_client()
        await close_image_client()
        await dispose_engine()
        log.info("dramatarr stopped")


__all__ = ["alembic_config", "lifespan", "run_migrations"]
