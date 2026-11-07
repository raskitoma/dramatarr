"""M6: the REST surface your downstream app consumes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import get_session
from app.ingest import ensure_source, ingest_record
from app.main import app
from app.models import Base, Source, Title
from app.search import reindex
from app.sources import dramabox, reelshort
from app.sources.base import SourceRecord
from tests.conftest import load_fixture

KEY = "test-key"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "api_key", KEY)
    engine = create_engine(f"sqlite:///{tmp_path}/api.db", future=True)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(engine, expire_on_commit=False, future=True)

    with Sess() as session:
        for module, records in (
            (dramabox, dramabox.parse_listing(load_fixture("dramabox/browse_ok"))),
            (reelshort, [reelshort.parse_detail(load_fixture("reelshort/detail_ok"))]),
        ):
            source, studio = ensure_source(
                session,
                module.KEY,
                module.STUDIO_NAME,
                module.BASE_URL,
                module.WATCH_URL_TEMPLATE,
            )
            for record in records:
                ingest_record(session, source, studio, record)
        session.commit()
        reindex(session)

    def _override():
        with Sess() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    yield TestClient(app), Sess
    app.dependency_overrides.clear()


def get(client, path, **kw):
    return client.get(path, headers={"X-API-Key": KEY}, **kw)


# --- auth --------------------------------------------------------------------


def test_catalogue_requires_a_key(client) -> None:
    c, _ = client
    assert c.get("/titles").status_code == 401
    assert c.get("/stats").status_code == 401


def test_health_stays_open(client) -> None:
    c, _ = client
    assert c.get("/health").status_code == 200


# --- the watch URL, which is the point of the whole exercise -----------------


def test_every_title_exposes_a_watch_url_per_studio(client) -> None:
    c, _ = client
    items = get(c, "/titles?limit=100").json()["items"]
    assert items

    for item in items:
        assert item["studios"], f"{item['primary_title']} has no studio link"
        for studio in item["studios"]:
            assert studio["watch_url"].startswith("https://")
            # Built from the id, not the stored slug, so a retitle cannot
            # break the link.
            assert studio["external_id"] in studio["watch_url"]


def test_title_detail_carries_the_brief(client) -> None:
    c, _ = client
    title_id = get(c, "/titles?studio=reelshort").json()["items"][0]["id"]
    body = get(c, f"/titles/{title_id}").json()

    assert body["primary_title"] == "Bound by Love"
    assert body["synopsis"] and body["year"] and body["episode_count"]
    assert body["audience_rating"] == "All Ages"
    assert body["aliases"] == ["His Cure His Wife"]
    assert body["cast"] and body["tags"]


def test_ratings_are_returned_flagged_editorial(client) -> None:
    """A consumer must be able to tell a promotional score from a real one."""
    c, _ = client
    title_id = get(c, "/titles?studio=reelshort").json()["items"][0]["id"]
    ratings = get(c, f"/titles/{title_id}").json()["ratings"]

    assert ratings
    assert all(r["is_editorial"] for r in ratings)


def test_episodes_endpoint(client) -> None:
    c, _ = client
    title_id = get(c, "/titles?studio=reelshort").json()["items"][0]["id"]
    episodes = get(c, f"/titles/{title_id}/episodes").json()

    # 118: the payload's 119th item is a preview, not an episode.
    assert len(episodes) == 118
    assert episodes[0]["number"] == 1


# --- filtering and search ----------------------------------------------------


def test_filter_by_studio(client) -> None:
    c, _ = client
    assert get(c, "/titles?studio=reelshort").json()["count"] == 1
    assert get(c, "/titles?studio=dramabox").json()["count"] == 12


def test_free_text_search_finds_a_title(client) -> None:
    c, _ = client
    body = get(c, "/titles?q=Bound").json()
    assert body["count"] >= 1
    assert any(i["primary_title"] == "Bound by Love" for i in body["items"])


def test_search_with_punctuation_does_not_error(client) -> None:
    """FTS5 treats punctuation as syntax; an unescaped query would 500."""
    c, _ = client
    for query in ("billionaire's", 'a "quoted" thing', "NOT AND OR", "*"):
        assert get(c, f"/titles?q={query}").status_code == 200


def test_search_miss_returns_an_empty_page(client) -> None:
    c, _ = client
    body = get(c, "/titles?q=zzzznotathing").json()
    assert body["count"] == 0 and body["items"] == []


# --- keyset pagination -------------------------------------------------------


def test_pagination_walks_every_row_exactly_once(client) -> None:
    c, _ = client
    seen, cursor = [], None
    for _ in range(20):
        url = "/titles?limit=5" + (f"&cursor={cursor}" if cursor else "")
        body = get(c, url).json()
        seen.extend(i["id"] for i in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break

    assert len(seen) == len(set(seen)), "a row was returned twice"
    assert len(seen) == 13


def test_pagination_is_stable_while_rows_are_being_written(client) -> None:
    """The requirement that rules out offset pagination.

    Syncs write while the downstream app polls. With OFFSET, a row inserted
    mid-scan shifts the window and the client silently skips or repeats
    records. Keyset on (updated_at, id) does not.
    """
    c, Sess = client

    body = get(c, "/titles?limit=5").json()
    seen = [i["id"] for i in body["items"]]
    cursor = body["next_cursor"]

    # A sync lands more titles between the two pages.
    with Sess() as session:
        source = session.scalar(select_source(session, "dramabox"))
        studio = source.studios[0]
        for n in range(5):
            ingest_record(
                session,
                source,
                studio,
                SourceRecord(external_id=f"new-{n}", title=f"Inserted {n}", year=2026),
            )
        session.commit()

    while cursor:
        body = get(c, f"/titles?limit=5&cursor={cursor}").json()
        seen.extend(i["id"] for i in body["items"])
        cursor = body["next_cursor"]

    assert len(seen) == len(set(seen)), "concurrent writes caused duplicate rows"


def select_source(session: Session, key: str):
    from sqlalchemy import select

    return select(Source).where(Source.key == key)


def test_updated_since_returns_only_changed_rows(client) -> None:
    """How the downstream app syncs incrementally."""
    c, Sess = client
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert get(c, f"/titles?updated_since={future}").json()["count"] == 0

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert get(c, f"/titles?updated_since={past}").json()["count"] > 0


def test_a_bad_cursor_is_rejected_cleanly(client) -> None:
    c, _ = client
    assert get(c, "/titles?cursor=notbase64").status_code == 400


# --- stats and ops -----------------------------------------------------------


def test_stats_reports_coverage(client) -> None:
    c, _ = client
    body = get(c, "/stats").json()

    assert body["titles"] == 13
    assert body["by_studio"] == {"dramabox": 12, "reelshort": 1}
    assert body["coverage"]["synopsis"]["pct"] == 100.0
    # Cast is genuinely sparse; the point of reporting coverage is so that is
    # visible rather than mistaken for a bug.
    assert "cast" in body["coverage"]


def test_studios_endpoint_lists_both(client) -> None:
    c, _ = client
    studios = {s["key"]: s for s in get(c, "/studios").json()}
    assert studios["dramabox"]["title_count"] == 12
    assert studios["reelshort"]["name"] == "ReelShort"


# --- admin console -----------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/admin", "/admin/runs", "/admin/titles", "/admin/quarantine"]
)
def test_admin_pages_render(client, path) -> None:
    c, _ = client
    resp = get(c, path)
    assert resp.status_code == 200
    assert "Dramadex" in resp.text


def test_admin_requires_a_key(client) -> None:
    c, _ = client
    assert c.get("/admin").status_code == 401


def test_admin_title_detail_shows_the_watch_url(client) -> None:
    c, _ = client
    title_id = get(c, "/titles?studio=reelshort").json()["items"][0]["id"]
    resp = get(c, f"/admin/titles/{title_id}")

    assert resp.status_code == 200
    assert "https://www.reelshort.com/movie/" in resp.text
    assert "editorial" in resp.text


# --- browser access to the console -------------------------------------------


def test_console_redirects_a_browser_to_login(client) -> None:
    """A browser sends no custom headers when you navigate to a page, so a
    header-only guard makes the console unreachable in the one place it is
    meant to be used. This was missed by testing only with curl -H."""
    c, _ = client
    resp = c.get("/admin", headers={"Accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_api_clients_still_get_a_plain_401(client) -> None:
    c, _ = client
    assert c.get("/titles").status_code == 401
    assert c.get("/admin", headers={"Accept": "application/json"}).status_code == 401


def test_login_page_is_reachable_without_a_key(client) -> None:
    c, _ = client
    assert c.get("/admin/login").status_code == 200


def test_signing_in_sets_a_cookie_that_unlocks_the_console(client) -> None:
    c, _ = client
    resp = c.post("/admin/login", data={"key": KEY}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    cookie = resp.cookies.get("dramadex_key")
    assert cookie == KEY

    # And the console now renders for a plain browser request.
    page = c.get("/admin", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "Dashboard" in page.text


def test_a_wrong_key_does_not_sign_you_in(client) -> None:
    c, _ = client
    resp = c.post("/admin/login", data={"key": "wrong"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]
    assert "dramadex_key" not in resp.cookies


def test_posters_load_for_a_signed_in_browser(client) -> None:
    """<img> tags cannot carry an API-key header either; the cookie is what
    makes poster thumbnails render in the console."""
    c, _ = client
    c.post("/admin/login", data={"key": KEY})
    title_id = get(c, "/titles?limit=1").json()["items"][0]["id"]

    # No explicit header -- only the cookie the browser now holds.
    resp = c.get(f"/titles/{title_id}/poster")
    assert resp.status_code in (200, 404)  # 404 only if no poster stored
    assert resp.status_code != 401


# --- search ranking must survive the API ------------------------------------


def test_search_returns_the_exact_title_first(client) -> None:
    """The ranking was computed and then discarded: the route filtered by
    `id IN (ranked)` and re-sorted by updated_at for keyset pagination, so an
    exact-title query returned the right row somewhere on page one instead of
    at the top. Keyset is right for browsing, where the order is the data's
    own; it is wrong for search, where the order IS the answer.
    """
    c, Sess = client
    from app.search import reindex

    with Sess() as session:
        source = session.scalar(select_source(session, "dramabox"))
        studio = source.studios[0]
        for name in (
            "The Return of My Drama Queen",
            "The Heiress and Her Possessive Bodyguard",
            "The Return of the Lost Heiress",
            "The Return of the Abyss King",
        ):
            ingest_record(
                session, source, studio,
                SourceRecord(external_id=name.lower().replace(" ", "-"), title=name),
            )
        session.commit()
        reindex(session)

    body = get(c, "/titles?q=The%20Return%20of%20the%20Lost%20Heiress").json()
    assert body["items"], "an indexed exact title must be findable"
    assert body["items"][0]["primary_title"] == "The Return of the Lost Heiress"


def test_an_exact_hit_is_not_buried_under_weak_matches(client) -> None:
    """A precise query must not come back as a full page of near-misses --
    otherwise "one exact hit" and "no hit at all" look identical to a caller."""
    c, Sess = client
    from app.search import reindex

    with Sess() as session:
        source = session.scalar(select_source(session, "dramabox"))
        studio = source.studios[0]
        for name in ("Djinn Under Contract", "Under the Sun", "Contract Marriage"):
            ingest_record(
                session, source, studio,
                SourceRecord(external_id=name.lower().replace(" ", "-"), title=name),
            )
        session.commit()
        reindex(session)

    body = get(c, "/titles?q=Djinn%20Under%20Contract").json()
    assert body["items"][0]["primary_title"] == "Djinn Under Contract"
    assert body["count"] < 10, "precise queries must not be padded with weak matches"


def test_search_pagination_keeps_walking_the_ranked_list(client) -> None:
    c, _ = client
    first = get(c, "/titles?q=billionaire&limit=2").json()
    if not first["next_cursor"]:
        return
    second = get(c, f"/titles?q=billionaire&limit=2&cursor={first['next_cursor']}").json()
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})
