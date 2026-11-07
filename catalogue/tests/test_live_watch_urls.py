"""Live check: every generated watch URL actually resolves.

Marked `live` -- excluded from the default run, because it hits the real sites.
Run with:  uv run pytest -m live

This is the canary for template drift. The watch URL is built from the id
rather than the stored slug precisely so it survives a retitle, and this is
what proves that assumption still holds.
"""

from __future__ import annotations

import time

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.models import Source, TitleSource
from app.sources.base import watch_url

pytestmark = pytest.mark.live


def test_every_watch_url_resolves() -> None:
    with session_scope() as session:
        rows = [
            (s.key, s.watch_url_template, ts.external_id)
            for ts, s in session.execute(
                select(TitleSource, Source).join(Source, TitleSource.source_id == Source.id)
            )
        ]

    if not rows:
        pytest.skip("no titles ingested yet")

    failures: list[tuple[str, int]] = []
    with httpx.Client(
        headers={"User-Agent": settings.user_agent}, timeout=30, follow_redirects=True
    ) as client:
        for _key, template, external_id in rows:
            url = watch_url(template, external_id)
            resp = client.get(url)
            if resp.status_code != 200:
                failures.append((url, resp.status_code))
            time.sleep(0.3)

    assert not failures, f"{len(failures)}/{len(rows)} watch URLs failed: {failures[:5]}"
