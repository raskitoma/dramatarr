"""NetShort discovery, driven offline through a stub fetcher.

Written after a review found four defects in `discover` that the 31 parser tests
could not see, because every one of them tested a pure function. All four were
reproducible in seconds with the stub below -- the technique the suite already
used elsewhere -- which is the whole reason they shipped:

  * the watermark stored the OLDEST id, publishing the wrong progress marker;
  * genre pages were collected for English only, though LOCALES admits es and pt;
  * genre pagination was never followed, hiding 2/3 to 6/7 of every genre from
    the one route that exists to find what the sitemaps omit;
  * `is_detail_url`/`is_listing_url` were missing entirely, so `reparse` -- the
    documented recovery path -- died with AttributeError before parsing anything.
"""

from __future__ import annotations

import pytest

from app.fetch.types import RawDoc
from app.sources import netshort
from app.sources.base import Mode

SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex>
<sitemap><loc>https://netshort.com/site_play_1.xml</loc></sitemap>
<sitemap><loc>https://netshort.com/site_allsearch_1.xml</loc></sitemap>
<sitemap><loc>https://netshort.com/site_blog_1.xml</loc></sitemap>
</sitemapindex>"""

#: The shape the sitemaps ACTUALLY have. They stopped publishing
#: /full-episodes/ URLs entirely -- every entry is an episode, and an episode
#: URL carries its TITLE's id. Mixed locales, plus one bare (episode 1) URL,
#: which has no -ep-N suffix at all.
SITE_PLAY = """<?xml version="1.0"?><urlset>
<url><loc>https://netshort.com/episode/alpha-1000000000000000001-ep-2</loc></url>
<url><loc>https://netshort.com/episode/alpha-1000000000000000001-ep-9</loc></url>
<url><loc>https://netshort.com/episode/beta-1000000000000000004</loc></url>
<url><loc>https://netshort.com/es/episode/alfa-1000000000000000009-ep-3</loc></url>
<url><loc>https://netshort.com/pt/episode/alfa-1000000000000000007-ep-5</loc></url>
<url><loc>https://netshort.com/ja/episode/x-1000000000000000002-ep-2</loc></url>
<url><loc>https://netshort.com/ko/episode/x-1000000000000000003-ep-2</loc></url>
</urlset>"""

#: 200 of these exist and none carries a title id. Fetching them is pure waste.
SITE_ALLSEARCH = """<?xml version="1.0"?><urlset>
<url><loc>https://netshort.com/shortsearch/romance-drama</loc></url>
<url><loc>https://netshort.com/shortsearch/ceo-revenge</loc></url>
</urlset>"""

#: Route (b) is seeded from here, NOT from the sitemap index. The ENGLISH root
#: publishes only unprefixed links -- which is exactly why seeding from it alone
#: indexed the catalogue 100% in English.
ROOT_HTML = """<html><body>
<a href="/drama/Genre-2000000000000000001">Genre</a>
<a href="/ja/drama/X-2000000000000000003">X</a>
</body></html>"""

#: The Spanish catalogue is reachable ONLY from its own root.
ES_ROOT_HTML = """<html><body>
<a href="/es/drama/Genero-2000000000000000002">Genero</a>
</body></html>"""


def _genre_page(url: str, ids: list[str], next_page: str | None) -> str:
    cards = ",".join(
        '{\\"shortPlayId\\":\\"%s\\",\\"shortPlayName\\":\\"Show %s\\",'
        '\\"shortPlayLibraryId\\":\\"9%s\\",\\"totalEpisode\\":30,'
        '\\"language\\":\\"en_US\\"}' % (i, i, i[1:]) for i in ids
    )
    nav = f'<a href="{next_page}">next</a>' if next_page else ""
    return (
        '<html><head><script id="json-ld" type="application/ld+json">'
        '{"@graph":[{"@type":"CollectionPage"}]}</script></head>'
        f'<body>{nav}<script>self.__next_f.push([1,"[{cards}]"])</script>'
        "</body></html>"
    )


class _Result:
    def __init__(self, doc: RawDoc, ok: bool = True) -> None:
        self.doc, self.ok = doc, ok


class _Fetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str, classify=None) -> _Result:
        self.requested.append(url)
        body = self.pages.get(url)
        if body is None:
            return _Result(RawDoc(url=url, status=404, headers={}, body=b""), ok=False)
        ctype = "application/xml" if url.endswith(".xml") else "text/html"
        doc = RawDoc(url=url, status=200, headers={"content-type": ctype},
                     body=body.encode())
        return _Result(doc, ok=True)


class _Ctx:
    def __init__(self, fetcher, max_items: int = 0) -> None:
        self.fetcher, self.max_items = fetcher, max_items
        self.watermark = None
        self.marks: list[str] = []

    def set_watermark(self, value: str) -> None:
        self.marks.append(value)

    def progress(self, *a, **k) -> None:
        pass

    def cancelled(self) -> bool:
        return False


@pytest.fixture
def pages() -> dict[str, str]:
    return {
        f"{netshort.BASE_URL}/sitemap_netshortcom.xml": SITEMAP_INDEX,
        "https://netshort.com/site_play_1.xml": SITE_PLAY,
        "https://netshort.com/site_allsearch_1.xml": SITE_ALLSEARCH,
        f"{netshort.BASE_URL}/": ROOT_HTML,
        # No trailing slash: /es/ answers 308 to /es.
        f"{netshort.BASE_URL}/es": ES_ROOT_HTML,
        f"{netshort.BASE_URL}/pt": "<html><body></body></html>",
        "https://netshort.com/drama/Genre-2000000000000000001":
            _genre_page("x", ["3000000000000000001"],
                        "https://netshort.com/drama/Genre-2000000000000000001/page/2"),
        "https://netshort.com/drama/Genre-2000000000000000001/page/2":
            _genre_page("x", ["3000000000000000002"], None),
        "https://netshort.com/es/drama/Genero-2000000000000000002":
            _genre_page("x", ["3000000000000000003"], None),
    }


def _run(pages, mode=Mode.FULL, max_items=0):
    fetcher = _Fetcher(pages)
    ctx = _Ctx(fetcher, max_items=max_items)
    refs = list(netshort.discover(ctx, mode))
    return refs, ctx, fetcher


# --- the locale gate ----------------------------------------------------------


def test_only_the_crawled_locales_are_discovered(pages) -> None:
    """LOCALES is ("", "es", "pt"). The ja and ko editions of the same stories
    must not enter: each extra locale multiplies rows without adding titles."""
    refs, _, _ = _run(pages)
    ids = {r.external_id for r in refs}
    assert "1000000000000000001" in ids, "English"
    assert "1000000000000000009" in ids, "Spanish"
    assert "1000000000000000007" in ids, "Portuguese"
    assert "1000000000000000002" not in ids, "ja must be skipped"
    assert "1000000000000000003" not in ids, "ko must be skipped"


def test_a_title_is_recovered_from_its_episode_urls(pages) -> None:
    """The sitemaps carry ONLY episode URLs now, and every episode of a show
    carries its TITLE's id -- so 15,000 episode URLs collapse to 1,497 titles.
    Several episodes of one title must yield that title once."""
    refs, _, _ = _run(pages)
    ids = [r.external_id for r in refs]
    assert ids.count("1000000000000000001") == 1, "two episode URLs, one title"
    assert "1000000000000000004" in ids, "episode 1 has no -ep-N suffix at all"


def test_the_seo_search_sitemaps_are_never_fetched(pages) -> None:
    """200 `site_allsearch_*` files of /shortsearch/ pages carry no title ids.
    A filter loose enough to admit them fetches every one for nothing."""
    _, _, fetcher = _run(pages)
    assert not any("allsearch" in u for u in fetcher.requested)
    assert not any("blog" in u for u in fetcher.requested)


# --- the genre route ----------------------------------------------------------


def test_genre_pages_are_seeded_from_the_root_not_the_sitemap(pages) -> None:
    """Both routes used to read the same sitemap child list, so ONE renamed file
    blinded the adapter completely: `site_list_*` began returning 404, the
    substring matched nothing, and a full crawl discovered zero while reporting
    success. Route (b) exists precisely to cover what the sitemaps miss, so it
    must not depend on them."""
    _, _, fetcher = _run(pages)
    assert f"{netshort.BASE_URL}/" in fetcher.requested


def test_a_dead_sitemap_index_does_not_blind_the_genre_route(pages) -> None:
    """The regression test for the outage: with the sitemaps gone entirely,
    discovery must still find titles through the genre pages."""
    crippled = dict(pages)
    del crippled[f"{netshort.BASE_URL}/sitemap_netshortcom.xml"]
    refs, _, _ = _run(crippled)
    assert refs, "route (b) must survive the sitemaps disappearing"
    assert "3000000000000000001" in {r.external_id for r in refs}


def test_genre_pages_are_walked_for_every_crawled_locale(pages) -> None:
    """Route (b) exists because titles exist that no sitemap lists, and the es/pt
    genre trees have the same sitemap-absent tail the English one does."""
    _, _, fetcher = _run(pages)
    assert "https://netshort.com/es/drama/Genero-2000000000000000002" in fetcher.requested
    assert "https://netshort.com/ja/drama/X-2000000000000000003" not in fetcher.requested


def test_genre_pagination_is_followed(pages) -> None:
    """Reading page 1 only left most of every genre invisible: no `/drama/` URL
    in any sitemap carries a `/page/` suffix, so nothing else reaches page 2."""
    refs, _, fetcher = _run(pages)
    assert "https://netshort.com/drama/Genre-2000000000000000001/page/2" in fetcher.requested
    assert "3000000000000000002" in {r.external_id for r in refs}


def test_the_genre_walk_is_bounded() -> None:
    """A markup change that made every page advertise a next link must not spin
    forever."""
    self_ref = "https://netshort.com/drama/Loop-2000000000000000001/page/2"
    pages = {
        f"{netshort.BASE_URL}/sitemap_netshortcom.xml": SITEMAP_INDEX,
        "https://netshort.com/site_play_1.xml": "<urlset></urlset>",
        f"{netshort.BASE_URL}/":
            '<html><a href="/drama/Loop-2000000000000000001">L</a></html>',
    }
    # Every page claims a next page one higher, forever.
    for n in range(1, 40):
        url = ("https://netshort.com/drama/Loop-2000000000000000001"
               + ("" if n == 1 else f"/page/{n}"))
        pages[url] = _genre_page("x", [f"30000000000000000{n:02d}"],
                                 f"https://netshort.com/drama/Loop-2000000000000000001/page/{n+1}")
    _, _, fetcher = _run(pages)
    walked = [u for u in fetcher.requested if "/drama/" in u]
    assert len(walked) <= netshort.MAX_GENRE_PAGES


def test_incremental_skips_the_genre_walk(pages) -> None:
    """It exists to close a completeness gap; the next full run will."""
    _, _, fetcher = _run(pages, mode=Mode.INCREMENTAL)
    assert not any("/drama/" in u for u in fetcher.requested)


# --- watermark and caps -------------------------------------------------------


def test_the_watermark_is_the_highest_id_seen(pages) -> None:
    """Sitemap order is not id order, so taking the first stored the OLDEST id
    and published it as the source's progress marker on the dashboard."""
    refs, ctx, _ = _run(pages)
    assert ctx.marks, "a full run must set a watermark"
    assert ctx.marks[-1] == max(r.external_id for r in refs)


