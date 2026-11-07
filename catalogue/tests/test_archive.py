"""Archive round-trips and conditional-request support.

The archive is what makes `reparse` possible: fix a parser, re-run it over
every stored payload, zero requests.
"""

from __future__ import annotations

from app.fetch.archive import Archive
from app.fetch.types import RawDoc


def make_doc(url: str, body: bytes, **headers: str) -> RawDoc:
    return RawDoc(url=url, status=200, headers=headers, body=body)


def test_put_and_get_round_trip(tmp_path) -> None:
    archive = Archive(tmp_path)
    doc = make_doc("https://example.test/a", b'{"hello":"world"}')
    sha = archive.put(doc)

    restored = archive.get("https://example.test/a")
    assert restored is not None
    assert restored.body == doc.body
    assert restored.sha256 == sha
    assert restored.status == 200


def test_identical_bodies_are_stored_once(tmp_path) -> None:
    archive = Archive(tmp_path)
    body = b"same bytes"
    sha_a = archive.put(make_doc("https://example.test/a", body))
    sha_b = archive.put(make_doc("https://example.test/b", body))

    assert sha_a == sha_b
    assert len(list((tmp_path / "blobs").rglob("*.gz"))) == 1


def test_conditional_headers_come_from_the_last_response(tmp_path) -> None:
    archive = Archive(tmp_path)
    archive.put(
        make_doc(
            "https://example.test/a",
            b"body",
            etag='W/"abc"',
            **{"last-modified": "Wed, 21 Oct 2026 07:28:00 GMT"},
        )
    )

    headers = archive.conditional_headers("https://example.test/a")
    assert headers["If-None-Match"] == 'W/"abc"'
    assert headers["If-Modified-Since"] == "Wed, 21 Oct 2026 07:28:00 GMT"


def test_no_conditional_headers_for_unseen_url(tmp_path) -> None:
    assert Archive(tmp_path).conditional_headers("https://example.test/never") == {}


def test_superseded_blob_is_dropped_when_unreferenced(tmp_path) -> None:
    archive = Archive(tmp_path, keep_versions=1)
    archive.put(make_doc("https://example.test/a", b"v1"))
    archive.put(make_doc("https://example.test/a", b"v2"))

    assert archive.get("https://example.test/a").body == b"v2"
    assert len(list((tmp_path / "blobs").rglob("*.gz"))) == 1


def test_shared_blob_survives_supersession_by_another_url(tmp_path) -> None:
    """Content addressing means two URLs can point at one blob; dropping a
    version must not delete bytes another URL still needs."""
    archive = Archive(tmp_path, keep_versions=1)
    archive.put(make_doc("https://example.test/a", b"shared"))
    archive.put(make_doc("https://example.test/b", b"shared"))
    archive.put(make_doc("https://example.test/a", b"changed"))

    assert archive.get("https://example.test/b").body == b"shared"


def test_iter_urls_lists_everything_stored(tmp_path) -> None:
    archive = Archive(tmp_path)
    archive.put(make_doc("https://example.test/a", b"1"))
    archive.put(make_doc("https://example.test/b", b"2"))

    assert set(archive.iter_urls()) == {"https://example.test/a", "https://example.test/b"}
