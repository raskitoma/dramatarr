"""GoodShort adapter.

The easiest of the three platforms to index, which was not what the other two
led me to expect. Site notes that shape this code, all measured:

  * Pages are **server-rendered HTML with schema.org `ld+json`** -- no
    `__NEXT_DATA__`, no Nuxt, no rotating buildId, no WAF challenge seen. There
    is no internal API to reverse-engineer, so `preflight` has nothing to do and
    the parsers read a documented, stable format instead of a private one.
  * Browse pages carry an `ItemList` of drama URLs AND a `<link rel="next">`, so
    discovery is "parse the list, follow next" rather than guessing page counts.
  * `robots.txt` permits everything except `/subscription`, `/results` and
    `/pay`. **`/results` is the search path**, so on-demand search is off-limits
    here exactly as it is on DramaBox; the browse tree is the only way in.
  * There is no `sitemap.xml` (404).
  * **The `aggregateRating` on a detail page is NOT the drama's.** It sits on a
    `SoftwareApplication` block and is the GoodShort app's own store rating --
    4.5 from 622,044 reviews, identical on every page. Reading it would stamp one
    invented score across the whole catalogue. This adapter publishes no rating
    at all, which is the honest answer: the site shows none per title.
  * The detail page lists only the first ~11 episodes; the rest lazy-load behind
    a request this adapter does not make. `numberOfEpisodes` from the `ld+json`
    is authoritative for the count, so `episode_count` is right even though the
    episode ROWS are partial. They are separate facts and the schema already
    treats them separately.
  * **No cast anywhere.** The `ld+json` has no `actor` field, so this source can
    never contribute credits -- worth knowing before anyone adds it hoping to
    improve cast coverage.

Identity is the full `{slug}-{id}` path, not the numeric id alone. Unlike
DramaBox and ReelShort, GoodShort does NOT 301 an id-only URL -- both
`/drama/31001424670` and `/drama/wrong-slug-31001424670` are hard 404s -- so the
slug is load-bearing and the watch link cannot be rebuilt from the id. The cost
is that a retitled drama arrives as a new `external_id`; the `dedup` job is what
reconciles that, since it is exactly "same show, two rows".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime

from app.fetch.types import RateSpec, RawDoc, Verdict
from app.precedence import PLATFORM
from app.sources.base import EpisodeRecord, Ctx, Mode, Ref, SourceRecord

KEY = "goodshort"
STUDIO_NAME = "GoodShort"
#: Authoritative for its own catalogue; see app/precedence.py.
KIND = PLATFORM
BASE_URL = "https://www.goodshort.com"
#: external_id is the full `{slug}-{id}` path segment -- see the module docstring.
WATCH_URL_TEMPLATE = "https://www.goodshort.com/drama/{external_id}"

#: Plain HTML off a CDN with no challenge observed. Kept conservative anyway:
#: politeness here is a choice, not a response to being blocked.
RATE = RateSpec(concurrency=2, min_interval=0.5)

#: Robots-disallowed. `/results` is the search endpoint, which is why there is no
#: on-demand "index this title by name" for this source either.
FORBIDDEN_PATHS = ("/subscription", "/results", "/pay")

#: Representative paths this adapter requests, for robots_check.
CRAWL_PATHS = (
    "/",
    "/dramas/playlets",
    "/dramas/playlets?page=2",
    "/drama/dragon-tamer-31001424670",
)

#: The catalogue-wide browse view. The genre views under /dramas/<genre>-N-playlets
#: are strict subsets of it, and the 2,000 pages under /tag/ are SEO surfaces with
#: heavy overlap -- crawling those would multiply requests for nothing.
BROWSE_PATH = "/dramas/playlets"

_LD_JSON = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.S | re.I
)
_NEXT_LINK = re.compile(r'<link[^>]+rel="next"[^>]+href="([^"]+)"', re.I)
_DRAMA_PATH = re.compile(r"/drama/([a-z0-9][a-z0-9\-]*-\d+)/?$", re.I)

NS_GENRE = "goodshort.genre"


# --- classification ----------------------------------------------------------


def classify(doc: RawDoc) -> Verdict:
    """No silent challenge has been observed on this host, so this is mostly
    ordinary status reasoning -- deliberately so, rather than copying ReelShort's
    WAF handling for a site that does not have one.

    The 404 page is a real 73KB HTML page, so "the body is non-empty" proves
    nothing and status has to be checked first.
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

    ctype = doc.header("content-type") or ""
    if "text/plain" in ctype or "xml" in ctype:
        return Verdict.OK

    # Every catalogue page we consume carries ld+json. Its absence on a 200 means
    # the markup changed under us, which should stop the run rather than quietly
    # yield zero records.
    if b"application/ld+json" not in doc.body:
        return Verdict.EMPTY
    return Verdict.OK


