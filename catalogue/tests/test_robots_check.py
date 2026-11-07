"""robots.txt compliance.

The job exists so that a source changing its mind about being crawled is
discovered by us, not by being blocked. The behaviour that matters is that a
tightened rule STOPS the crawler rather than merely logging a warning nobody
reads.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.jobs import robots_check
from app.jobs.locks import ensure_state
from app.models import Base, Source

PERMISSIVE = "User-agent: *\nAllow: /\n"
TIGHTENED = "User-agent: *\nDisallow: /browse\nDisallow: /drama\n"
DELAYED = "User-agent: *\nAllow: /\nCrawl-delay: 10\n"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/r.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="dramabox", base_url="https://www.dramabox.com"))
        s.commit()
        yield s


def serve(monkeypatch, body: str | None, status: int = 200) -> None:
    monkeypatch.setattr(
        robots_check, "fetch_robots", lambda *a, **k: (body, status)
    )


def test_a_permissive_robots_leaves_the_source_enabled(session, monkeypatch) -> None:
    serve(monkeypatch, PERMISSIVE)
    report = robots_check.check_source(session, session.get(Source, 1))

    assert report["allowed"] is True
    assert report["action"] == "none"
    assert session.get(Source, 1).enabled is True


def test_the_hash_is_recorded_so_a_change_can_be_noticed(session, monkeypatch) -> None:
    serve(monkeypatch, PERMISSIVE)
    robots_check.check_source(session, session.get(Source, 1))

    state = ensure_state(session, 1)
    assert state.robots_sha256
    assert state.robots_checked_at is not None


def test_an_unchanged_robots_is_not_reported_as_changed(session, monkeypatch) -> None:
    serve(monkeypatch, PERMISSIVE)
    robots_check.check_source(session, session.get(Source, 1))
    second = robots_check.check_source(session, session.get(Source, 1))

    assert second["changed"] is False


def test_a_changed_but_still_permissive_robots_is_flagged_not_halted(
    session, monkeypatch
) -> None:
    """A site reorganising its rules is not a reason to stop crawling."""
    serve(monkeypatch, PERMISSIVE)
    robots_check.check_source(session, session.get(Source, 1))

    serve(monkeypatch, DELAYED)
    report = robots_check.check_source(session, session.get(Source, 1))

    assert report["changed"] is True
    assert report["allowed"] is True
    assert report["crawl_delay"] == 10.0
    assert session.get(Source, 1).enabled is True


def test_a_tightened_robots_disables_the_source(session, monkeypatch) -> None:
    """The whole point. Scheduled runs skip disabled sources, so crawling stops
    at once instead of continuing until someone reads a log line."""
    serve(monkeypatch, TIGHTENED)
    report = robots_check.check_source(session, session.get(Source, 1))

    assert report["allowed"] is False
    assert "/browse" in report["disallowed_paths"]
    assert "DISABLED" in report["action"]
    assert session.get(Source, 1).enabled is False


def test_an_unreachable_robots_changes_nothing(session, monkeypatch) -> None:
    """A network blip is not evidence of a policy change, and disabling a
    source over one would be its own kind of outage."""
    serve(monkeypatch, None, status=0)
    report = robots_check.check_source(session, session.get(Source, 1))

    assert "skipped" in report["action"]
    assert session.get(Source, 1).enabled is True


def test_a_404_robots_changes_nothing(session, monkeypatch) -> None:
    serve(monkeypatch, None, status=404)
    report = robots_check.check_source(session, session.get(Source, 1))

    assert "skipped" in report["action"]
    assert session.get(Source, 1).enabled is True


def test_the_run_summarises_every_source(session, monkeypatch) -> None:
    serve(monkeypatch, TIGHTENED)
    stats = robots_check.run_robots_check(session)

    assert stats["checked"] == 1
    assert stats["disabled"] == ["dramabox"]


def test_adapters_declare_the_paths_they_actually_crawl() -> None:
    """The check is only meaningful if it asks about real paths."""
    from app.sources import dramabox, reelshort

    for module in (dramabox, reelshort):
        assert module.CRAWL_PATHS
        assert all(p.startswith("/") for p in module.CRAWL_PATHS)


def test_dramabox_honeypot_is_not_among_the_paths_we_check() -> None:
    """We must never request it, so it must not appear in a compliance probe
    either -- the honeypot exists to fingerprint crawlers that ignore robots."""
    from app.sources import dramabox

    for forbidden in dramabox.FORBIDDEN_PATHS:
        assert forbidden not in dramabox.CRAWL_PATHS
