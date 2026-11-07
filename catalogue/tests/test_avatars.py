"""Actor headshots: capture, storage and the API surface.

The data was always there and always discarded -- both adapters parsed
`avatar_url` into PersonCredit and `_sync_cast` dropped it, while
`PersonSource.avatar_image_id` sat unpopulated. These tests pin the pipeline end
to end, and especially the cache-buster, which is the one thing here that fails
quietly rather than loudly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.images import AVATAR_EDGE, fetch_avatars
from app.ingest import _avatar_key, ensure_source, ingest_record
from app.models import Base, Image, ImageKind, Person, PersonSource
from app.precedence import PLATFORM
from app.sources import dramabox
from app.sources.base import PersonCredit, SourceRecord


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


def _put(session, record):
    source, studio = ensure_source(
        session, dramabox.KEY, dramabox.STUDIO_NAME, dramabox.BASE_URL,
        dramabox.WATCH_URL_TEMPLATE, kind=PLATFORM,
    )
    ingest_record(session, source, studio, record)
    session.commit()
    return source


AVATAR = "https://hwztchapter.dramaboxdb.com/data/cppartner/0x0/19800/19800.jpg"


def _record(avatar, external_id="d-1"):
    return SourceRecord(
        external_id=external_id, title="A Show", year=2026, episode_count=40,
        cast=[PersonCredit(external_id="p-19800", name="Ada Lovelace",
                           billing_order=1, avatar_url=avatar)],
    )


# --- capture -----------------------------------------------------------------


def test_the_avatar_url_is_stored_instead_of_discarded(session) -> None:
    """The whole gap: both adapters parsed this and ingest threw it away."""
    _put(session, _record(AVATAR))

    link = session.scalars(select(PersonSource)).one()
    assert link.avatar_url == AVATAR
    assert link.avatar_image_id is None, "the image itself is fetched later"


def test_a_person_with_no_photo_stores_nothing(session) -> None:
    _put(session, _record(None))
    assert session.scalars(select(PersonSource)).one().avatar_url is None


# --- the cache-buster --------------------------------------------------------


def test_the_cache_buster_is_stripped(session) -> None:
    """DramaBox appends ?time=<epoch-ms>, regenerated per response."""
    _put(session, _record(f"{AVATAR}?time=1786343375170"))
    assert session.scalars(select(PersonSource)).one().avatar_url == AVATAR


def test_a_regenerated_timestamp_is_not_a_new_photo(session) -> None:
    """The quiet failure this guards.

    1,956 archived URLs carry a `?time=`, and comparing the raw string would read
    as "the photo changed" on every single sync -- clearing avatar_image_id and
    re-downloading the entire roster nightly, forever, with nothing in the logs
    to suggest anything was wrong.
    """
    _put(session, _record(f"{AVATAR}?time=1111111111111"))
    link = session.scalars(select(PersonSource)).one()
    link.avatar_image_id = 4242          # pretend avatar_fetch already ran
    session.commit()

    _put(session, _record(f"{AVATAR}?time=9999999999999"))

    link = session.scalars(select(PersonSource)).one()
    assert link.avatar_image_id == 4242, "a new timestamp must not drop the image"
    assert link.avatar_url == AVATAR


def test_a_genuinely_different_photo_drops_the_stored_one(session) -> None:
    """The flip side: a real change must invalidate, or the face goes stale."""
    _put(session, _record(f"{AVATAR}?time=1"))
    link = session.scalars(select(PersonSource)).one()
    link.avatar_image_id = 4242
    session.commit()

    _put(session, _record("https://cdn.example/other-headshot.jpg"))

    link = session.scalars(select(PersonSource)).one()
    assert link.avatar_url == "https://cdn.example/other-headshot.jpg"
    assert link.avatar_image_id is None


def test_avatar_key_handles_the_edges() -> None:
    assert _avatar_key(None) is None
    assert _avatar_key("") is None
    assert _avatar_key("https://x/a.jpg") == "https://x/a.jpg"
    assert _avatar_key("https://x/a.jpg?time=1&v=2") == "https://x/a.jpg"


# --- fetching ----------------------------------------------------------------


class _Doc:
    def __init__(self, body):
        self.body = body
        self.url = AVATAR
        self.status = 200

    def header(self, _name):
        return None


class _Result:
    def __init__(self, doc, ok=True):
        self.doc, self.ok = doc, ok


class _Fetcher:
    """Serves one real PNG, and counts requests."""

    def __init__(self, payload, ok=True):
        self.payload, self.ok, self.calls = payload, ok, []

    def get(self, url, classify=None):
        self.calls.append(url)
        return _Result(_Doc(self.payload), ok=self.ok)


def _png(size=(600, 600), colour=(180, 40, 40)):
    from io import BytesIO

    from PIL import Image as PILImage

    buf = BytesIO()
    PILImage.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def test_fetching_stores_the_image_and_links_it(session, tmp_path) -> None:
    _put(session, _record(AVATAR))
    fetcher = _Fetcher(_png())

    stats = fetch_avatars(session, fetcher, tmp_path / "img")

    assert stats["fetched"] == 1 and stats["failed"] == 0
    link = session.scalars(select(PersonSource)).one()
    assert link.avatar_image_id is not None
    image = session.get(Image, link.avatar_image_id)
    assert image.kind == ImageKind.AVATAR
    assert (tmp_path / "img" / image.local_path).exists()


def test_avatars_are_stored_smaller_than_posters(session, tmp_path) -> None:
    """There are thousands of headshots and every UI renders them small."""
    _put(session, _record(AVATAR))
    fetch_avatars(session, _Fetcher(_png(size=(1400, 1400))), tmp_path / "img")

    image = session.scalars(select(Image)).one()
    assert max(image.width, image.height) == AVATAR_EDGE


def test_a_second_run_fetches_nothing(session, tmp_path) -> None:
    """Idempotency: the job selects only people with no image linked."""
    _put(session, _record(AVATAR))
    fetch_avatars(session, _Fetcher(_png()), tmp_path / "img")

    again = _Fetcher(_png())
    stats = fetch_avatars(session, again, tmp_path / "img")

    assert again.calls == []
    assert stats["considered"] == 0


def test_a_failed_download_leaves_the_row_retryable(session, tmp_path) -> None:
    _put(session, _record(AVATAR))
    stats = fetch_avatars(session, _Fetcher(b"", ok=False), tmp_path / "img")

    assert stats["failed"] == 1
    assert session.scalars(select(PersonSource)).one().avatar_image_id is None


def test_an_undecodable_body_is_counted_not_raised(session, tmp_path) -> None:
    _put(session, _record(AVATAR))
    stats = fetch_avatars(session, _Fetcher(b"not an image"), tmp_path / "img")

    assert stats["failed"] == 1
    assert session.scalars(select(Person)).one() is not None


# --- the API surface ---------------------------------------------------------


@pytest.fixture
def api(session, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.auth import require_api_key
    from app.config import settings
    from app.db import get_session
    from app.main import app

    _put(session, _record(AVATAR))
    images = tmp_path / "img"
    fetch_avatars(session, _Fetcher(_png()), images)
    monkeypatch.setattr(type(settings), "image_dir", property(lambda _: images))

    def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_api_key] = lambda: None
    person_id = session.scalars(select(Person.id)).one()
    yield TestClient(app), person_id
    app.dependency_overrides.clear()


def test_person_detail_advertises_the_photo(api) -> None:
    client, person_id = api
    body = client.get(f"/people/{person_id}").json()

    assert body["photo_url"] == f"/people/{person_id}/photo"


def test_the_photo_is_served_from_our_own_host(api) -> None:
    """Never the source CDN: it rotates, and hotlinking would leak a downstream
    page's viewers to a third party on every render."""
    client, person_id = api
    response = client.get(f"/people/{person_id}/photo")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert "dramaboxdb.com" not in response.headers.get("content-location", "")


def test_a_person_without_a_photo_reports_none_and_404s(api, session) -> None:
    """Common by design -- the platforms publish a headshot for only some cast."""
    client, _ = api
    faceless = Person(name="No Photo", normalized_name="no photo")
    session.add(faceless)
    session.commit()

    assert client.get(f"/people/{faceless.id}").json()["photo_url"] is None
    assert client.get(f"/people/{faceless.id}/photo").status_code == 404


def test_the_cast_list_on_a_title_carries_photos(api, session) -> None:
    """Where dramatarr will actually read it -- one round trip for the cast."""
    client, person_id = api
    from app.models import Title

    title_id = session.scalars(select(Title.id)).one()
    cast = client.get(f"/titles/{title_id}").json()["cast"]

    assert cast and cast[0]["photo_url"] == f"/people/{person_id}/photo"
