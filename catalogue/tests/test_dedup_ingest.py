"""Prevention: cross-source identity at ingest time.

A merge lasts exactly until the next sync unless `ingest_record` recognises a show
it already holds. These tests are the ones that decide whether deduplication is a
fix or a nightly chore.

Order-independence is the property under test throughout. `_resolve_title` already
recomputes from all sources so field values do not depend on sync order; the hook
has to extend that to identity, so that whichever platform syncs first, the second
joins the existing row rather than minting a rival.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.ingest import ensure_source, ingest_record
from app.models import Base, Title, TitleSource
from app.precedence import PLATFORM
from app.sources import dramabox, reelshort
from app.sources.base import SourceRecord


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


def _put(session, module, record) -> bool:
    source, studio = ensure_source(
        session, module.KEY, module.STUDIO_NAME, module.BASE_URL,
        module.WATCH_URL_TEMPLATE, kind=PLATFORM,
    )
    created = ingest_record(session, source, studio, record)
    session.commit()
    return created


#: The real pair, verbatim. DramaBox dropped the space in "LightRedemption".
DRAMABOX_RECORD = SourceRecord(
    external_id="42000023511",
    title="Fruit Dimension: Orange LightRedemption. 1",
    year=2026,
    episode_count=20,
    synopsis="Orange thinks she is the foster daughter of the fruit family.",
)
REELSHORT_RECORD = SourceRecord(
    external_id="6a630a3b53c25feab006dfb6",
    title="Fruit Dimension: Orange Light Redemption.1",
    year=2026,
    episode_count=20,
    synopsis="Orange believed she was the adopted daughter of the Fruit Family.",
)


def test_the_second_platform_joins_the_existing_title(session) -> None:
    """The bug, fixed. Before the hook this produced ids 720 and 9776."""
    assert _put(session, dramabox, DRAMABOX_RECORD) is True
    assert _put(session, reelshort, REELSHORT_RECORD) is False, "no new title"

    assert session.scalar(select(func.count()).select_from(Title)) == 1
    assert session.scalar(select(func.count()).select_from(TitleSource)) == 2


def test_the_result_does_not_depend_on_which_platform_syncs_first(session) -> None:
    """ReelShort first, DramaBox second -- same end state."""
    assert _put(session, reelshort, REELSHORT_RECORD) is True
    assert _put(session, dramabox, DRAMABOX_RECORD) is False

    assert session.scalar(select(func.count()).select_from(Title)) == 1
    links = session.scalars(select(TitleSource)).all()
    assert {link.external_id for link in links} == {
        "42000023511", "6a630a3b53c25feab006dfb6"
    }


def test_re_running_both_syncs_writes_no_new_rows(session) -> None:
    """Idempotency. If the hook churned rows, ?updated_since= would report the
    whole catalogue as changed every night."""
    _put(session, dramabox, DRAMABOX_RECORD)
    _put(session, reelshort, REELSHORT_RECORD)
    before = session.get(Title, 1).updated_at

    assert _put(session, dramabox, DRAMABOX_RECORD) is False
    assert _put(session, reelshort, REELSHORT_RECORD) is False

    assert session.scalar(select(func.count()).select_from(Title)) == 1
    assert session.scalar(select(func.count()).select_from(TitleSource)) == 2
    assert session.get(Title, 1).updated_at == before, "updated_at must not churn"


def test_a_disagreeing_episode_count_creates_a_separate_title(session) -> None:
    """Real pair: "Crown of Vengeance" is 100 episodes on one platform and 36 on
    the other. The hook must refuse and leave it for the review queue -- guessing
    here would fuse two shows."""
    _put(session, dramabox, SourceRecord(
        external_id="d-crown", title="Crown of Vengeance", year=2024, episode_count=100
    ))
    _put(session, reelshort, SourceRecord(
        external_id="r-crown", title="Crown of Vengeance", year=2024, episode_count=36
    ))

    assert session.scalar(select(func.count()).select_from(Title)) == 2


def test_a_missing_episode_count_creates_a_separate_title(session) -> None:
    """Absence proves nothing. Creating a duplicate is the safe failure -- the
    nightly job finds it; a wrong link is permanent."""
    _put(session, dramabox, SourceRecord(
        external_id="d-x", title="Chained Wings", year=2026, episode_count=50
    ))
    _put(session, reelshort, SourceRecord(
        external_id="r-x", title="Chained Wings", year=2026
    ))

    assert session.scalar(select(func.count()).select_from(Title)) == 2


def test_the_fated_family_stays_six_separate_titles(session) -> None:
    """The negative control, driven all the way through ingest."""
    for i, name in enumerate([
        "Fated to My Cruel CEO",
        "Fated to My Forbidden Alpha",
        "Fated to My Forbidden Vampire",
        "Fated to My Neighbor Boss",
        "Fated to My Ruthless Alpha",
        "Fated to My Swapped Alpha",
    ]):
        # Identical year AND episode count across all six: evidence agrees
        # everywhere, so only the exact key keeps them apart.
        _put(session, dramabox, SourceRecord(
            external_id=f"fated-{i}", title=name, year=2026, episode_count=60
        ))

    assert session.scalar(select(func.count()).select_from(Title)) == 6


def test_one_platform_listing_a_name_twice_does_not_self_merge(session) -> None:
    """Two DramaBox ids sharing a name must NOT collapse at ingest time.

    Their evidence agrees, so the rule alone would merge them -- but a title
    already holding a link from THIS source is a different show that shares a
    name, not this record's twin. Same-platform duplicates go through the review
    queue, where a human can see both, rather than being fused on a name alone.
    """
    assert _put(session, dramabox, SourceRecord(
        external_id="d-1", title="Divorced at the Wedding Day", year=2026, episode_count=51
    )) is True
    assert _put(session, dramabox, SourceRecord(
        external_id="d-2", title="Divorced at the Wedding Day", year=2026, episode_count=51
    )) is True

    assert session.scalar(select(func.count()).select_from(Title)) == 2


def test_two_links_from_one_source_do_not_flip_the_rating_nightly(session) -> None:
    """The hazard a same-platform merge creates.

    After a `(DUBBED)` merge a title holds two DramaBox links, and `_sync_rating`
    is scoped by (title_id, source_id) rather than by the link -- so both records
    write the same row. An unconditional overwrite would set `value` from whichever
    synced last, every night, forever. The better-established figure wins instead,
    so the outcome does not depend on order.
    """
    from app.merge import merge_titles
    from app.models import Rating

    _put(session, dramabox, SourceRecord(
        external_id="d-1", title="How the Tables Turned", year=2026,
        episode_count=40, rating=7.5, rating_votes=2,
    ))
    _put(session, dramabox, SourceRecord(
        external_id="d-2", title="How the Tables Turned（DUBBED）", year=2026,
        episode_count=40, rating=9.9, rating_votes=500,
    ))
    ids = sorted(session.scalars(select(Title.id)).all())
    merge_titles(session, ids[0], ids[1])
    session.commit()

    source, studio = ensure_source(
        session, dramabox.KEY, dramabox.STUDIO_NAME, dramabox.BASE_URL,
        dramabox.WATCH_URL_TEMPLATE, kind=PLATFORM,
    )
    # Re-sync both, in both orders. The rating must not budge.
    for external_id, rating, votes in (
        ("d-1", 7.5, 2), ("d-2", 9.9, 500), ("d-1", 7.5, 2),
    ):
        ingest_record(session, source, studio, SourceRecord(
            external_id=external_id,
            title="How the Tables Turned" if external_id == "d-1"
            else "How the Tables Turned（DUBBED）",
            year=2026, episode_count=40, rating=rating, rating_votes=votes,
        ))
        session.commit()

    rows = session.scalars(select(Rating).where(Rating.title_id == ids[0])).all()
    assert len(rows) == 1
    assert rows[0].votes == 500, "the better-established rating must hold"
    assert rows[0].value == 9.9


def test_the_identity_key_is_stored_for_the_index(session) -> None:
    """The hook is a single indexed equality lookup; an unset key would make it
    scan nothing and silently never match."""
    _put(session, dramabox, DRAMABOX_RECORD)
    row = session.scalars(select(Title)).one()
    assert row.identity_key == "fruitdimensionorangelightredemption1"
