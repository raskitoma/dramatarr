"""NetShort adapter.

Server-rendered Next.js (App Router). No rotating buildId, no WAF challenge
observed, no `202` stub -- closer to GoodShort than to the other two. What makes
it worth adding is a fourth independent catalogue of at least 6,594 English
titles, a genuine `first_paid_episode`, and per-episode durations and stills.
What it does NOT add is cast: zero person fields on 10/10 detail pages and no
person URL among 205,914 sitemap URLs, so it moves cast coverage by exactly zero
(see `roster`).

Site facts that shape this code, all measured during recon:

  * **`robots.txt` disallows `/prod-web-api*`, which is the site's own JSON API.**
    It is therefore out of bounds, permanently and by design, not "not yet
    implemented". Nothing is lost: `grep -c prod-web-api` over 14 saved page
    bodies is 0 in every file -- the pages never call it -- and everything we
    need is inline in the allowed HTML response.
  * **The UA string is load-bearing.** Protego matches user-agents by SUBSTRING,
    and NetShort blanket-disallows seven tokens (`BadBot`, `python-requests`,
    `Scrapy`, `crawler4j`, `HTTrack`, `wget`, `libwww-perl`). `DramadexBot/0.1`
    contains none, and appending any of them -- even parenthetically -- flips the
    whole site to DENY. Changing `settings.user_agent` is a robots decision here.
  * **`/drama/` is the GENRE namespace, not the title namespace.** A title lives
    at `/full-episodes/<slug>-<id>`. Both URL shapes are `<slug>-<id>` and both
    return 200 with valid structured data, so an adapter pointed at `/drama/`
    indexes taxonomy pages as if they were shows and never notices. The id
    spaces are disjoint: 5,848 `/drama/` ids vs 55,138 `/full-episodes/` ids,
    intersection exactly 0.
  * **The id-only URL resolves** (`/full-episodes/<id>` -> 200 with the right
    TVSeries), so unlike GoodShort the slug is not load-bearing and `external_id`
    is the bare numeric id. A retitle does not mint a new record.
  * **`shortPlayLibraryId` is a published cross-locale key** -- see `LOCALES`.
  * **There is no rating anywhere.** The 4.7 on every page is the app's Google
    Play score, byte-identical catalogue-wide, self-identified by the adjacent
    i18n key as "Google Play average". This is the GoodShort
    `SoftwareApplication` trap one layer deeper, and this adapter publishes no
    rating at all.
  * **No status field exists** (`serialStatus`, `isEnd`, `totalChapter` all
    absent), so `completed` is never set. Nothing in the markup distinguishes a
    finished 70-episode show from an ongoing one at 70.
  * **Dates are unusable.** `TVEpisode.datePublished` is a series-level constant
    -- identical for episodes 1, 5, 70 AND for phantom 999 -- and
    `VideoObject.uploadDate` clusters in seconds-wide batches that read as an
    ingest timestamp. They disagree by three months where both exist, so
    `published_at` and `year` are deliberately left None rather than publishing a
    plausible wrong date.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator

from app.fetch.types import RateSpec, RawDoc, Verdict
from app.precedence import PLATFORM
from app.sources.base import Ctx, EpisodeRecord, Mode, Ref, SourceRecord

log = logging.getLogger(__name__)

KEY = "netshort"
STUDIO_NAME = "NetShort"
#: Authoritative for its own catalogue; see app/precedence.py.
KIND = PLATFORM
#: The apex. `www.netshort.com` 301s here and serves a byte-identical robots.txt.
BASE_URL = "https://netshort.com"
#: external_id is the bare numeric shortPlayId -- the id-only form resolves.
WATCH_URL_TEMPLATE = "https://netshort.com/full-episodes/{external_id}"

#: No Crawl-delay and no Request-rate are published. Every HTML page returns
#: `cf-cache-status: DYNAMIC`, so every title fetch is origin load. Rate
#: behaviour is essentially UNMEASURED -- roughly 27 header-captured requests at
#: 1 req/s from one IP saw no 429, which is not evidence about sustained
#: crawling. Deliberately conservative until a staircase probe says otherwise.
RATE = RateSpec(concurrency=2, min_interval=1.0)

#: Robots-disallowed. `/prod-web-api` is the site's JSON API and must never be
#: requested; the rest are app and legal surfaces. `*/agreement/*` denies the
#: apex form too, because the leading `*` matches the empty string.
FORBIDDEN_PATHS = (
    "/prod-web-api", "/welcome", "/sharing", "/base", "/appActivity",
    "/7xW2", "/adhtml/", "/socia/", "/agreement/",
)

#: Representative paths this adapter requests, for robots_check.
CRAWL_PATHS = (
    "/",
    "/sitemap_netshortcom.xml",
    "/site_list_1.xml",
    "/drama/Good%20vs.%20Evil-1983832035849748482",
    "/full-episodes/love-lights-my-way-back-home-1808387443220733954",
    "/episode/right-beside-me-1808055875428081665-ep-2",
)

#: NetShort runs seventeen language catalogues. Only these are crawled, for the
#: same reason ReelShort's are limited: each additional locale is a near-complete
#: translation of the same stories, so it multiplies rows without adding titles.
#: en+es+pt is 15,621 titles of 55,138; all seventeen would be 110,276 requests.
#: English is unprefixed. Note `zh` (4,548) and `ja` (4,529) are each LARGER than
#: `pt` -- this is a project-consistency choice, not a size-optimal one.
LOCALES: tuple[str, ...] = ("", "es", "pt")

SITEMAP_INDEX = "/sitemap_netshortcom.xml"
#: Genre pages paginate 3-7 deep. Bounded so a markup change that made every
#: page advertise a next link cannot spin forever.
MAX_GENRE_PAGES = 12
#: How far `_enclosing_object` may widen before giving up on a key.
_MAX_WIDEN = 24
NS_GENRE = "netshort.genre"

#: The `id` attribute PRECEDES `type`, so GoodShort's `_LD_JSON` regex -- which
#: anchors on `<script type=`  -- matches ZERO blocks here and cannot be copied.
_LD_JSON = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I
)
_OG_IMAGE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', re.I)
#: A title id is the 19-digit tail of a /full-episodes/ path. `/page/N` suffixes
#: are episode-list pagination and are not titles.
_TITLE_PATH = re.compile(r"/full-episodes/([^/?#]*?)(\d{15,25})/?$")
_EPISODE_SUFFIX = re.compile(r"-ep-(\d+)$")
#: A title id sitting in an /episode/ URL, with or without the -ep-N suffix.
#: The sitemaps stopped publishing /full-episodes/ URLs entirely, so this is now
#: the only route from a sitemap to a title.
_EPISODE_TITLE_PATH = re.compile(r"/episode/[^/?#]*?(\d{15,25})(?:-ep-\d+)?/?$")
_LOC = re.compile(r"^/([a-z]{2})/")
_PAGE_HREF = re.compile(r'href="([^"]*?/page/(\d+))"')
_PAGE_NUM = re.compile(r"/page/(\d+)/?$")


class NoStructuredData(RuntimeError):
    pass


# --- classification ----------------------------------------------------------


def classify(doc: RawDoc) -> Verdict:
    """Status first, then two NetShort-specific rules.

    **Never key on the strings `404` or `notFound`.** Both are Next.js RSC
    boilerplate present on perfectly healthy pages -- the flight carries
    `"notFound":"$undefined"` and the i18n bundle ships a `messages.404.*` block.
    A marker-string check would classify the whole catalogue as missing.

    The genuine 404 is recognisable by absence instead: it carries no `ld+json`
    and no `<title>` tag at all.
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

    if b"application/ld+json" not in doc.body:
        return Verdict.EMPTY

    # The soft-404, and the reason "200 + ld+json + TVSeries = success" is unsafe
    # on this host. A fabricated episode number returns 200 with a ~300KB page
    # and a TVEpisode node reporting that very number -- and reporting
    # `isAccessibleForFree: true`, so a naive crawl publishes non-existent FREE
    # episodes and probing -ep-N until failure never terminates. The tell is the
    # VideoObject: present on every real episode, absent on every phantom.
    if "/episode/" in doc.url:
        body = doc.body
        if b'"TVEpisode"' in body and b'"VideoObject"' not in body:
            return Verdict.NOT_FOUND
    return Verdict.OK


