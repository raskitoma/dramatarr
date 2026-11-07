"""ReelShort's per-language catalogues.

The crawler walked exactly one tree -- the English one -- while ReelShort runs
twelve, and gives the same show a DIFFERENT book_id in each. So a Spanish title
was not missed, it was never a candidate: "Atada por amor"
(6a166376c040bd96890c71bd) and "Bound by Love" (69d91b3ee15105112e0dc022) share
no id and no words.

Linking editions is only allowed here if the SOURCE publishes the link, since
this codebase refuses identity by name. It does: `t_book_id` is
`<locale prefix><serial>` and the serial is shared across editions. Measured over
12 listing pages per locale -- 0/2/1 serial collisions within en/es/pt, 77 shared
en-es serials and 79 en-pt, every joined pair a correct translation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ingest import ensure_source, ingest_record
from app.models import Base, Title, TitleAlias, TitleSource
from app.precedence import PLATFORM
from app.sources import reelshort
from app.sources.base import SourceRecord


def test_the_serial_is_shared_across_editions() -> None:
    """The real ids: English 200000…428, Spanish 300000…428."""
    assert reelshort.locale_serial("200000000000000428") == "428"
    assert reelshort.locale_serial("300000000000000428") == "428"
    # The edition prefix is not a single digit -- Spanish uses both 300000 and
    # 300001 -- so only the trailing serial may be compared.
    assert reelshort.locale_serial("300001000000000355") == "355"
    assert reelshort.locale_serial("200000000000000355") == "355"


def test_rubbish_yields_no_key() -> None:
    """A None key must fall through to the ordinary path, never match everything."""
    for value in (None, "", "abc", "123", 42):
        assert reelshort.locale_serial(value) is None


def test_each_locale_has_its_own_tree() -> None:
    assert reelshort.browse_url(2) == "https://www.reelshort.com/tags/mc-types/2"
    assert reelshort.browse_url(2, "es") == "https://www.reelshort.com/es/tags/mc-types/2"
    assert "" in reelshort.LOCALES, "the English tree must always be crawled"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


def _put(session, record):
    source, studio = ensure_source(
        session, reelshort.KEY, reelshort.STUDIO_NAME, reelshort.BASE_URL,
        reelshort.WATCH_URL_TEMPLATE, kind=PLATFORM,
    )
    out = ingest_record(session, source, studio, record)
    session.commit()
    return out


EN = SourceRecord(external_id="69d91b3ee15105112e0dc022", title="Bound by Love",
                  episode_count=118, lang="en", locale_key="428")
ES = SourceRecord(external_id="6a166376c040bd96890c71bd", title="Atada por amor",
                  episode_count=118, lang="es", locale_key="428")


def test_an_edition_joins_the_title_it_translates(session) -> None:
    """Not a second title. The whole point of the source-published key."""
    _put(session, EN)
    _put(session, ES)

    titles = session.scalars(select(Title)).all()
    assert len(titles) == 1, "one show, two editions"
    links = session.scalars(select(TitleSource)).all()
    assert {l.external_id for l in links} == {EN.external_id, ES.external_id}


def test_the_edition_keeps_its_own_watch_link(session) -> None:
    """An alias alone would make the Spanish version findable but not watchable,
    which is the half that mattered to the person who reported it missing."""
    _put(session, EN)
    _put(session, ES)

    spanish = session.scalar(
        select(TitleSource).where(TitleSource.external_id == ES.external_id)
    )
    assert spanish.lang == "es"
    assert spanish.locale_key == "428"


def test_the_localised_name_becomes_an_alternate_title(session) -> None:
    _put(session, EN)
    _put(session, ES)

    alias = session.scalar(
        select(TitleAlias).where(TitleAlias.normalized_alias == "atada por amor")
    )
    assert alias is not None
    assert alias.kind == "localized"
    assert alias.region == "es"


def test_the_english_title_stays_the_headline(session) -> None:
    """Editions are alternates, not competitors: the entry must still read as one
    show rather than flipping language with whichever tree synced last."""
    _put(session, EN)
    _put(session, ES)

    assert session.scalar(select(Title)).primary_title == "Bound by Love"


def test_order_does_not_matter(session) -> None:
    """Whichever language is crawled first, the second joins it -- the same
    property cross-source matching already relies on."""
    _put(session, ES)
    _put(session, EN)

    assert len(session.scalars(select(Title)).all()) == 1


def test_a_different_show_is_not_absorbed(session) -> None:
    """A distinct serial must stay a distinct title, or the key would be worse
    than no key at all."""
    _put(session, EN)
    _put(session, SourceRecord(external_id="deadbeef", title="Otra historia",
                               episode_count=60, lang="es", locale_key="999"))

    assert len(session.scalars(select(Title)).all()) == 2


def test_a_record_without_a_key_uses_the_ordinary_path(session) -> None:
    """Every pre-existing row has no locale_key, so a null must never match."""
    _put(session, EN)
    _put(session, SourceRecord(external_id="nokey", title="Something Else",
                               episode_count=10))

    assert len(session.scalars(select(Title)).all()) == 2
