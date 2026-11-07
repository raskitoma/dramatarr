"""The API contract for titles that were merged away.

A merge removes ids that a deployed consumer already stores, so these tests are
about the consumer more than about this service. dramatarr polls
`?updated_since=` and keeps `studios[0].watch_url` per show; it was written
before merges existed and does not read `merged_into`.

The contract therefore has to satisfy both readers at once: an old consumer must
keep working without changing a line, and an updated one must be able to converge.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.ingest import ensure_source, ingest_record
from app.main import app
from app.merge import merge_titles
from app.config import settings
from app.models import Base, Image, Title, TitleImage
from app.precedence import PLATFORM
from app.search import reindex
from app.sources import dramabox, reelshort
from app.sources.base import EpisodeRecord, SourceRecord

DB_TITLE = "Fruit Dimension: Orange LightRedemption. 1"
RS_TITLE = "Fruit Dimension: Orange Light Redemption.1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(engine)

    with Session(engine, future=True) as s:
        for module, external_id, title, count, prefix in (
            (dramabox, "42000023511", DB_TITLE, 20, "db"),
            (reelshort, "6a630a3b53c25feab006dfb6", RS_TITLE, 40, "rs"),
        ):
            source, studio = ensure_source(
                s, module.KEY, module.STUDIO_NAME, module.BASE_URL,
                module.WATCH_URL_TEMPLATE, kind=PLATFORM,
            )
            ingest_record(s, source, studio, SourceRecord(
                external_id=external_id, title=title, year=2026,
                episode_count=count, synopsis="A synopsis.",
                # Both platforms publish the same show, so both carry a full run
                # of episode rows -- that is what makes the merged list double.
                episodes=[
                    EpisodeRecord(external_id=f"{prefix}-{n}", number=n, is_free=n < 3)
                    for n in range(1, 6)
                ],
            ))
        s.commit()
        ids = sorted(s.scalars(select(Title.id)).all())
        assert len(ids) == 2

        # A poster on the row that is about to be absorbed. Its title_image link
        # moves to the survivor during the merge, which is exactly what made
        # /titles/{old}/poster 404 for art that still exists.
        images = tmp_path / "images"
        images.mkdir()
        (images / "poster.webp").write_bytes(b"RIFF____WEBPVP8 ")
        image = Image(kind="poster", local_path="poster.webp", sha256="deadbeef",
                      width=100, height=150, bytes=16)
        s.add(image)
        s.flush()
        s.add(TitleImage(title_id=ids[1], image_id=image.id, kind="poster",
                         is_primary=True))
        s.commit()

        merge_titles(s, ids[0], ids[1])
        s.commit()
        reindex(s)

    monkeypatch.setattr(type(settings), "image_dir", property(lambda _: images))

    from app.db import get_session

    def _session():
        with Session(engine, future=True) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_api_key] = lambda: None
    yield TestClient(app), ids[0], ids[1]
    app.dependency_overrides.clear()


def test_a_superseded_id_serves_the_survivor(client) -> None:
    """A 404 would tell a consumer nothing about where the title went."""
    api, survivor, loser = client
    response = api.get(f"/titles/{loser}")

    assert response.status_code == 200
    assert response.json()["id"] == survivor
    assert response.headers["content-location"] == f"/titles/{survivor}"


def test_the_change_feed_carries_the_tombstone(client) -> None:
    """The only route by which a consumer can ever learn its stored id moved. If
    the tombstone were filtered out here the title would simply stop appearing and
    the consumer would keep a dead id forever."""
    api, survivor, loser = client
    items = api.get("/titles?updated_since=2000-01-01T00:00:00%2B00:00").json()["items"]

    tombstone = next((i for i in items if i["id"] == loser), None)
    assert tombstone is not None
    assert tombstone["merged_into"] == survivor


def test_a_tombstone_in_the_feed_carries_the_survivors_links(client) -> None:
    """The regression that would have broken dramatarr silently.

    Every `title_source` row moved to the survivor, so a tombstone serialised from
    its own id has `studios: []`. A consumer that does not read `merged_into`
    upserts that over its stored record and destroys a working watch_url --
    turning a metadata change into data loss downstream.
    """
    api, survivor, loser = client
    items = api.get("/titles?updated_since=2000-01-01T00:00:00%2B00:00").json()["items"]
    tombstone = next(i for i in items if i["id"] == loser)

    assert [s["key"] for s in tombstone["studios"]] == ["dramabox", "reelshort"]
    assert tombstone["poster_url"] in (None, f"/titles/{survivor}/poster")
    assert all(s["watch_url"] for s in tombstone["studios"])


def test_studio_order_is_stable(client) -> None:
    """`studios[0]` was single-element before merges, so consumers read it
    positionally. Without an ORDER BY the two links can come back either way round
    and the show's platform label flips between polls."""
    api, survivor, _ = client
    first = api.get(f"/titles/{survivor}").json()["studios"]
    second = api.get(f"/titles/{survivor}").json()["studios"]

    assert [s["key"] for s in first] == [s["key"] for s in second] == ["dramabox", "reelshort"]


def test_browsing_hides_tombstones(client) -> None:
    api, survivor, loser = client
    items = api.get("/titles").json()["items"]

    assert [i["id"] for i in items] == [survivor]


def test_search_does_not_return_a_superseded_id(client) -> None:
    """`exact_ids` is a live query over `title.normalized_title`, not the index, so
    reindexing does not filter it -- and merged pairs almost always share a
    normalized title, which is how they were found."""
    api, survivor, loser = client
    items = api.get(f"/titles?q={DB_TITLE}").json()["items"]

    assert [i["id"] for i in items] == [survivor]