# --- payload extraction ------------------------------------------------------


def ld_graph(raw: RawDoc) -> list[dict]:
    """The schema.org nodes, flattened out of the single real block.

    `application/ld+json` occurs TWICE in the markup but only one is a script
    tag: the second is a Next.js flight string that merely mentions the type. A
    parser counting string occurrences double-parses the page.
    """
    text = raw.body.decode("utf-8", "replace")
    nodes: list[dict] = []
    for chunk in _LD_JSON.findall(text):
        try:
            parsed = json.loads(chunk.strip())
        except json.JSONDecodeError:
            continue  # one malformed block must not lose the others
        for item in parsed if isinstance(parsed, list) else [parsed]:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            nodes.extend(n for n in graph if isinstance(n, dict)) if isinstance(
                graph, list
            ) else nodes.append(item)
    return nodes


def _of_type(nodes: list[dict], wanted: str) -> dict | None:
    for node in nodes:
        if node.get("@type") == wanted:
            return node
    return None


def _forward_object(text: str, start: int) -> str | None:
    """The `{...}` beginning at `start`, tracking strings and escapes."""
    depth, i, in_string, escaped = 0, start, False, False
    while i < len(text):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def _enclosing_object(text: str, at: int, field: str) -> dict | None:
    """The object owning the key at `at`, found by widening until one PARSES.

    Candidates are simply every `{` to the left, nearest first. There is
    deliberately NO depth counting: a brace in a drama title is
    indistinguishable from structure while reading right-to-left, and counting
    made it actively harmful -- a `}` in the title paired itself with the card's
    real opening brace, so the correct object was skipped and never tried.

    Validation does the work instead. A candidate counts only if it parses as
    JSON and carries `field`, so junk candidates fail harmlessly and the search
    widens to the real object. The old version accepted an overshoot silently
    and returned a card with no `shortPlayLibraryId`: `locale_key` came back
    None and that title failed to link its Spanish and Portuguese editions,
    while title, poster, episode_count and tags stayed correct and the verdict
    stayed OK. Nothing would have reported it.

    A whole-document forward scan is not an option: the flight is embedded in
    HTML, and one unbalanced quote anywhere earlier desynchronises every span
    after it.
    """
    i, tried = at, 0
    while i >= 0 and tried < _MAX_WIDEN:
        if text[i] == "{":
            tried += 1
            chunk = _forward_object(text, i)
            if chunk:
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and field in parsed:
                    return parsed
        i -= 1
    return None


