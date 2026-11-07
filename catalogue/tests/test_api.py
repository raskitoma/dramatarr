from __future__ import annotations
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    # A clean monorepo has no development database. Health must test an isolated engine,
    # rather than relying on the original developer's data/ directory being populated.
    from sqlalchemy import create_engine
    import app.db as db_module
    import app.main as main_module

    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}")
    monkeypatch.setattr(settings, "api_key", "test-key")
    monkeypatch.setattr(settings, "db_url", str(engine.url))
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(main_module, "engine", engine)
    yield TestClient(app)
    engine.dispose()


def test_health_needs_no_key(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_authenticated_endpoint_rejects_a_missing_key(client) -> None:
    assert client.get("/whoami").status_code == 401


def test_authenticated_endpoint_rejects_a_wrong_key(client) -> None:
    assert client.get("/whoami", headers={"X-API-Key": "nope"}).status_code == 401


def test_authenticated_endpoint_accepts_the_right_key(client) -> None:
    resp = client.get("/whoami", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True


def test_unconfigured_key_fails_closed(client, monkeypatch) -> None:
    """An empty DRAMADEX_API_KEY must not silently disable auth."""
    monkeypatch.setattr(settings, "api_key", "")
    assert client.get("/whoami", headers={"X-API-Key": ""}).status_code == 500


def test_health_answers_head_as_well_as_get(client) -> None:
    """Uptime monitors commonly probe with HEAD. A 405 there looks like an
    outage, which is the opposite of what a health endpoint is for."""
    assert client.get("/health").status_code == 200
    assert client.head("/health").status_code == 200


# --- old failures must not read as current ones -------------------------------


def test_a_failure_a_later_success_supersedes_is_marked_stale(tmp_path) -> None:
    """A failed run stays in sync_run forever, because that table is history.

    Three of the failures on the live box were for bugs fixed releases earlier --
    a SQLite-only backup, a "database is locked" from before the Postgres move,
    and a crawl that broke on code fixed minutes later. Rendered identically to a
    fresh failure, they turn the runs list into noise, which is how a real failure
    gets missed.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.api.admin import superseded_failures
    from app.models import Base, RunState, Source, SyncRun

    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="dramabox", base_url="https://a.test"))
        s.add(Source(id=2, key="reelshort", base_url="https://b.test"))
        rows = [
            # fixed: a later run of the same job succeeded
            SyncRun(id=1, source_id=1, job_type="backup", run_date=date(2026, 8, 1),
                    state=RunState.FAILED),
            SyncRun(id=2, source_id=1, job_type="backup", run_date=date(2026, 8, 2),
                    state=RunState.SUCCEEDED),
            # still broken: nothing has succeeded since
            SyncRun(id=3, source_id=1, job_type="incremental", run_date=date(2026, 8, 2),
                    state=RunState.FAILED),
            # a success for a DIFFERENT job must not clear it
            SyncRun(id=4, source_id=1, job_type="poster_fetch", run_date=date(2026, 8, 3),
                    state=RunState.SUCCEEDED),
            # a success on a different SOURCE must not clear it either
            SyncRun(id=5, source_id=2, job_type="incremental", run_date=date(2026, 8, 3),
                    state=RunState.SUCCEEDED),
        ]
        s.add_all(rows)
        s.commit()

        stale = superseded_failures(s, rows)

        assert stale == {1}, "only the failure a later same-job success followed"


def test_an_earlier_success_does_not_excuse_a_later_failure(tmp_path) -> None:
    """Order matters: succeeding yesterday says nothing about today's break."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.api.admin import superseded_failures
    from app.models import Base, RunState, Source, SyncRun

    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="dramabox", base_url="https://a.test"))
        rows = [
            SyncRun(id=1, source_id=1, job_type="incremental", run_date=date(2026, 8, 1),
                    state=RunState.SUCCEEDED),
            SyncRun(id=2, source_id=1, job_type="incremental", run_date=date(2026, 8, 2),
                    state=RunState.FAILED),
        ]
        s.add_all(rows)
        s.commit()

        assert superseded_failures(s, rows) == set()
