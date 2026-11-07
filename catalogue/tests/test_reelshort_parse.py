"""M3: ReelShort parsing, against real recorded payloads."""

from __future__ import annotations

import pytest

from app.sources import reelshort
from tests.conftest import load_fixture


@pytest.fixture
def detail():
    return reelshort.parse_detail(load_fixture("reelshort/detail_ok"))


# --- listing -----------------------------------------------------------------


def test_listing_yields_a_page_of_books() -> None:
    records = reelshort.parse_listing(load_fixture("reelshort/tags_ok"))
    assert len(records) == 10
    assert all(r.external_id and r.title for r in records)


def test_listing_reports_total_pages() -> None:
    assert reelshort.listing_total_pages(load_fixture("reelshort/tags_ok")) == 374


def test_end_of_pagination_yields_no_records_despite_having_next_data() -> None:
    """The 404 page carries a complete-looking __NEXT_DATA__ blob with
    tagBooks:null. A parser keying on __NEXT_DATA__ presence would treat it as
    a real page."""
    doc = load_fixture("reelshort/tags_404_page375")
    assert b'id="__NEXT_DATA__"' in doc.body
    assert reelshort.parse_listing(doc) == []


def test_listing_is_thin_which_is_why_details_are_mandatory() -> None:
    """Unlike DramaBox, the listing has no tags, dates, cast or audience
    rating. Skipping detail fetches here would lose most of the brief."""
    rec = reelshort.parse_listing(load_fixture("reelshort/tags_ok"))[0]
    assert rec.title and rec.synopsis and rec.poster_url
    assert rec.tags == []
    assert rec.year is None
    assert rec.cast == []


# --- detail ------------------------------------------------------------------


def test_detail_covers_the_brief(detail) -> None:
    assert detail.title == "Bound by Love"
    assert detail.synopsis
    assert detail.year == 2026
    # 118, not the 119 the payload's own `total` claims: `total` counts the
    # preview item, which is not an episode. See the preview tests below.
    assert detail.episode_count == 118
    assert detail.poster_url.startswith("https://")
    assert detail.tags
    assert detail.cast


def test_string_typed_numerics_are_coerced(detail) -> None:
    """total, online_at, paid_start and is_paid all arrive as strings."""
    assert isinstance(detail.episode_count, int)
    assert isinstance(detail.first_paid_episode, int)
    assert detail.first_paid_episode == 16


def test_alternate_title_is_captured(detail) -> None:
    """book_sub_title is the only genuine alternate title this project gets
    from a platform -- DramaBox's are slug variants of the primary title."""
    assert detail.aliases == ["His Cure His Wife"]


def test_audience_rating_comes_from_tag_namespace_1015(detail) -> None:
    """The only audience/content rating available anywhere in the project."""
    assert detail.audience_rating == "All Ages"


def test_ld_json_content_rating_is_ignored_because_it_is_junk() -> None:
    """contentRating is the literal string 'RATING NOT KNOWN' on every title."""
    raw = load_fixture("reelshort/detail_ok")
    assert reelshort._ld_json_series(raw)["contentRating"] == "RATING NOT KNOWN"
    assert reelshort.parse_detail(raw).audience_rating == "All Ages"


def test_platform_rating_is_flagged_editorial(detail) -> None:
    """popularityScore is 4.5-4.9 across the whole catalogue: marketing filler,
    not a distribution."""
    assert detail.rating == 4.5
    assert detail.rating_is_editorial is True


def test_ld_json_series_is_nested_inside_graph() -> None:
    """A parser reading top-level @type finds nothing here."""
    import json
    import re

    html = load_fixture("reelshort/detail_ok").text()
    blocks = [
        json.loads(m.group(1))
        for m in re.finditer(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
        )
    ]
    assert not any(b.get("@type") == "TVSeries" for b in blocks)
    assert reelshort._ld_json_series(load_fixture("reelshort/detail_ok"))


