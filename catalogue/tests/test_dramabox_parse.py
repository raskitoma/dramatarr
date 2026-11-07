"""M2: DramaBox parsing, against real recorded payloads."""

from __future__ import annotations

import pytest

from app.sources import dramabox
from app.sources.base import merge_records
from tests.conftest import load_fixture


@pytest.fixture
def listing():
    return dramabox.parse_listing(load_fixture("dramabox/browse_ok"))


@pytest.fixture
def detail():
    return dramabox.parse_detail(load_fixture("dramabox/detail_ok"))


# --- listing -----------------------------------------------------------------


def test_listing_yields_a_full_page(listing) -> None:
    assert len(listing) == 12
    assert all(r.external_id for r in listing)
    assert all(r.title for r in listing)


def test_listing_carries_the_fields_the_brief_asked_for(listing) -> None:
    rec = listing[0]
    assert rec.title
    assert rec.synopsis
    assert rec.episode_count and rec.episode_count > 0
    assert rec.poster_url and rec.poster_url.startswith("https://")
    assert rec.tags
    assert rec.year and 2020 <= rec.year <= 2100


def test_year_is_derived_because_the_source_has_no_year_field(listing) -> None:
    """DramaBox publishes no year; it comes from firstShelfTime."""
    rec = listing[0]
    assert rec.released_at is not None
    assert rec.year == rec.released_at.year


def test_genres_and_tags_land_in_separate_namespaces(listing) -> None:
    namespaces = {ns for rec in listing for ns, _ in rec.tags}
    assert dramabox.NS_CATEGORY in namespaces
    assert dramabox.NS_TAG in namespaces


def test_platform_rating_is_flagged_editorial(listing) -> None:
    """DramaBox ratings are floor-clamped at 7.1 and never reach 0, so they are
    promotional filler. Stored, but never usable as a real score."""
    rated = [r for r in listing if r.rating is not None]
    assert rated, "fixture should contain rated titles"
    assert all(r.rating_is_editorial for r in rated)
    assert all(r.rating >= 7.0 for r in rated)


def test_poster_size_suffix_is_rewritten_upward(listing) -> None:
    assert "@w=720&h=1280" in listing[0].poster_url
    assert "@w=240" not in listing[0].poster_url


def test_redirect_stub_raises_rather_than_returning_empty(listing) -> None:
    """Page 1 of a category has no page segment; the stub must not be mistaken
    for a page that legitimately has no records."""
    with pytest.raises(dramabox.StaleBuildId):
        dramabox.parse_listing(load_fixture("dramabox/redirect_stub"))


def test_valid_empty_page_parses_to_no_records() -> None:
    """The mirror case: an out-of-range page really is empty, and that is fine."""
    assert dramabox.parse_listing(load_fixture("dramabox/browse_empty")) == []


# --- detail ------------------------------------------------------------------


def test_detail_carries_the_episode_manifest(detail) -> None:
    assert detail.episodes
    assert len(detail.episodes) == detail.episode_count
    first = detail.episodes[0]
    assert first.number == 1
    assert first.duration_ms and first.duration_ms > 0


def test_episodes_never_carry_a_signed_video_url(detail) -> None:
    """The payload hands us signed, expiring mp4 links. We take metadata and
    leave those: they expire, and this is a metadata indexer.

    Note the still image is named `<id>.mp4.jpg` -- a thumbnail OF the video --
    so a naive ".mp4 not in url" check would fail on a perfectly good JPEG.
    What matters is that no signed playback URL survives parsing.
    """
    import json

    raw = json.loads(load_fixture("dramabox/detail_ok").text())
    signed = raw["pageProps"]["chapterList"][0]["mp4"]
    assert "Signature=" in signed and "Key-Pair-Id" in signed, (
        "fixture must actually contain a signed URL, or this test proves nothing"
    )

    dumped = repr(detail.episodes)
    for marker in ("Signature=", "Key-Pair-Id", "Expires="):
        assert marker not in dumped
    assert all(
        e.still_url is None or ".jpg" in e.still_url for e in detail.episodes
    ), "only image URLs may be retained"


