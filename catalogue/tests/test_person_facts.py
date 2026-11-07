"""Structured facts about people, mined from DramaBox performer pages.

This code exists because a recorded measurement was wrong in a specific and
instructive way. `bookInfo.performerList` really does carry only five fields and
no biography, and from that the project concluded the SOURCE had none -- so bio
coverage was written down as a hard ceiling near 10-15%. DramaBox publishes
biographies at `/name/{performerId}`, keyed on the id we already store. Nobody
had looked, because the first measurement felt conclusive.

Two constraints shape what is stored, and both are pinned here:

  * The prose is Chinese and only exists in the zhHans locale. The index's
    reader does not read Chinese, so the sentence is mined for facts and then
    discarded. A test asserts the prose never reaches the database.
  * The Han name is kept as an ALIAS. `person.name` stays romanised, because a
    name the reader cannot type is not a usable search key.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.fetch.types import RawDoc
from app.jobs.person_facts import run_person_facts
from app.models import Base, Person, PersonAlias, PersonSource, Source
from app.sources import dramabox


def _page(**props) -> RawDoc:
    return RawDoc(
        url="https://www.dramabox.com/_next/data/b/zhHans/name/1.json",
        status=200, headers={},
        body=json.dumps({"pageProps": props}).encode(),
    )


# --- parsing ------------------------------------------------------------------


def test_the_formulaic_intro_yields_every_fact() -> None:
    """The real shape, verbatim from performer 18356."""
    doc = _page(
        performerId="18356", performerName="王鹏",
        introduction="王鹏，性别男，身高188cm，体重75kg，天蝎座，经验丰富，"
                     "出演过多部爆款，曾参演《我的女上司》《惊龙》等作品。",
    )
    parsed = dramabox.parse_performer(doc)
    assert parsed["gender"] == "male"
    assert parsed["height_cm"] == 188
    assert parsed["weight_kg"] == 75
    assert parsed["zodiac"] == "Scorpio"
    assert parsed["han_name"] == "王鹏"


def test_each_field_is_read_independently() -> None:
    """Most intros are partial: of 48 sampled, 45 gave gender but only 22 a sign.
    A sentence missing one field must still yield the others."""
    parsed = dramabox.parse_performer(_page(
        performerName="刘博洋",
        introduction="刘博洋，性别男，曾参演《暗潮》《恰如其分》等作品。",
    ))
    assert parsed["gender"] == "male"
    assert parsed["height_cm"] is None
    assert parsed["weight_kg"] is None
    assert parsed["zodiac"] is None


def test_a_stat_block_intro_still_parses() -> None:
    """Some intros are not sentences at all -- no name, no career, just numbers."""
    parsed = dramabox.parse_performer(_page(
        performerName="某人", introduction="身高165cm，星座双子座，籍贯安徽",
    ))
    assert parsed["height_cm"] == 165
    assert parsed["zodiac"] == "Gemini"
    assert parsed["gender"] is None


def test_an_empty_introduction_is_not_an_error() -> None:
    """`/en/name/{id}` returns exactly this: the same shape, no prose. 22 of 70
    sampled zhHans pages were also empty."""
    parsed = dramabox.parse_performer(_page(performerId="9", performerName="Peng Wang"))
    assert parsed["gender"] is None and parsed["zodiac"] is None
    assert parsed["han_name"] == "Peng Wang"


def test_implausible_measurements_are_dropped() -> None:
    """A parser reading a formulaic string will eventually read a malformed one.
    Serving an actor 8cm tall is worse than serving no height."""
    parsed = dramabox.parse_performer(_page(
        introduction="性别女，身高8cm，体重999kg，双鱼座",
    ))
    assert parsed["height_cm"] is None
    assert parsed["weight_kg"] is None
    assert parsed["gender"] == "female", "the sane fields still land"
    assert parsed["zodiac"] == "Pisces"


def test_every_sign_maps_to_english() -> None:
    """Twelve fixed terms, translated by lookup rather than by guessing."""
    assert len(dramabox.ZODIAC_EN) == 12
    for han, english in dramabox.ZODIAC_EN.items():
        parsed = dramabox.parse_performer(_page(introduction=f"性别男，{han}座"))
        assert parsed["zodiac"] == english


# --- the job ------------------------------------------------------------------


class _Ctx:
    state: dict = {}

    def cancelled(self) -> bool:
        return False

    def progress(self, *a, **k) -> None:
        pass


class _Module:
    KEY = "dramabox"
    parse_performer = staticmethod(dramabox.parse_performer)

    #: No preflight: the real module needs a rotating buildId, and this stub
    #: proves the job tolerates an adapter without one.
    @staticmethod
    def fetch_performer(ctx, performer_id):
        return _page(
            performerId=performer_id, performerName="王鹏",
            introduction="王鹏，性别男，身高188cm，天蝎座，曾参演《惊龙》等作品。",
        )


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="dramabox", base_url="https://a.test"))
        s.add(Person(id=1, name="Peng Wang", normalized_name="peng wang"))
        s.add(PersonSource(person_id=1, source_id=1, external_id="18356"))
        s.commit()
        yield s


def test_the_job_stores_facts_and_not_the_prose(session) -> None:
    """The whole point: the biography is Chinese, the reader is not. Storing
    text nobody can read is weight, not data."""
    stats = run_person_facts(_Module, session, _Ctx())

    person = session.get(Person, 1)
    assert person.gender == "male"
    assert person.height_cm == 188
    assert person.zodiac == "Scorpio"
    assert person.bio is None, "the Chinese prose is mined, never stored"
    assert stats["people_enriched"] == 1


def test_the_han_name_becomes_an_alias_not_the_name(session) -> None:
    """`person.name` is what search matches on, and the reader cannot type
    Chinese -- so the Han form is stored beside the name, never as it."""
    run_person_facts(_Module, session, _Ctx())

    assert session.get(Person, 1).name == "Peng Wang", "still romanised"
    alias = session.scalar(select(PersonAlias))
    assert alias.alias == "王鹏"
    assert alias.lang == "zh-Hans"
    assert alias.source_id == 1


def test_the_alias_survives_normalisation(session) -> None:
    """A normaliser that strips to ASCII would reduce a Han name to '' and the
    alias would be silently dropped -- this project has already shipped exactly
    that bug once, in `match_key`."""
    run_person_facts(_Module, session, _Ctx())
    assert session.scalar(select(PersonAlias)).normalized_alias == "王鹏"


def test_a_second_run_changes_nothing(session) -> None:
    """Converges rather than re-fetching the roster nightly, and does not pile
    up duplicate aliases."""
    run_person_facts(_Module, session, _Ctx())
    again = run_person_facts(_Module, session, _Ctx())

    assert again["considered"] == 1, "still missing weight, so still considered"
    assert again["aliases_added"] == 0
    assert len(session.scalars(select(PersonAlias)).all()) == 1


def test_existing_facts_are_never_overwritten(session) -> None:
    """A later page that omits a field must not blank what is already known."""
    session.get(Person, 1).gender = "female"
    session.commit()

    run_person_facts(_Module, session, _Ctx())

    assert session.get(Person, 1).gender == "female"


def test_a_tombstoned_person_is_skipped(session) -> None:
    """Merged rows own nothing and are never served; fetching pages for them
    spends a source's capacity on a row no reader can reach."""
    session.add(Person(id=2, name="Dupe", normalized_name="dupe", merged_into_id=1))
    session.add(PersonSource(person_id=2, source_id=1, external_id="99999"))
    session.commit()

    stats = run_person_facts(_Module, session, _Ctx())

    assert stats["considered"] == 1
    assert session.get(Person, 2).gender is None


