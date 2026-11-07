"""NetShort parsing, and the nine traps that all look like working code.

Every assertion here is pinned to a measurement taken during recon, because the
failure mode on this source is uniformly "the parser returns something plausible
and wrong" rather than "the parser raises".

The two that would have been most expensive: a `/drama/` page is a GENRE
collection with the same `<slug>-<id>` URL shape as a title and a valid
structured-data graph, so an adapter pointed there indexes taxonomy as shows;
and a fabricated episode number returns HTTP 200 with a ~300KB page that reports
that very number AND claims the episode is free.
"""

from __future__ import annotations

import json

import pytest

from app.fetch.types import RawDoc, Verdict
from app.sources import netshort


@pytest.fixture
def detail(fixture):
    return fixture("netshort_detail_ok")


@pytest.fixture
def record(detail):
    return netshort.parse_detail(detail)


# --- the brief ----------------------------------------------------------------


def test_detail_covers_the_brief(record) -> None:
    assert record.external_id == "1808387443220733954"
    assert record.title == "Love Lights My Way Back Home"
    assert record.lang == "en"
    assert record.synopsis and len(record.synopsis) > 40
    assert record.poster_url and record.poster_url.startswith("https://")
    assert ("netshort.genre", "Modern Romance") in record.tags


def test_the_watch_url_is_built_from_the_id_not_the_slug() -> None:
    """The id-only form is served directly, unlike GoodShort where both
    `/drama/<id>` and a wrong slug are hard 404s. So a retitle does not mint a
    new external_id here, and the link survives it."""
    assert netshort.watch_url_for("1808387443220733954") == (
        "https://netshort.com/full-episodes/1808387443220733954"
    )
    assert netshort.WATCH_URL_TEMPLATE.format(
        external_id="1808387443220733954"
    ).endswith("/full-episodes/1808387443220733954")


# --- trap 1: the genre namespace ---------------------------------------------


def test_a_genre_page_is_not_mistaken_for_a_title(fixture) -> None:
    """`/drama/<slug>-<id>` is a genre collection, not a show. The id spaces are
    disjoint -- 5,848 `/drama/` ids vs 55,138 `/full-episodes/` ids, intersection
    exactly 0 -- so reading one as a title indexes taxonomy and never notices."""
    genre = fixture("netshort_genre_listing")
    assert netshort.external_id_from_url(genre.url) is None
    assert netshort.external_id_from_url(
        "https://netshort.com/full-episodes/love-lights-1808387443220733954"
    ) == "1808387443220733954"


def test_a_genre_page_yields_the_shows_it_lists(fixture) -> None:
    records = netshort.parse_listing(fixture("netshort_genre_listing"))
    assert len(records) > 20, "roughly 45 shows per genre page is the point of it"
    assert all(r.external_id.isdigit() for r in records)
    assert all(r.title for r in records)


def test_the_genre_page_finds_a_title_no_sitemap_lists(fixture) -> None:
    """Why discovery cannot rest on sitemaps. This title and all eight of its
    locale siblings appear in NONE of the six list sitemaps (0 hits across
    205,914 URLs) while higher ids are present, so it is not merely new. The
    genre walk is the only route that sees it."""
    records = netshort.parse_listing(fixture("netshort_genre_listing"))
    assert "2090980782421671938" in {r.external_id for r in records}


# --- trap 2: the soft-404 -----------------------------------------------------


def test_a_phantom_episode_number_classifies_as_not_found(fixture) -> None:
    """HTTP 200, ~300KB, a real <title>, and a TVEpisode reporting episode
    99999. The only tell is the missing VideoObject."""
    phantom = fixture("netshort_episode_phantom")
    assert phantom.status == 200, "the trap is that the status is fine"
    assert b'"TVEpisode"' in phantom.body
    assert b'"VideoObject"' not in phantom.body
    assert netshort.classify(phantom) is Verdict.NOT_FOUND


def test_a_real_episode_page_is_not_rejected_by_that_rule(fixture) -> None:
    """The other half: the discriminator must not reject healthy pages."""
    real = fixture("netshort_episode_ok")
    assert b'"VideoObject"' in real.body
    assert netshort.classify(real) is Verdict.OK


def test_the_phantom_free_flag_is_never_published(fixture) -> None:
    """The phantom claims `isAccessibleForFree`, so a crawl that trusted the
    status would publish non-existent FREE episodes -- and probing -ep-N until
    failure would never terminate."""
    phantom = fixture("netshort_episode_phantom")
    assert b"isAccessibleForFree" in phantom.body
    assert netshort.classify(phantom) is not Verdict.OK


# --- trap 3: the app store rating --------------------------------------------


