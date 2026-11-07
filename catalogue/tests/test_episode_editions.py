"""Which edition an episode belongs to, and the `?lang=` filter that needs it.

`episode` carried `title_id` and `source_id` and nothing more, which is enough
only while one platform lists a show once. It does not: 1,080 (title, source)
pairs already hold more than one link -- 501 DramaBox, 391 GoodShort, 188
ReelShort -- from dubbed twins and republishes, covering 92,026 rows.

That was merely redundant while every edition was English. ReelShort's Spanish
catalogue ends it: title 1065 answers `/episodes` with 236 rows numbered 1..118
twice, and `?source=` cannot split them because both editions ARE reelshort.
Language is a property of the LINK, so the row has to name its link.

The rule these tests defend is that an unknown edition is admitted, never
guessed. Handing the ambiguous rows to the oldest link would file Spanish
episodes under the English edition, and a `?lang=en` reader would get them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.episodes import tag_episode_editions
from app.ingest import ensure_source, ingest_record
from app.models import Base, Episode, TitleSource
from app.precedence import PLATFORM
from app.sources import reelshort
from app.sources.base import EpisodeRecord, SourceRecord


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


def _eps(prefix: str, n: int) -> list[EpisodeRecord]:
    return [EpisodeRecord(external_id=f"{prefix}{i}", number=i) for i in range(1, n + 1)]


#: The real pair. Same show, same source, different language, different book_id.
EN = SourceRecord(
    external_id="69d91b3ee15105112e0dc022", title="Bound by Love", lang="en",
    locale_key="428", episode_count=3, url="https://www.reelshort.com/movie/en-bound",
    synopsis="She married him to save her father.", episodes=_eps("en-", 3),
)
ES = SourceRecord(
    external_id="6a166376c040bd96890c71bd", title="Atada por amor", lang="es",
    locale_key="428", episode_count=3, url="https://www.reelshort.com/movie/es-atada",
    synopsis="Se casó con él para salvar a su padre.", episodes=_eps("es-", 3),
)


class _Module:
    """Stands in for an adapter. `parse_detail` is identity because what is
    under test is which link an episode is filed under, not the parser."""

    KEY = "reelshort"
    BASE_URL = "https://www.reelshort.com"

    @staticmethod
    def parse_detail(doc):
        return doc


class _Archive:
    """Mirrors the real `app.fetch.archive.Archive` surface used by the backfill.

    `iter_urls` is part of that surface: the backfill scans the archive to build
    an external_id -> URL index, because DramaBox's archive key
    (`/_next/data/<buildId>/drama/<id>/<slug>.json`) cannot be reconstructed
    from the pretty URL we store. A stub missing it would let that code path go
    untested, which is how the guess-the-URL version survived review.
    """

    def __init__(self, mapping: dict) -> None:
        self._m = mapping

    def get(self, url):
        return self._m.get(url)

    def iter_urls(self) -> list[str]:
        return list(self._m)


# --- ingest -------------------------------------------------------------------


def test_ingest_files_each_episode_under_its_own_edition(session) -> None:
    """The forward fix. Both editions are one title on one source, so nothing
    else in the row distinguishes them."""
    _put(session, EN)
    _put(session, ES)

    links = {l.lang: l.id for l in session.scalars(select(TitleSource))}
    filed = {
        (e.external_id, e.title_source_id) for e in session.scalars(select(Episode))
    }
    assert ("en-1", links["en"]) in filed
    assert ("es-1", links["es"]) in filed
    assert all(ts is not None for _, ts in filed), "no row left unattributed"


def test_a_resync_does_not_refile_an_episode(session) -> None:
    """A row already pointing at one link is left alone when the OTHER edition
    is re-ingested; otherwise the last sync to run would own every episode."""
    _put(session, EN)
    _put(session, ES)
    english = session.scalar(select(TitleSource).where(TitleSource.lang == "en")).id

    _put(session, ES)

    row = session.scalar(select(Episode).where(Episode.external_id == "en-1"))
    assert row.title_source_id == english


# --- the backfill -------------------------------------------------------------


def _orphan(session) -> None:
    """Simulate rows written before the column existed."""
    session.execute(text("UPDATE episode SET title_source_id = NULL"))
    session.commit()


def test_a_sole_link_claims_its_episodes_without_the_archive(session) -> None:
    """86.5% of real rows resolve this way: if a title+source pair holds exactly
    one link, that link published every episode under it. No adapter, no
    archive, and it cannot be wrong."""
    _put(session, EN)
    _orphan(session)

    stats = tag_episode_editions(session, dry_run=False)

    assert stats["phase1_assigned"] == 3
    assert stats["unassigned_after"] == 0


def test_a_contested_pair_is_settled_by_the_archive(session) -> None:
    """Two editions under one title+source: arithmetic cannot choose, so the
    payload each link was parsed from is re-read and the episode ids it lists
    are assigned to it."""
    _put(session, EN)
    _put(session, ES)
    _orphan(session)

    stats = tag_episode_editions(
        session,
        _Archive({EN.url: EN, ES.url: ES}),
        {"reelshort": _Module},
        dry_run=False,
    )

    assert stats["phase1_eligible"] == 0, "both links share the pair"
    assert stats["phase2_assigned"] == 6
    links = {l.lang: l.id for l in session.scalars(select(TitleSource))}
    spanish = session.scalars(
        select(Episode).where(Episode.title_source_id == links["es"])
    ).all()
    assert {e.external_id for e in spanish} == {"es-1", "es-2", "es-3"}


def test_an_episode_the_archive_cannot_place_stays_unknown(session) -> None:
    """The load-bearing refusal. With no archived payload for the Spanish link
    there is no evidence, so its rows keep a NULL edition rather than being
    handed to the English one."""
    _put(session, EN)
    _put(session, ES)
    _orphan(session)

    tag_episode_editions(
        session, _Archive({EN.url: EN}), {"reelshort": _Module}, dry_run=False
    )

    unplaced = session.scalars(
        select(Episode).where(Episode.title_source_id.is_(None))
    ).all()
    assert {e.external_id for e in unplaced} == {"es-1", "es-2", "es-3"}


def test_a_dry_run_writes_nothing(session) -> None:
    _put(session, EN)
    _orphan(session)

    stats = tag_episode_editions(session, dry_run=True)

    assert stats["phase1_eligible"] == 3
    assert stats["unassigned_after"] == 3, "counted, not written"


def test_running_it_twice_changes_nothing(session) -> None:
    """Every write is guarded on IS NULL, so a second pass assigns only what the
    first could not reach."""
    _put(session, EN)
    _put(session, ES)
    _orphan(session)
    archive, modules = _Archive({EN.url: EN, ES.url: ES}), {"reelshort": _Module}

    tag_episode_editions(session, archive, modules, dry_run=False)
    again = tag_episode_editions(session, archive, modules, dry_run=False)

    assert again["phase2_assigned"] == 0
    assert again["unassigned_after"] == 0


# --- the API ------------------------------------------------------------------


@pytest.fixture
def client(session):
    from fastapi.testclient import TestClient

    from app.api.auth import require_api_key
    from app.db import get_session
    from app.main import app

    _put(session, EN)
    _put(session, ES)

    def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_api_key] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _title_id(session) -> int:
    return session.scalar(select(TitleSource.title_id))


def test_without_a_filter_every_edition_is_returned(client, session) -> None:
    """Unchanged contract: filtering stays opt-in, because silently picking one
    edition hides episodes a consumer already fetched."""
    body = client.get(f"/titles/{_title_id(session)}/episodes").json()
    assert len(body) == 6
    assert sorted(e["lang"] for e in body) == ["en", "en", "en", "es", "es", "es"]


def test_lang_returns_only_that_edition(client, session) -> None:
    body = client.get(f"/titles/{_title_id(session)}/episodes", params={"lang": "es"}).json()
    assert [e["number"] for e in body] == [1, 2, 3]
    assert {e["lang"] for e in body} == {"es"}


def test_the_available_languages_are_advertised(client, session) -> None:
    """So a consumer can offer a language switch without fetching every episode
    and inferring the options -- the same reason X-Episode-Sources exists."""
    r = client.get(f"/titles/{_title_id(session)}/episodes")
    assert r.headers["X-Episode-Languages"] == "en,es"


def test_an_unavailable_language_says_what_there_is(client, session) -> None:
    r = client.get(f"/titles/{_title_id(session)}/episodes", params={"lang": "fr"})
    assert r.status_code == 404
    assert "'en', 'es'" in r.json()["detail"] or "en" in r.json()["detail"]


def test_editions_come_back_in_grouped_runs(client, session) -> None:
    """Ordering by number alone would interleave the languages, so a reader sees
    episode 1 twice before episode 2 -- which reads as corrupt data, exactly as
    it did when the platforms interleaved."""
    body = client.get(f"/titles/{_title_id(session)}/episodes").json()
    assert [e["lang"] for e in body] == ["en", "en", "en", "es", "es", "es"]


def test_an_untagged_episode_is_still_served(client, session) -> None:
    """A row the backfill could not place must never disappear from the
    unfiltered list: it is a real episode, and hiding it is the failure this
    endpoint's docstring exists to prevent."""
    session.execute(
        text("UPDATE episode SET title_source_id = NULL WHERE external_id = 'es-2'")
    )
    session.commit()

    body = client.get(f"/titles/{_title_id(session)}/episodes").json()
    assert len(body) == 6, "still all six"
    assert any(e["lang"] is None for e in body)


