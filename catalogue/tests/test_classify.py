"""M1 verification: the per-source classifiers.

Every case here is a trap that recon found on a live site. Getting any of them
wrong produces silent data loss rather than a visible error, which is why they
are the first tests in the project.
"""

from __future__ import annotations

from app.fetch.types import RawDoc, Verdict
from app.sources import dramabox, reelshort
from tests.conftest import load_fixture


# --- ReelShort ---------------------------------------------------------------


def test_reelshort_silent_waf_challenge_is_not_success() -> None:
    """The headline failure mode.

    The WAF answers with HTTP 202 and an empty body -- a 2xx. curl calls this
    success. A crawler that believes it records blank pages by the hundred and
    reports a clean run having indexed nothing.
    """
    doc = load_fixture("reelshort/challenge_202")
    assert doc.status == 202
    assert doc.body == b""
    assert reelshort.classify(doc) is Verdict.CHALLENGE


def test_reelshort_challenge_detected_without_the_header() -> None:
    """x-amzn-waf-action is exposed via CORS and can be stripped in transit, so
    a 2xx with an empty body must be enough on its own."""
    doc = RawDoc(
        url="https://www.reelshort.com/tags/mc-types/120",
        status=202,
        headers={"content-length": "0"},
        body=b"",
    )
    assert reelshort.classify(doc) is Verdict.CHALLENGE


def test_reelshort_200_with_empty_body_is_a_challenge_not_ok() -> None:
    doc = RawDoc(url="https://www.reelshort.com/x", status=200, headers={}, body=b"")
    assert reelshort.classify(doc) is Verdict.CHALLENGE


def test_reelshort_end_of_pagination_404_still_carries_next_data() -> None:
    """The sentinel trap.

    Page 375 returns 404 while still embedding a complete __NEXT_DATA__ blob
    (page:"/404", tagBooks:null). "Body contains __NEXT_DATA__" is therefore
    NOT a valid success test -- status has to be checked first.
    """
    doc = load_fixture("reelshort/tags_404_page375")
    assert doc.status == 404
    assert b'id="__NEXT_DATA__"' in doc.body  # the trap is real
    assert reelshort.classify(doc) is Verdict.NOT_FOUND


def test_reelshort_real_listing_is_ok() -> None:
    doc = load_fixture("reelshort/tags_ok")
    assert reelshort.classify(doc) is Verdict.OK


def test_reelshort_html_without_next_data_is_empty() -> None:
    doc = RawDoc(
        url="https://www.reelshort.com/x",
        status=200,
        headers={"content-type": "text/html"},
        body=b"<html><body>nothing useful</body></html>",
    )
    assert reelshort.classify(doc) is Verdict.EMPTY


# --- DramaBox ----------------------------------------------------------------


def test_dramabox_empty_booklist_is_a_valid_result_not_an_error() -> None:
    """The mirror image of ReelShort's trap, which is why classify is
    per-source rather than shared.

    Out-of-range pages and nonexistent category ids both return HTTP 200 with
    bookList: []. That is a legitimate empty result. Treating it as failure
    would retry pages that are correctly empty -- and, worse, a shared
    classifier tuned for ReelShort would call it a block.
    """
    doc = load_fixture("dramabox/browse_empty")
    assert doc.status == 200
    assert b'"bookList":[]' in doc.body.replace(b" ", b"")
    assert dramabox.classify(doc) is Verdict.OK


def test_dramabox_page_one_redirect_stub_is_an_error() -> None:
    """Page 1 of a category has no page segment; asking for /1 returns a
    200-wrapped __N_REDIRECT stub with no records in it."""
    doc = load_fixture("dramabox/redirect_stub")
    assert b"__N_REDIRECT" in doc.body
    assert dramabox.classify(doc) is Verdict.ERROR


def test_dramabox_real_listing_is_ok() -> None:
    doc = load_fixture("dramabox/browse_ok")
    assert dramabox.classify(doc) is Verdict.OK


def test_dramabox_not_found_body() -> None:
    doc = RawDoc(
        url="https://www.dramabox.com/_next/data/b/name/1.json",
        status=404,
        headers={},
        body=b'{"notFound":true}',
    )
    assert dramabox.classify(doc) is Verdict.NOT_FOUND


def test_dramabox_malformed_json_is_an_error() -> None:
    doc = RawDoc(
        url="https://www.dramabox.com/_next/data/b/browse.json",
        status=200,
        headers={},
        body=b"<html>not json</html>",
    )
    assert dramabox.classify(doc) is Verdict.ERROR


# --- the two sources genuinely disagree --------------------------------------


def test_empty_body_means_opposite_things_on_the_two_sources() -> None:
    """This is the whole argument for a per-source classifier.

    An HTTP 200 with an empty JSON list is a normal result on DramaBox. On
    ReelShort an empty body at 2xx means you were blocked. One shared
    classifier would necessarily be wrong for one of them.
    """
    empty_list = RawDoc(
        url="https://www.dramabox.com/_next/data/b/browse/0/251.json",
        status=200,
        headers={},
        body=b'{"pageProps":{"bookList":[],"pages":0}}',
    )
    assert dramabox.classify(empty_list) is Verdict.OK

    empty_body = RawDoc(
        url="https://www.reelshort.com/tags/mc-types/120",
        status=200,
        headers={},
        body=b"",
    )
    assert reelshort.classify(empty_body) is Verdict.CHALLENGE


def test_reelshort_fandom_pages_are_ok_without_next_data() -> None:
    """The /fandom/ section is a separate WordPress site on the same host and
    carries no __NEXT_DATA__. Requiring it there classified a good 328KB roster
    page as EMPTY, and actor_roster reported "publishes no actor roster" — a
    silent nothing, not an error. Caught only by running the job for real.
    """
    doc = load_fixture("reelshort/fandom_roster")
    assert b'id="__NEXT_DATA__"' not in doc.body
    assert reelshort.classify(doc) is Verdict.OK


def test_reelshort_fandom_actor_page_is_ok() -> None:
    assert reelshort.classify(load_fixture("reelshort/fandom_actor")) is Verdict.OK


def test_a_fandom_challenge_is_still_a_challenge() -> None:
    """The WAF is host-wide: the exemption must not swallow a real block."""
    doc = RawDoc(
        url="https://www.reelshort.com/fandom/reelshort-actors-actresses/",
        status=202,
        headers={"x-amzn-waf-action": "challenge"},
        body=b"",
    )
    assert reelshort.classify(doc) is Verdict.CHALLENGE