def test_cast_tags_are_excluded_from_the_tag_namespaces(detail) -> None:
    """Actors appear in tag_list under 1001/1005 as well as in actor_info.
    Keeping both would duplicate every credit as a genre-like tag."""
    namespaces = {ns for ns, _ in detail.tags}
    assert f"{reelshort.KEY}.actor" not in namespaces
    assert f"{reelshort.KEY}.actress" not in namespaces
    assert f"{reelshort.KEY}.genre" in namespaces


def test_cast_carries_free_imdb_person_ids(detail) -> None:
    """actor_info.outside_url is an IMDb person URL. This removes fuzzy
    matching entirely wherever it is present."""
    names = [c.name for c in detail.cast]
    assert "Savannah Coffee" in names

    lead = detail.cast[0]
    assert lead.imdb_nconst == "nm14779729"
    assert lead.billing_order == 0
    assert lead.avatar_url.startswith("https://")
    assert lead.external_id.startswith("https://www.reelshort.com/fandom/")


def test_episodes_parse_with_paywall_position(detail) -> None:
    assert len(detail.episodes) == 118
    assert detail.episodes[0].number == 1
    # paid_start is 16, so episodes 1-15 are free and 16+ are not.
    assert detail.episodes[0].is_free is True
    assert detail.episodes[15].is_free is False


def test_the_preview_is_not_an_episode(detail) -> None:
    """`online_base` carries 119 items and one of them is a trailer:
    chapter_type 2, is_preview 1, serial_number 0. Counting it told a viewer who
    had finished the show that one episode was still missing -- on 755 of 3,558
    titles, always the last one, which is how a downstream consumer spotted it.
    """
    assert len(detail.episodes) == 118, "119 items, one of which is the preview"
    assert "7akknc9ej0" not in {e.external_id for e in detail.episodes}


def test_episode_numbers_are_taken_as_published(detail) -> None:
    """serial_number is ALREADY 1-based for real episodes -- unanimous across
    3,530 archived titles. The parser used to add 1 to it, so every ReelShort
    episode was numbered one too high and a consumer's file 1 was our number 2.
    """
    numbers = [e.number for e in detail.episodes]
    assert numbers == list(range(1, 119))


def test_a_title_without_a_preview_is_unaffected(detail) -> None:
    """2,803 of 3,558 titles carry no preview at all; the correction must not
    invent one or shift their count."""
    import json
    import re

    from app.sources import reelshort

    doc = load_fixture("reelshort/detail_ok")
    payload = json.loads(
        re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", doc.text(), re.S).group(1)
    )
    base = payload["props"]["pageProps"]["data"]
    base["online_base"] = [
        e for e in base["online_base"]
        if not (e.get("is_preview") or e.get("chapter_type") == 2)
    ]
    base["total"] = len(base["online_base"])
    body = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></body></html>"
    ).encode()
    stripped = doc.__class__(
        url=doc.url, status=doc.status, headers=doc.headers, body=body
    )

    record = reelshort.parse_detail(stripped)
    assert len(record.episodes) == 118
    assert record.episode_count == 118, "no preview, so nothing is subtracted"


def test_episodes_carry_no_video_urls(detail) -> None:
    dumped = repr(detail.episodes)
    for marker in ("Signature=", "Key-Pair-Id", "Expires=", ".m3u8"):
        assert marker not in dumped


# --- the edge case recon flagged ---------------------------------------------


def test_a_title_with_zero_episodes_is_still_a_valid_record() -> None:
    """Poolboy is real, cast-credited and IMDb-linked, but reports total=0 with
    an empty manifest (app-only or geo-restricted). Dropping such records as
    invalid would lose genuine titles."""
    record = reelshort.parse_detail(load_fixture("reelshort/detail_no_episodes"))

    assert record.title == "Poolboy"
    assert record.episode_count == 0
    assert record.episodes == []
    assert record.cast, "it does have cast"
    assert record.imdb_tconst == "tt37749017"


