"""ReelShort adapter.

Site notes that shape this code (all measured; see PLAN.md 1.2):

  * The AWS WAF challenge is SILENT: HTTP 202, content-length 0, header
    `x-amzn-waf-action: challenge`. Not a 403, not a 429. curl reports success.
    A crawler that trusts the status code indexes hundreds of blank pages and
    believes it worked. This is why `classify` is per-source at all.
  * Concurrency, not volume, is the trigger: 180 requests at concurrency 4 ran
    clean, concurrency 16 tripped it. Challenges clear in ~34s and are
    host-wide, so /fandom is not a separate budget.
  * The end-of-pagination page returns HTTP 404 while STILL containing valid
    __NEXT_DATA__ (page:"/404"). The sentinel must be status or a null
    tagBooks, never "the body has __NEXT_DATA__".
  * The browse listing is NON-DETERMINISTIC: repeated fetches of one page
    return different items. A single pass misses titles, so discovery loops
    until the unique-id set stops growing.
  * The listing is thin -- no tags, no dates, no cast -- so unlike DramaBox the
    detail fetch is mandatory rather than an enrichment.
  * Numeric fields arrive as STRINGS ('119', '1779951600'). Coerce everything.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime

from app.fetch.types import RateSpec, RawDoc, Verdict
from app.precedence import PLATFORM
from app.sources.base import (
    Ctx,
    EpisodeRecord,
    Mode,
    PersonCredit,
    PersonRef,
    Ref,
    SourceRecord,
)

log = logging.getLogger(__name__)

KEY = "reelshort"
STUDIO_NAME = "ReelShort"
#: Authoritative for its own catalogue; see app/precedence.py.
KIND = PLATFORM
BASE_URL = "https://www.reelshort.com"
WATCH_URL_TEMPLATE = "https://www.reelshort.com/movie/{external_id}"

# Empirically clean at 4; the WAF trips at 16. Stay at 4.
RATE = RateSpec(concurrency=4, min_interval=0.4)

WAF_HEADER = "x-amzn-waf-action"
#: The WordPress section; plain HTML, no __NEXT_DATA__.
FANDOM_PREFIX = "/fandom/"
NEXT_DATA_MARKER = b'id="__NEXT_DATA__"'
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)

#: The widest catalogue view: 3,738 titles over 374 pages.
BROWSE_PATH = "tags/mc-types"
#: Best incremental surface: 200 records ~= 3 weeks. Its own pagination is
#: fake (pages 1..17 are byte-identical), so only page 1 is ever fetched.
NEW_RELEASE_SHELF = "shelf/new-release-short-movies-dramas-51001290"

#: Stop looping when a pass adds nothing new; cap so a pathological run ends.
MAX_PASSES = 6

#: Representative paths this adapter requests. See app/jobs/robots_check.py.
CRAWL_PATHS = (
    "/",
    "/tags/mc-types/1",
    "/movie/69d91b3ee15105112e0dc022",
    "/shelf/new-release-short-movies-dramas-51001290",
    "/fandom/reelshort-actors-actresses/",
    "/fandom/wp-json/wp/v2/posts",
)

#: tag_list category_id -> meaning. Kept as separate namespaces rather than
#: flattened: 15 namespaces and 228 distinct tags appeared in 30 sampled pages.
TAG_CATEGORIES = {
    "1000": "mc-type",
    "1001": "actor",
    "1005": "actress",
    "1010": "genre",
    "1010001": "sub-genre",
    "1011": "theme",
    "1012": "mood",
    "1013": "region",
    "1014": "period",
    "1015": "audience",
    "1020": "identity",
    "1022": "beat",
    "1023": "setting",
    "1024": "trope",
}
CAST_CATEGORIES = {"1001", "1005"}
AUDIENCE_CATEGORY = "1015"

_IMDB_NAME = re.compile(r"/name/(nm\d+)")


# --- classification ----------------------------------------------------------


def classify(doc: RawDoc) -> Verdict:
    """Order matters: the WAF check comes before any status reasoning, because
    a challenge arrives as a 2xx."""
    if doc.header(WAF_HEADER):
        return Verdict.CHALLENGE
    # The header is CORS-exposed and can be stripped by an intermediary, so a
    # 202 with an empty body has to be enough on its own.
    if doc.status == 202 and not doc.body:
        return Verdict.CHALLENGE

    if doc.status == 404:
        return Verdict.NOT_FOUND
    if doc.status == 429:
        return Verdict.RATE_LIMITED
    if doc.status in (403, 503):
        return Verdict.CHALLENGE
    if doc.status >= 400 or doc.status == 0:
        return Verdict.ERROR
    if not doc.body:
        # A 200 with no body is not a success on this host.
        return Verdict.CHALLENGE

    ctype = doc.header("content-type") or ""
    if "application/json" in ctype or "xml" in ctype:
        return Verdict.OK

    # The /fandom/ section is a separate WordPress site on the same host and
    # carries no __NEXT_DATA__ at all. Requiring it there classified a perfectly
    # good 328KB roster page as EMPTY, and the actor_roster job silently
    # reported "publishes no actor roster".
    if FANDOM_PREFIX in doc.url:
        return Verdict.OK if doc.body else Verdict.EMPTY

    if NEXT_DATA_MARKER not in doc.body:
        return Verdict.EMPTY

    return Verdict.OK


# --- payload extraction ------------------------------------------------------


class NoNextData(RuntimeError):
    pass


def next_data(raw: RawDoc) -> dict:
    match = _NEXT_DATA.search(raw.text())
    if not match:
        raise NoNextData(f"no __NEXT_DATA__ in {raw.url}")
    return json.loads(match.group(1))


def _int(value: object) -> int | None:
    """Numeric fields arrive as strings on this source."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _poster(url: str | None) -> str | None:
    return url or None


