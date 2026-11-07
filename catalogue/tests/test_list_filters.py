"""Filtered listing must not lose rows to its own JOIN.

`?studio=netshort` returned 5,925 of 6,223 matching titles on the live
catalogue -- 298 missing, 4.8% -- with a cleanly exhausted cursor and no error.
Nothing looked wrong from outside, which is why a consumer found it by counting
against the database rather than by anything failing.

The mechanism: the filter JOINed `title_source`, which emits one row per matching
link. Keyset pagination applies LIMIT to ROWS and dedupes afterwards, so a window
of `limit + 1` rows containing any duplicate collapses to `<= limit` titles, the
"is there another page?" test reads that as the end of the scan, and the entire
remaining tail is dropped. 399 NetShort titles carry two links; that was enough.

The same shape was latent on `?genre=` and `?person=` -- a title credited to two
actors who both match one substring duplicates identically.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Person, Source, Studio, Title, TitlePerson, TitleSource


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/f.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Source(id=1, key="netshort", base_url="https://netshort.com",
                     watch_url_template="https://netshort.com/full-episodes/{external_id}"))
        s.add(Studio(id=1, source_id=1, name="NetShort", slug="netshort"))
        # Every title carries TWO links from the one source -- the `（DUBBED）`
        # twin case, which is 399 titles on the live catalogue.
        for i in range(1, 11):
            s.add(Title(id=i, primary_title=f"Show {i}",
                        normalized_title=f"show {i}", identity_key=f"show{i}"))
            s.add(TitleSource(title_id=i, source_id=1, studio_id=1,
                              external_id=f"{i}-a", lang="en"))
            s.add(TitleSource(title_id=i, source_id=1, studio_id=1,
                              external_id=f"{i}-b", lang="en"))
        s.commit()
        yield s


@pytest.fixture
def client(session):
    from fastapi.testclient import TestClient

    from app.api.auth import require_api_key
    from app.db import get_session
    from app.main import app

    def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_api_key] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _walk(client, **params) -> list[int]:
    """Follow the cursor to exhaustion, exactly as a consumer inventorying does."""
    seen: list[int] = []
    cursor = None
    for _ in range(50):  # guard against a cursor that never terminates
        page = client.get("/titles", params={**params, "cursor": cursor} if cursor
                          else params).json()
        seen.extend(t["id"] for t in page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return seen


def test_a_studio_scan_returns_every_matching_title(client) -> None:
    """The regression. Ten titles, each with two links, a page size that forces
    several pages: every title must come back exactly once."""
    ids = _walk(client, studio="netshort", limit=3)

    assert sorted(ids) == list(range(1, 11)), "no title may be dropped"
    assert len(ids) == len(set(ids)), "and none duplicated"


def test_the_scan_does_not_stop_early(client) -> None:
    """The precise failure: duplicates consumed row slots, the page deduped to
    `<= limit`, and pagination declared itself finished with the tail unread."""
    first = client.get("/titles", params={"studio": "netshort", "limit": 3}).json()
    assert first["next_cursor"], "a full first page must advertise another page"
    assert len(first["items"]) == 3


def test_a_person_matching_twice_does_not_truncate(session, client) -> None:
    """Latent variant: two credited actors both matching one `?person=`
    substring duplicate the title exactly as two source links do."""
    for pid, name in ((1, "Li Wei"), (2, "Li Ming")):
        session.add(Person(id=pid, name=name, normalized_name=name.lower()))
    for i in range(1, 11):
        session.add(TitlePerson(title_id=i, person_id=1, source_id=1, role="actor"))
        session.add(TitlePerson(title_id=i, person_id=2, source_id=1, role="actor"))
    session.commit()

    ids = _walk(client, person="Li", limit=3)
    assert sorted(ids) == list(range(1, 11))


def test_a_short_page_never_claims_to_be_the_last(client, session) -> None:
    """The lie, which is worse than the undercount and is a separate failure.

    Counting rows is not enough: a page can return the right rows and still end
    the scan early. Measured live before the fix, `?person=Marc Herrmann` against
    28 distinct titles:

        limit=5  -> 4 items, next_cursor: null
        limit=10 -> 6 items, next_cursor: null
        limit=25 -> 14 items, next_cursor: null

    Half the results, and the API stating there were no more. A consumer
    paginating correctly had no way to notice -- which is exactly how it went
    unnoticed in a user-facing actor search.

    So the invariant is not "a full page has a cursor". It is: a page returning
    fewer than `limit` items must be genuinely exhausted -- asking again from
    that point must yield nothing.
    """
    from app.models import Person, TitlePerson

    for pid, name in ((1, "Marc Herrmann"), (2, "Marc Herrmannsen")):
        session.add(Person(id=pid, name=name, normalized_name=name.lower()))
    # Every title credits BOTH matching people: the dense-duplicate case, where
    # the window collapses on every page rather than occasionally.
    for i in range(1, 11):
        session.add(TitlePerson(title_id=i, person_id=1, source_id=1, role="actor"))
        session.add(TitlePerson(title_id=i, person_id=2, source_id=1, role="actor"))
    session.commit()

    for limit in (3, 5, 7):
        page = client.get("/titles", params={"person": "Marc Herrmann",
                                             "limit": limit}).json()
        if page["next_cursor"] is None:
            # It claims to be finished. Prove it: everything must be here.
            assert len(page["items"]) == 10, (
                f"limit={limit} returned {len(page['items'])} of 10 and reported "
                "no further pages"
            )
        else:
            assert len(page["items"]) == limit, "a page with a cursor must be full"


def test_the_walk_reaches_everything_however_it_is_paged(client, session) -> None:
    """Page size must not change the answer."""
    from app.models import Person, TitlePerson

    session.add(Person(id=1, name="Marc Herrmann", normalized_name="marc herrmann"))
    session.add(Person(id=2, name="Marc Herrmannsen", normalized_name="marc herrmannsen"))
    for i in range(1, 11):
        session.add(TitlePerson(title_id=i, person_id=1, source_id=1, role="actor"))
        session.add(TitlePerson(title_id=i, person_id=2, source_id=1, role="actor"))
    session.commit()

    for limit in (1, 2, 3, 5, 25):
        assert sorted(_walk(client, person="Marc Herrmann", limit=limit)) == \
            list(range(1, 11)), f"page size {limit} changed the result set"


# --- review queue ordering ----------------------------------------------------


def test_the_review_queue_ranks_likely_duplicates_first() -> None:
    """A RANKING, not a rule -- it reorders what a human sees and hides nothing.

    Hand-adjudicated by the dramatarr session: where exactly one member of a pair
    publishes a year, the year-less member having MORE episodes went with a
    genuine duplicate 9 times in 11, against 27% among mere title collisions.
    """
    from app.api.admin import _merge_rank
    from app.models import Title

    def t(year, eps):
        return Title(primary_title="x", normalized_title="x", year=year, episode_count=eps)

    # netshort-shaped (no year) with MORE episodes -> look first
    assert _merge_rank(t(None, 52), t(2026, 35)) == 0
    assert _merge_rank(t(2026, 35), t(None, 52)) == 0, "order of the pair must not matter"

    # the two real counterexamples: year-less side has FEWER. Ranked later --
    # NOT rejected, because both are genuine duplicates a human must still see.
    assert _merge_rank(t(None, 40), t(2026, 50)) == 1
    assert _merge_rank(t(None, 47), t(2026, 67)) == 1


def test_the_rank_abstains_when_the_signal_does_not_apply() -> None:
    """Both years known, neither known, or a missing count: no opinion."""
    from app.api.admin import _merge_rank
    from app.models import Title

    def t(year, eps):
        return Title(primary_title="x", normalized_title="x", year=year, episode_count=eps)

    assert _merge_rank(t(2026, 60), t(2025, 61)) == 1, "both dated: signal undefined"
    assert _merge_rank(t(None, 60), t(None, 61)) == 1, "neither dated: signal undefined"
    assert _merge_rank(t(None, None), t(2026, 61)) == 1, "no count to compare"