def test_legacy_titles_fall_back_to_publish_at_for_the_year() -> None:
    """online_at is literally '0' across the legacy catalogue -- 21 of the
    first 50 titles ingested -- while publish_at is populated. Without the
    fallback, year goes missing on a large slice of the back catalogue.
    """
    raw = load_fixture("reelshort/detail_legacy_dates")

    import json
    import re

    data = json.loads(
        re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw.text(), re.S).group(1)
    )["props"]["pageProps"]["data"]
    assert data["online_at"] in (0, "0"), "fixture must have the zeroed field"
    assert data["publish_at"], "and a usable publish_at"

    record = reelshort.parse_detail(raw)
    assert record.year == 2023
    assert record.released_at is not None


def test_title_level_imdb_id_is_captured() -> None:
    """Where ReelShort hands us a tconst directly, IMDb matching is skipped
    entirely -- no fuzzy matching, no review queue."""
    record = reelshort.parse_detail(load_fixture("reelshort/detail_no_episodes"))
    assert record.imdb_tconst.startswith("tt")


def test_empty_title_level_imdb_id_is_not_stored_as_empty_string(detail) -> None:
    assert detail.imdb_tconst is None


# --- URL shapes --------------------------------------------------------------


def test_watch_url_is_built_from_the_id_not_the_slug() -> None:
    from app.sources.base import watch_url

    url = watch_url(reelshort.WATCH_URL_TEMPLATE, "69d91b3ee15105112e0dc022")
    assert url == "https://www.reelshort.com/movie/69d91b3ee15105112e0dc022"


def test_resolved_url_is_the_canonical_slug_form(detail) -> None:
    """raw.url is post-redirect, so the stored pretty URL has the slug even
    though we requested the bare id."""
    assert detail.url.startswith("https://www.reelshort.com/movie/bound-by-love")


# --- repairing what the old parser stored -------------------------------------