# --- parsing (pure: no network, no database) ---------------------------------


def locale_serial(t_book_id: object) -> str | None:
    """The cross-locale half of `t_book_id`.

    ReelShort runs a separate catalogue per language, and the SAME show has a
    different `book_id` in each -- which is why the English-only crawl could not
    see a Spanish title at all. `t_book_id` is what ties them together: it is
    `<locale prefix><serial>`, with the prefix identifying the edition (200000
    English, 300000 Spanish, 400000 Portuguese) and the serial identical across
    all of them.

    Measured over 12 listing pages per locale: 111/113/107 distinct serials with
    0/2/1 collisions inside a locale, and 77 EN-ES plus 79 EN-PT shared serials,
    every joined pair a correct translation ("Two Alphas Are in Love with Me" /
    "Dos Alfas Se Enamoraron de Mí" / "Dois Alfas Estão Apaixonados por Mim").

    So this is a source-published identifier, not a guess about names -- the only
    kind of key `app/dedup.py` and `app/people.py` allow identity to rest on.
    """
    raw = str(t_book_id or "")
    if not raw.isdigit() or len(raw) < 10:
        return None
    # The trailing block is the work; everything before it is the edition.
    return raw[-9:].lstrip("0") or "0"


def _base_record(book: dict, url: str | None = None) -> SourceRecord:
    book_id = str(book.get("book_id") or "")
    episodes = _int(book.get("total"))
    if episodes is None:
        episodes = _int(book.get("chapter_count"))

    return SourceRecord(
        external_id=book_id,
        title=book.get("book_title") or "",
        url=url,
        synopsis=book.get("special_desc") or None,
        episode_count=episodes,
        poster_url=_poster(book.get("book_pic") or book.get("default_pic")),
        # `lang` is absent from listing rows and present on detail pages; the
        # locale key is on both, which is what lets a listing sweep group
        # editions before any detail is fetched.
        lang=(book.get("lang") or None),
        locale_key=locale_serial(book.get("t_book_id")),
    )


def record_from_hint(hint: dict) -> SourceRecord:
    """The listing is thin: title, synopsis, poster and an episode count. Tags,
    dates, cast and audience rating all require the detail fetch."""
    return _base_record(hint)


def parse_listing(raw: RawDoc) -> list[SourceRecord]:
    payload = next_data(raw)
    props = payload.get("props", {}).get("pageProps") or {}
    tag_books = props.get("tagBooks")
    if not tag_books:
        # Also the end-of-pagination shape: a /404 page still carries
        # __NEXT_DATA__, but tagBooks is null.
        return []
    return [_base_record(b) for b in tag_books.get("books") or []]