def flight_objects(raw: RawDoc, field: str) -> list[dict]:
    """Every RSC flight object carrying `field`, brace-matched and parsed.

    The flight is embedded in JS string literals, so the JSON inside is
    backslash-escaped and has to be unescaped before it will parse.
    """
    text = raw.body.decode("utf-8", "replace").replace('\\"', '"').replace("\\\\", "\\")
    out: list[dict] = []
    seen: set[str] = set()
    for match in re.finditer(rf'"{re.escape(field)}"\s*:', text):
        parsed = _enclosing_object(text, match.start(), field)
        if parsed is None:
            continue
        # The same card is reachable from several keys on a page that repeats a
        # rail; dedupe on the id so callers see each object once.
        marker = str(parsed.get("shortPlayId") or id(parsed))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(parsed)
    return out


def external_id_from_url(url: str) -> str | None:
    """`https://netshort.com/full-episodes/love-lights-…-1808387443220733954`
    -> `1808387443220733954`. Returns None for `/page/N` and for `/drama/`."""
    match = _TITLE_PATH.search(url.split("?")[0].split("#")[0])
    return match.group(2) if match else None


def title_id_from_episode_url(url: str) -> str | None:
    """`…/episode/right-beside-me-1808055875428081665-ep-2` -> the title id.

    Every episode of a show carries its TITLE's id, not an episode id, so a
    sitemap of episode URLs collapses to a set of titles. Verified against the
    live database: `1808055875428081665` is "Right Beside Me", already stored.
    """
    match = _EPISODE_TITLE_PATH.search(url.split("?")[0].split("#")[0])
    return match.group(1) if match else None


