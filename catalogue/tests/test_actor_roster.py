"""Actor roster ingestion.

Cast from detail pages covers only ~20% of ReelShort titles, so the curated
roster is the other half. The property that matters is the JOIN: roster entries
and detail-page cast must resolve to one person via an identifier both sides
publish, never via name similarity.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ingest import ensure_source, ingest_record
from app.jobs.actor_roster import run_actor_roster
from app.models import Base, Person, PersonSource, TitlePerson
from app.sources import dramabox, reelshort
from tests.conftest import load_fixture


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/a.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


class FakeCtx:
    """Serves the recorded roster and actor pages; no network."""

    def __init__(self, roster_doc, actor_doc):
        self.max_items = 0
        self.watermark = None
        self.state: dict = {}
        self._roster = roster_doc
        self._actor = actor_doc
        self.fetched = 0

    def cancelled(self) -> bool:
        return False

    def progress(self, *a, **k) -> None:
        pass


class FakeModule:
    """The reelshort adapter with its two network calls stubbed."""

    KEY = reelshort.KEY
    STUDIO_NAME = reelshort.STUDIO_NAME
    BASE_URL = reelshort.BASE_URL
    WATCH_URL_TEMPLATE = reelshort.WATCH_URL_TEMPLATE
    parse_actor_page = staticmethod(reelshort.parse_actor_page)

    def __init__(self, roster_doc, actor_doc):
        self._roster_doc = roster_doc
        self._actor_doc = actor_doc

    def roster(self, ctx):
        return reelshort.parse_roster(self._roster_doc)

    def fetch_actor(self, ctx, ref):
        ctx.fetched += 1
        return self._actor_doc


@pytest.fixture
def module():
    return FakeModule(
        load_fixture("reelshort/fandom_roster"), load_fixture("reelshort/fandom_actor")
    )


@pytest.fixture
def ctx(module):
    return FakeCtx(module._roster_doc, module._actor_doc)


# --- parsing -----------------------------------------------------------------


def test_the_roster_page_yields_every_actor() -> None:
    refs = reelshort.parse_roster(load_fixture("reelshort/fandom_roster"))
    assert len(refs) == 229
    assert all(r.name and r.external_id.startswith("https://") for r in refs)


def test_roster_urls_are_harvested_not_synthesised() -> None:
    """Slugs are irregular -- `adam-huss-01-6-3faa2218`,
    `reelshort-actor-aaron-oberst-01-6`, `adam-daniel-actor-01-6`, and at least
    one typo. Any attempt to construct them would miss most of the roster."""
    refs = reelshort.parse_roster(load_fixture("reelshort/fandom_roster"))
    slugs = [r.external_id.split("/fandom/")[1] for r in refs]

    assert any(s.startswith("reelshort-actor-") for s in slugs)
    assert any(s.endswith("-01-6") for s in slugs)
    assert any("-actor-" in s for s in slugs)
    # No single pattern covers them all.
    assert len({s.count("-") for s in slugs}) > 2


def test_an_actor_page_yields_a_bio_and_a_filmography() -> None:
    profile = reelshort.parse_actor_page(load_fixture("reelshort/fandom_actor"))
    assert profile["book_ids"] == [
        "673c6358eb602c35440e014a",
        "677a0db90fc9407e14077c4c",
        "677f61d34c0424fd9a082408",
    ]
    assert profile["bio"] and len(profile["bio"]) > 40


def test_an_actor_page_yields_a_headshot() -> None:
    """The one place ReelShort publishes a face for people its detail-page cast
    never mentions. 113 of its 250 actors had no photo purely because the
    og:image on the page we were ALREADY fetching went unread."""
    profile = reelshort.parse_actor_page(load_fixture("reelshort/fandom_actor"))
    assert profile["avatar_url"] == (
        "https://www.reelshort.com/fandom/wp-content/uploads/2025/02/"
        "aaron-oberst-chiropractic.jpg"
    )


def test_the_longest_description_wins() -> None:
    """Yoast emits several. Taking the FIRST and testing its length yielded no
    bio at all whenever a short site description came first, which left 13
    actors bio-less while holding a page that contained one."""
    doc = load_fixture("reelshort/fandom_actor")
    descriptions = reelshort._LD_DESCRIPTION.findall(doc.text())
    assert len(descriptions) > 1, "the fixture must actually exercise the choice"

    profile = reelshort.parse_actor_page(doc)
    assert profile["bio"] == max(
        (json.loads(f'"{d}"') for d in descriptions), key=len
    ).strip()


def test_a_page_with_no_image_reports_none() -> None:
    """A missing headshot is normal and must stay None rather than becoming a
    string, since `avatar_fetch` treats any non-empty URL as fetchable."""
    from app.fetch.types import RawDoc

    doc = RawDoc(url="https://www.reelshort.com/fandom/x", status=200, headers={},
                 body=b"<html><head><title>x</title></head><body></body></html>")
    assert reelshort.parse_actor_page(doc)["avatar_url"] is None


def test_a_relative_image_is_refused() -> None:
    """The fetcher resolves nothing: a relative src would be requested verbatim
    and 404 on every run."""
    from app.fetch.types import RawDoc

    doc = RawDoc(url="https://www.reelshort.com/fandom/x", status=200, headers={},
                 body=b'<meta property="og:image" content="/uploads/a.jpg" />')
    assert reelshort.parse_actor_page(doc)["avatar_url"] is None


def test_key_art_is_never_mistaken_for_a_face() -> None:
    """og:image is a generic tag: on a DRAMA page it is the cover art, served
    from a different host entirely. Accepting any absolute URL would store that
    as an actor's headshot -- a wrong face is far worse than no face, and
    nothing downstream would flag it."""
    doc = load_fixture("reelshort/detail_ok")
    assert "og:image" in doc.text(), "the fixture must actually carry one"
    assert reelshort.parse_actor_page(doc)["avatar_url"] is None


def test_the_bio_survives_utf8_intact() -> None:
    """A unicode_escape round-trip turns a curly apostrophe into mojibake."""
    profile = reelshort.parse_actor_page(load_fixture("reelshort/fandom_actor"))
    assert "’" in profile["bio"]
    assert "â€" not in profile["bio"]


# --- ingestion ---------------------------------------------------------------


def test_the_roster_creates_people(session, module, ctx) -> None:
    stats = run_actor_roster(module, session, ctx, fetch_profiles=False)

    assert stats["listed"] == 229
    assert stats["people_created"] == 229
    assert session.query(Person).count() == 229


def test_running_twice_creates_nobody_twice(session, module, ctx) -> None:
    run_actor_roster(module, session, ctx, fetch_profiles=False)
    second = run_actor_roster(module, session, ctx, fetch_profiles=False)

    assert second["people_created"] == 0
    assert second["people_linked"] == 229
    assert session.query(Person).count() == 229


def test_roster_and_detail_page_cast_merge_into_one_person(session, module, ctx) -> None:
    """The join that matters.

    An actor scraped from a title detail page carries `inside_url` as their
    external_id; the roster publishes the same URL. They must resolve to ONE
    person -- not two rows that a later name match has to reconcile.
    """
    source, studio = ensure_source(
        session, reelshort.KEY, reelshort.STUDIO_NAME,
        reelshort.BASE_URL, reelshort.WATCH_URL_TEMPLATE,
    )
    record = reelshort.parse_detail(load_fixture("reelshort/detail_ok"))
    ingest_record(session, source, studio, record)
    session.commit()

    from_cast = session.query(Person).count()
    assert from_cast > 0

    # Those cast rows use the fandom URL as their id, minus the trailing slash
    # difference the roster parser normalises away.
    cast_ids = {
        ps.external_id.rstrip("/")
        for ps in session.scalars(select(PersonSource))
    }
    roster_ids = {r.external_id for r in reelshort.parse_roster(module._roster_doc)}
    assert cast_ids & roster_ids, "cast and roster must share identifiers"

    run_actor_roster(module, session, ctx, fetch_profiles=False)

    # Every shared identifier resolved to the existing person rather than a
    # duplicate: total is roster size plus only the cast members not listed.
    duplicates = (
        session.query(PersonSource)
        .filter(PersonSource.external_id.in_(list(cast_ids & roster_ids)))
        .count()
    )
    assert duplicates == len(cast_ids & roster_ids)


def test_profiles_add_bios_and_credits(session, module, ctx) -> None:
    source, studio = ensure_source(
        session, reelshort.KEY, reelshort.STUDIO_NAME,
        reelshort.BASE_URL, reelshort.WATCH_URL_TEMPLATE,
    )
    # A title the fixture actor appears in.
    from app.sources.base import SourceRecord

    ingest_record(
        session, source, studio,
        SourceRecord(external_id="673c6358eb602c35440e014a", title="Daddy December"),
    )
    session.commit()

    stats = run_actor_roster(module, session, ctx, fetch_profiles=True)

    assert stats["profiles_fetched"] == 229
    assert stats["bios"] > 0
    assert stats["credits_created"] > 0
    assert session.query(TitlePerson).count() > 0


def test_a_filmography_entry_we_do_not_hold_is_not_an_error(session, module, ctx) -> None:
    """The roster covers the whole catalogue while our ingest may be capped."""
    stats = run_actor_roster(module, session, ctx, fetch_profiles=True)
    assert stats["unmatched_titles"] > 0
    assert stats["credits_created"] == 0  # nothing ingested to link to


def test_dramabox_reports_that_it_has_no_roster(session) -> None:
    """It publishes no actor index, and probing /name/{id} would take roughly
    19,000 requests. Its people come from performerList during a normal sync."""
    class Ctx:
        def cancelled(self): return False
        def progress(self, *a, **k): pass

    stats = run_actor_roster(dramabox, session, Ctx())
    assert stats["listed"] == 0
    assert "no actor roster" in stats["note"]


# --- bios for people the roster never lists ----------------------------------


def test_bio_backfill_covers_cast_the_roster_misses(tmp_path) -> None:
    """The gap this closes.

    The curated A-Z page lists ~229 actors; detail pages credit more, and those
    people arrive via `actor_info.actors[].inside_url` with a fandom URL as their
    external_id. Nothing ever fetched their profile, so 137 of 358 ReelShort
    people had no bio while 220 did.
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.jobs.actor_roster import _backfill_missing_bios
    from app.models import Base, Person, PersonSource, Source

    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)

    class _Module:
        KEY = "reelshort"

        @staticmethod
        def fetch_actor(ctx, ref):
            return ref.external_id  # the stub parser reads this straight back

        @staticmethod
        def parse_actor_page(doc):
            slug = doc.rsplit('/', 1)[-1]
            return {"bio": f"A biography for {slug}",
                    "avatar_url": f"https://img.test/{slug}.jpg", "book_ids": []}

    class _Ctx:
        def cancelled(self):
            return False

        def progress(self, *a, **k):
            pass

    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="reelshort", base_url="https://x.test"))
        # one person with a bio already, one without, one whose id is not a URL
        for pid, name, ext, bio in (
            (1, "Has Bio", "https://www.reelshort.com/fandom/has-bio", "already written"),
            (2, "No Bio", "https://www.reelshort.com/fandom/no-bio", None),
            (3, "Not A Url", "12345", None),
        ):
            s.add(Person(id=pid, name=name, normalized_name=name.lower(), bio=bio))
            s.add(PersonSource(person_id=pid, source_id=1, external_id=ext))
        s.commit()

        stats = _backfill_missing_bios(_Module, s, _Ctx(), source_id=1)
        s.commit()

        # Two, not one: person 1 already has a bio but still has no headshot, and
        # the headshot lives on the same page. Considering only the bio-less left
        # 113 of ReelShort's 250 actors permanently faceless.
        assert stats["bio_backfill_considered"] == 2
        assert stats["bio_backfill_bios"] == 1, "only the bio-less one gains a bio"
        assert stats["bio_backfill_avatars"] == 2, "both gain a headshot"
        assert s.get(Person, 1).bio == "already written", "an existing bio is not overwritten"
        assert "no-bio" in s.get(Person, 2).bio
        assert s.get(Person, 3).bio is None, "a non-URL id has no profile page to fetch"

        links = {
            l.external_id: l.avatar_url
            for l in s.scalars(select(PersonSource)).all()
        }
        assert links["https://www.reelshort.com/fandom/has-bio"] == "https://img.test/has-bio.jpg"
        assert links["12345"] is None, "never fetched, so nothing recorded"


def test_bio_backfill_survives_one_bad_profile(tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.jobs.actor_roster import _backfill_missing_bios
    from app.models import Base, Person, PersonSource, Source

    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)

    class _Module:
        KEY = "reelshort"

        @staticmethod
        def fetch_actor(ctx, ref):
            raise RuntimeError("502 from the fandom host")

        @staticmethod
        def parse_actor_page(doc):
            raise AssertionError("never reached")

    class _Ctx:
        def cancelled(self):
            return False

        def progress(self, *a, **k):
            pass

    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="reelshort", base_url="https://x.test"))
        s.add(Person(id=1, name="X", normalized_name="x"))
        s.add(PersonSource(person_id=1, source_id=1,
                           external_id="https://www.reelshort.com/fandom/x"))
        s.commit()

        stats = _backfill_missing_bios(_Module, s, _Ctx(), source_id=1)

        assert stats["bio_backfill_failed"] == 1
        assert stats["bio_backfill_bios"] == 0