def listing_total_pages(raw: RawDoc) -> int:
    payload = next_data(raw)
    props = payload.get("props", {}).get("pageProps") or {}
    return _int(props.get("totalPage")) or 1


def _ld_json_series(raw: RawDoc) -> dict:
    """The TVSeries node is nested inside @graph, not at the top level -- a
    parser reading top-level @type gets nothing."""
    for match in re.finditer(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>', raw.text(), re.S
    ):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        for node in payload.get("@graph") or [payload]:
            if isinstance(node, dict) and node.get("@type") == "TVSeries":
                return node
    return {}


def parse_detail(raw: RawDoc) -> SourceRecord:
    payload = next_data(raw)
    data = payload.get("props", {}).get("pageProps", {}).get("data")
    if not data:
        raise NoNextData(f"no title data in {raw.url}")

    # raw.url is post-redirect, so this is the resolved canonical page.
    record = _base_record(data, url=raw.url)

    # book_sub_title is a genuine alternate title -- and the only real one this
    # project gets from a platform, since DramaBox's are slug variants.
    subtitles = data.get("book_sub_title")
    if isinstance(subtitles, list):
        record.aliases = [str(s) for s in subtitles if s]
    elif subtitles:
        record.aliases = [str(subtitles)]

    # Prefer online_at, but it is literally 0 on the legacy catalogue -- 21 of
    # the first 50 titles ingested, all low t_book_ids from 2023 -- where
    # publish_at is the only date available. Without the fallback, year is
    # missing on a large slice of the back catalogue.
    released = _int(data.get("online_at")) or _int(data.get("publish_at"))
    if released:
        record.released_at = datetime.fromtimestamp(released, tz=UTC)
        record.year = record.released_at.year

    paid_start = _int(data.get("paid_start"))
    if paid_start:
        record.first_paid_episode = paid_start

    update_status = _int(data.get("update_status"))
    if update_status is not None:
        record.completed = update_status == 1

    for tag in data.get("tag_list") or []:
        category = str(tag.get("category_id") or "")
        text = tag.get("text")
        if not text:
            continue
        if category == AUDIENCE_CATEGORY:
            # The only audience/content rating available anywhere in this
            # project. ld+json contentRating is the literal string
            # "RATING NOT KNOWN" on every title, so it is ignored.
            record.audience_rating = str(text)
        if category in CAST_CATEGORIES:
            continue  # cast comes from actor_info, which carries IMDb ids
        namespace = TAG_CATEGORIES.get(category, category)
        record.tags.append((f"{KEY}.{namespace}", str(text)))

    _parse_cast(record, data)

    # `online_base` mixes two kinds of item. A real episode carries
    # chapter_type 1 and a 1-BASED serial_number; a trailer carries
    # chapter_type 2, is_preview 1 and serial_number 0. Measured across 3,558
    # archived payloads: 755 titles carry exactly one preview, always first,
    # never two, and the three markers agree on all 755.
    #
    # Both facts were got wrong here, and a downstream consumer found it. The
    # preview was ingested as an episode, and `serial + 1` renumbered every real
    # episode one too high -- so a viewer who had finished a show was told one
    # episode was still missing, on 755 titles, always the last one.
    previews = 0
    for entry in data.get("online_base") or []:
        chapter_id = entry.get("chapter_id")
        if not chapter_id:
            continue
        if entry.get("is_preview") or entry.get("chapter_type") == 2:
            previews += 1
            continue
        serial = _int(entry.get("serial_number"))
        record.episodes.append(
            EpisodeRecord(
                external_id=str(chapter_id),
                # Taken as published. serial_number is already 1-based for real
                # episodes -- unanimous across 3,530 titles.
                number=serial,
                is_free=(
                    None
                    if record.first_paid_episode is None or serial is None
                    else serial < record.first_paid_episode
                ),
                still_url=entry.get("video_pic") or None,
            )
        )

    # `total` counts the preview too -- all 755 of them -- so the source's own
    # figure has to be corrected by what we just skipped, or episode_count would
    # keep claiming one episode more than exists.
    if previews and record.episode_count is not None:
        record.episode_count = max(record.episode_count - previews, 0)

    series = _ld_json_series(raw)
    score = (series.get("popularityScore") or {}).get("value")
    if isinstance(score, int | float):
        # 4.5-4.9 across the entire catalogue: promotional filler, not a
        # distribution. Stored, but flagged so it can never drive sorting.
        record.rating = float(score)
        record.rating_is_editorial = True

    record.raw_sha256 = raw.sha256
    return record


