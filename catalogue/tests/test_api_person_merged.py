"""The API contract for people who were merged away.

Person merges used to delete the absorbed row, and 118 ids started answering 404
on a live deployment -- the downstream app renders cast lists and caches what it
renders, so those ids were held somewhere. These tests pin the repair: a
superseded person id resolves to whoever absorbed them, and says so.

The title routes learned this lesson first (`test_api_merged.py`). What differs
here is that a person has no `?updated_since=` feed to converge through, so the
`Content-Location` header is the ONLY signal a consumer gets. It has to be right
on every route, including the one that returns a file rather than a model.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.config import settings
from app.main import app
from app.models import (
    Base,
    Image,
    ImageKind,
    Person,
    PersonSource,
    Source,
    Title,
    TitlePerson,
)
from app.people import PersonMergeError, merge_people, resolve_person

FANDOM = "https://www.reelshort.com/fandom/aaron-oberst-01-6"


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)

    images = tmp_path / "images"
    images.mkdir()
    (images / "face.webp").write_bytes(b"RIFF____WEBPVP8 ")

    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="reelshort", base_url="https://a.test"))
        s.add(Title(id=1, primary_title="A Show", normalized_title="a show"))
        image = Image(id=7, kind=ImageKind.AVATAR, local_path="face.webp",
                      sha256="deadbeef", width=320, height=320, bytes=16)
        s.add(image)

        # The two halves the trailing slash produced. The cast row carries the
        # photo, the roster row carries the bio; both credit the same title.
        s.add(Person(id=1, name="Aaron Oberst", normalized_name="aaron oberst"))
        s.add(Person(id=2, name="Aaron Oberst", normalized_name="aaron oberst",
                     bio="A real bio."))
        s.add(PersonSource(person_id=1, source_id=1, external_id=FANDOM + "/",
                           avatar_image_id=7))
        s.add(PersonSource(person_id=2, source_id=1, external_id=FANDOM))
        s.add(TitlePerson(title_id=1, person_id=1, role="actor", billing_order=0))
        s.add(TitlePerson(title_id=1, person_id=2, role="actor", billing_order=1))
        s.commit()

        merge_people(s, 1, 2)
        s.commit()

    monkeypatch.setattr(type(settings), "image_dir", property(lambda _: images))

    from app.db import get_session

    def _session():
        with Session(engine, future=True) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_api_key] = lambda: None
    yield TestClient(app), engine
    app.dependency_overrides.clear()


# --- the superseded id keeps working -----------------------------------------


def test_a_superseded_person_id_serves_the_survivor(client) -> None:
    """A 404 tells a consumer nothing about where the actor went."""
    api, _ = client
    r = api.get("/people/2")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 1, "answers as the survivor, not the id that was asked for"
    assert body["name"] == "Aaron Oberst"


def test_the_superseded_id_says_it_moved(client) -> None:
    """Content-Location is the whole repair: it names the id to store instead."""
    api, _ = client
    r = api.get("/people/2")
    assert r.headers["Content-Location"] == "/people/1"
    assert r.headers["Deprecation"] == "true"


def test_a_live_id_carries_no_deprecation_headers(client) -> None:
    """The header's presence has to be a reliable 'update this id' signal, so it
    must be absent when nothing moved."""
    api, _ = client
    r = api.get("/people/1")
    assert r.status_code == 200
    assert "Content-Location" not in r.headers
    assert "Deprecation" not in r.headers


def test_the_survivor_carries_both_halves(client) -> None:
    """The merge is what produces a person with a bio AND a face -- before it,
    each half sat on a different row and a cast list showed the name twice."""
    api, _ = client
    body = api.get("/people/2").json()
    assert body["bio"] == "A real bio."
    assert body["photo_url"] == "/people/1/photo"


def test_a_superseded_id_still_serves_the_photo(client) -> None:
    """The avatar link moved to the survivor, so the old id owns no image row.
    Without resolution this 404s for a face that exists."""
    api, _ = client
    r = api.get("/people/2/photo")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"


def test_the_photo_route_keeps_the_deprecation_headers(client) -> None:
    """FastAPI merges an injected Response's headers only when the handler
    returns a model. This route returns a FileResponse, so it has to carry them
    across by hand -- the poster route had exactly this bug."""
    api, _ = client
    r = api.get("/people/2/photo")
    assert r.headers["Content-Location"] == "/people/1/photo"
    assert r.headers["Deprecation"] == "true"
    assert "max-age" in r.headers["Cache-Control"], "and keeps its own headers"


def test_an_id_that_never_existed_still_404s(client) -> None:
    """Resolution must not turn a genuine miss into a 200."""
    api, _ = client
    assert api.get("/people/9999").status_code == 404


# --- tombstones stay out of everything else ----------------------------------


def test_a_tombstone_is_not_listed(client) -> None:
    """It has a name but owns no credits, links or photo, so surfacing one in a
    search result offers a dead entry."""
    api, _ = client
    ids = [p["id"] for p in api.get("/people").json()]
    assert ids == [1]


def test_a_tombstone_is_not_counted(client) -> None:
    """`people` in /stats is the number reported to the operator; counting
    tombstones would inflate it by every merge ever run."""
    api, _ = client
    assert api.get("/stats").json()["people"] == 1


def test_the_cast_list_shows_the_actor_once(client) -> None:
    """The point of the whole exercise: two half-populated rows became one."""
    api, _ = client
    cast = api.get("/titles/1").json()["cast"]
    assert [c["id"] for c in cast] == [1]
    assert cast[0]["photo_url"] == "/people/1/photo"


def test_searching_by_person_does_not_match_a_tombstone(client) -> None:
    """The credits moved, so an inner join cannot reach a tombstone today. The
    filter is here so that stays true if a merge ever half-fails."""
    api, _ = client
    body = api.get("/titles", params={"person": "Aaron"}).json()
    assert [t["id"] for t in body["items"]] == [1]


# --- chains and guards --------------------------------------------------------


def test_a_chain_resolves_all_the_way(client) -> None:
    """A third duplicate found after the first pair merged points at a row that
    is itself a tombstone. One hop would strand the consumer one short."""
    api, engine = client
    with Session(engine, future=True) as s:
        s.add(Person(id=3, name="Aaron Oberst", normalized_name="aaron oberst"))
        s.add(PersonSource(person_id=3, source_id=1, external_id=FANDOM + "?x"))
        s.commit()
        merge_people(s, 1, 3)
        s.commit()
        # Hand-build the chain 2 -> 3 -> 1 that a re-merge would produce.
        s.execute(Person.__table__.update().where(Person.id == 2).values(
            merged_into_id=3))
        s.commit()
        assert resolve_person(s, 2) == 1

    r = api.get("/people/2")
    assert r.status_code == 200
    assert r.json()["id"] == 1
    assert r.headers["Content-Location"] == "/people/1"


def test_a_cycle_is_refused_rather_than_looped(client) -> None:
    api, engine = client
    with Session(engine, future=True) as s:
        s.execute(Person.__table__.update().where(Person.id == 1).values(
            merged_into_id=2))
        s.commit()
        with pytest.raises(PersonMergeError, match="cycle"):
            resolve_person(s, 1)


def test_merging_into_a_tombstone_is_refused(client) -> None:
    """It would build a chain that resolves but points readers through a row
    holding nothing."""
    _, engine = client
    with Session(engine, future=True) as s:
        s.add(Person(id=3, name="Someone", normalized_name="someone"))
        s.commit()
        with pytest.raises(PersonMergeError, match="tombstone"):
            merge_people(s, 2, 3)


def test_re_merging_a_tombstone_is_refused(client) -> None:
    """Overwriting a good pointer with a worse one loses the survivor."""
    _, engine = client
    with Session(engine, future=True) as s:
        s.add(Person(id=3, name="Someone", normalized_name="someone"))
        s.commit()
        with pytest.raises(PersonMergeError, match="already merged"):
            merge_people(s, 3, 2)


def test_the_tombstone_keeps_its_own_bio(client) -> None:
    """Kept whole rather than blanked: a loser's bio can differ from the
    survivor's, and after the merge the tombstone is the only copy left. That is
    precisely why every listing and count filters on the pointer."""
    _, engine = client
    with Session(engine, future=True) as s:
        loser = s.get(Person, 2)
        assert loser.bio == "A real bio."
        assert loser.merged_into_id == 1
        assert s.scalar(
            select(Person).where(Person.merged_into_id.is_(None))
        ).id == 1