def test_the_app_store_rating_is_not_published_as_a_title_rating(record, detail) -> None:
    """4.7 is the Google Play score for the NetShort app, byte-identical on every
    page -- the GoodShort SoftwareApplication trap one layer deeper. We saw it
    and declined it."""
    assert b"4.7" in detail.body, "the number really is on the page"
    assert record.rating is None
    assert record.rating_votes is None


# --- trap 4: the capped episode list -----------------------------------------


def test_episode_count_is_the_declared_number_not_the_rows(record) -> None:
    """`numberOfItems` is honest (70) while `itemListElement` is hard-capped at
    20 on every page sampled. Two separate facts, and `len(rows)` looks entirely
    reasonable for a short-drama platform."""
    assert record.episode_count == 70
    assert len(record.episodes) == 20
    assert record.episode_count != len(record.episodes)


# --- trap 5: episode one has no suffix ---------------------------------------


def test_episode_one_has_no_ep_suffix_and_is_still_numbered_one() -> None:
    """`-ep-1` occurs 0 times in 20,467 episode URLs; the bare URL IS episode 1.
    A `-ep-(\\d+)` regex drops it and undercounts by exactly one."""
    base = "https://netshort.com/episode/love-lights-1808387443220733954"
    assert netshort.episode_number_from_url(base) == 1
    assert netshort.episode_number_from_url(f"{base}-ep-2") == 2
    assert netshort.episode_number_from_url(f"{base}-ep-70") == 70


def test_the_parsed_rows_start_at_one(record) -> None:
    assert min(e.number for e in record.episodes) == 1


# --- trap 6: the phantom cast labels -----------------------------------------


def test_cast_is_empty_because_the_source_publishes_none(record, detail) -> None:
    """The UI ships i18n labels "Director"/"Stars" that no data ever fills, so
    grepping the markup for "Stars" suggests a cast block exists. It does not:
    0 person fields on 10/10 detail pages, no person URL in 205,914 sitemap URLs.
    NetShort therefore moves cast coverage by exactly zero."""
    assert record.cast == []
    assert netshort.roster(object()) is None


# --- trap 7: the recommendation-rail poster ----------------------------------


def test_poster_is_the_title_not_the_first_recommendation_card(record, detail) -> None:
    """A first-match regex mis-assigned the poster on 5 of 10 pages: the detail
    page carries 25 objects of the same shape and the title's is not reliably
    first. The parsed poster must equal og:image."""
    og = netshort._OG_IMAGE.search(detail.body.decode()).group(1)
    assert record.poster_url == og


def test_the_page_really_does_carry_many_lookalike_objects(detail) -> None:
    """Pins the hazard the test above defends against."""
    assert len(netshort.flight_objects(detail, "shortPlayLibraryId")) > 10


# --- trap 8: the ld+json extraction ------------------------------------------


def test_only_one_ld_json_block_is_extracted_despite_two_matches(detail) -> None:
    """`application/ld+json` appears twice but only one is a script tag; the
    other is a flight string that merely mentions the type."""
    text = detail.body.decode()
    assert text.count("application/ld+json") == 2
    nodes = netshort.ld_graph(detail)
    assert sum(1 for n in nodes if n.get("@type") == "TVSeries") == 1


def test_the_goodshort_regex_would_have_found_nothing(detail) -> None:
    """Recorded because copying it is the obvious move: the `id` attribute
    precedes `type`, so an anchored `<script type=` regex matches ZERO here."""
    import re

    naive = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
    assert naive.findall(detail.body.decode()) == []
    assert netshort.ld_graph(detail), "ours still finds it"


# --- trap 9: the synthesized episode names -----------------------------------


def test_a_real_episode_title_is_kept_without_its_prefix(record) -> None:
    """9 of 14 sampled titles synthesize `"EP N - <series title>"`, but the other
    5 carry genuine episode titles. Dropping all of them would discard real data
    on a third of the catalogue."""
    assert record.episodes[0].name == "The Jinxed Twin"
    assert record.episodes[1].name == "The Silent Beginning"


def test_a_synthesized_episode_name_is_dropped() -> None:
    """And keeping all of them writes up to 20 fake names per title."""
    assert netshort._episode_name("EP 1 - Bow to the Scorned Maid", 1,
                                  "Bow to the Scorned Maid") is None
    assert netshort._episode_name("EP 7 - The Jinxed Twin", 7,
                                  "Love Lights My Way Back Home") == "The Jinxed Twin"


# --- the cross-locale key -----------------------------------------------------


def test_locale_key_is_the_published_library_id_shared_by_the_spanish_edition(
    fixture,
) -> None:
    """The whole basis for treating a Spanish edition as the same work. Read
    verbatim from `shortPlayLibraryId`, which is an exact source-published field
    -- this codebase refuses identity by name, and "Love Lights My Way Back Home"
    and "Perdoname, te amo" share no words."""
    en = netshort.parse_detail(fixture("netshort_detail_ok"))
    es = netshort.parse_detail(fixture("netshort_detail_es"))

    assert en.external_id != es.external_id, "different ids, as on ReelShort"
    assert en.lang == "en" and es.lang == "es"
    assert en.locale_key == es.locale_key == "1894647833539604482"