def _parse_cast(record: SourceRecord, data: dict) -> None:
    """actor_info is the crown jewel: it carries IMDb ids for free.

    Coverage is roughly 20% -- the key exists on every detail page while
    `actors` is empty on most of them. Key-presence is not data-presence.
    """
    actor_info = data.get("actor_info") or {}

    tconst = actor_info.get("imdb_id")
    if tconst:
        record.imdb_tconst = str(tconst)

    for order, actor in enumerate(actor_info.get("actors") or []):
        name = actor.get("actor_name")
        if not name:
            continue
        outside = actor.get("outside_url") or ""
        nconst_match = _IMDB_NAME.search(outside)
        # There is no numeric person id on this source; the fandom URL is the
        # stable per-actor key, with the name as a fallback.
        inside = actor.get("inside_url")
        external_id = person_key(inside) if inside else f"name:{name}"
        record.cast.append(
            PersonCredit(
                external_id=str(external_id),
                name=str(name),
                role="actor",
                billing_order=order,
                avatar_url=actor.get("actor_pic") or None,
                imdb_nconst=nconst_match.group(1) if nconst_match else None,
            )
        )


# --- discovery ---------------------------------------------------------------


#: Language catalogues to crawl. "" is the default (English) tree, which was the
#: ONLY one crawled until now -- and is why a Spanish title could not be found at
#: any point: ReelShort gives each edition its own book_id in its own tree, so a
#: Spanish show was never a candidate rather than being missed.
#:
#: Deliberately a short list rather than all twelve locales the site serves (ar,
#: de, fr, id, it, ja, ko, pt, es, th, tr and English). Each is a near-complete
#: translation of the same catalogue, so every locale added multiplies rows
#: without adding stories, and lengthens the nightly crawl by its own page count:
#: es is 192 pages and pt 156, against English's 380.
LOCALES: tuple[str, ...] = ("", "es", "pt")


def browse_url(page: int, locale: str = "") -> str:
    prefix = f"/{locale}" if locale else ""
    return f"{BASE_URL}{prefix}/{BROWSE_PATH}/{page}"


def detail_url(book_id: str) -> str:
    """The id-only form canonicalises on its own; no slug needed."""
    return f"{BASE_URL}/movie/{book_id}"


def preflight(ctx: Ctx) -> None:
    result = ctx.fetcher.get(browse_url(1), classify=classify)
    if not result.ok:
        raise RuntimeError(f"reelshort preflight failed: {result.verdict}")
    ctx.state["total_pages"] = listing_total_pages(result.doc)

    # Each language tree paginates independently. A locale that fails to answer
    # is skipped rather than failing the run: the English catalogue is the one
    # the index cannot do without.
    pages_by_locale: dict[str, int] = {"": ctx.state["total_pages"]}
    for locale in LOCALES:
        if not locale:
            continue
        probe = ctx.fetcher.get(browse_url(1, locale), classify=classify)
        if not probe.ok:
            log.warning("reelshort locale %s unavailable: %s", locale, probe.verdict)
            continue
        pages_by_locale[locale] = listing_total_pages(probe.doc)
    ctx.state["pages_by_locale"] = pages_by_locale
    # The listing reports the catalogue size exactly. Without it an uncapped
    # run has no denominator, so a multi-hour crawl shows a rising counter and
    # no ETA -- precisely the run where an ETA matters.
    payload = next_data(result.doc)
    props = payload.get("props", {}).get("pageProps") or {}
    ctx.state["catalogue_estimate"] = _int(props.get("total"))


