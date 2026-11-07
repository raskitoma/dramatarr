"""Source registration.

A fresh deployment used to be inert: `source` rows were created only as a side
effect of running a sync, so on a clean install the API answered "unknown
source" and the scheduler found nothing due -- silently, with no error. Only a
deploy onto an empty volume showed it, because any machine where the CLI had
ever run already had the rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.jobs import daemon, scheduler
from app.models import Base, JobType, Source
from app.sources.registry import ADAPTERS, PLATFORM_KEYS, load, register_all


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/reg.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


def test_a_fresh_database_has_no_sources(session) -> None:
    assert session.scalars(select(Source)).all() == []


def test_registration_makes_a_fresh_instance_usable(session) -> None:
    """The regression. Without this, a clean deploy can never sync anything."""
    registered = register_all(session)

    assert set(registered) == set(ADAPTERS)
    keys = {s.key for s in session.scalars(select(Source))}
    assert keys == set(ADAPTERS)


def test_the_api_can_trigger_a_sync_right_after_registration(session) -> None:
    register_all(session)
    run = scheduler.create_run(session, "dramabox", JobType.FULL_CATALOG, manual=True)
    assert run.id is not None


def test_triggering_before_registration_is_what_used_to_fail(session) -> None:
    with pytest.raises(scheduler.JobRejected, match="unknown source"):
        scheduler.create_run(session, "dramabox", JobType.FULL_CATALOG, manual=True)


def test_the_scheduler_finds_work_after_registration(session, monkeypatch) -> None:
    import contextlib

    register_all(session)

    @contextlib.contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(daemon, "session_scope", _scope)
    monkeypatch.setattr(daemon.settings, "sync_hour", None)
    monkeypatch.setattr(daemon.scheduler, "spawn", lambda run_id: None)

    assert daemon.tick()["started"], "a registered instance must have work to do"


def test_registration_is_idempotent(session) -> None:
    register_all(session)
    register_all(session)
    assert len(session.scalars(select(Source)).all()) == len(ADAPTERS)


def test_imdb_is_registered_as_an_enricher_with_no_watch_url(session) -> None:
    register_all(session)
    imdb = session.scalar(select(Source).where(Source.key == "imdb"))
    assert imdb.watch_url_template == ""
    assert "imdb" not in PLATFORM_KEYS


def test_every_adapter_exposes_the_attributes_registration_needs() -> None:
    """One list, so a new adapter cannot be half-added."""
    for key in ADAPTERS:
        module = load(key)
        for attr in ("KEY", "STUDIO_NAME", "BASE_URL", "WATCH_URL_TEMPLATE"):
            assert hasattr(module, attr), f"{key} is missing {attr}"
        assert module.KEY == key


def test_every_registered_adapter_can_be_dispatched_by_the_runner() -> None:
    """The runner builds a Fetcher for every job, including ones that never use
    it. IMDb declares no crawl RATE because it downloads bulk TSVs directly, and
    an unguarded `module.RATE` killed imdb_refresh on the first real deploy --
    invisible locally, where that job had only ever been run from the CLI.
    """
    from app.fetch.ratelimit import RateLimiterRegistry
    from app.fetch.types import RateSpec

    for key in ADAPTERS:
        module = load(key)
        rate = getattr(module, "RATE", RateSpec())
        assert RateLimiterRegistry(rate) is not None


def test_platform_adapters_do_declare_a_crawl_rate() -> None:
    """Enrichers may omit it; anything that actually crawls must not."""
    for key in PLATFORM_KEYS:
        assert hasattr(load(key), "RATE"), f"{key} crawls and must declare RATE"