def test_max_items_stops_the_run_and_still_marks(pages) -> None:
    refs, ctx, _ = _run(pages, max_items=2)
    assert len(refs) == 2
    assert ctx.marks, "an early return must not lose the watermark"


def test_discovery_yields_no_duplicates(pages) -> None:
    refs, _, _ = _run(pages)
    ids = [r.external_id for r in refs]
    assert len(ids) == len(set(ids))


def test_a_totally_dead_site_yields_nothing() -> None:
    """Nothing to discover, and no crash. The CALLER decides that a full crawl
    finding zero is a failure -- see the guard in app/jobs/catalog.py."""
    refs, ctx, _ = _run({})
    assert refs == []


# --- the reparse contract -----------------------------------------------------


def test_the_reparse_predicates_exist_and_discriminate() -> None:
    """`app/jobs/reparse.py` calls `is_detail_url` unguarded, so an adapter
    without it takes out the recovery path the catalogue job depends on."""
    assert netshort.is_detail_url(
        "https://netshort.com/full-episodes/alpha-1000000000000000001")
    assert not netshort.is_detail_url(
        "https://netshort.com/full-episodes/alpha-1000000000000000001/page/2")
    assert not netshort.is_detail_url("https://netshort.com/drama/Genre-2000000000000000001")
    assert netshort.is_listing_url("https://netshort.com/drama/Genre-2000000000000000001")