def locale_of(url: str) -> str:
    """The locale path segment, `""` for the unprefixed English tree.

    NOTE the path segment is NOT the hreflang code: `zh-CN` lives at `/cn/` and
    `zh-TW` at `/zh/`.
    """
    path = url.split("://", 1)[-1]
    path = path[path.find("/") :] if "/" in path else "/"
    match = _LOC.match(path)
    return match.group(1) if match else ""


def normalize_lang(value: str | None) -> str | None:
    """`en-US` -> `en`. The primary subtag only, so `?lang=es` means the same
    thing across every source -- ReelShort already stores bare `en`/`es`."""
    # Only a string. schema.org allows a Language OBJECT here, and stringifying
    # one would write "{'@type': 'language', ...}" into TitleSource.lang and
    # surface it through ?lang= and X-Episode-Languages.
    if not isinstance(value, str):
        return None
    return value.strip().replace("_", "-").split("-")[0].lower() or None


def episode_number_from_url(url: str) -> int | None:
    """**Episode 1 has no `-ep-1` suffix**; its URL is the bare title path.

    `-ep-1` occurs 0 times in 20,467 episode URLs and the lowest suffix that
    exists is `-ep-2`, while 1,496 ids carry a bare URL. The source confirms the
    bare form IS episode 1: `hasPart.itemListElement[0]` has `episodeNumber: 1`
    and no suffix. An adapter that discovers episodes by regexing `-ep-(\\d+)`
    silently drops episode 1 and undercounts by exactly one -- the same class of
    bug as ReelShort's trailer off-by-one, arriving from the other direction.
    """
    path = url.split("?")[0].split("#")[0].rstrip("/")
    if "/episode/" not in path:
        return None
    match = _EPISODE_SUFFIX.search(path)
    return int(match.group(1)) if match else 1


def episode_external_id(title_id: str, number: int | None) -> str | None:
    """The stable id for one episode: `<title id>-ep-<n>`.

    Deliberately NOT the URL tail. The tail carries the slug, and the slug is
    both mutable and unbounded: `episode.external_id` is `String(128)` while a
    real CJK-slugged title in the English tree yields a 130-character tail by
    episode 1 and 136 by episode 20. Postgres rejects that with
    StringDataRightTruncation; SQLite ignores column lengths entirely, which is
    why a full offline suite passed while the first real crawl would have
    aborted -- and, because `discover` never reads the watermark, aborted at the
    same row on every retry.

    Keying on the id also makes a retitle harmless. The slug form minted a
    SECOND set of rows for every episode when a title was renamed (verified on
    Postgres: 20 rows became 40, each number twice, with nothing to collapse
    them), which is exactly the reasoning that put the bare id on the Title.

    Both parsers derive ids through this function so they agree; keying detail
    rows on the URL tail and episode-page rows on `episodeId` gave the same
    episodes two disjoint id spaces.
    """
    if not title_id or number is None:
        return None
    return f"{title_id}-ep-{number}"


def watch_url_for(external_id: str, locale: str = "") -> str:
    prefix = f"/{locale}" if locale else ""
    return f"{BASE_URL}{prefix}/full-episodes/{external_id}"


