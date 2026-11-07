"""robots.txt compliance check.

One request per source per day. It answers a question nothing else asks: has
this site changed its mind about being crawled?

Without it, a source tightening its rules is discovered by being blocked --
which is both bad citizenship and a worse failure mode, because a block looks
like a bug. Here, the crawler stops on its own and says why.

A source whose rules now forbid our paths is DISABLED, not merely flagged.
Scheduled runs skip disabled sources, so the crawling stops immediately; a
manual trigger still works, so an operator who has resolved things with the
site can proceed deliberately.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.jobs import locks
from app.models import Source

log = logging.getLogger(__name__)

from app.sources.registry import ADAPTERS as SOURCE_MODULES


def robots_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/robots.txt"


def fetch_robots(url: str, user_agent: str, timeout: float = 20.0) -> tuple[str | None, int]:
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        log.warning("robots fetch failed for %s: %s", url, exc)
        return None, 0
    if response.status_code != 200:
        return None, response.status_code
    return response.text, response.status_code


def check_source(session: Session, source: Source) -> dict:
    """Check one source. Returns a report; disables the source if now forbidden."""
    import importlib

    from protego import Protego

    report: dict = {
        "source": source.key,
        "status": None,
        "changed": False,
        "allowed": True,
        "disallowed_paths": [],
        "crawl_delay": None,
        "action": "none",
    }

    text, status = fetch_robots(robots_url(source.base_url), settings.user_agent)
    report["status"] = status
    if text is None:
        # An unreachable robots.txt is not permission to crawl freely, but it is
        # also not evidence of a policy change. Leave the source alone and say so.
        report["action"] = "skipped (robots.txt unreachable)"
        return report

    digest = hashlib.sha256(text.encode()).hexdigest()
    state = locks.ensure_state(session, source.id)
    report["changed"] = bool(state.robots_sha256 and state.robots_sha256 != digest)
    report["first_seen"] = state.robots_sha256 is None

    parser = Protego.parse(text)
    module_name = SOURCE_MODULES.get(source.key)
    paths = []
    if module_name:
        module = importlib.import_module(module_name)
        paths = list(getattr(module, "CRAWL_PATHS", ()))

    for path in paths:
        url = source.base_url.rstrip("/") + path
        if not parser.can_fetch(url, settings.user_agent):
            report["disallowed_paths"].append(path)

    delay = parser.crawl_delay(settings.user_agent)
    if delay is not None:
        report["crawl_delay"] = float(delay)

    report["allowed"] = not report["disallowed_paths"]

    if not report["allowed"]:
        # Stop crawling now and let a human decide what to do about it.
        if source.enabled:
            source.enabled = False
            report["action"] = "SOURCE DISABLED: robots.txt now forbids paths we crawl"
            log.error(
                "robots.txt for %s now disallows %s -- source disabled",
                source.key,
                report["disallowed_paths"],
            )
        else:
            report["action"] = "still forbidden (source already disabled)"
    elif report["changed"]:
        report["action"] = "robots.txt changed but our paths are still allowed"
        log.warning("robots.txt for %s changed; paths still allowed", source.key)

    state.robots_sha256 = digest
    state.robots_checked_at = datetime.now(UTC)
    return report


def run_robots_check(session: Session, ctx=None) -> dict:
    """Check every source with a base URL. One request each."""
    sources = [
        s for s in session.scalars(select(Source)) if s.base_url.startswith("http")
    ]
    reports = []
    for index, source in enumerate(sources, start=1):
        if ctx is not None:
            if ctx.cancelled():
                break
            ctx.progress(index, len(sources), "robots")
        reports.append(check_source(session, source))
    session.commit()

    return {
        "checked": len(reports),
        "changed": [r["source"] for r in reports if r["changed"]],
        "disabled": [r["source"] for r in reports if "DISABLED" in r["action"]],
        "reports": reports,
    }