def test_every_platform_adapter_publishes_the_reparse_predicates() -> None:
    """Pinned across all of them, since netshort was the only one missing it."""
    from app.sources import registry

    for key in registry.PLATFORM_KEYS:
        module = registry.load(key)
        assert callable(getattr(module, "is_detail_url", None)), key
        assert callable(getattr(module, "is_listing_url", None)), key


def test_every_platform_source_is_scheduled() -> None:
    """Registered but unscheduled means the dashboard shows it enabled while no
    incremental is ever created and the catalogue silently stops growing."""
    from app.jobs.daemon import SCHEDULE
    from app.sources import registry

    scheduled = {entry.source_key for entry in SCHEDULE}
    assert set(registry.PLATFORM_KEYS) <= scheduled


# --- the guard that would have caught all three incidents ---------------------


def test_a_full_crawl_that_discovers_nothing_fails_loudly(tmp_path) -> None:
    """The lesson from three separate incidents in two days.

    Each was a correct feature sitting inert behind no data, and each was
    invisible because the run reported `succeeded`. The last: NetShort renamed
    its sitemaps out from under a substring filter, so discovery matched nothing,
    yielded nothing, and the dashboard showed a clean green run indistinguishable
    from "nothing new to fetch".

    An empty INCREMENTAL is legitimate -- it means nothing changed since the
    watermark. An empty FULL is not: there is no reading of "walk the entire
    catalogue" that ends at zero.
    """
    import pytest as _pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as _Session

    from app.jobs.catalog import run_catalog_sync
    from app.models import Base

    class _Blind:
        KEY, STUDIO_NAME = "netshort", "NetShort"
        BASE_URL, WATCH_URL_TEMPLATE = netshort.BASE_URL, netshort.WATCH_URL_TEMPLATE

        @staticmethod
        def preflight(ctx):
            pass

        @staticmethod
        def discover(ctx, mode):
            return iter(())  # the site changed shape under us

    engine = create_engine(f"sqlite:///{tmp_path}/g.db", future=True)
    Base.metadata.create_all(engine)
    ctx = _Ctx(_Fetcher({}))
    ctx.state = {}
    ctx.fetcher.verdicts = {}

    with _Session(engine, future=True) as session:
        with _pytest.raises(RuntimeError, match="discovery is blind"):
            run_catalog_sync(_Blind, session, ctx, Mode.FULL)

        # The same emptiness is fine on an incremental pass.
        stats = run_catalog_sync(_Blind, session, ctx, Mode.INCREMENTAL)
        assert stats["discovered"] == 0


