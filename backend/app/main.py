"""FastAPI application factory."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import select

from app.api import admin, auth, library, me, requests, stream, watch
from app.api.deps import SessionDep
from app.config import get_settings
from app.errors import install_error_handlers
from app.lifespan import lifespan
from app.models import User
from app.schemas.common import APIModel
from app.services.oauth import google_config
from app.services.settings import SettingsService

API_PREFIX = "/api"

try:
    VERSION = version("dramatarr")
except PackageNotFoundError:  # running from a source checkout that was never installed
    VERSION = "0.0.0+dev"

#: (router, sub-prefix, tag) — the paths in §5 of the architecture contract.
ROUTERS: tuple[tuple[APIRouter, str, str], ...] = (
    (auth.router, "/auth", "auth"),
    (me.router, "/me", "me"),
    (library.router, "/library", "library"),
    (requests.router, "/requests", "requests"),
    (stream.router, "/stream", "stream"),
    (watch.router, "/watch", "watch"),
    (admin.router, "/admin", "admin"),
)


class HealthOut(APIModel):
    status: str
    version: str


class PublicConfigOut(APIModel):
    """What the login page needs before it can authenticate anyone."""

    site_name: str
    default_locale: str
    magic_link_enabled: bool
    #: Whether the login screen should offer the Google button. False unless the feature is
    #: switched on *and* a client id is configured — an install that only flipped the switch
    #: would otherwise advertise a button that dead-ends at Google.
    google_enabled: bool
    has_users: bool


meta_router = APIRouter(tags=["meta"])


@meta_router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    """Deliberately free of database access — it backs the container healthcheck."""
    return HealthOut(status="ok", version=VERSION)


@meta_router.get("/config", response_model=PublicConfigOut)
async def public_config(session: SessionDep) -> PublicConfigOut:
    settings = get_settings()
    service = SettingsService(session)
    has_users = await session.scalar(select(User.id).limit(1)) is not None
    google = await google_config(session)
    return PublicConfigOut(
        site_name=await service.get_str("site.name", "dramatarr"),
        default_locale=await service.get_str("site.default_locale", settings.default_locale),
        magic_link_enabled=await service.get_bool("auth.magic_link_enabled", True),
        google_enabled=google.usable,
        has_users=has_users,
    )


def configure_logging(level: str) -> None:
    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    # `basicConfig` is a no-op once the root logger has a handler — which is the case under
    # gunicorn and under any container that configures logging before importing the app.
    # Without this, DRAMATARR_LOG_LEVEL would be silently ignored in exactly those setups.
    logging.getLogger().setLevel(resolved)

    # These libraries log every statement and every request they make, *with the parameters
    # bound into them*, at DEBUG. Raising DRAMATARR_LOG_LEVEL to debug to follow the app's own
    # code would otherwise write the Google client secret, session and invite token hashes and
    # live PKCE verifiers into the log file as a side effect. Floored at INFO, never below.
    for noisy in ("aiosqlite", "httpcore", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(max(resolved, logging.INFO))
    # SQLAlchemy is the same hazard one level higher: it emits statements *and their bound
    # parameters* at INFO, not DEBUG, and it decides whether to do so by asking this logger.
    # It defaults itself to WARNING at import; the floor is WARNING so that raising the app's
    # level cannot switch that on, and never lowers a level an operator chose deliberately.
    logging.getLogger("sqlalchemy").setLevel(max(resolved, logging.WARNING))


def _mount(app: FastAPI, router: APIRouter, prefix: str, tag: str) -> None:
    """Supply whatever the router left out.

    Routers set their own prefix and tag today; adding them again here would produce
    `/api/auth/auth`, and omitting them would break a router that does not set them.
    """
    sub = "" if router.prefix == prefix else prefix
    app.include_router(
        router,
        prefix=f"{API_PREFIX}{sub}",
        tags=None if tag in router.tags else [tag],
    )


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="dramatarr",
        version=VERSION,
        lifespan=lifespan,
        # nginx only proxies /api, so the schema and docs have to live under it to be reachable.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        # Same reason: the default lives at `/docs/oauth2-redirect`, outside the only prefix
        # nginx proxies, so Swagger would point the browser at the SPA.
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect",
        redoc_url=None,
    )

    install_error_handlers(app)

    # Added last means outermost, so CORS headers are present on error responses too.
    # GZip skips 206 and video/*, which keeps range streaming and SSE intact.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    origins = settings.cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(meta_router, prefix=API_PREFIX)
    for router, prefix, tag in ROUTERS:
        _mount(app, router, prefix, tag)

    return app


app = create_app()


__all__ = ["VERSION", "app", "create_app"]
