"""GoodShort parsing, against recorded responses.

The site is the friendliest of the three -- server-rendered HTML with schema.org
markup -- which is exactly why the traps here are quiet ones. Every test below
pins something that looked correct and was not.
"""

from __future__ import annotations

import pytest

from app.fetch.types import Verdict
from app.sources import goodshort
from tests.conftest import load_fixture


# --- classification ----------------------------------------------------------


def test_a_browse_page_is_ok() -> None:
    assert goodshort.classify(load_fixture("goodshort_browse_p1")) is Verdict.OK


def test_a_detail_page_is_ok() -> None:
    assert goodshort.classify(load_fixture("goodshort_detail_dragon")) is Verdict.OK


def test_a_missing_drama_is_not_found_despite_a_full_page_body() -> None:
    """The 404 is a real 73KB HTML page. "The body is non-empty" proves nothing
    here, so status has to be read first."""
    raw = load_fixture("goodshort_missing")
    assert len(raw.body) > 50_000
    assert goodshort.classify(raw) is Verdict.NOT_FOUND


# --- discovery ---------------------------------------------------------------


def test_a_browse_page_yields_every_linked_drama() -> None:
    """The regression that matters most.

    The page carries a tidy schema.org ItemList AND 24 anchors, and the ItemList
    holds only 10 of them. Reading the structured data alone indexes 40% of the
    catalogue while reporting success -- no error, no empty result, just a
    quietly short catalogue.
    """
    records = goodshort.parse_listing(load_fixture("goodshort_browse_p1"))
    assert len(records) == 24


def test_the_last_browse_page_is_shorter_and_has_no_next() -> None:
    """How discovery knows to stop: the site's own rel=next, not a guessed
    page count."""
    raw = load_fixture("goodshort_browse_p218")
    assert len(goodshort.parse_listing(raw)) == 20
    assert goodshort.next_page_url(raw) is None


def test_a_browse_page_points_at_the_next_one() -> None:
    raw = load_fixture("goodshort_browse_p1")
    assert goodshort.next_page_url(raw) == (
        "https://www.goodshort.com/dramas/playlets?page=2"
    )


def test_listing_ids_are_unique_and_ordered() -> None:
    """Document order is newest-first and incremental discovery leans on it."""
    ids = [r.external_id for r in goodshort.parse_listing(load_fixture("goodshort_browse_p1"))]
    assert len(ids) == len(set(ids))
    assert ids[0] == "blood-and-bones-of-the-disowned-daughter-31001113972"


# --- identity ----------------------------------------------------------------


def test_the_external_id_keeps_the_slug() -> None:
    """GoodShort 404s an id-only URL -- verified against the live site for both
    `/drama/31001424670` and a wrong slug -- so the slug is load-bearing and the
    watch link cannot be rebuilt from the number alone."""
    assert goodshort.external_id_from_url(
        "https://www.goodshort.com/drama/dragon-tamer-31001424670"
    ) == "dragon-tamer-31001424670"


def test_a_query_string_does_not_leak_into_the_id() -> None:
    assert goodshort.external_id_from_url(
        "https://www.goodshort.com/drama/dragon-tamer-31001424670?utm_source=x"
    ) == "dragon-tamer-31001424670"


def test_a_non_drama_url_has_no_id() -> None:
    assert goodshort.external_id_from_url("https://www.goodshort.com/dramas/playlets") is None
    assert goodshort.external_id_from_url("https://www.goodshort.com/") is None


def test_the_watch_url_round_trips() -> None:
    from app.sources.base import watch_url

    record = goodshort.parse_detail(load_fixture("goodshort_detail_dragon"))
    built = watch_url(goodshort.WATCH_URL_TEMPLATE, record.external_id)
    assert built == "https://www.goodshort.com/drama/dragon-tamer-31001424670"


# --- detail ------------------------------------------------------------------


def test_detail_fields_come_from_the_tvseries_block() -> None:
    record = goodshort.parse_detail(load_fixture("goodshort_detail_dragon"))

    assert record.title == "Awakened as a Dragon Tamer After Dragon Extinction"
    assert record.episode_count == 68
    assert record.year == 2026
    assert record.poster_url.endswith(".jpg")
    assert "dragons" in (record.synopsis or "")
    assert record.tags == [("goodshort.genre", "Fantasy")]


