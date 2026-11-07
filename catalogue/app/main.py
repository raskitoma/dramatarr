"""FastAPI application.

M0 ships liveness plus the auth boundary. The catalogue and sync endpoints
described in PLAN.md 2.9 land in M6, once there is data to serve.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from sqlalchemy import text

from app import __version__
from app.api.auth import require_api_key
from app.config import settings
from app.db import describe_database, engine, integrity_check, session_scope
from app.jobs.locks import release_all
from app.sources.registry import register_all
from app.jobs.daemon import SchedulerDaemon

# Without this the app's own loggers are silent under uvicorn, so the
# scheduler's decisions and every worker warning vanish.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Say which database this instance is actually on, every start.
    #
    # `settings.db_url` DEFAULTS to sqlite:///./data/dramadex.db, so losing
    # DRAMADEX_DB_URL -- a stack edit, a `docker run` without the env, a CLI
    # invocation outside the container -- does not fail. It silently opens a
    # SQLite file in the data volume instead, and if a pre-migration database is
    # still sitting there the app comes up on stale data with no error anywhere.
    # A line in the log is what makes that a thirty-second diagnosis.
    logging.getLogger(__name__).info("database: %s", describe_database())
    if settings.db_url.startswith("sqlite"):
        logging.getLogger(__name__).warning(
            "running on SQLite (%s). Expected for local development; in the "
            "container this means DRAMADEX_DB_URL was not set.",
            settings.sqlite_path,
        )

    verdict = integrity_check()
    if verdict != "ok":
        logging.getLogger(__name__).error(
            "DATABASE INTEGRITY CHECK FAILED: %s -- refusing to start the "
            "scheduler. Restore from backup, or rebuild with `dramadex reparse`.",
            verdict,
        )

    try:
        with session_scope() as session:
            freed = release_all(session)
        if freed:
            logging.getLogger(__name__).info(
                "released %s stale source lease(s) left by a previous run", freed
            )
    except Exception:  # noqa: BLE001 - never block startup on this
        logging.getLogger(__name__).warning("could not clear stale leases")

    if verdict == "ok":
        try:
            with session_scope() as session:
                registered = register_all(session)
            logging.getLogger(__name__).info(
                "sources registered: %s", ", ".join(registered)
            )
        except Exception:  # noqa: BLE001 - never block startup on this
            logging.getLogger(__name__).exception("could not register sources")

    daemon = None
    if settings.scheduler_enabled and verdict == "ok":
        daemon = SchedulerDaemon(settings.scheduler_interval_seconds)
        daemon.start()
    try:
        yield
    finally:
        if daemon is not None:
            daemon.stop()


app = FastAPI(
    title="Dramadex",
    version=__version__,
    description="A self-hosted indexer for vertical short-drama metadata.",
    lifespan=lifespan,
)

from fastapi import Request  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from app.api import admin, routes  # noqa: E402

from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

app.include_router(routes.router)
app.include_router(admin.public)
app.include_router(admin.router)


@app.exception_handler(StarletteHTTPException)
async def _unauthenticated_console(request: Request, exc: StarletteHTTPException):
    """Send a browser hitting the console without a cookie to the login page.

    API clients still get a plain 401 -- only /admin redirects, and only for a
    request that is actually a page navigation.
    """
    from fastapi.responses import JSONResponse

    if (
        exc.status_code == 401
        and request.url.path.startswith("/admin")
        and "text/html" in request.headers.get("accept", "")
    ):
        return RedirectResponse("/admin/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# HEAD as well as GET: uptime monitors default to HEAD surprisingly often, and
# a permanent 405 on the health endpoint reads as an outage.
@app.api_route("/health", methods=["GET", "HEAD"], tags=["ops"])
def health() -> dict:
    """Liveness. Deliberately unauthenticated and dependency-light."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    integrity = integrity_check() if db_ok else "unreadable"
    healthy = db_ok and integrity == "ok"
    return {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "database": "ok" if db_ok else "unreachable",
        "integrity": integrity,
        "run_date": settings.today().isoformat(),
    }


@app.get("/whoami", tags=["ops"], dependencies=[Depends(require_api_key)])
def whoami() -> dict:
    """Smallest possible authenticated endpoint: proves a key works."""
    return {"authenticated": True, "timezone": settings.tz}