def test_an_untagged_episode_is_never_claimed_by_a_language(client, session) -> None:
    """The other half: it is unknown, not English."""
    session.execute(
        text("UPDATE episode SET title_source_id = NULL WHERE external_id = 'es-2'")
    )
    session.commit()

    for lang in ("en", "es"):
        body = client.get(
            f"/titles/{_title_id(session)}/episodes", params={"lang": lang}
        ).json()
        assert all(e["lang"] == lang for e in body)
    assert len(client.get(
        f"/titles/{_title_id(session)}/episodes", params={"lang": "es"}
    ).json()) == 2


# --- editions on the title detail ---------------------------------------------


def test_the_spanish_edition_is_exposed_with_its_language(client, session) -> None:
    """The gap this closes: both names already reached consumers through
    `aliases[]`, but as bare strings -- "Atada por amor" was indistinguishable
    from any other alias, so a Spanish household could SEARCH the Spanish name
    and still had to display the English one. The data was never missing, only
    the label."""
    body = client.get(f"/titles/{_title_id(session)}").json()
    editions = {e["lang"]: e for e in body["editions"]}

    assert set(editions) == {"en", "es"}
    assert editions["es"]["title"] == "Atada por amor"
    assert editions["en"]["title"] == "Bound by Love"


def test_each_edition_carries_its_own_synopsis(client, session) -> None:
    """The text a reader in that language actually reads. `TitleDetail.synopsis`
    is resolved by precedence and is in practice English, so without this a
    consumer knew the Spanish edition existed and still had only the English
    blurb."""
    editions = {e["lang"]: e for e in client.get(
        f"/titles/{_title_id(session)}").json()["editions"]}
    assert editions["es"]["synopsis"] == "Se casó con él para salvar a su padre."
    assert editions["en"]["synopsis"] == "She married him to save her father."