def _listing_page(ctx: Ctx, page: int, locale: str = "") -> list[dict] | None:
    """Returns book dicts, or None at the end of pagination.

    The sentinel is status plus a null tagBooks -- NOT the presence of
    __NEXT_DATA__, which the 404 page also has.
    """
    result = ctx.fetcher.get(browse_url(page, locale), classify=classify)
    if result.verdict is Verdict.NOT_FOUND or not result.ok:
        return None
    payload = next_data(result.doc)
    tag_books = payload.get("props", {}).get("pageProps", {}).get("tagBooks")
    if not tag_books:
        return None
    return tag_books.get("books") or []


def discover(ctx: Ctx, mode: Mode) -> Iterator[Ref]:
    if mode is Mode.INCREMENTAL:
        yield from _discover_incremental(ctx)
    else:
        yield from _discover_full(ctx)


def _discover_full(ctx: Ctx) -> Iterator[Ref]:
    """Loop passes until the unique-id set stops growing.

    The listing is non-deterministic: one full pass returns roughly 10% dupes
    and misses titles outright, while repeated passes converge. Stopping after
    one pass would silently under-collect.
    """
    seen: set[str] = set()
    pages_by_locale = ctx.state.get("pages_by_locale") or {
        "": int(ctx.state.get("total_pages") or 1)
    }

    # Locale by locale, English first. Each tree is its own catalogue with its
    # own book ids, so the saturation loop has to run per tree rather than once
    # across all of them -- a pass that added nothing in Spanish says nothing
    # about whether Portuguese is exhausted.
    for locale in LOCALES:
        total_pages = int(pages_by_locale.get(locale) or 0)
        if not total_pages:
            continue
        for _pass in range(MAX_PASSES):
            added = 0
            for page in range(1, total_pages + 1):
                if ctx.cancelled() or (ctx.max_items and len(seen) >= ctx.max_items):
                    return
                books = _listing_page(ctx, page, locale)
                if books is None:
                    break
                for book in books:
                    book_id = str(book.get("book_id") or "")
                    if not book_id or book_id in seen:
                        continue
                    seen.add(book_id)
                    added += 1
                    yield Ref(external_id=book_id, hint=book)
                    if ctx.max_items and len(seen) >= ctx.max_items:
                        return
            if added == 0:
                break  # this tree is saturated; move to the next language


def _discover_incremental(ctx: Ctx) -> Iterator[Ref]:
    """Diff the new-release shelf by id.

    Deliberately not "stop at the first known title": the shelf is editorial,
    only roughly date-ordered, so an early match does not mean the rest are
    known. Its pagination is also fake, so only page 1 exists.
    """
    result = ctx.fetcher.get(f"{BASE_URL}/{NEW_RELEASE_SHELF}", classify=classify)
    if not result.ok:
        return
    props = next_data(result.doc).get("props", {}).get("pageProps") or {}
    seen = 0
    for book in props.get("list") or []:
        if ctx.cancelled():
            return
        book_id = str(book.get("book_id") or "")
        if book_id:
            yield Ref(external_id=book_id, hint=book)
            seen += 1
            if ctx.max_items and seen >= ctx.max_items:
                return


def fetch_detail(ctx: Ctx, ref: Ref) -> RawDoc:
    return ctx.fetcher.get(detail_url(ref.external_id), classify=classify).doc




def is_detail_url(url: str) -> bool:
    """Which archived URLs `reparse` should re-run through parse_detail."""
    return "/movie/" in url


def is_listing_url(url: str) -> bool:
    return f"/{BROWSE_PATH}/" in url


# --- actor roster ------------------------------------------------------------

FANDOM_ROSTER_PATH = "/fandom/reelshort-actors-actresses/"

