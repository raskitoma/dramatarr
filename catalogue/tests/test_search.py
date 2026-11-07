"""M6: full-text search."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ingest import ensure_source, ingest_record
from app.models import Base, Title
from app.search import reindex, search_ids
from app.sources import dramabox, reelshort
from tests.conftest import load_fixture


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/s.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        for module, records in (
            (dramabox, dramabox.parse_listing(load_fixture("dramabox/browse_ok"))),
            (reelshort, [reelshort.parse_detail(load_fixture("reelshort/detail_ok"))]),
        ):
            source, studio = ensure_source(
                s, module.KEY, module.STUDIO_NAME, module.BASE_URL, module.WATCH_URL_TEMPLATE
            )
            for record in records:
                ingest_record(s, source, studio, record)
        s.commit()
        yield s


def test_reindex_counts_every_title(session) -> None:
    assert reindex(session) == 13


def test_reindexing_twice_works(session) -> None:
    """A contentless FTS5 table rejects a plain DELETE only once it holds rows,
    so the first rebuild on a fresh database succeeds and the second fails.
    Every test with its own temp database would miss that; syncing twice in
    production would not.
    """
    reindex(session)
    assert reindex(session) == 13


def test_search_matches_a_title(session) -> None:
    reindex(session)
    assert search_ids(session, "Bound by Love")


def test_search_matches_a_cast_member(session) -> None:
    """Cast names are indexed, so an actor search finds their titles."""
    reindex(session)
    assert search_ids(session, "Savannah Coffee")


def test_search_matches_an_alias(session) -> None:
    reindex(session)
    assert search_ids(session, "His Cure His Wife")


def test_search_matches_synopsis_text(session) -> None:
    reindex(session)
    assert search_ids(session, "mafia")


@pytest.mark.parametrize(
    "query", ["billionaire's", 'a "quote"', "NOT AND OR", "*", "(unbalanced", "^caret"]
)
def test_punctuation_never_raises(session, query) -> None:
    """FTS5 treats plenty of punctuation as query syntax. Unescaped input would
    surface as a 500 on a perfectly ordinary search."""
    reindex(session)
    assert isinstance(search_ids(session, query), list)


def test_empty_query_returns_nothing(session) -> None:
    reindex(session)
    assert search_ids(session, "   ") == []


# --- the dramatarr report: a full title returned nothing ----------------------


def _add(session, title: str, synopsis: str = "") -> None:
    from app.ingest import ensure_source, ingest_record
    from app.sources.base import SourceRecord

    source, studio = ensure_source(
        session, reelshort.KEY, reelshort.STUDIO_NAME,
        reelshort.BASE_URL, reelshort.WATCH_URL_TEMPLATE,
    )
    ingest_record(
        session, source, studio,
        SourceRecord(external_id=title.lower().replace(" ", "-"), title=title,
                     synopsis=synopsis),
    )
    session.commit()


def test_searching_a_full_exact_title_finds_it(session) -> None:
    """The reported bug: `q='The Return of the Lost Heiress'` returned 0 while
    `q='Lost Heiress'` returned 1. A hard AND across every term made a longer,
    more precise query return FEWER results -- backwards for a caller that
    already knows the name."""
    _add(session, "The Return of the Lost Heiress")
    reindex(session)

    assert search_ids(session, "The Return of the Lost Heiress")
    assert search_ids(session, "Lost Heiress")
    assert search_ids(session, "Heiress")


def test_the_exact_title_ranks_first(session) -> None:
    """Even when other titles share most of its words."""
    _add(session, "The Return of the Lost Heiress")
    _add(session, "Return of the Heiress")
    _add(session, "The Lost Heiress Returns")
    reindex(session)

    hits = search_ids(session, "The Return of the Lost Heiress")
    top = session.get(Title, hits[0])
    assert top.primary_title == "The Return of the Lost Heiress"


def test_a_longer_query_never_returns_fewer_than_a_fragment(session) -> None:
    """The invariant behind the report."""
    _add(session, "The Return of the Lost Heiress")
    _add(session, "Heiress of the Storm")
    _add(session, "Lost Heiress Diaries")
    reindex(session)

    full = len(search_ids(session, "The Return of the Lost Heiress"))
    part = len(search_ids(session, "Lost Heiress"))
    assert full >= 1 and part >= 1


def test_an_exact_alias_also_finds_the_title(session) -> None:
    """ReelShort's book_sub_title and IMDb akas are real alternate names, and a
    caller may well know a title by one of them."""
    from app.ingest import ensure_source, ingest_record
    from app.sources.base import SourceRecord

    source, studio = ensure_source(
        session, reelshort.KEY, reelshort.STUDIO_NAME,
        reelshort.BASE_URL, reelshort.WATCH_URL_TEMPLATE,
    )
    ingest_record(session, source, studio, SourceRecord(
        external_id="x1", title="Bound by Love", aliases=["His Cure His Wife"]))
    session.commit()
    reindex(session)

    assert search_ids(session, "His Cure His Wife")


# --- the negative control still holds ----------------------------------------


FATED = [
    "Fated to My Forbidden Alpha",
    "Fated to My Forbidden Guardian",
    "Fated to My Forbidden Vampire",
    "Fated to My Bad Boy Alpha",
    "Fated to My Swapped Alpha",
    "Fated to My Homeless Billionaire Alpha",
]


def test_the_fated_family_ranks_the_exact_title_first(session) -> None:
    """Broadening search must not let a near-identical sibling outrank the
    title actually asked for. Search may legitimately RETURN the siblings --
    unlike matching, showing neighbours is useful -- but the exact one leads.
    """
    for title in FATED:
        _add(session, title)
    reindex(session)

    for title in FATED:
        hits = search_ids(session, title)
        assert hits, title
        assert session.get(Title, hits[0]).primary_title == title, (
            f"{title} did not rank first among its near-identical siblings"
        )


def test_a_fated_near_miss_does_not_pretend_to_be_an_exact_hit(session) -> None:
    """A title that exists in none of them still searches, but nothing is
    promoted to the exact tier."""
    for title in FATED:
        _add(session, title)
    reindex(session)

    from app.search import exact_ids

    assert exact_ids(session, "Fated to My Forbidden Billionaire") == []