# --- payload extraction ------------------------------------------------------


class NoStructuredData(RuntimeError):
    pass


def ld_blocks(raw: RawDoc) -> list[dict]:
    """Every schema.org block on the page, flattened.

    A page carries five: TVSeries, BreadcrumbList, ImageObject, ItemList and
    SoftwareApplication. Callers pick by `@type` and must never take the first.
    """
    text = raw.body.decode("utf-8", "replace")
    out: list[dict] = []
    for chunk in _LD_JSON.findall(text):
        try:
            parsed = json.loads(chunk.strip())
        except json.JSONDecodeError:
            continue  # one malformed block must not lose the others
        for item in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(item, dict):
                out.append(item)
    return out


def _of_type(blocks: list[dict], wanted: str) -> dict | None:
    for block in blocks:
        if block.get("@type") == wanted:
            return block
    return None


def external_id_from_url(url: str) -> str | None:
    """`https://…/drama/dragon-tamer-31001424670` -> `dragon-tamer-31001424670`."""
    match = _DRAMA_PATH.search(url.split("?")[0].split("#")[0])
    return match.group(1) if match else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- parsers -----------------------------------------------------------------


def parse_listing(raw: RawDoc) -> list[SourceRecord]:
    """Every drama linked from a browse page.

    Reads the anchors AND the `ItemList`, because the structured data is a
    partial view: page 1 links 24 dramas and lists only 10 of them, page 218
    links 20 and lists 8. Trusting the tidier source alone would have quietly
    indexed 40% of the catalogue and looked like it worked -- the run would
    report success, and only a title-by-title audit would show the gap.

    Anchors come first so document order is preserved (the browse view is
    newest-first, which incremental discovery depends on); the ItemList then
    contributes anything the markup did not link directly.

    Deliberately thin otherwise: unlike DramaBox, no field here exists only on
    the listing, so the detail fetch is mandatory and nothing is lost by it.
    """
    text = raw.body.decode("utf-8", "replace")
    records: list[SourceRecord] = []
    seen: set[str] = set()

    def add(external_id: str | None) -> None:
        if external_id and external_id not in seen:
            seen.add(external_id)
            records.append(SourceRecord(external_id=external_id, title=""))

    for match in re.finditer(r'href="(/drama/[^"?#]+)"', text):
        add(external_id_from_url(match.group(1)))

    item_list = _of_type(ld_blocks(raw), "ItemList")
    for element in (item_list or {}).get("itemListElement") or []:
        if isinstance(element, dict):
            add(external_id_from_url(str(element.get("url") or "")))

    return records


def parse_detail(raw: RawDoc) -> SourceRecord:
    blocks = ld_blocks(raw)
    series = _of_type(blocks, "TVSeries")
    if series is None:
        raise NoStructuredData(f"no TVSeries block at {raw.url}")

    external_id = external_id_from_url(raw.url) or ""
    released = _parse_dt(series.get("dateCreated"))
    record = SourceRecord(
        external_id=external_id,
        title=(series.get("name") or "").strip(),
        url=raw.url,
        synopsis=(series.get("description") or "").strip() or None,
        episode_count=_int(series.get("numberOfEpisodes")),
        released_at=released,
        year=released.year if released else None,
        poster_url=_image(series.get("image")),
    )

    genre = series.get("genre")
    for value in genre if isinstance(genre, list) else [genre]:
        if isinstance(value, str) and value.strip():
            record.tags.append((NS_GENRE, value.strip()))

    # Rating is deliberately absent. The only aggregateRating on the page belongs
    # to the SoftwareApplication block -- the app's store rating, identical on
    # every drama -- and attributing it to a title would be inventing data.

    _parse_episodes(record, blocks, raw)
    return record