_FANDOM_LINK = re.compile(
    r'<a[^>]+href="(https://www\.reelshort\.com/fandom/[^"]+)"[^>]*>(.*?)</a>', re.S
)
_TAGS = re.compile(r"<[^>]+>")
#: /movie/<slug>-<24-hex book_id> -- the id is the tail, and it is what joins an
#: actor page to titles we already hold.
_MOVIE_ID = re.compile(r"https://www\.reelshort\.com/movie/[A-Za-z0-9-]*?([0-9a-f]{24})\b")
_LD_DESCRIPTION = re.compile(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"')
#: The per-actor headshot. Every fandom profile page carries one in <head>, and
#: it is the ONLY place ReelShort publishes a face for people the detail-page
#: cast never mentions -- 113 of its 250 actors had no photo purely because this
#: was not read.
_OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE
)
#: Where headshots actually live: all 245 archived profile pages agree on this
#: one prefix and one host. The guard is a prefix rather than "is it a URL"
#: because og:image is a GENERIC tag -- on a drama page it is the key art, and
#: accepting any absolute URL would happily store cover art as an actor's face.
#: Narrowing it closes that by construction instead of by remembering.
_HEADSHOT_PREFIX = "https://www.reelshort.com/fandom/wp-content/"


def person_key(url: str) -> str:
    """The stable per-actor identifier: a fandom URL with its trailing slash gone.

    This source has no numeric person id, so the fandom URL IS the key -- and it
    arrives by two routes that disagreed about one character. `parse_roster`
    stripped the trailing slash; `_parse_cast` took `inside_url` verbatim, which
    carries one. The same actor therefore landed as two `person` rows: 502 of
    2,306 people were duplicates, and a cast list showed the same name twice --
    once with the roster's bio and no photo, once with the cast row's photo and
    no bio.

    Both paths go through here now, so the join works on exactly the identifier
    both sides publish.
    """
    return (url or "").strip().rstrip("/")


def parse_roster(raw: RawDoc) -> list[PersonRef]:
    """The curated A-Z actor list.

    Plain WordPress HTML -- no __NEXT_DATA__ here, unlike the rest of the site.

    Slugs are irregular and MUST be harvested rather than synthesised: observed
    forms include `adam-huss-01-6-3faa2218/`, `reelshort-actor-aaron-oberst-01-6/`,
    `adam-daniel-actor-01-6/` and at least one typo. The fandom URL doubles as
    the person's external_id, which is what joins this roster to the cast we
    already scrape from detail pages (`actor_info.actors[].inside_url`).
    """
    seen: dict[str, PersonRef] = {}
    for url, label in _FANDOM_LINK.findall(raw.text()):
        name = _TAGS.sub("", label).strip()
        # Skip navigation and category links: real entries are person names.
        if not name or len(name) > 60 or "\n" in name:
            continue
        key = person_key(url)
        if key not in seen:
            seen[key] = PersonRef(external_id=key, name=name, url=url)
    return list(seen.values())


def parse_actor_page(raw: RawDoc) -> dict:
    """A single actor's profile: a bio and the titles they appear in.

    The filmography here is a useful cross-check on cast scraped from detail
    pages -- recon found both routes agreeing exactly for Adam Huss.
    """
    text = raw.text()

    # Yoast emits SEVERAL "description" fields and the actor bio is the long
    # one, so every match has to be weighed -- taking the first and testing its
    # length simply yielded no bio whenever a short site description came first,
    # which is why 13 actors held a page containing a bio and still had none.
    bio = None
    for raw_description in _LD_DESCRIPTION.findall(text):
        # json.loads, NOT unicode_escape: the latter mangles UTF-8, turning a
        # curly apostrophe into "Obersta€™s".
        try:
            candidate = json.loads(f'"{raw_description}"')
        except json.JSONDecodeError:
            candidate = raw_description
        candidate = candidate.strip()
        if len(candidate) > 40 and (bio is None or len(candidate) > len(bio)):
            bio = candidate

    image = _OG_IMAGE.search(text)
    headshot = image.group(1) if image else None
    return {
        "bio": bio,
        "avatar_url": headshot if (headshot or "").startswith(_HEADSHOT_PREFIX) else None,
        "book_ids": sorted(set(_MOVIE_ID.findall(text))),
    }


def roster_url() -> str:
    return BASE_URL + FANDOM_ROSTER_PATH


def roster(ctx: Ctx):
    """Yield every actor on the curated roster. One request."""
    result = ctx.fetcher.get(roster_url(), classify=classify)
    if not result.ok:
        return []
    return parse_roster(result.doc)


def fetch_actor(ctx: Ctx, ref: PersonRef) -> RawDoc:
    return ctx.fetcher.get(ref.url or ref.external_id, classify=classify).doc
