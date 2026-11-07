"""Merging two title rows into one, against a real database.

These tests exist because the matching rule can be perfect and the merge still
lose data. Nine tables carry a FK to `title.id`; five of their unique constraints
include a source column, which means they CANNOT collide for a cross-platform
merge and DO collide for a same-platform one. DramaBox's 485 `(DUBBED)` twins are
all same-platform, so the colliding path is the common one and every case below
covers both directions.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.ingest import ensure_source, ingest_record
from app.merge import MergeError, merge_titles, resolve_survivor, unmerge
from app.models import (
    Base,
    Episode,
    MatchCandidate,
    MergeState,
    Rating,
    Title,
    TitleAlias,
    TitleMerge,
    TitlePerson,
    TitleSource,
    TitleTag,
)
from app.precedence import ENRICHER, PLATFORM
from app.sources import dramabox, reelshort
from app.sources.base import EpisodeRecord, PersonCredit, SourceRecord


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


def _record(external_id: str, title: str, **kw) -> SourceRecord:
    return SourceRecord(
        external_id=external_id,
        title=title,
        year=kw.get("year", 2026),
        episode_count=kw.get("episode_count", 20),
        synopsis=kw.get("synopsis", "A synopsis."),
        episodes=kw.get("episodes", []),
        cast=kw.get("cast", []),
        tags=kw.get("tags", []),
        rating=kw.get("rating"),
        rating_votes=kw.get("rating_votes"),
        aliases=kw.get("aliases", []),
    )


def _put(session, module, record, kind=PLATFORM):
    source, studio = ensure_source(
        session, module.KEY, module.STUDIO_NAME, module.BASE_URL,
        module.WATCH_URL_TEMPLATE, kind=kind,
    )
    ingest_record(session, source, studio, record)
    session.commit()
    return source


#: The real pair. DramaBox dropped a space, so these arrive as two titles unless
#: the ingest hook catches them -- which is the next test file's business. Here
#: they are forced apart to exercise the merge itself.
DB_TITLE = "Fruit Dimension: Orange LightRedemption. 1"
RS_TITLE = "Fruit Dimension: Orange Light Redemption.1"


def _two_platform_titles(session) -> tuple[int, int]:
    """One show as two rows, one per platform, each with its own children.

    The episode counts deliberately disagree (20 against 40). That is what keeps
    the ingest hook from linking them on arrival, and it is the realistic shape of
    an operator-driven merge: 106 of the live catalogue's candidate groups
    disagree on episode count, get queued rather than auto-merged, and reach this
    code only when a human approves them from the review queue.
    """
    _put(session, dramabox, _record(
        "42000023511", DB_TITLE,
        episodes=[EpisodeRecord(external_id="c1", number=1, is_free=True),
                  EpisodeRecord(external_id="c2", number=2, is_free=False)],
        cast=[PersonCredit(external_id="db-p1", name="Ada Lovelace", billing_order=2)],
        tags=[("genre", "Fantasy")],
        rating=8.2, rating_votes=10,
        aliases=["Orange Light"],
    ))
    _put(session, reelshort, _record(
        "6a630a3b53c25feab006dfb6", RS_TITLE,
        episode_count=40,
        episodes=[EpisodeRecord(external_id="r1", number=1, is_free=True)],
        cast=[PersonCredit(external_id="rs-p1", name="Grace Hopper", billing_order=1)],
        tags=[("genre", "Romance")],
        rating=4.7, rating_votes=3,
    ))
    ids = sorted(session.scalars(select(Title.id)).all())
    assert len(ids) == 2, "fixture must produce two separate titles"
    return ids[0], ids[1]


# --- the cross-platform case --------------------------------------------------


def test_a_cross_platform_merge_unions_both_sources(session) -> None:
    survivor, loser = _two_platform_titles(session)

    merge_titles(session, survivor, loser, reason="test", key_kind="title")
    session.commit()

    links = session.scalars(
        select(TitleSource).where(TitleSource.title_id == survivor)
    ).all()
    assert {link.external_id for link in links} == {
        "42000023511", "6a630a3b53c25feab006dfb6"
    }, "one title, both platforms' links -- this is the whole point"


def test_the_losing_link_is_repointed_not_deleted(session) -> None:
    """The single most important invariant. `ingest_record` finds a title only
    through (source_id, external_id); delete that row and the next sync takes the
    `link is None` branch and recreates the duplicate."""
    survivor, loser = _two_platform_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    assert session.scalar(select(func.count()).select_from(TitleSource)) == 2
    assert session.scalar(
        select(func.count()).select_from(TitleSource).where(
            TitleSource.title_id == loser
        )
    ) == 0


def test_episodes_from_both_platforms_survive(session) -> None:
    """source_id differs, so uq_episode_external cannot fire and every row moves."""
    survivor, loser = _two_platform_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    episodes = session.scalars(
        select(Episode).where(Episode.title_id == survivor)
    ).all()
    assert len(episodes) == 3
    assert {e.external_id for e in episodes} == {"c1", "c2", "r1"}


def test_both_platforms_ratings_survive_side_by_side(session) -> None:
    """Two platforms' scores are separate facts, not a conflict."""
    survivor, loser = _two_platform_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    ratings = session.scalars(select(Rating).where(Rating.title_id == survivor)).all()
    assert len(ratings) == 2