def _episode_name(raw_name: str | None, number: int | None, series: str) -> str | None:
    """Strip the `EP N - ` prefix, and drop the name entirely when it is
    synthesized from the series title.

    The ld+json manufactures `"EP 1 - <series title>"` when the source has no
    episode title -- measured on 9 of 14 sampled titles, while the other 5 carry
    real ones ("EP 1 - The Jinxed Twin"). Keeping them all writes up to 20 fake
    episode names per title; dropping them all discards real data on a third of
    the catalogue. So the prefix is removed and the remainder compared to the
    series name.
    """
    if not raw_name:
        return None
    name = raw_name.strip()
    if number is not None:
        name = re.sub(rf"^EP\s*{number}\s*[-–—]\s*", "", name).strip()
    if not name or name == series.strip():
        return None
    return name


# --- parsers -----------------------------------------------------------------


def parse_detail(raw: RawDoc) -> SourceRecord:
    """One title, from BOTH layers of the same response.

    The schema.org graph carries almost everything, but `shortPlayLibraryId` --
    the cross-locale key -- exists only in the RSC flight, so the flight is
    mandatory even though the ld+json looks sufficient.
    """
    nodes = ld_graph(raw)
    series = _of_type(nodes, "TVSeries")
    if series is None:
        raise NoStructuredData(f"no TVSeries node at {raw.url}")

    external_id = external_id_from_url(str(series.get("url") or "")) or \
        external_id_from_url(raw.url) or ""
    # The object describing THIS title, not a recommendation card. The page
    # carries 25 objects with the same shape and the title's is NOT reliably
    # first -- a first-match regex mis-assigned the poster on 5 of 10 pages.
    own: dict = {}
    for obj in flight_objects(raw, "shortPlayLibraryId"):
        if str(obj.get("shortPlayId") or "") == external_id:
            own = obj
            break

    has_part = series.get("hasPart")
    items = []
    if isinstance(has_part, dict):
        items = has_part.get("itemListElement") or []
    elif isinstance(has_part, list):
        items = has_part

    title = str(series.get("name") or "").strip()
    episodes: list[EpisodeRecord] = []
    seen_episodes: set[str] = set()
    for element in items:
        if not isinstance(element, dict):
            continue
        item = element.get("item") if isinstance(element.get("item"), dict) else element
        url = str(item.get("url") or "")
        number = item.get("episodeNumber")
        number = int(number) if isinstance(number, (int, str)) and str(number).isdigit() else \
            episode_number_from_url(url)
        episode_id = episode_external_id(external_id, number)
        if not episode_id or episode_id in seen_episodes:
            # The SOURCE publishes duplicate rows: "The Hidden Tyrant 2"
            # (2022149023086936065) lists 20 hasPart entries whose episodeNumbers
            # are {1: TWICE, 2..19: once} -- the first two rows are identical,
            # same bare URL, both numbered 1. Any stable id derivation therefore
            # yields the same id twice and the INSERT violates
            # uq_episode_external, which aborted a 15k-title crawl 2,251 titles
            # in. Keep the first and drop the repeat; the count comes from
            # `numberOfEpisodes` regardless, so nothing is lost.
            continue
        seen_episodes.add(episode_id)
        episodes.append(
            EpisodeRecord(
                external_id=episode_id,
                number=number,
                name=_episode_name(item.get("name"), number, title),
                # published_at deliberately unset -- see the module docstring.
            )
        )

    tags: list[tuple[str, str]] = []
    genres = series.get("genre") or []
    # A single-genre title publishes a bare string, and iterating that yields one
    # tag per CHARACTER.
    for genre in genres if isinstance(genres, list) else [genres]:
        if isinstance(genre, str) and genre.strip():
            tags.append((NS_GENRE, genre.strip()))

    poster = series.get("image")
    if isinstance(poster, list):
        poster = poster[0] if poster else None
    if isinstance(poster, dict):
        poster = poster.get("url")
    if not poster:
        match = _OG_IMAGE.search(raw.body.decode("utf-8", "replace"))
        poster = match.group(1) if match else None

    count = series.get("numberOfEpisodes")
    return SourceRecord(
        external_id=external_id,
        title=title,
        url=str(series.get("url") or raw.url),
        lang=normalize_lang(series.get("inLanguage")),
        # Read verbatim, never computed: the library id is genuinely independent
        # of the play id (0 of 312 cases where they are equal, and the offsets
        # between them have no fixed relationship).
        locale_key=str(own.get("shortPlayLibraryId") or "") or None,
        synopsis=(str(series.get("description")).strip() or None)
        if series.get("description") else None,
        # The DECLARED count, never len(episodes): `numberOfItems` equals it
        # (70/70, 80/80) while `itemListElement` is hard-capped at 20 on every
        # page sampled. They are separate facts.
        episode_count=int(count) if isinstance(count, (int, str)) and str(count).isdigit() else None,
        poster_url=str(poster) if poster else None,
        tags=tags,
        episodes=episodes,
        # No rating, no cast, no year, no completed -- see the module docstring.
        cast=[],
    )