def test_locale_key_is_never_computed_from_the_short_play_id(fixture) -> None:
    """0 of 312 measured cases have libraryId == playId, and the offsets between
    them span eight orders of magnitude. Arithmetic was never a key here."""
    en = netshort.parse_detail(fixture("netshort_detail_ok"))
    assert en.locale_key != en.external_id


def test_language_is_normalised_to_the_primary_subtag() -> None:
    """The source says `en-US`; ReelShort already stores bare `en`. `?lang=es`
    has to mean the same thing across sources or the filter is per-source
    trivia."""
    assert netshort.normalize_lang("en-US") == "en"
    assert netshort.normalize_lang("es-ES") == "es"
    assert netshort.normalize_lang("pt_BR") == "pt"
    assert netshort.normalize_lang(None) is None


def test_the_locale_path_segment_is_read_from_the_url() -> None:
    assert netshort.locale_of("https://netshort.com/full-episodes/x-1") == ""
    assert netshort.locale_of("https://netshort.com/es/full-episodes/x-1") == "es"


# --- the paywall boundary -----------------------------------------------------


def test_one_episode_fetch_yields_the_whole_series_paywall(fixture) -> None:
    """`videoEpisodeInfos` carries isLock for every episode, so the boundary for
    an N-episode show costs one request rather than N."""
    record = netshort.parse_episode_page(fixture("netshort_episode_ok"))
    assert record.first_paid_episode == 12
    assert len(record.episodes) > 40
    free = [e.number for e in record.episodes if e.is_free]
    assert free == list(range(1, 12)), "contiguous free window below the boundary"


def test_no_playback_url_is_ever_carried(fixture) -> None:
    """The page holds signed, expiring CDN links. This is a metadata indexer and
    SourceRecord has nowhere to put them -- pinned so a future field addition
    does not quietly start storing them."""
    record = netshort.parse_episode_page(fixture("netshort_episode_ok"))
    blob = json.dumps(
        [vars(e) for e in record.episodes] + [{"u": record.url}], default=str
    )
    assert "auth_key" not in blob
    assert "cfcdn" not in blob


# --- classification -----------------------------------------------------------


def test_a_real_404_has_no_structured_data(fixture) -> None:
    missing = fixture("netshort_detail_404")
    assert netshort.classify(missing) is Verdict.NOT_FOUND
    assert b"application/ld+json" not in missing.body


def test_the_string_404_on_a_healthy_page_is_not_a_failure_marker(detail) -> None:
    """Next.js ships `"notFound":"$undefined"` in the flight and a messages.404
    i18n bundle on every healthy page. Keying on those strings would classify the
    whole catalogue as missing."""
    assert b"404" in detail.body or b"notFound" in detail.body
    assert netshort.classify(detail) is Verdict.OK


def test_challenge_and_rate_limit_are_distinguished() -> None:
    def doc(status: int) -> RawDoc:
        return RawDoc(url=f"{netshort.BASE_URL}/full-episodes/1", status=status,
                      headers={"content-type": "text/html"}, body=b"x")

    assert netshort.classify(doc(429)) is Verdict.RATE_LIMITED
    assert netshort.classify(doc(403)) is Verdict.CHALLENGE
    assert netshort.classify(doc(500)) is Verdict.ERROR


# --- robots -------------------------------------------------------------------


def test_the_disallowed_api_is_declared_forbidden() -> None:
    """`/prod-web-api*` is the site's own JSON API and is robots-disallowed. It
    is recorded here so the constraint is visible in code, not only in a memo."""
    assert "/prod-web-api" in netshort.FORBIDDEN_PATHS
    assert not any(p.startswith("/prod-web-api") for p in netshort.CRAWL_PATHS)


def test_no_crawl_path_is_forbidden() -> None:
    for path in netshort.CRAWL_PATHS:
        assert not any(path.startswith(bad) for bad in netshort.FORBIDDEN_PATHS), path


def test_english_is_always_crawled() -> None:
    assert "" in netshort.LOCALES, "the unprefixed tree is the English catalogue"


# --- the episode id: the defect that would have aborted the first crawl -------


def test_episode_ids_are_bounded_and_built_from_the_numeric_id(record) -> None:
    """`episode.external_id` is String(128). Keying on the URL tail put a real
    CJK-slugged title at 130 characters by episode 1 and 136 by episode 20 --
    Postgres raises StringDataRightTruncation, SQLite ignores column lengths
    entirely. So a fully green offline suite would have aborted the crawl at
    title 5,322 of 15,621, and aborted at the same row on every retry."""
    for episode in record.episodes:
        assert len(episode.external_id) <= 64
        assert episode.external_id.startswith(record.external_id)
    assert record.episodes[0].external_id == "1808387443220733954-ep-1"
    assert record.episodes[1].external_id == "1808387443220733954-ep-2"