def test_cast_is_parsed_when_the_source_provides_it() -> None:
    """Cast coverage is patchy -- `performerList` is absent entirely on many
    newer titles -- so the cast path is pinned to a title known to have one.
    Without this, an empty-cast sample would let a broken parser pass.
    """
    record = dramabox.parse_detail(load_fixture("dramabox/detail_with_cast"))

    assert len(record.cast) >= 2
    names = [c.name for c in record.cast]
    assert "Evan Adams" in names

    lead = record.cast[0]
    assert lead.external_id.isdigit()
    assert lead.role == "actor"
    assert lead.billing_order == 0
    assert lead.avatar_url and lead.avatar_url.startswith("https://")


def test_absent_cast_is_not_an_error() -> None:
    """Key-presence is not data-presence on these platforms, and a title with
    no credited cast is ordinary, not a parse failure."""
    record = dramabox.parse_detail(load_fixture("dramabox/detail_ok"))
    assert record.cast == []
    assert record.episodes, "the rest of the record must still parse"


def test_detail_has_no_rating_which_is_why_merging_matters(detail) -> None:
    """`bookInfo` carries no ratings/status/free. A detail-only crawl would
    lose them, and a naive overwrite would blank them."""
    assert detail.rating is None


# --- merge -------------------------------------------------------------------


def test_merge_keeps_listing_fields_the_detail_lacks(listing, detail) -> None:
    base = next(r for r in listing if r.external_id == detail.external_id)
    assert base.rating is not None

    merged = merge_records(base, detail)

    assert merged.rating == base.rating, "detail must not blank the listing rating"
    assert merged.episodes == detail.episodes, "detail must contribute episodes"
    assert merged.external_id == base.external_id
    assert merged.title == base.title


def test_merge_does_not_let_empty_overlay_values_win() -> None:
    from app.sources.base import SourceRecord

    base = SourceRecord(external_id="1", title="Real", synopsis="kept", rating=8.4)
    empty = SourceRecord(external_id="1", title="Real", synopsis=None, aliases=[])

    merged = merge_records(base, empty)
    assert merged.synopsis == "kept"
    assert merged.rating == 8.4


# --- URL shapes --------------------------------------------------------------


def test_page_one_url_omits_the_page_segment() -> None:
    assert dramabox.browse_url("b", 161, 1).endswith("/browse/161.json")
    assert dramabox.browse_url("b", 161, 2).endswith("/browse/161/2.json")


def test_all_category_page_one_uses_the_bare_browse_route() -> None:
    assert dramabox.browse_url("b", 0, 1).endswith("/browse.json")
    assert dramabox.browse_url("b", 0, 2).endswith("/browse/0/2.json")


def test_watch_url_is_built_from_the_id_not_the_slug() -> None:
    from app.sources.base import watch_url

    url = watch_url(dramabox.WATCH_URL_TEMPLATE, "42000024152")
    assert url == "https://www.dramabox.com/drama/42000024152"


def test_honeypot_paths_are_recorded_so_nothing_requests_them() -> None:
    assert "/server_api/honey" in dramabox.FORBIDDEN_PATHS


# --- discovery caps ----------------------------------------------------------


class _Ctx:
    """Minimal Ctx double: discovery must respect the cap without a database."""

    def __init__(self, pages, max_items):
        self.state = {"build_id": "b", "categories": [0]}
        self.max_items = max_items
        self.watermark = None
        self._pages = pages
        self._new_watermark = None

    def cancelled(self):
        return False

    def set_watermark(self, value):
        self._new_watermark = value

    def progress(self, *a, **k):
        pass


def test_incremental_discovery_respects_the_item_cap(monkeypatch) -> None:
    """With no stored watermark an incremental run sweeps the whole recent
    list. Without the cap here, a test-mode run reported '131 / 50' and fetched
    far more than asked."""
    page = {
        "bookList": [
            {"bookId": f"{i}", "bookNameEn": "x", "firstShelfTime": "2026-08-16 10:00:00"}
            for i in range(12)
        ]
    }
    monkeypatch.setattr(dramabox, "_page", lambda ctx, cat, pg: page)

    ctx = _Ctx(page, max_items=5)
    refs = list(dramabox._discover_incremental(ctx))
    assert len(refs) == 5


def test_incremental_discovery_is_unbounded_when_uncapped(monkeypatch) -> None:
    pages = iter([{"bookList": [{"bookId": "1", "firstShelfTime": "2026-08-16 10:00:00"}]}, {"bookList": []}])
    monkeypatch.setattr(dramabox, "_page", lambda ctx, cat, pg: next(pages, {"bookList": []}))

    ctx = _Ctx(None, max_items=0)
    assert len(list(dramabox._discover_incremental(ctx))) == 1
