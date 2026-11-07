"""Merging duplicate person rows.

The bug these repair came from one character: ReelShort has no numeric person id,
so the fandom URL is the key, and it arrived by two routes that disagreed about a
trailing slash. 118 of 2,306 people were the same actor twice -- the roster row
holding the bio, the cast row holding the photo -- so a cast list showed one name
twice, each half-populated.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, Person, PersonSource, Source, Title, TitlePerson
from app.people import (
    PersonMergeError,
    duplicate_groups,
    merge_duplicate_people,
    merge_people,
    restore_tombstones,
)

FANDOM = "https://www.reelshort.com/fandom/reelshort-actor-aaron-oberst-01-6"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="reelshort", base_url="https://a.test"))
        s.add(Source(id=2, key="dramabox", base_url="https://b.test"))
        s.add(Title(id=10, primary_title="A Show", normalized_title="a show"))
        s.commit()
        yield s


def _person(session, pid, name, source_id, external_id, *, bio=None, image=None):
    session.add(Person(id=pid, name=name, normalized_name=name.lower(), bio=bio))
    session.flush()
    session.add(PersonSource(person_id=pid, source_id=source_id,
                             external_id=external_id, avatar_image_id=image))
    session.commit()


# --- detection ----------------------------------------------------------------


def test_a_trailing_slash_is_the_same_person(session) -> None:
    """The exact bug. Both rows are ReelShort's own identifier for one actor."""
    _person(session, 1, "Aaron Oberst", 1, FANDOM, bio="A bio.")
    _person(session, 2, "Aaron Oberst", 1, FANDOM + "/", image=None)

    assert duplicate_groups(session) == [[1, 2]]


def test_two_actors_sharing_a_name_are_not_merged(session) -> None:
    """Never name matching -- several actors here genuinely share a name, and a
    wrong person merge has no watch link to notice it by."""
    _person(session, 1, "Aaron Oberst", 1, FANDOM)
    _person(session, 2, "Aaron Oberst", 1, FANDOM + "-different-actor")

    assert duplicate_groups(session) == []


def test_the_same_name_on_two_platforms_is_not_a_duplicate(session) -> None:
    """DramaBox ids and ReelShort URLs are different id spaces; matching across
    them would be name matching in disguise."""
    _person(session, 1, "Aaron Oberst", 1, FANDOM)
    _person(session, 2, "Aaron Oberst", 2, "29208")

    assert duplicate_groups(session) == []


def test_a_three_way_duplicate_collapses_once(session) -> None:
    _person(session, 1, "X", 1, FANDOM)
    _person(session, 2, "X", 1, FANDOM + "/")
    _person(session, 3, "X", 1, FANDOM + "//")

    assert duplicate_groups(session) == [[1, 2, 3]]


# --- merging ------------------------------------------------------------------


def test_the_survivor_gains_the_bio_and_keeps_the_photo(session) -> None:
    """The halves are complementary: the roster row has the bio, the cast row has
    the photo. Merging is what produces a person with both."""
    _person(session, 1, "Aaron Oberst", 1, FANDOM + "/", image=None)   # cast row
    _person(session, 2, "Aaron Oberst", 1, FANDOM, bio="A real bio.")  # roster row

    merge_people(session, 1, 2)
    session.commit()

    survivor = session.get(Person, 1)
    assert survivor.bio == "A real bio."

    # Tombstoned, not deleted: the id a consumer already stored has to keep
    # resolving. The row keeps its own bio -- it may differ from the survivor's
    # and this is the last copy -- which is why listings filter on the pointer.
    loser = session.get(Person, 2)
    assert loser is not None
    assert loser.merged_into_id == 1
    assert loser.merged_at is not None


def test_the_colliding_link_keeps_the_row_carrying_the_image(session) -> None:
    """uq_person_source_external is (source_id, external_id): once the keys are
    normalised the two links collide by definition. The one with a stored photo
    has to win, or merging would throw the face away."""
    from app.models import Image

    session.add(Image(id=7, kind="avatar", local_path="a.webp", sha256="abc"))
    session.commit()
    _person(session, 1, "A", 1, FANDOM, bio="bio")        # no image
    _person(session, 2, "A", 1, FANDOM + "/", image=7)    # has the image

    merge_people(session, 1, 2)
    session.commit()

    links = session.scalars(select(PersonSource)).all()
    assert len(links) == 1
    assert links[0].person_id == 1
    assert links[0].avatar_image_id == 7, "the photo must survive the merge"


def test_credits_move_and_duplicates_collapse(session) -> None:
    _person(session, 1, "A", 1, FANDOM)
    _person(session, 2, "A", 1, FANDOM + "/")
    session.add_all([
        TitlePerson(title_id=10, person_id=1, role="actor", source_id=1, billing_order=5),
        TitlePerson(title_id=10, person_id=2, role="actor", source_id=1, billing_order=1),
    ])
    session.commit()

    merge_people(session, 1, 2)
    session.commit()

    credits = session.scalars(select(TitlePerson)).all()
    assert len(credits) == 1, "the cast list stops showing the actor twice"
    assert credits[0].person_id == 1
    assert credits[0].billing_order == 1, "the better billing is kept"


def test_merging_a_person_into_themselves_is_refused(session) -> None:
    _person(session, 1, "A", 1, FANDOM)
    with pytest.raises(PersonMergeError):
        merge_people(session, 1, 1)


