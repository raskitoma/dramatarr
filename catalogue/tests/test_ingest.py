"""M4: ingest, resolution and the storage guarantees."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ingest import ensure_source, ingest_record, record_values
from app.models import Base, Episode, Rating, Title, TitleSource
from app.precedence import ENRICHER, PLATFORM
from app.sources import dramabox, reelshort
from app.sources.base import SourceRecord
from tests.conftest import load_fixture


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


def _ingest(session, module, record, kind=PLATFORM):
    source, studio = ensure_source(
        session,
        module.KEY,
        module.STUDIO_NAME,
        module.BASE_URL,
        module.WATCH_URL_TEMPLATE,
        kind=kind,
    )
    created = ingest_record(session, source, studio, record)
    session.commit()
    return source, created


# --- round trip from real payloads -------------------------------------------


def test_a_real_dramabox_record_ingests_completely(session) -> None:
    record = dramabox.parse_listing(load_fixture("dramabox/browse_ok"))[0]
    _ingest(session, dramabox, record)

    title = session.scalar(select(Title))
    assert title.primary_title == record.title
    assert title.synopsis and title.year and title.episode_count
    assert title.normalized_title == title.primary_title.lower().replace(",", "")


def test_a_real_reelshort_record_ingests_completely(session) -> None:
    record = reelshort.parse_detail(load_fixture("reelshort/detail_ok"))
    _ingest(session, reelshort, record)

    title = session.scalar(select(Title))
    assert title.primary_title == "Bound by Love"
    assert title.audience_rating == "All Ages"
    assert title.first_paid_episode == 16
    assert session.scalar(select(Episode).where(Episode.title_id == title.id))


def test_ingesting_twice_creates_nothing_the_second_time(session) -> None:
    record = reelshort.parse_detail(load_fixture("reelshort/detail_ok"))

    _, created_first = _ingest(session, reelshort, record)
    before = session.scalar(select(Title)).updated_at
    _, created_again = _ingest(session, reelshort, record)

    assert created_first is True
    assert created_again is False
    assert session.query(Title).count() == 1
    # 118: the payload's 119th item is a preview, not an episode.
    assert session.query(Episode).count() == 118
    # updated_at must not churn, or ?updated_since= reports the whole catalogue
    # as changed on every sync.
    assert session.scalar(select(Title)).updated_at == before


# --- resolution across two sources on one title ------------------------------


def test_two_sources_on_one_title_resolve_by_precedence(session) -> None:
    """The mechanism cross-source merging (M8) will rely on: the canonical row
    is recomputed from every contribution, so it does not matter which source
    wrote last."""
    record = dramabox.parse_listing(load_fixture("dramabox/browse_ok"))[0]
    source_a, _ = _ingest(session, dramabox, record)
    title = session.scalar(select(Title))

    # An enricher that disagrees on the title and supplies a year.
    imdb, _ = ensure_source(
        session, "imdb", "IMDb", "https://www.imdb.com", "", kind=ENRICHER
    )
    session.add(
        TitleSource(
            title_id=title.id,
            source_id=imdb.id,
            external_id="tt123",
            parsed_json=json.dumps(
                {"primary_title": "Retitled By IMDb", "year": 1999, "synopsis": "nope"}
            ),
        )
    )
    session.commit()

    # Re-resolve by re-ingesting the platform record.
    _ingest(session, dramabox, record)
    refreshed = session.scalar(select(Title))

    assert refreshed.primary_title == record.title, "platform owns its own title"
    assert refreshed.year == record.year, "platform release date wins"
    assert refreshed.synopsis == record.synopsis, "enricher cannot supply synopsis"


def test_record_values_exposes_only_resolvable_scalars() -> None:
    record = reelshort.parse_detail(load_fixture("reelshort/detail_ok"))
    values = record_values(record)

    from app.precedence import RESOLVED_FIELDS

    # Every resolvable field must be offered...
    assert set(RESOLVED_FIELDS) <= set(values)
    # ...plus imdb_tconst, which is CARRIED but never resolved: it lets IMDb
    # matching skip entirely where a platform already told us the answer.
    assert set(values) == set(RESOLVED_FIELDS) | {"imdb_tconst"}
    assert "imdb_tconst" not in RESOLVED_FIELDS

    # Relations are unioned in their own tables, never resolved as scalars.
    assert "cast" not in values and "episodes" not in values and "tags" not in values


# --- storage guarantees ------------------------------------------------------


def test_platform_ratings_are_always_stored_editorial(session) -> None:
    for module, record in (
        (dramabox, dramabox.parse_listing(load_fixture("dramabox/browse_ok"))[0]),
        (reelshort, reelshort.parse_detail(load_fixture("reelshort/detail_ok"))),
    ):
        _ingest(session, module, record)

    ratings = list(session.scalars(select(Rating)))
    assert ratings
    assert all(r.is_editorial for r in ratings), (
        "platform scores are promotional filler and must never be usable as real"
    )


def test_no_signed_video_url_reaches_the_database(session) -> None:
    """Both sources hand us expiring playback URLs in the same payload we
    parse. Asserted at the storage layer, not just the parser."""
    for module, record in (
        (dramabox, dramabox.parse_detail(load_fixture("dramabox/detail_with_cast"))),
        (reelshort, reelshort.parse_detail(load_fixture("reelshort/detail_ok"))),
    ):
        _ingest(session, module, record)

    columns = " ".join(
        str(v)
        for row in session.execute(select(Episode)).scalars()
        for v in (row.name, row.external_id)
    )
    for marker in ("Signature=", "Key-Pair-Id", "Expires=", ".m3u8", "http"):
        assert marker not in columns


def test_absent_values_never_blank_existing_ones(session) -> None:
    """A later payload that omits a field must not clear it."""
    full = SourceRecord(
        external_id="x1", title="Kept", synopsis="original", year=2026, episode_count=40
    )
    _ingest(session, dramabox, full)

    sparse = SourceRecord(external_id="x1", title="Kept")
    _ingest(session, dramabox, sparse)

    title = session.scalar(select(Title))
    assert title.synopsis == "original"
    assert title.year == 2026
    assert title.episode_count == 40


def test_two_titles_may_share_a_placeholder_episode_id(session) -> None:
    """ReelShort reuses placeholder chapter ids -- '0000' appears on more than
    one title. A globally unique (source_id, external_id) constraint killed an
    uncapped crawl 386 titles in. An episode id is only meaningful within the
    title it belongs to.
    """
    from app.sources.base import EpisodeRecord

    for n in (1, 2):
        _ingest(
            session,
            reelshort,
            SourceRecord(
                external_id=f"title-{n}",
                title=f"Title {n}",
                episodes=[EpisodeRecord(external_id="0000", number=1, is_free=True)],
            ),
        )

    assert session.query(Episode).count() == 2, "both titles keep their episode"


def test_one_title_still_cannot_have_the_same_episode_twice(session) -> None:
    """Scoping to the title must not weaken idempotency."""
    from app.sources.base import EpisodeRecord

    record = SourceRecord(
        external_id="t1",
        title="T",
        episodes=[EpisodeRecord(external_id="0000", number=1)],
    )
    _ingest(session, reelshort, record)
    _ingest(session, reelshort, record)

    assert session.query(Episode).count() == 1


def test_ingest_does_not_hold_a_write_lock_across_a_fetch(tmp_path) -> None:
    """Two workers run concurrently by design -- different sources in parallel --
    but SQLite allows one writer. The catalogue loop used to commit every 25
    items with the detail FETCH inside the transaction, holding the write lock
    across 25 network round-trips; a concurrent poster job died with
    "database is locked".

    Asserts the loop commits per item, so the lock is held only for the database
    work and never across I/O.
    """
    import inspect

    from app.jobs import catalog

    source = inspect.getsource(catalog.run_catalog_sync)
    assert "% 25 == 0" not in source, "batched commits reintroduce the lock hold"
    body = source.split("for ref in module.discover")[1]
    assert "session.commit()" in body, "each item must commit inside the loop"
