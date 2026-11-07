"""DramaBox adapter.

Site notes that shape this code (all measured; see PLAN.md 1.1):

  * No bot protection observed at any concurrency tested. We stay polite anyway.
  * Next.js Pages Router, so `/_next/data/{buildId}/...json` is pure JSON and
    no HTML parsing is needed anywhere.
  * `buildId` is date-stamped and rotates on deploy, so it is re-read every run.
    A stale one 404s, which is fail-fast rather than silent.
  * Page 1 of a category has NO page segment. Asking for `/1` returns a
    200-wrapped `__N_REDIRECT` stub with no records in it.
  * Invalid and out-of-range requests return HTTP 200 with an EMPTY LIST, never
    404. Only the `/name/{id}` space 404s cleanly.
  * Category 0 ("all") is capped at 250 pages and only reaches back ~4 months;
    the per-category lists are uncapped and reach 2023. A full crawl must sweep
    all 53 categories and union on bookId.
  * `/server_api/honey` is robots-disallowed and echoes your IP and UA back. It
    exists to fingerprint crawlers that ignore robots. Never request it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime

from app.fetch.types import RateSpec, RawDoc, Verdict
from app.precedence import PLATFORM
from app.normalize import year_from_timestamp
from app.sources.base import (
    Ctx,
    EpisodeRecord,
    Mode,
    PersonCredit,
    Ref,
    SourceRecord,
)

KEY = "dramabox"
STUDIO_NAME = "DramaBox"
#: Authoritative for its own catalogue; see app/precedence.py.
KIND = PLATFORM
BASE_URL = "https://www.dramabox.com"
WATCH_URL_TEMPLATE = "https://www.dramabox.com/drama/{external_id}"

# Recon ran 1,055 requests at concurrency 8 with zero failures, twice. We stay
# well under that: there is no reason to spend a site's capacity just because
# it is on offer.
RATE = RateSpec(concurrency=4, min_interval=0.25)

#: Never fetch. Robots-disallowed honeypots that fingerprint crawlers.
FORBIDDEN_PATHS = ("/server_api/honey", "/api/honeyPot/honey")

#: Representative paths this adapter requests. robots_check asks whether each
#: is still permitted, so "the rules changed" becomes a concrete yes or no
#: rather than a diff someone has to read.
CRAWL_PATHS = (
    "/",
    "/browse",
    "/browse/161",
    "/drama/42000013483/Revenge-Puck",
    "/name/12198",
    "/_next/data/build/browse.json",
)

_BUILD_ID = re.compile(r'"buildId":"([^"]+)"')

#: Tag namespaces. Kept separate rather than flattened into one vocabulary.
NS_CATEGORY = "dramabox.category"
NS_TAG = "dramabox.tag"


class StaleBuildId(RuntimeError):
    pass


# --- URL construction --------------------------------------------------------


def data_url(build_id: str, path: str) -> str:
    return f"{BASE_URL}/_next/data/{build_id}/{path}"


def browse_url(build_id: str, category: int, page: int) -> str:
    """Page 1 has no page segment; requesting `/1` yields a redirect stub."""
    if category == 0:
        tail = "browse.json" if page == 1 else f"browse/0/{page}.json"
    else:
        tail = f"browse/{category}.json" if page == 1 else f"browse/{category}/{page}.json"
    return data_url(build_id, tail)


def detail_url(build_id: str, book_id: str, slug: str) -> str:
    """The slug is required: `/drama/{id}.json` alone returns a redirect stub."""
    return data_url(build_id, f"drama/{book_id}/{slug}.json")


# --- classification ----------------------------------------------------------


def classify(doc: RawDoc) -> Verdict:
    """DramaBox verdicts.

    The important case: HTTP 200 with `bookList: []` is a VALID EMPTY RESULT,
    not an error and not a block. Out-of-range pages and nonexistent category
    ids both return exactly that.
    """
    if doc.status == 404:
        return Verdict.NOT_FOUND
    if doc.status == 429:
        return Verdict.RATE_LIMITED
    if doc.status in (403, 503):
        return Verdict.CHALLENGE
    if doc.status >= 400 or doc.status == 0:
        return Verdict.ERROR
    if not doc.body:
        return Verdict.EMPTY

    if doc.url.endswith(".json"):
        try:
            payload = json.loads(doc.text())
        except json.JSONDecodeError:
            return Verdict.ERROR
        page_props = payload.get("pageProps")
        if isinstance(page_props, dict) and "__N_REDIRECT" in page_props:
            return Verdict.ERROR
        if payload.get("notFound"):
            return Verdict.NOT_FOUND

    return Verdict.OK


# --- parsing (pure: no network, no database) ---------------------------------


def _cover_url(raw: str | None) -> str | None:
    """Cover URLs carry a size suffix (`...jpg@w=240&h=400`). Request a larger
    render rather than warehousing the thumbnail."""
    if not raw:
        return None
    base = raw.split("@", 1)[0]
    return f"{base}@w=720&h=1280"


def _book_to_record(book: dict) -> SourceRecord:
    book_id = str(book["bookId"])
    slug = book.get("bookNameEn") or ""

    aliases = {
        str(book.get(k))
        for k in ("bookNameEn", "replacedBookName", "bookNameLower")
        if book.get(k)
    }
    aliases.discard(book.get("bookName"))

    tags: list[tuple[str, str]] = []
    for name in book.get("typeTwoNames") or []:
        tags.append((NS_CATEGORY, str(name)))
    for name in book.get("tags") or book.get("labels") or []:
        tags.append((NS_TAG, str(name)))

    shelf = book.get("firstShelfTime") or book.get("shelfTime")

    rating = book.get("ratings")
    return SourceRecord(
        external_id=book_id,
        title=book.get("bookName") or "",
        url=f"{BASE_URL}/drama/{book_id}/{slug}" if slug else None,
        aliases=sorted(a.replace("-", " ") for a in aliases),
        # No year field exists on this source; derive it from the shelf date.
        year=year_from_timestamp(shelf),
        synopsis=book.get("introduction") or None,
        episode_count=book.get("chapterCount"),
        completed=None,  # lastUpdateTimeDisplay is "Completed" on 100% of rows
        released_at=_parse_dt(shelf),
        poster_url=_cover_url(book.get("cover")),
        tags=tags,
        # Floor-clamped 7.1-10 promotional score. Stored, but flagged editorial
        # so it can never leak into sorting.
        rating=float(rating) if isinstance(rating, int | float) and rating else None,
        rating_is_editorial=True,
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def record_from_hint(hint: dict) -> SourceRecord:
    """Build a record from listing data carried on a Ref.

    The browse listing already holds ~95% of what we want, so a full crawl gets
    a complete record per title without any extra request.
    """
    return _book_to_record(hint)


def parse_listing(raw: RawDoc) -> list[SourceRecord]:
    """Records from a browse page. Carries ~95% of the metadata we want,
    including ratings, which the detail response does NOT have."""
    payload = json.loads(raw.text())
    props = payload.get("pageProps") or {}
    if "__N_REDIRECT" in props:
        raise StaleBuildId("redirect stub: wrong URL shape for page 1")
    return [_book_to_record(b) for b in props.get("bookList") or []]


def parse_detail(raw: RawDoc) -> SourceRecord:
    """Cast, exact view counts and the episode manifest.

    Note what is NOT here: `bookInfo` carries no ratings, status, free flag or
    author. Those exist only on the listing, which is why the two parses are
    merged rather than one replacing the other.
    """
    payload = json.loads(raw.text())
    props = payload.get("pageProps") or {}
    if "__N_REDIRECT" in props:
        raise StaleBuildId("redirect stub: detail URL needs the slug")

    info = props.get("bookInfo") or {}
    record = _book_to_record(info)

    for order, performer in enumerate(info.get("performerList") or []):
        pid = performer.get("performerId")
        name = performer.get("performerName") or performer.get("performerFormatName")
        if pid is None or not name:
            continue
        record.cast.append(
            PersonCredit(
                external_id=str(pid),
                name=str(name),
                role="actor",
                billing_order=order,
                avatar_url=performer.get("performerAvatar") or None,
            )
        )

    for chapter in props.get("chapterList") or []:
        cid = chapter.get("id")
        if cid is None:
            continue
        # `mp4` is a signed, expiring CloudFront URL. Deliberately not stored:
        # it expires, it is not metadata, and this is a metadata indexer.
        record.episodes.append(
            EpisodeRecord(
                external_id=str(cid),
                number=(chapter.get("index") or 0) + 1,
                name=chapter.get("name") or None,
                duration_ms=chapter.get("duration"),
                is_free=bool(chapter.get("unlock")),
                still_url=_cover_url(chapter.get("cover")),
                published_at=_parse_dt_date(chapter.get("utime")),
            )
        )

    paid = [e.number for e in record.episodes if e.is_free is False and e.number]
    record.first_paid_episode = min(paid) if paid else None
    record.raw_sha256 = raw.sha256
    return record


def _parse_dt_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


# --- discovery ---------------------------------------------------------------


def preflight(ctx: Ctx) -> None:
    """Re-read the rotating buildId and the category list."""
    result = ctx.fetcher.get(f"{BASE_URL}/", classify=classify)
    match = _BUILD_ID.search(result.doc.text())
    if not match:
        raise StaleBuildId("could not read buildId from the homepage")
    ctx.state["build_id"] = match.group(1)

    browse = ctx.fetcher.get(browse_url(match.group(1), 0, 1), classify=classify)
    props = json.loads(browse.doc.text()).get("pageProps") or {}
    ctx.state["categories"] = [
        int(t["id"]) for t in props.get("types") or [] if t.get("id") is not None
    ]
    ctx.state["catalogue_estimate"] = _estimate_catalogue(ctx)


#: Categories overlap heavily: a measured full crawl produced 12,343 listing
#: slots that collapsed to 6,516 unique titles. Used to turn a sum of page
#: counts into an estimate of distinct titles.
UNIQUE_RATIO = 6516 / 12343


def _estimate_catalogue(ctx: Ctx) -> int | None:
    """Estimate the number of distinct titles, for a progress denominator.

    Costs one request per category (53) before a crawl that will make several
    thousand, and it is what lets an uncapped run report an ETA at all.
    """
    categories = [c for c in ctx.state.get("categories") or [] if c != 0]
    slots = 0
    for category in categories:
        result = ctx.fetcher.get(browse_url(ctx.state["build_id"], category, 1),
                                 classify=classify)
        if not result.ok:
            continue
        props = json.loads(result.doc.text()).get("pageProps") or {}
        pages = int(props.get("pages") or 0)
        slots += pages * 12
    return int(slots * UNIQUE_RATIO) or None


def _page(ctx: Ctx, category: int, page: int) -> dict | None:
    url = browse_url(ctx.state["build_id"], category, page)
    result = ctx.fetcher.get(url, classify=classify)
    if not result.ok:
        return None
    return json.loads(result.doc.text()).get("pageProps") or {}


def discover(ctx: Ctx, mode: Mode) -> Iterator[Ref]:
    if mode is Mode.INCREMENTAL:
        yield from _discover_incremental(ctx)
    else:
        yield from _discover_full(ctx)


def _refs(props: dict) -> Iterator[Ref]:
    for book in props.get("bookList") or []:
        book_id = str(book.get("bookId") or "")
        if not book_id:
            continue
        # The listing is rich enough to be the primary record; the hint carries
        # it forward so the detail fetch only has to add cast and episodes.
        yield Ref(external_id=book_id, url=None, hint=book)


def _discover_full(ctx: Ctx) -> Iterator[Ref]:
    """Sweep every category and union on bookId.

    Category 0 alone is capped at 3,000 titles with a ~4 month floor, so it
    cannot stand in for the rest.
    """
    seen: set[str] = set()
    categories = ctx.state.get("categories") or [0]
    for category in categories:
        page = 1
        pages = 1
        while page <= pages:
            if ctx.cancelled() or (ctx.max_items and len(seen) >= ctx.max_items):
                return
            props = _page(ctx, category, page)
            if props is None:
                break
            pages = min(int(props.get("pages") or 1), 250)
            for ref in _refs(props):
                if ref.external_id in seen:
                    continue
                seen.add(ref.external_id)
                yield ref
                if ctx.max_items and len(seen) >= ctx.max_items:
                    return
            page += 1


def _discover_incremental(ctx: Ctx) -> Iterator[Ref]:
    """The 'all' list is strictly shelfTime-descending, so page from the front
    and stop at the watermark. ~25 titles/day means a daily run is ~3 requests.
    """
    watermark = ctx.watermark
    newest: str | None = None
    seen = 0
    page = 1
    while page <= 40:  # generous ceiling; a burst would have to be enormous
        if ctx.cancelled():
            return
        props = _page(ctx, 0, page)
        if props is None:
            return
        books = props.get("bookList") or []
        if not books:
            return
        for book in books:
            shelf = book.get("firstShelfTime") or book.get("shelfTime") or ""
            if newest is None or shelf > newest:
                newest = shelf
            if watermark and shelf <= watermark:
                if newest:
                    ctx.set_watermark(newest)
                return
            book_id = str(book.get("bookId") or "")
            if book_id:
                yield Ref(external_id=book_id, hint=book)
                seen += 1
                # With no stored watermark an incremental run sweeps the whole
                # recent list, so the cap has to apply here as well.
                if ctx.max_items and seen >= ctx.max_items:
                    if newest:
                        ctx.set_watermark(newest)
                    return
        page += 1
    if newest:
        ctx.set_watermark(newest)


def fetch_detail(ctx: Ctx, ref: Ref) -> RawDoc:
    slug = ref.hint.get("bookNameEn") or ref.hint.get("replacedBookName") or ""
    url = detail_url(ctx.state["build_id"], ref.external_id, slug)
    return ctx.fetcher.get(url, classify=classify).doc


def roster(ctx: Ctx) -> None:
    """DramaBox has no actor index page. The roster is derived from the
    `performerList` on detail records we already fetch, at zero extra cost."""
    return None


# --- performer detail --------------------------------------------------------
#
# `performerList` on a detail record carries only performerId, performerName,
# performerAvatar and videoCount -- which is why this project recorded, wrongly,
# that DramaBox publishes no biographical data at all. It publishes it somewhere
# else: `/name/{performerId}` is a page in its own right, keyed on the SAME id
# we already store as PersonSource.external_id. No name matching is involved.
#
# The locale matters and is the whole design constraint here. `/en/name/{id}`
# returns the romanised name with `introduction` EMPTY; only `/zhHans/name/{id}`
# carries the biography, and it is Chinese prose. So this reads the zhHans page
# for the FACTS inside it -- gender, height, weight, star sign -- and the Han
# name, and deliberately keeps none of the prose: the index's reader does not
# read Chinese, and storing text nobody can read is not data, it is weight.


def performer_url(build_id: str, performer_id: str) -> str:
    """The zhHans page, because it is the only locale with a populated bio."""
    return data_url(build_id, f"zhHans/name/{performer_id}.json")


#: The introduction is formulaic to the point of being a record: a measured
#: 33 of 33 populated samples matched gender and works, 19 height, 16 zodiac,
#: 11 weight. Each field is read independently, so a sentence that omits one
#: still yields the rest.
_PERFORMER_FIELDS = {
    "gender": re.compile(r"性别\s*([男女])"),
    "height_cm": re.compile(r"身高\s*(\d{2,3})\s*(?:cm|CM|厘米)"),
    "weight_kg": re.compile(r"体重\s*(\d{2,3})\s*(?:kg|KG|公斤)"),
}
_ZODIAC = re.compile(r"(白羊|金牛|双子|巨蟹|狮子|处女|天秤|天蝎|射手|摩羯|水瓶|双鱼)\s*座")

#: Translated by lookup, not by machine translation: twelve fixed terms with
#: exact English equivalents, so there is nothing to get subtly wrong.
ZODIAC_EN = {
    "白羊": "Aries", "金牛": "Taurus", "双子": "Gemini", "巨蟹": "Cancer",
    "狮子": "Leo", "处女": "Virgo", "天秤": "Libra", "天蝎": "Scorpio",
    "射手": "Sagittarius", "摩羯": "Capricorn", "水瓶": "Aquarius", "双鱼": "Pisces",
}
GENDER_EN = {"男": "male", "女": "female"}

#: Sanity bounds. A parser reading a formulaic string will eventually read a
#: malformed one, and an actor 8cm tall should be dropped rather than served.
_HEIGHT_RANGE = (100, 250)
_WEIGHT_RANGE = (30, 200)


def parse_performer(raw: RawDoc) -> dict:
    """Structured facts about one performer. Never the Chinese prose itself."""
    props = json.loads(raw.text()).get("pageProps") or {}
    introduction = (props.get("introduction") or "").strip()

    out: dict = {
        "external_id": str(props.get("performerId") or "") or None,
        # zhHans, so this is the Han form. It is stored as an ALIAS: the
        # romanised name stays the searchable one, because the index's reader
        # cannot type Chinese.
        "han_name": (props.get("performerName") or "").strip() or None,
        "avatar_url": (props.get("performerAvatar") or "").strip() or None,
        "gender": None, "height_cm": None, "weight_kg": None, "zodiac": None,
    }
    if not introduction:
        return out

    for field, pattern in _PERFORMER_FIELDS.items():
        match = pattern.search(introduction)
        if not match:
            continue
        value = match.group(1)
        if field == "gender":
            out["gender"] = GENDER_EN.get(value)
        else:
            number = int(value)
            low, high = _HEIGHT_RANGE if field == "height_cm" else _WEIGHT_RANGE
            if low <= number <= high:
                out[field] = number

    zodiac = _ZODIAC.search(introduction)
    if zodiac:
        out["zodiac"] = ZODIAC_EN.get(zodiac.group(1))
    return out


def fetch_performer(ctx: Ctx, performer_id: str) -> RawDoc:
    return ctx.fetcher.get(
        performer_url(ctx.state["build_id"], performer_id), classify=classify
    ).doc


def is_detail_url(url: str) -> bool:
    """Which archived URLs `reparse` should re-run through parse_detail."""
    return "/drama/" in url and url.endswith(".json")


def is_listing_url(url: str) -> bool:
    """Browse pages. Ratings, status and the free flag live ONLY here, so a
    reparse that skipped them would silently drop those fields."""
    return "/browse" in url and url.endswith(".json")