def parse_listing(raw: RawDoc) -> list[SourceRecord]:
    """Every show on a `/drama/` genre page.

    Kept because it makes discovery cheap -- roughly 45 shows per request versus
    one -- not because it holds unique fields. Unlike DramaBox, the detail
    response is close to a superset of this, so nothing is lost by fetching it.
    """
    records: list[SourceRecord] = []
    seen: set[str] = set()
    for obj in flight_objects(raw, "shortPlayId"):
        external_id = str(obj.get("shortPlayId") or "")
        if not external_id or external_id in seen:
            continue
        name = obj.get("shortPlayName") or obj.get("shortPlayNameEn")
        if not name:
            continue
        seen.add(external_id)
        count = obj.get("totalEpisode")
        records.append(
            SourceRecord(
                external_id=external_id,
                title=str(name).strip(),
                url=watch_url_for(external_id, locale_of(raw.url)),
                lang=normalize_lang(obj.get("language")),
                locale_key=str(obj.get("shortPlayLibraryId") or "") or None,
                synopsis=(str(obj.get("shotIntroduce")).strip() or None)
                if obj.get("shotIntroduce") else None,
                episode_count=int(count) if isinstance(count, (int, str))
                and str(count).isdigit() else None,
                poster_url=str(obj.get("shortPlayCover") or "") or None,
            )
        )
    return records


def parse_episode_page(raw: RawDoc) -> SourceRecord:
    """The third response, and the only one carrying the paywall boundary.

    ONE `/episode/` fetch returns `videoEpisodeInfos` with `isLock` for the whole
    series, so the boundary for an N-episode show costs one request rather than
    N. Do NOT derive it from the play sitemap instead: `(max -ep-N) + 1` matched
    on 2 of 2 titles, which is a coincidence with a sample size, not a rule.

    No playback URL is read here. The page carries signed, expiring CDN links and
    this is a metadata indexer -- see `Episode` in app/models.py.
    """
    nodes = ld_graph(raw)
    series = _of_type(nodes, "TVSeries")
    external_id = external_id_from_url(str((series or {}).get("url") or "")) or ""

    by_number: dict[int, dict] = {}
    for obj in flight_objects(raw, "videoEpisodeInfos"):
        for entry in obj.get("videoEpisodeInfos") or []:
            if not isinstance(entry, dict):
                continue
            number = entry.get("episodeNo")
            if isinstance(number, (int, str)) and str(number).isdigit():
                by_number.setdefault(int(number), entry)

    first_paid = None
    for number in sorted(by_number):
        if by_number[number].get("isLock"):
            first_paid = number
            break

    episodes = [
        EpisodeRecord(
            # Same derivation as parse_detail, so the two describe the same rows.
            external_id=episode_external_id(external_id, number) or str(number),
            number=number,
            is_free=not entry.get("isLock") if entry.get("isLock") is not None else None,
        )
        for number, entry in sorted(by_number.items())
    ]
    return SourceRecord(
        external_id=external_id,
        title=str((series or {}).get("name") or "").strip(),
        first_paid_episode=first_paid,
        episodes=episodes,
    )


# --- discovery ---------------------------------------------------------------