def test_no_rating_is_taken_from_the_page() -> None:
    """The page's only aggregateRating is 4.5 from 622,044 reviews -- and it
    belongs to a SoftwareApplication block describing the GoodShort APP. It is
    identical on every drama, so reading it would stamp one invented score across
    the whole catalogue. Pinned because the field is right there and looks usable.
    """
    raw = load_fixture("goodshort_detail_dragon")
    blocks = goodshort.ld_blocks(raw)
    app_block = next(b for b in blocks if b.get("@type") == "SoftwareApplication")
    assert app_block["aggregateRating"]["ratingValue"] == "4.5"

    record = goodshort.parse_detail(raw)
    assert record.rating is None, "the app's store rating is not the drama's"


def test_the_page_carries_five_structured_blocks_and_we_pick_by_type() -> None:
    """Taking the first ld+json block would give TVSeries here by luck. Pinned so
    a page-order change does not silently start parsing a BreadcrumbList."""
    kinds = [b.get("@type") for b in goodshort.ld_blocks(load_fixture("goodshort_detail_dragon"))]
    assert kinds == ["TVSeries", "BreadcrumbList", "ImageObject", "ItemList",
                     "SoftwareApplication"]


def test_episode_rows_are_partial_and_the_count_is_not_derived_from_them() -> None:
    """Only the first handful of episodes are in the markup; the rest lazy-load.
    `numberOfEpisodes` is authoritative, so the count stays right even though the
    rows are incomplete -- they are separate facts."""
    record = goodshort.parse_detail(load_fixture("goodshort_detail_dragon"))

    assert record.episode_count == 68
    assert 0 < len(record.episodes) < record.episode_count
    assert [e.number for e in record.episodes[:3]] == [1, 2, 3]
    assert len({e.external_id for e in record.episodes}) == len(record.episodes)


def test_a_page_without_structured_data_is_refused() -> None:
    """Better to stop than to return an empty record that ingests as a title
    with no name."""
    raw = load_fixture("goodshort_missing")
    with pytest.raises(goodshort.NoStructuredData):
        goodshort.parse_detail(raw)


# --- the crawl path ----------------------------------------------------------
#
# The parsers were covered by fixtures from the first commit and every one passed
# while `discover` crashed on the real site: it called `ctx.fetcher.get(url)` and
# read `.status` off the result, but the fetcher returns a FetchResult wrapping
# the RawDoc. Fixture tests cannot see that, because they hand the parsers a
# RawDoc directly. These drive the crawl functions through a stub instead.


class _Result:
    def __init__(self, doc, ok=True):
        self.doc = doc
        self.ok = ok


class _Doc:
    """Minimal RawDoc stand-in for hand-built markup."""

    def __init__(self, body: bytes, url: str = "https://www.goodshort.com/x",
                 status: int = 200):
        self.body = body
        self.url = url
        self.status = status

    def header(self, name):
        return None


class _Fetcher:
    """Serves recorded pages and records what was asked for."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url, classify=None):
        self.requested.append(url)
        fixture = self.pages.get(url)
        if fixture is None:
            return _Result(load_fixture("goodshort_missing"), ok=False)
        doc = load_fixture(fixture)
        return _Result(doc, ok=classify is None or classify(doc) is Verdict.OK)


class _Ctx:
    def __init__(self, fetcher, max_items=0, watermark=None):
        self.fetcher = fetcher
        self.max_items = max_items
        self.watermark = watermark
        self.state: dict = {}
        self.watermarks: list[str] = []

    def set_watermark(self, value):
        self.watermarks.append(value)

    def progress(self, done, total=None, phase=None):
        pass

    def cancelled(self):
        return False


PAGES = {
    "https://www.goodshort.com/dramas/playlets": "goodshort_browse_p1",
    "https://www.goodshort.com/dramas/playlets?page=2": "goodshort_browse_p218",
}


def test_discover_walks_pages_and_yields_refs() -> None:
    ctx = _Ctx(_Fetcher(PAGES))
    refs = list(goodshort.discover(ctx, goodshort.Mode.FULL))

    assert len(refs) == 44, "both pages' dramas, deduplicated"
    assert all(r.url.startswith("https://www.goodshort.com/drama/") for r in refs)
    assert len({r.external_id for r in refs}) == 44


def test_discover_follows_rel_next_to_the_end_of_each_pass() -> None:
    """Pagination ends on the site's own rel=next, not a guessed page count."""
    fetcher = _Fetcher(PAGES)
    list(goodshort.discover(_Ctx(fetcher), goodshort.Mode.FULL))

    first_pass = fetcher.requested[:2]
    assert first_pass == [
        "https://www.goodshort.com/dramas/playlets",
        "https://www.goodshort.com/dramas/playlets?page=2",
    ]