def test_each_edition_carries_its_own_watch_link(client, session) -> None:
    """A localised edition is a different id on the platform, so the English
    link would send a Spanish reader to the wrong page."""
    editions = {e["lang"]: e for e in client.get(
        f"/titles/{_title_id(session)}").json()["editions"]}
    assert editions["es"]["watch_url"] != editions["en"]["watch_url"]
    assert ES.external_id in editions["es"]["watch_url"]


def test_the_edition_precedence_chose_is_marked(client, session) -> None:
    """So a consumer can tell which one it is already showing."""
    editions = {e["lang"]: e for e in client.get(
        f"/titles/{_title_id(session)}").json()["editions"]}
    assert editions["en"]["is_primary"] is True
    assert editions["es"]["is_primary"] is False


def test_aliases_keep_their_shape(client, session) -> None:
    """Additive only. `aliases[]` stays a list of plain strings -- turning it
    into objects would break every consumer reading it today."""
    body = client.get(f"/titles/{_title_id(session)}").json()
    assert "Atada por amor" in body["aliases"]
    assert all(isinstance(a, str) for a in body["aliases"])


def test_a_single_language_title_reports_no_editions(session) -> None:
    """The ordinary case. Returning one self-referential entry for every title
    in the catalogue would make consumers filter noise on every request."""
    from fastapi.testclient import TestClient

    from app.api.auth import require_api_key
    from app.db import get_session
    from app.main import app

    _put(session, EN)

    def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_api_key] = lambda: None
    try:
        body = TestClient(app).get(f"/titles/{_title_id(session)}").json()
        assert body["editions"] == []
    finally:
        app.dependency_overrides.clear()