def current_page(url: str) -> int:
    match = _PAGE_NUM.search(url.split("?")[0].split("#")[0])
    return int(match.group(1)) if match else 1


def next_page_url(raw: RawDoc) -> str | None:
    """The next genre page, or None at the end of the walk.

    The markup carries exactly ONE `/page/N` href -- a next link, not a page
    list -- so this follows it the way the GoodShort adapter follows
    `rel="next"`, rather than reading a total and counting. Reading only page 1
    left 2/3 to 6/7 of every genre invisible to the ONE route that exists to
    find titles the sitemaps omit: no `/drama/` URL in any sitemap carries a
    `/page/` suffix, so nothing else would ever reach them.
    """
    want = current_page(raw.url) + 1
    for href, number in _PAGE_HREF.findall(raw.body.decode("utf-8", "replace")):
        if int(number) == want:
            return href if href.startswith("http") else f"{BASE_URL}{href}"
    return None


def preflight(ctx: Ctx) -> None:
    """Nothing to establish. Unlike DramaBox there is no rotating buildId to
    read: the plain-HTML fetch never references one."""


def _sitemap_locs(raw: RawDoc) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", raw.body.decode("utf-8", "replace"))


def discover(ctx: Ctx, mode: Mode) -> Iterator[Ref]:
    """Union of two routes, because neither is complete on its own.

    (a) The `site_list_N.xml` sitemaps. (b) The `/drama/` genre pages. Route (b)
    is mandatory rather than an optimisation: a live English title
    (`crushing-the-village-bully-2090980782421671938`) and all eight of its
    locale siblings appear in NONE of the six sitemaps, and it is not merely new
    -- higher ids are present. So the sitemaps are demonstrably incomplete and
    "6,594 English titles" is a floor of unknown tightness.
    """
    seen: set[str] = set()
    newest: str | None = None

    def emit(external_id: str, url: str | None = None) -> Ref | None:
        nonlocal newest
        if external_id in seen:
            return None
        seen.add(external_id)
        # The MAXIMUM, not the first seen: sitemap order is not id order, so
        # taking the first stored the oldest id in the catalogue and published it
        # as the source's progress marker on the dashboard.
        if newest is None or external_id > newest:
            newest = external_id
        return Ref(external_id=external_id, url=url or watch_url_for(external_id))

    index = ctx.fetcher.get(f"{BASE_URL}{SITEMAP_INDEX}", classify=classify)
    children: list[str] = []
    if index.ok:
        # `site_play_*` holds episode URLs, and an episode URL carries its
        # TITLE's id -- which is the only route from a sitemap to a title now.
        #
        # It used to be `site_list_*`, which published /full-episodes/ URLs
        # directly. Those files 404 today and the index no longer lists them:
        # NetShort restructured within hours of the recon that measured them, so
        # a crawl that had worked returned zero and reported success. That is
        # why `site_allsearch_*` is excluded by name rather than by accident --
        # it is 200 files of /shortsearch/ SEO pages carrying no title ids, and
        # a laxer filter would fetch every one of them for nothing.
        children = [u for u in _sitemap_locs(index.doc) if "site_play_" in u]

    for child in children:
        if ctx.cancelled():
            return
        result = ctx.fetcher.get(child, classify=classify)
        if not result.ok:
            continue
        for url in _sitemap_locs(result.doc):
            if locale_of(url) not in LOCALES:
                continue
            external_id = title_id_from_episode_url(url) or external_id_from_url(url)
            if not external_id:
                continue
            ref = emit(external_id)
            if ref is not None:
                yield ref
                if ctx.max_items and len(seen) >= ctx.max_items:
                    if newest:
                        ctx.set_watermark(newest)
                    return
        ctx.progress(len(seen), None, "sitemaps")

    # Route (b). Incremental runs skip it: it exists to close a completeness gap,
    # and the next full run will.
    if mode is Mode.INCREMENTAL:
        if newest:
            ctx.set_watermark(newest)
        return

    # Genre pages are seeded from the SITE ROOT, deliberately not from the
    # sitemap index. Both routes used to read the same child list, so one
    # renamed file blinded the adapter completely -- route (b) exists precisely
    # to cover what the sitemaps miss, and it cannot do that while it depends on
    # them. The root publishes 51 genre links and is the one URL that cannot be
    # renamed out from under us.
    # ONE ROOT PER LOCALE. Seeding only from `/` indexed the catalogue 100% in
    # English -- 6,622 titles, zero es, zero pt -- because the English root
    # publishes only unprefixed `/drama/` links, so the locale filter had nothing
    # to admit. Route (a) cannot help either: `site_play_*` carries no
    # locale-prefixed URL at all, so the sitemaps are an English-only surface and
    # es/pt are reachable ONLY through here.
    #
    # NOTE the missing trailing slash. `/es/` answers 308 to `/es`, so seeding
    # `f"{BASE_URL}/{locale}/"` yields a bodyless redirect, `root.ok` is false,
    # and the locale silently contributes nothing while the run reports success
    # -- the same zero-yield-reported-as-fine failure this adapter has already
    # shipped twice.
    genre_ids: list[str] = []
    for locale in LOCALES:
        if ctx.cancelled():
            return
        root_url = f"{BASE_URL}/" if not locale else f"{BASE_URL}/{locale}"
        root = ctx.fetcher.get(root_url, classify=classify)
        if not root.ok:
            log.warning("netshort: locale root %s did not answer", root_url)
            continue
        body = root.doc.body.decode("utf-8", "replace")
        found = 0
        for href in dict.fromkeys(re.findall(r'href="([^"]*/drama/[^"?#]+)"', body)):
            full = href if href.startswith("http") else f"{BASE_URL}{href}"
            if locale_of(full) in LOCALES:
                genre_ids.append(full)
                found += 1
        log.info("netshort: locale %r seeded %d genre pages", locale or "en", found)

    for page in genre_ids:
        url: str | None = page
        walked = 0
        while url and walked < MAX_GENRE_PAGES:
            if ctx.cancelled():
                return
            result = ctx.fetcher.get(url, classify=classify)
            if not result.ok:
                break
            walked += 1
            for record in parse_listing(result.doc):
                ref = emit(record.external_id, record.url)
                if ref is not None:
                    yield ref
                    if ctx.max_items and len(seen) >= ctx.max_items:
                        if newest:
                            ctx.set_watermark(newest)
                        return
            ctx.progress(len(seen), None, "genre pages")
            url = next_page_url(result.doc)

    if newest:
        ctx.set_watermark(newest)