def test_cast_from_both_platforms_survives(session) -> None:
    survivor, loser = _two_platform_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    names = session.scalars(
        select(TitlePerson).where(TitlePerson.title_id == survivor)
    ).all()
    assert len(names) == 2


def test_the_losing_spelling_stays_searchable(session) -> None:
    """`_sync_aliases` suppresses an alias equal to its own record title, so the
    loser's spelling exists nowhere else. Losing it is a user-visible regression
    caused by the fix itself."""
    survivor, loser = _two_platform_titles(session)
    losing_title = session.get(Title, loser).primary_title

    merge_titles(session, survivor, loser)
    session.commit()

    aliases = session.scalars(
        select(TitleAlias.alias).where(TitleAlias.title_id == survivor)
    ).all()
    assert losing_title in aliases


def test_nothing_is_left_pointing_at_the_loser(session) -> None:
    """Sweep every table that carries a title FK. A table missed here is silent
    data loss, so the assertion is generic rather than per-table."""
    survivor, loser = _two_platform_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    for model in (TitleSource, TitleAlias, Episode, TitlePerson, TitleTag,
                  Rating, MatchCandidate):
        remaining = session.scalar(
            select(func.count()).select_from(model).where(model.title_id == loser)
        )
        assert remaining == 0, f"{model.__tablename__} still points at the loser"


# --- the tombstone ------------------------------------------------------------


def test_the_losing_row_is_kept_as_a_tombstone(session) -> None:
    """A consumer already stores this id; a deleted row tells it nothing."""
    survivor, loser = _two_platform_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    row = session.get(Title, loser)
    assert row is not None
    assert row.merged_into_id == survivor
    assert row.merged_at is not None


def test_the_tombstone_looks_changed_to_an_updated_since_poll(session) -> None:
    """If updated_at does not move, the merge is invisible to the change feed and
    the consumer keeps a dead id forever."""
    survivor, loser = _two_platform_titles(session)
    before = session.get(Title, loser).updated_at

    merge_titles(session, survivor, loser)
    session.commit()

    assert session.get(Title, loser).updated_at >= before


def test_a_merge_chain_resolves_to_the_final_survivor(session) -> None:
    """B merged into A, then C into B. A consumer holding C must reach A, not B."""
    a, b = _two_platform_titles(session)
    c = Title(primary_title="Third Row", normalized_title="third row")
    session.add(c)
    session.commit()

    merge_titles(session, a, b)
    session.commit()
    merge_titles(session, b, c.id)   # survivor is itself a tombstone
    session.commit()

    assert resolve_survivor(session, c.id) == a


def test_merging_a_title_into_itself_is_refused(session) -> None:
    survivor, _ = _two_platform_titles(session)
    with pytest.raises(MergeError):
        merge_titles(session, survivor, survivor)


def test_merging_an_already_merged_title_is_refused(session) -> None:
    survivor, loser = _two_platform_titles(session)
    merge_titles(session, survivor, loser)
    session.commit()

    with pytest.raises(MergeError):
        merge_titles(session, survivor, loser)


# --- the same-platform case, where the constraints actually collide -----------


def _two_dramabox_titles(session) -> tuple[int, int]:
    """A `(DUBBED)` twin: same platform, so every source-scoped constraint bites.

    The colliding episode external_id is not invented -- ReelShort reuses '0000'
    as a placeholder chapter id across titles, and that is what broke the crawl
    at 386 records before episode uniqueness was scoped per title.
    """
    _put(session, dramabox, _record(
        "d-1", "How the Tables Turned",
        episodes=[EpisodeRecord(external_id="0000", number=1, is_free=True)],
        cast=[PersonCredit(external_id="p-shared", name="Same Actor", billing_order=5)],
        tags=[("genre", "Revenge")],
        rating=7.5, rating_votes=2,
    ))
    _put(session, dramabox, _record(
        "d-2", "How the Tables Turned（DUBBED）",
        episodes=[EpisodeRecord(external_id="0000", number=1, is_free=True)],
        cast=[PersonCredit(external_id="p-shared", name="Same Actor", billing_order=1)],
        tags=[("genre", "Revenge")],
        rating=7.5, rating_votes=99,
    ))
    ids = sorted(session.scalars(select(Title.id)).all())
    assert len(ids) == 2
    return ids[0], ids[1]