def test_the_repair_drops_previews_and_renumbers(tmp_path) -> None:
    """The two bugs cannot be fixed by re-ingesting alone: the preview row is not
    in the new record, so nothing deletes it, and it collides with the real first
    episode the moment numbers shift. So previews go first, then everything moves
    down by one."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session as _Session

    from app.episodes import repair_reelshort_numbering
    from app.models import Base, Episode, Source, Title, TitleSource

    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with _Session(engine, future=True) as s:
        s.add(Source(id=1, key="reelshort", base_url="https://a.test",
                     config_json='{"kind": "platform"}'))
        # One title that carried a preview (stored 1..4), one that did not (2..4).
        s.add(Title(id=1, primary_title="With Preview", normalized_title="w",
                    episode_count=4))
        s.add(Title(id=2, primary_title="No Preview", normalized_title="n",
                    episode_count=3))
        s.add(TitleSource(title_id=1, source_id=1, external_id="a",
                          parsed_json='{"episode_count": 4}'))
        s.add(TitleSource(title_id=2, source_id=1, external_id="b",
                          parsed_json='{"episode_count": 3}'))
        for n in (1, 2, 3, 4):
            s.add(Episode(title_id=1, source_id=1, external_id=f"a{n}", number=n))
        for n in (2, 3, 4):
            s.add(Episode(title_id=2, source_id=1, external_id=f"b{n}", number=n))
        s.commit()

        dry = repair_reelshort_numbering(s, dry_run=True)
        assert dry["preview_rows"] == 1 and dry["preview_titles"] == 1
        assert s.scalar(select(Episode).where(Episode.external_id == "a1")) is not None

        stats = repair_reelshort_numbering(s, dry_run=False)
        assert stats["deleted"] == 1
        assert stats["renumbered"] == 6

        with_preview = sorted(
            e.number for e in s.scalars(
                select(Episode).where(Episode.title_id == 1)).all()
        )
        without = sorted(
            e.number for e in s.scalars(
                select(Episode).where(Episode.title_id == 2)).all()
        )
        assert with_preview == [1, 2, 3], "the trailer is gone and the rest start at 1"
        assert without == [1, 2, 3], "never had a preview, but was still numbered +1"

        # episode_count is deliberately NOT this function's job any more: a
        # merged title carries several ReelShort links and each needs its own
        # value read from its own payload. See resync_episode_counts.
        assert "resync_episode_counts" in str(stats["counts_note"])


def test_the_repair_refuses_to_run_twice(tmp_path) -> None:
    """The dangerous case. Before the repair a preview sits at number 1 and real
    episodes start at 2; afterwards REAL first episodes sit at 1. Running it
    again would read those as previews and delete the opening episode of every
    ReelShort title, so it must detect that it has already run."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session as _Session

    from app.episodes import repair_reelshort_numbering
    from app.models import Base, Episode, Source, Title, TitleSource

    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with _Session(engine, future=True) as s:
        s.add(Source(id=1, key="reelshort", base_url="https://a.test",
                     config_json='{"kind": "platform"}'))
        s.add(Title(id=1, primary_title="X", normalized_title="x", episode_count=4))
        s.add(TitleSource(title_id=1, source_id=1, external_id="a",
                          parsed_json='{"episode_count": 4}'))
        for n in (1, 2, 3, 4):
            s.add(Episode(title_id=1, source_id=1, external_id=f"a{n}", number=n))
        # A title with no preview, so something starts at 2 -- the production
        # shape, where 2,799 of 3,553 titles did. The guard is deliberately
        # conservative: on a database where EVERY title carried a preview it
        # would refuse and ask for a human, which is the safe way to be wrong.
        s.add(Title(id=2, primary_title="Y", normalized_title="y", episode_count=2))
        s.add(TitleSource(title_id=2, source_id=1, external_id="b",
                          parsed_json='{"episode_count": 2}'))
        for n in (2, 3):
            s.add(Episode(title_id=2, source_id=1, external_id=f"b{n}", number=n))
        s.commit()

        repair_reelshort_numbering(s, dry_run=False)
        surviving = sorted(e.number for e in s.scalars(select(Episode)).all())
        assert surviving == [1, 1, 2, 2, 3]

        again = repair_reelshort_numbering(s, dry_run=False)
        assert "already repaired" in str(again.get("note", ""))
        assert sorted(e.number for e in s.scalars(select(Episode)).all()) == [1, 1, 2, 2, 3], \
            "the second run must not touch a single row"


def test_resync_counts_fixes_every_link_of_a_merged_title(tmp_path) -> None:
    """A merged `（DUBBED）` twin contributes its own ReelShort link, and 187
    titles have more than one. Correcting only the first left the other inflated,
    and precedence went on picking it -- which is exactly how three titles kept a
    +1 after the first repair."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as _Session

    from app.episodes import resync_episode_counts
    from app.models import Base, Source, Title, TitleSource
    from app.sources import reelshort

    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)

    doc = load_fixture("reelshort/detail_ok")

    class _Archive:
        def get(self, url):
            return doc if url else None

    with _Session(engine, future=True) as s:
        s.add(Source(id=1, key="reelshort", base_url="https://a.test",
                     config_json='{"kind": "platform"}'))
        s.add(Title(id=1, primary_title="Merged", normalized_title="m",
                    episode_count=119))
        # Both links carry the stale, preview-inflated 119.
        s.add(TitleSource(id=1, title_id=1, source_id=1, external_id="a",
                          url="https://a.test/movie/a", parsed_json='{"episode_count": 119}'))
        s.add(TitleSource(id=2, title_id=1, source_id=1, external_id="b",
                          url="https://a.test/movie/b", parsed_json='{"episode_count": 119}'))
        s.commit()

        stats = resync_episode_counts(s, _Archive(), reelshort)

        assert stats["corrected"] == 2, "BOTH links, not just the first"
        assert s.get(Title, 1).episode_count == 118

        again = resync_episode_counts(s, _Archive(), reelshort)
        assert again["corrected"] == 0 and again["unchanged"] == 2, "idempotent"