def test_a_source_without_performer_pages_is_a_no_op(session) -> None:
    class _Bare:
        KEY = "goodshort"

    stats = run_person_facts(_Bare, session, _Ctx())
    assert stats["fetched"] == 0 and "note" in stats


# --- the API surface ----------------------------------------------------------


@pytest.fixture
def client(session):
    from fastapi.testclient import TestClient

    from app.api.auth import require_api_key
    from app.db import get_session
    from app.main import app

    run_person_facts(_Module, session, _Ctx())

    def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_api_key] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_person_detail_carries_the_facts(client) -> None:
    body = client.get("/people/1").json()
    assert body["gender"] == "male"
    assert body["height_cm"] == 188
    assert body["zodiac"] == "Scorpio"
    assert body["weight_kg"] is None, "absence is normal, not an error"
    assert body["bio"] is None


def test_person_detail_carries_the_alias_with_its_language(client) -> None:
    """Tagged rather than left for the consumer to detect from the characters."""
    aliases = client.get("/people/1").json()["aliases"]
    assert aliases == [{"alias": "王鹏", "lang": "zh-Hans", "source": "dramabox"}]


def test_search_matches_the_romanised_name(client) -> None:
    assert [p["id"] for p in client.get("/people", params={"q": "Peng"}).json()] == [1]


def test_search_does_not_match_the_han_alias(client) -> None:
    """Deliberate. The romanised name is the only form this index's reader can
    type, so it stays the single search key; the Han name is display data. If
    aliases ever become searchable this test should be changed knowingly, not
    discovered to have broken."""
    assert client.get("/people", params={"q": "王鹏"}).json() == []