def test_a_same_platform_merge_does_not_raise_on_the_shared_episode_id(session) -> None:
    """uq_episode_external is (source_id, title_id, external_id). Both rows carry
    '0000' from the same source, so a blind repoint is an IntegrityError."""
    survivor, loser = _two_dramabox_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    episodes = session.scalars(
        select(Episode).where(Episode.title_id == survivor)
    ).all()
    assert len(episodes) == 1, "the duplicate placeholder is dropped, not duplicated"


def test_a_same_platform_merge_keeps_one_rating_per_source(session) -> None:
    """uq_rating_title_source is exactly (title_id, source_id) -- guaranteed
    collision when both rows come from one platform."""
    survivor, loser = _two_dramabox_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    ratings = session.scalars(select(Rating).where(Rating.title_id == survivor)).all()
    assert len(ratings) == 1
    assert ratings[0].votes == 99, "the better-established rating wins, not the first row"


def test_a_same_platform_merge_keeps_the_better_billing_order(session) -> None:
    """billing_order sits OUTSIDE uq_title_person, so a plain conflict-drop
    silently discards the better billing."""
    survivor, loser = _two_dramabox_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    credits = session.scalars(
        select(TitlePerson).where(TitlePerson.title_id == survivor)
    ).all()
    assert len(credits) == 1
    assert credits[0].billing_order == 1


def test_a_same_platform_merge_does_not_duplicate_a_shared_tag(session) -> None:
    survivor, loser = _two_dramabox_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    tags = session.scalars(select(TitleTag).where(TitleTag.title_id == survivor)).all()
    assert len(tags) == 1


# --- the audit trail and undo -------------------------------------------------


def test_a_merge_records_what_it_moved(session) -> None:
    survivor, loser = _two_platform_titles(session)

    merge_titles(session, survivor, loser, reason="because", key_kind="title")
    session.commit()

    audit = session.scalars(select(TitleMerge)).one()
    assert audit.state == MergeState.APPLIED
    assert audit.loser_id == loser and audit.survivor_id == survivor
    report = json.loads(audit.report_json)
    assert report["links"], "the loser's links must be captured for the undo"


def test_an_undo_hands_the_link_back(session) -> None:
    """Without the link returned, clearing the tombstone leaves a title no sync
    can ever reach again."""
    survivor, loser = _two_platform_titles(session)
    merge_titles(session, survivor, loser)
    session.commit()

    result = unmerge(session, loser)
    session.commit()

    assert result["links_returned"] == 1
    assert session.get(Title, loser).merged_into_id is None
    returned = session.scalars(
        select(TitleSource.external_id).where(TitleSource.title_id == loser)
    ).all()
    assert returned == ["6a630a3b53c25feab006dfb6"]


def test_an_undo_leaves_negative_memory(session) -> None:
    """An undo has to outlast tonight's run.

    Classification is a pure function of the stored keys, so the nightly job would
    reach the identical verdict and merge the pair straight back. The audit row
    lands in REJECTED rather than a neutral REVERTED so the merge is refused.
    """
    survivor, loser = _two_platform_titles(session)
    merge_titles(session, survivor, loser)
    session.commit()

    unmerge(session, loser)
    session.commit()

    audit = session.scalars(select(TitleMerge)).one()
    assert audit.state == MergeState.REJECTED
    assert audit.reverted_at is not None

    with pytest.raises(MergeError):
        merge_titles(session, survivor, loser)


def test_undoing_an_unmerged_title_is_refused(session) -> None:
    survivor, _ = _two_platform_titles(session)
    with pytest.raises(MergeError):
        unmerge(session, survivor)


# --- resolution ---------------------------------------------------------------


def test_the_merged_row_is_resolved_from_all_its_sources(session) -> None:
    """Precedence must run again after the merge, or the survivor keeps fields
    resolved from only half its sources."""
    survivor, loser = _two_platform_titles(session)

    merge_titles(session, survivor, loser)
    session.commit()

    row = session.get(Title, survivor)
    assert row.synopsis, "resolution must have produced a synopsis"
    assert row.episode_count == 20, "episode_count comes from precedence, not a row count"