def test_a_full_run_repeats_passes_until_nothing_new_appears() -> None:
    """The listing is non-deterministic -- page 5 fetched twice shared 12 of 24
    ids -- so one pass samples the catalogue instead of enumerating it. The first
    real crawl got 2,596 unique titles out of 5,232 page slots and reported
    success. A full run repeats until a whole pass adds nothing.
    """
    fetcher = _Fetcher(PAGES)
    list(goodshort.discover(_Ctx(fetcher), goodshort.Mode.FULL))

    assert len(fetcher.requested) == 4, "one productive pass, then one that saturates"


def test_an_incremental_run_does_a_single_pass() -> None:
    """Incremental only needs what is new; the next full run closes any sampling
    gap, and re-walking 218 pages nightly for that would be rude."""
    fetcher = _Fetcher(PAGES)
    list(goodshort.discover(_Ctx(fetcher), goodshort.Mode.INCREMENTAL))

    assert len(fetcher.requested) == 2


def test_saturation_collects_what_one_pass_misses() -> None:
    """A listing that shows half its catalogue per fetch must still be fully
    collected."""

    class _Shifting:
        """Page 1 alternates between two disjoint halves; no rel=next."""

        def __init__(self):
            self.calls = 0
            self.requested = []

        def get(self, url, classify=None):
            self.requested.append(url)
            self.calls += 1
            half = "ab" if self.calls % 2 else "cd"
            links = "".join(
                f'<a href="/drama/show-{c}-{i}00001"></a>' for i, c in enumerate(half)
            )
            body = f"<html>{links}<script type=\"application/ld+json\">{{}}</script></html>"
            doc = _Doc(body.encode())
            return _Result(doc, ok=True)

    fetcher = _Shifting()
    refs = list(goodshort.discover(_Ctx(fetcher), goodshort.Mode.FULL))

    assert {r.external_id for r in refs} == {
        "show-a-000001", "show-b-100001", "show-c-000001", "show-d-100001",
    }, "both halves collected across passes"


def test_discover_honours_max_items() -> None:
    """The capped first run every source gets."""
    ctx = _Ctx(_Fetcher(PAGES), max_items=10)
    refs = list(goodshort.discover(ctx, goodshort.Mode.FULL))

    assert len(refs) == 10
    assert ctx.watermarks, "a capped run still records where it got to"


def test_discover_records_the_newest_id_as_the_watermark() -> None:
    ctx = _Ctx(_Fetcher(PAGES))
    refs = list(goodshort.discover(ctx, goodshort.Mode.FULL))

    assert ctx.watermarks[-1] == refs[0].external_id


def test_fetch_detail_returns_the_document_not_the_result() -> None:
    """The exact crash: `.doc` is what the parsers take."""
    fetcher = _Fetcher({"https://www.goodshort.com/drama/dragon-tamer-31001424670":
                        "goodshort_detail_dragon"})
    ref = goodshort.Ref(external_id="dragon-tamer-31001424670",
                        url="https://www.goodshort.com/drama/dragon-tamer-31001424670")

    doc = goodshort.fetch_detail(_Ctx(fetcher), ref)

    assert goodshort.parse_detail(doc).episode_count == 68


def test_discover_stops_when_a_page_is_not_ok() -> None:
    """A challenge or an error ends the walk rather than being parsed as empty."""
    ctx = _Ctx(_Fetcher({}))
    assert list(goodshort.discover(ctx, goodshort.Mode.FULL)) == []


# --- politeness --------------------------------------------------------------


def test_the_search_path_is_treated_as_forbidden() -> None:
    """robots.txt disallows /results, which is the search endpoint -- so this
    source cannot support on-demand "index this title by name" either."""
    assert "/results" in goodshort.FORBIDDEN_PATHS


def test_the_declared_crawl_paths_are_all_permitted_by_robots() -> None:
    """robots_check asks this question live; here it is pinned against the
    recorded robots.txt so a rule change shows up as a failing test too."""
    from protego import Protego

    robots = Protego.parse(
        load_fixture("goodshort_robots").body.decode("utf-8", "replace")
    )
    for path in goodshort.CRAWL_PATHS:
        assert robots.can_fetch(goodshort.BASE_URL + path, "DramadexBot"), path

    for path in goodshort.FORBIDDEN_PATHS:
        assert not robots.can_fetch(goodshort.BASE_URL + path, "DramadexBot"), path