def fetch_detail(ctx: Ctx, ref: Ref) -> RawDoc:
    """By id, with no slug: the id-only form is served directly, so this avoids
    the 301 the sitemap's lowercase-hyphen slug would cost."""
    url = ref.url or watch_url_for(ref.external_id)
    return ctx.fetcher.get(url, classify=classify).doc


def is_detail_url(url: str) -> bool:
    """Does this archived response hold one title?

    `app/jobs/reparse.py` calls this unguarded, so an adapter without it raises
    AttributeError before parsing anything -- taking out the very recovery path
    `catalog.py` depends on when it quarantines a failed detail parse.
    """
    return "/full-episodes/" in url and external_id_from_url(url) is not None


def is_listing_url(url: str) -> bool:
    """A `/drama/` genre collection. Needed as well as `is_detail_url`: reparse
    defaults this to a predicate that skips everything, so archived genre pages
    would be silently passed over."""
    return "/drama/" in url


def roster(ctx: Ctx) -> None:
    """NetShort publishes no cast, so there is no roster to enumerate.

    Recorded so it is not re-litigated: the UI template ships i18n labels
    `"yx08":"Director"` and `"yx09":"Stars"` (zh `導演`/`主演`), so grepping the
    markup for "Stars" returns two hits on EVERY detail page and looks like
    evidence of a cast block. The data behind those labels is never populated --
    0 person fields on 10/10 detail pages, and no person URL among 205,914
    sitemap URLs.
    """
    return None