# --- the sweep ----------------------------------------------------------------


def test_a_dry_run_writes_nothing(session) -> None:
    _person(session, 1, "A", 1, FANDOM, bio="bio")
    _person(session, 2, "A", 1, FANDOM + "/")

    stats = merge_duplicate_people(session, dry_run=True)

    assert stats["groups"] == 1 and stats["people_absorbed"] == 1
    assert session.get(Person, 2) is not None


def test_applying_merges_every_group(session) -> None:
    _person(session, 1, "A", 1, FANDOM, bio="bio a")
    _person(session, 2, "A", 1, FANDOM + "/")
    _person(session, 3, "B", 1, FANDOM + "-b")
    _person(session, 4, "B", 1, FANDOM + "-b/")

    stats = merge_duplicate_people(session, dry_run=False)

    assert stats["merged"] == 2 and stats["failed"] == 0
    assert session.get(Person, 2).merged_into_id == 1
    assert session.get(Person, 4).merged_into_id == 3
    # Idempotent BECAUSE duplicate_groups skips tombstones. Without that filter
    # the merged rows keep matching their survivor's key and every later run
    # proposes the same pair again.
    assert duplicate_groups(session) == [], "idempotent: nothing left to merge"


# --- restoring ids a pre-tombstone merge deleted ------------------------------


def _pre_merge_backup(path):
    """A backup shaped like the real ones: the two duplicate rows still present,
    because every backup on the box predates the merge that deleted them."""
    import sqlite3

    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT,"
        "                     normalized_name TEXT, bio TEXT);"
        "CREATE TABLE person_source (id INTEGER PRIMARY KEY, person_id INTEGER,"
        "                            source_id INTEGER, external_id TEXT);"
    )
    db.executemany(
        "INSERT INTO person VALUES (?, ?, ?, ?)",
        [(1, "A", "a", None), (2, "A", "a", "a bio"), (9, "B", "b", None)],
    )
    db.executemany(
        "INSERT INTO person_source (person_id, source_id, external_id) VALUES (?, ?, ?)",
        [(1, 1, FANDOM), (2, 1, FANDOM + "/"), (9, 1, FANDOM + "-b")],
    )
    db.commit()
    db.close()
    return str(path)


def test_restoring_recreates_the_deleted_id_as_a_tombstone(session, tmp_path) -> None:
    """The mapping is recoverable because group_links is deterministic and the
    survivor is always the lowest id -- replaying it over a pre-merge backup says
    exactly which id was folded into which."""
    backup = _pre_merge_backup(tmp_path / "b.db")
    _person(session, 1, "A", 1, FANDOM)          # survivor, as after the merge
    _person(session, 9, "B", 1, FANDOM + "-b")   # untouched
    session.commit()

    stats = restore_tombstones(session, backup, dry_run=False)

    assert stats["restored"] == 1
    restored = session.get(Person, 2)
    assert restored is not None
    assert restored.merged_into_id == 1
    assert restored.merged_at is not None
    assert restored.bio == "a bio", "restored whole, like a live merge's tombstone"


def test_restoring_is_a_dry_run_by_default(session, tmp_path) -> None:
    backup = _pre_merge_backup(tmp_path / "b.db")
    _person(session, 1, "A", 1, FANDOM)
    session.commit()

    stats = restore_tombstones(session, backup)

    assert stats["restored"] == 1 and stats["dry_run"] is True
    assert session.get(Person, 2) is None, "nothing written"


def test_restoring_twice_changes_nothing(session, tmp_path) -> None:
    """Re-runnable: only ids missing from the live database are restored, so a
    second pass over the same backup is a no-op rather than a primary key clash."""
    backup = _pre_merge_backup(tmp_path / "b.db")
    _person(session, 1, "A", 1, FANDOM)
    session.commit()

    restore_tombstones(session, backup, dry_run=False)
    again = restore_tombstones(session, backup, dry_run=False)

    assert again["restored"] == 0
    assert again["already_present"] == 1


def test_restoring_skips_a_survivor_that_is_gone(session, tmp_path) -> None:
    """Pointing a restored row at an id that does not exist would trade a 404 for
    a broken chain, which is worse: the consumer gets a 200 and no person."""
    backup = _pre_merge_backup(tmp_path / "b.db")
    # Neither half of the pair is live -- the survivor was removed by something
    # other than this merge.
    _person(session, 9, "B", 1, FANDOM + "-b")
    session.commit()

    stats = restore_tombstones(session, backup, dry_run=False)

    assert stats["restored"] == 0 and stats["unknown_survivor"] == 1
    assert session.get(Person, 2) is None


def test_a_restored_tombstone_follows_a_later_merge(session, tmp_path) -> None:
    """If the survivor has since been merged again, the restored row must point
    at whoever is actually live, not at the intermediate tombstone."""
    backup = _pre_merge_backup(tmp_path / "b.db")
    _person(session, 1, "A", 1, FANDOM)
    _person(session, 9, "B", 1, FANDOM + "-b")
    session.commit()
    # 1 later folded into 9 by a different rule.
    survivor = session.get(Person, 1)
    survivor.merged_into_id = 9
    session.commit()

    restore_tombstones(session, backup, dry_run=False)

    assert session.get(Person, 2).merged_into_id == 9