def test_a_retitle_does_not_change_any_episode_id(fixture) -> None:
    """The slug is mutable and the id is not. Slug-derived episode ids minted a
    SECOND full set of rows on a rename -- 20 became 40, every number twice, with
    nothing to collapse them -- which is the very reasoning that put the bare
    numeric id on the Title."""
    original = fixture("netshort_detail_ok")
    renamed = RawDoc(
        url=original.url.replace("love-lights-my-way-back-home", "a-brand-new-slug"),
        status=200, headers=original.headers,
        body=original.body.replace(b"love-lights-my-way-back-home", b"a-brand-new-slug"),
    )
    before = netshort.parse_detail(original)
    after = netshort.parse_detail(renamed)

    assert before.external_id == after.external_id
    assert [e.external_id for e in before.episodes] == [
        e.external_id for e in after.episodes
    ]


def test_both_parsers_agree_on_episode_ids(fixture) -> None:
    """They describe the same episodes. Keying detail rows on the URL tail and
    episode-page rows on `episodeId` gave the two parsers disjoint id spaces, so
    wiring the second one in would have inserted a parallel set of rows."""
    detail = netshort.parse_detail(fixture("netshort_detail_ok"))
    assert netshort.episode_external_id(detail.external_id, 1) == \
        detail.episodes[0].external_id

    page = netshort.parse_episode_page(fixture("netshort_episode_ok"))
    for episode in page.episodes[:5]:
        assert episode.external_id == netshort.episode_external_id(
            page.external_id, episode.number
        )


def test_a_scalar_genre_is_not_iterated_as_characters() -> None:
    """A single-genre title publishes a bare string; iterating it writes one tag
    per character."""
    import json as _json

    body = (
        '<script id="json-ld" type="application/ld+json">'
        + _json.dumps({"@graph": [{
            "@type": "TVSeries", "name": "Solo",
            "url": "https://netshort.com/full-episodes/solo-1000000000000000001",
            "genre": "Romance", "numberOfEpisodes": 5, "inLanguage": "en-US",
        }]})
        + "</script>"
    )
    parsed = netshort.parse_detail(RawDoc(
        url="https://netshort.com/full-episodes/solo-1000000000000000001",
        status=200, headers={}, body=body.encode()))
    assert parsed.tags == [("netshort.genre", "Romance")]


def test_language_never_becomes_whitespace_or_an_object() -> None:
    """Anything returned here is written to TitleSource.lang and surfaced through
    ?lang= and X-Episode-Languages."""
    assert netshort.normalize_lang("   ") is None
    assert netshort.normalize_lang({"@type": "Language", "name": "English"}) is None
    assert netshort.normalize_lang(123) is None


def test_a_brace_in_a_drama_title_does_not_lose_the_locale_key(detail) -> None:
    """The silent failure this parser is built to survive. A `}` inside a title
    made the old backward scan overshoot into the parent object and return a
    card with no `shortPlayLibraryId` -- so `locale_key` came back None and that
    title quietly failed to link its Spanish and Portuguese editions, while the
    title, poster, episode_count and tags all stayed correct and the verdict
    stayed OK. Nothing anywhere would have reported it."""
    poisoned = RawDoc(
        url=detail.url, status=200, headers=detail.headers,
        body=detail.body.replace(
            b"Love Lights My Way Back Home", b"Love } Lights { My Way Back Home"),
    )
    parsed = netshort.parse_detail(poisoned)
    assert parsed.locale_key == "1894647833539604482"
    assert parsed.external_id == "1808387443220733954"


def test_a_source_published_duplicate_episode_is_dropped(fixture) -> None:
    """Trap 10, found by a real crawl rather than by reading pages.

    NetShort itself publishes some episodes twice: "The Hidden Tyrant 2" lists 20
    `hasPart` rows whose episodeNumbers are {1: TWICE, 2..19: once}, the first two
    byte-identical with the same bare URL. Any STABLE id derivation therefore
    produces the same id twice, the second INSERT violates `uq_episode_external`,
    and the exception ended a 15,000-title crawl 2,251 titles in -- dying on the
    same title on every retry.

    The count is unaffected because it comes from `numberOfEpisodes`, never from
    the rows.
    """
    record = netshort.parse_detail(fixture("netshort_detail_dup_ep1"))
    ids = [e.external_id for e in record.episodes]

    assert len(ids) == len(set(ids)), "no duplicate episode ids may survive"
    assert [e.number for e in record.episodes][:3] == [1, 2, 3]
    assert record.episode_count == 86, "the declared count is untouched"
    assert len(record.episodes) == 19, "20 published rows, one of them a repeat"