# --- episodes ----------------------------------------------------------------


def test_a_merged_title_returns_both_platforms_episodes(client) -> None:
    """Not double-counting: two platforms publish the same show as two listings,
    each with its own numbering and free/paid split."""
    api, survivor, _ = client
    episodes = api.get(f"/titles/{survivor}/episodes").json()

    assert {e["source"] for e in episodes} == {"dramabox", "reelshort"}


def test_episodes_are_grouped_by_source_not_interleaved(client) -> None:
    """Ordering on `number` alone gives 1 dramabox, 2 dramabox, 2 reelshort,
    3 dramabox ... which reads as corrupt data."""
    api, survivor, _ = client
    sources = [e["source"] for e in api.get(f"/titles/{survivor}/episodes").json()]

    runs = [s for i, s in enumerate(sources) if i == 0 or sources[i - 1] != s]
    assert len(runs) == len(set(runs)), f"platforms interleave: {sources[:8]}"


def test_the_source_filter_returns_one_platform(client) -> None:
    api, survivor, _ = client
    episodes = api.get(f"/titles/{survivor}/episodes?source=reelshort").json()

    assert episodes
    assert {e["source"] for e in episodes} == {"reelshort"}


def test_the_available_sources_are_advertised(client) -> None:
    """So a consumer can offer a platform switch without fetching everything
    first and inferring the options."""
    api, survivor, _ = client
    response = api.get(f"/titles/{survivor}/episodes")

    assert response.headers["x-episode-sources"] == "dramabox,reelshort"


def test_an_unknown_source_is_an_error_not_an_empty_list(client) -> None:
    """An empty list is indistinguishable from "this show has no episodes there",
    and a typo would look like missing data."""
    api, survivor, _ = client
    response = api.get(f"/titles/{survivor}/episodes?source=netflix")

    assert response.status_code == 404
    assert "available" in response.json()["detail"]


def test_a_superseded_id_still_returns_its_episodes(client) -> None:
    """The regression. Every episode row moved to the survivor during the merge,
    so a tombstone returned an empty list -- silently, with a 200."""
    api, survivor, loser = client
    episodes = api.get(f"/titles/{loser}/episodes").json()

    assert episodes, "a superseded id must not return an empty episode list"
    assert len(episodes) == len(api.get(f"/titles/{survivor}/episodes").json())


def test_a_superseded_episodes_request_says_where_it_went(client) -> None:
    api, survivor, loser = client
    response = api.get(f"/titles/{loser}/episodes")

    assert response.headers["content-location"] == f"/titles/{survivor}/episodes"


def test_episode_count_is_one_platforms_figure_not_the_row_total(client) -> None:
    """Worth pinning: the two disagree on purpose, and a consumer that assumes
    they match will think the API is broken."""
    api, survivor, _ = client
    detail = api.get(f"/titles/{survivor}").json()
    episodes = api.get(f"/titles/{survivor}/episodes").json()
    one_platform = api.get(f"/titles/{survivor}/episodes?source=dramabox").json()

    # Resolved by precedence from the oldest link -- DramaBox's 20, not
    # ReelShort's 40, and not the number of rows in the list.
    assert detail["episode_count"] == 20
    assert len(episodes) != detail["episode_count"]
    assert len(episodes) == 2 * len(one_platform), "the list spans both platforms"


# --- poster, and the resolution that every id-keyed route now shares ---------


def test_a_superseded_id_serves_the_survivors_poster(client) -> None:
    """Every `title_image` row moved during the merge, so this route 404'd for a
    poster that exists."""
    api, survivor, loser = client
    response = api.get(f"/titles/{loser}/poster")

    assert response.status_code == 200
    assert response.headers["content-location"] == f"/titles/{survivor}/poster"


def test_every_id_keyed_title_route_resolves_a_tombstone(client) -> None:
    """The point of doing this with a dependency.

    Resolution was added by hand to the detail route, then forgotten on episodes
    (200 with an empty list) and again on poster (404 for art that exists). Both
    failures were silent. This sweeps the routes rather than trusting the next
    person to remember, and `Content-Location` is asserted per route because each
    has to advertise its own canonical URL, not the detail one.
    """
    api, survivor, loser = client
    for suffix in ("", "/episodes", "/poster"):
        response = api.get(f"/titles/{loser}{suffix}")
        assert response.status_code == 200, f"/titles/{{id}}{suffix} broke on a tombstone"
        assert response.headers.get("content-location") == f"/titles/{survivor}{suffix}"
        assert response.headers.get("deprecation") == "true"


def test_a_live_id_gets_no_deprecation_headers(client) -> None:
    """The headers have to mean something, so a normal request must not carry
    them."""
    api, survivor, _ = client
    for suffix in ("", "/episodes", "/poster"):
        response = api.get(f"/titles/{survivor}{suffix}")
        assert "content-location" not in response.headers
        assert "deprecation" not in response.headers


def test_a_title_that_never_existed_still_404s(client) -> None:
    """Resolution must not turn a genuinely missing id into something else."""
    api, _, _ = client
    assert api.get("/titles/999999").status_code == 404


def test_stats_counts_only_live_titles(client) -> None:
    api, _, _ = client
    stats = api.get("/stats").json()

    assert stats["titles"] == 1
    assert stats["merged"] == 1