def test_the_locale_and_genre_routes_are_actually_scheduled() -> None:
    """A capability that runs only in a mode nobody triggers does not run.

    ReelShort walks its language trees only in `_discover_full`, and NetShort's
    genre route -- the one that finds titles no sitemap lists -- is FULL-only.
    With `FULL_CATALOG` unscheduled, both sat idle: the catalogue held exactly
    one Spanish edition for days while the feature looked delivered.
    """
    from app.jobs.daemon import SCHEDULE
    from app.models import JobType

    full = {e.source_key for e in SCHEDULE if e.job_type == JobType.FULL_CATALOG}
    assert {"reelshort", "netshort"} <= full

    # Two multi-hour crawls must not share a night.
    days = [e.weekday for e in SCHEDULE if e.job_type == JobType.FULL_CATALOG]
    assert len(days) == len(set(days)), "full crawls must be staggered"
    assert all(d is not None for d in days), "a full pass is weekly, never daily"


def test_every_crawled_locale_gets_its_own_root(pages) -> None:
    """Seeding only `/` indexed the catalogue 100% English -- 6,622 titles, zero
    es, zero pt. The English root publishes only unprefixed `/drama/` links, so
    the locale filter had nothing to admit, and route (a) cannot compensate
    because `site_play_*` carries no locale-prefixed URL at all."""
    _, _, fetcher = _run(pages)
    for want in (f"{netshort.BASE_URL}/", f"{netshort.BASE_URL}/es",
                 f"{netshort.BASE_URL}/pt"):
        assert want in fetcher.requested, want


def test_a_locale_root_is_requested_without_a_trailing_slash(pages) -> None:
    """`/es/` answers 308 to `/es`. Seeding the slashed form yields a bodyless
    redirect, `root.ok` is false, and that locale contributes nothing while the
    run reports success -- the zero-yield-reported-as-fine failure this adapter
    has already shipped twice."""
    _, _, fetcher = _run(pages)
    assert not any(u.endswith("/es/") or u.endswith("/pt/") for u in fetcher.requested)


def test_the_spanish_catalogue_is_actually_reached(pages) -> None:
    _, _, fetcher = _run(pages)
    assert "https://netshort.com/es/drama/Genero-2000000000000000002" in fetcher.requested