def _parse_episodes(record: SourceRecord, blocks: list[dict], raw: RawDoc) -> None:
    """Episodes visible on the page, which is the first handful only.

    `numberOfEpisodes` already gave the real count, so a short list here is a
    partial view rather than a contradiction -- the two are separate facts and
    `episode_count` is not derived from these rows.
    """
    text = raw.body.decode("utf-8", "replace")
    seen: set[str] = set()
    for href in re.findall(r'href="(/episode/[^"?#]+)"', text):
        parts = href.rstrip("/").rsplit("/", 1)
        if len(parts) != 2:
            continue
        tail = parts[1]                      # e.g. 001-42779796
        number, _, episode_id = tail.partition("-")
        if not episode_id or episode_id in seen:
            continue
        seen.add(episode_id)
        record.episodes.append(
            EpisodeRecord(
                external_id=episode_id,
                number=_int(number),
                name=None,
            )
        )


def _image(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value) -> int | None:
    """Numbers arrive as strings in places, as they do on ReelShort."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --- crawl -------------------------------------------------------------------


def preflight(ctx: Ctx) -> None:
    """Nothing to do: no rotating buildId, no token, no session to establish.

    Kept so the adapter satisfies the Source protocol, and as the place to put
    something if that ever stops being true.
    """
    return None


def browse_url(page: int) -> str:
    return f"{BASE_URL}{BROWSE_PATH}" + (f"?page={page}" if page > 1 else "")


def next_page_url(raw: RawDoc) -> str | None:
    """The site's own `<link rel="next">`, so pagination ends when it says so
    rather than when a guessed page count runs out."""
    match = _NEXT_LINK.search(raw.body.decode("utf-8", "replace"))
    if not match:
        return None
    href = match.group(1)
    return href if href.startswith("http") else BASE_URL + href


def is_detail_url(url: str) -> bool:
    return external_id_from_url(url) is not None


def is_listing_url(url: str) -> bool:
    return BROWSE_PATH in url


#: A full pass samples rather than enumerates, so passes repeat until the unique
#: set stops growing. Bounded so a pathological listing cannot loop forever.
MAX_PASSES = 6


def discover(ctx: Ctx, mode: Mode) -> Iterator[Ref]:
    """Walk the browse tree, newest first, following `rel="next"`.

    **The listing is non-deterministic**, exactly as ReelShort's is: page 5
    fetched twice returned 24 ids each time and shared only 12 of them. Adjacent
    pages barely overlap (0-1 items), so pagination itself is sound -- it is the
    contents of a given page that shift between requests. One pass over all 218
    pages yielded 2,596 unique titles out of 5,232 slots, so a single pass
    silently under-collects and reports success.

    Full runs therefore repeat passes until a whole pass adds nothing new.
    Incremental runs do one pass and stop early, because they only need what
    appeared since the watermark and the next full run will pick up any sampling
    gap.
    """
    seen: set[str] = set()
    newest: str | None = None
    passes = 1 if mode is Mode.INCREMENTAL else MAX_PASSES

    for pass_number in range(passes):
        added = 0
        url = browse_url(1)
        pages = 0

        while url:
            if ctx.cancelled():
                return
            # The fetcher applies `classify` itself and returns a FetchResult;
            # the RawDoc the parsers want is `.doc`. Handing it the classifier is
            # also what feeds the circuit breaker and the archive.
            result = ctx.fetcher.get(url, classify=classify)
            if not result.ok:
                return
            raw = result.doc
            pages += 1

            fresh = 0
            for record in parse_listing(raw):
                if record.external_id in seen:
                    continue
                seen.add(record.external_id)
                if newest is None:
                    newest = record.external_id
                fresh += 1
                added += 1
                yield Ref(external_id=record.external_id,
                          url=f"{BASE_URL}/drama/{record.external_id}")
                if ctx.max_items and len(seen) >= ctx.max_items:
                    if newest:
                        ctx.set_watermark(newest)
                    return

            ctx.progress(len(seen), None, f"pass {pass_number + 1} page {pages}")

            # Incremental has caught up with what the last run already saw.
            if mode is Mode.INCREMENTAL and ctx.watermark and fresh == 0:
                break

            url = next_page_url(raw)

        if added == 0:
            break  # saturated: a whole pass found nothing new

    if newest:
        ctx.set_watermark(newest)


def fetch_detail(ctx: Ctx, ref: Ref) -> RawDoc:
    url = ref.url or f"{BASE_URL}/drama/{ref.external_id}"
    return ctx.fetcher.get(url, classify=classify).doc


def roster(ctx: Ctx) -> None:
    """GoodShort publishes no cast at all -- no `actor` field in its structured
    data and no actor index -- so there is no roster to ingest."""
    raise NotImplementedError("GoodShort publishes no actor roster")
