"""Turning a web search into a linkable identifier.

TMDB's title search is the normal way into metadata, and for this household's library it
frequently fails: short-form Asian drama is filed under a title nobody typed, in a script the
admin cannot type, or not filed at all under the language the instance asks TMDB for. What is
left is a folder name and no metadata.

A *web* search does find these shows. Not as prose — as a **link**. The first page of results
for a drama's folder name is a TMDB page, an IMDb page, a Wikipedia article, a MyDramaList
entry. Every one of those carries its own identifier in the URL, and an identifier is something
`GET /api/library/tmdb/lookup` can already turn into real metadata. So this module reads
identifiers out of result URLs and hands them to the admin as one-click matches.

Two rules follow from that, and they are the whole design:

* **`extract_ids` never fetches anything.** It is a pure function over a URL string. The value
  is the identifier the URL already contains; following the link would mean this server making
  requests to whatever a search engine crawled, which is neither needed nor wanted. Nothing in
  this module opens a result page — see the SSRF note in `app.services.images`, whose split
  holds here unchanged: the configured SearXNG instance is trusted, every URL it hands back is
  not.
* **A snippet is never metadata.** SearXNG's `content` field is a truncated extract of someone
  else's page, cut mid-sentence, sometimes carrying a cookie banner or a navigation menu. It is
  genuinely useful for a human choosing between five results and worthless as a stored
  synopsis, so it travels to the UI as *display text next to a link* and is never written to
  `series.overview`. Structured metadata comes from TMDB, which is built to be consumed that
  way; this module's job ends at "here is which record it is".

The SearXNG configuration and its HTTP handling are reused wholesale from
`app.services.images` — same instance, same admin settings, same `SEARXNG_JSON_DISABLED`
diagnosis. There is deliberately no second place to configure the same instance, which is why
the two private helpers below are imported rather than reimplemented.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.schemas.library import DiscoveredIds, DiscoveryResult, DiscoveryResults
from app.services.images import (
    SEARXNG_SEARCH_PATH,
    SearxngConfig,
    _configured_searxng,
    _searxng_get,
)

log = logging.getLogger(__name__)

#: The only provider that answers here. Google CSE is an *image* API; it has no general-web
#: mode in this codebase, so `_configured_searxng` refusing it with SEARCH_NOT_CONFIGURED is
#: the right answer rather than a gap.
PROVIDER = "searxng"

#: Not `images`. This is the one place that asks the instance for the ordinary web index.
DISCOVER_CATEGORY = "general"

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

#: Snippets are already truncated by the engine that produced them; this is a second bound so
#: one engine's idea of an extract cannot bloat the response.
SNIPPET_MAX_CHARS = 400
TITLE_MAX_CHARS = 300

_SCHEMES = frozenset({"http", "https"})


# ==========================================================================================
# extract_ids — the heart of this module
# ==========================================================================================
#
# Everything below is matched against the *parsed* URL: the host decides which site's table
# entry applies, and the pattern runs against the path alone. Matching the whole URL string
# would make `themoviedb.org/search?query=tv/1396` look like a TMDB link, and a search-results
# page is exactly what a web search returns a lot of.
#
# The shapes each site is allowed to arrive in are not hypothetical — they are what search
# engines actually hand back: with and without `www`, on a mobile host, behind a language
# prefix, carrying a slug, a tracking query string, a trailing sub-page or a fragment.

#: `/es`, `/pt-br`, `/es-MX` — the language prefix IMDb and TMDB both accept. Optional, and
#: harmless when absent: `/tv/1396` briefly matches `tv` as a language and then backtracks.
_LANG_SEGMENT = r"(?:/[a-z]{2}(?:-[a-z]{2,4})?)?"

#: `/tv/1396`, `/tv/1396-breaking-bad`, `/movie/550`, `/es/tv/1396/season/1`.
_TMDB_PATH = re.compile(
    rf"^{_LANG_SEGMENT}/(?P<kind>tv|movie)/(?P<id>\d{{1,9}})(?:-[^/]*)?(?:/.*)?$",
    re.IGNORECASE,
)

#: `/title/tt0903747`, `/es/title/tt0903747/episodes`. The digit bound matches the one
#: `GET /api/library/tmdb/lookup` enforces on `imdbId`, so anything extracted here is
#: something that endpoint will accept.
_IMDB_PATH = re.compile(
    rf"^{_LANG_SEGMENT}/title/(?P<id>tt\d{{5,12}})(?:/.*)?$",
    re.IGNORECASE,
)

#: `en.`, `es.`, `simple.`, `zh-yue.`, and the `.m.` mobile host in front of any of them.
#: A language subdomain is *required*: `wikipedia.org/` is the project's portal page and
#: `www.wikipedia.org/` is the same thing, neither of which identifies an article.
#: The `/wiki/` prefix is matched case-insensitively — `(?i:…)` scoped to the prefix alone,
#: because the *title* is not: `Squid_Game` and `SQUID_GAME` are different strings to
#: Wikipedia's API and the one the URL actually said is the one worth keeping.
_WIKIPEDIA_HOST = re.compile(r"^(?P<lang>[a-z]{2,}(?:-[a-z\d]+)*)(?:\.m)?\.wikipedia\.org$")
_WIKIPEDIA_PATH = re.compile(r"^(?i:/wiki/)(?P<title>[^/].*?)/?$")

#: Hosts that end in `.wikipedia.org` without naming a language.
_NOT_A_LANGUAGE = frozenset({"www", "m", "meta", "commons", "species", "wikitech", "test"})

#: Everything before the first colon in a Wikipedia title, when it is one of these, means the
#: page is not an article. Article titles containing a colon are common (`Mission: Impossible`),
#: so this is a known-namespace list rather than a "has a colon" check.
_WIKIPEDIA_NAMESPACES = frozenset(
    {
        "special",
        "talk",
        "user",
        "user talk",
        "wikipedia",
        "wikipedia talk",
        "project",
        "file",
        "file talk",
        "image",
        "mediawiki",
        "template",
        "template talk",
        "help",
        "help talk",
        "category",
        "category talk",
        "portal",
        "draft",
        "module",
        "book",
    }
)

#: `/wiki/Q108808429`, and the concept URI form `/entity/Q108808429`.
_WIKIDATA_PATH = re.compile(r"^/(?:wiki|entity)/(?P<id>Q\d{1,12})/?$", re.IGNORECASE)

#: `/686327-crash-landing-on-you`, optionally followed by a sub-page. The id is the leading
#: number and the slug is decoration. `/people/1234-someone` does not match, because the first
#: segment there is not the id.
_MYDRAMALIST_PATH = re.compile(r"^/(?P<id>\d{1,9})-[^/]+(?:/.*)?$")

#: DramaBoxDB titles live at `/movie/<id>/<slug>`, with the site's language prefixes in front:
#: `/es/movie/12345/tempest-the-last-mecha`. Everything the app shows comes from the URL — the
#: slug is turned back into a readable title for the picker, and the page is never fetched.
_DRAMABOXDB_PATH = re.compile(
    r"^(?:/(?:[a-z]{2}|zhHans))?/movie/(?P<id>\d{1,12})(?:/(?P<slug>[^/?#]+))?/?$",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class _Site:
    """One recognizable site: which hosts it owns, and how to read its paths."""

    name: str
    domains: tuple[str, ...]
    path: re.Pattern[str]
    #: `(path_match, host) -> ids`, or None when the path matched but says nothing useful.
    build: Callable[[re.Match[str], str], DiscoveredIds | None]


def _build_tmdb(match: re.Match[str], host: str) -> DiscoveredIds | None:
    kind = match.group("kind").lower()
    return DiscoveredIds(
        tmdb_id=int(match.group("id")), tmdb_kind="tv" if kind == "tv" else "movie"
    )


def _build_imdb(match: re.Match[str], host: str) -> DiscoveredIds | None:
    return DiscoveredIds(imdb_id=match.group("id").lower())


def _build_wikipedia(match: re.Match[str], host: str) -> DiscoveredIds | None:
    """`en.wikipedia.org/wiki/Squid_Game` → (`en`, `Squid Game`).

    The title is percent-decoded and its underscores turned back into spaces, which is both
    what a human should read in the picker and what Wikipedia's own API takes as a title. A
    Korean article therefore comes back as 오징어 게임 rather than as an unreadable escape run.
    """
    host_match = _WIKIPEDIA_HOST.match(host)
    if host_match is None:
        return None
    lang = host_match.group("lang")
    if lang in _NOT_A_LANGUAGE:
        return None

    title = unquote(match.group("title")).replace("_", " ").strip()
    if not title or title.casefold() == "main page":
        return None
    prefix, sep, rest = title.partition(":")
    if sep and rest.strip() and prefix.strip().casefold() in _WIKIPEDIA_NAMESPACES:
        return None
    return DiscoveredIds(wikipedia_lang=lang, wikipedia_title=title)


def _build_wikidata(match: re.Match[str], host: str) -> DiscoveredIds | None:
    return DiscoveredIds(wikidata_id=match.group("id").upper())


def _build_mydramalist(match: re.Match[str], host: str) -> DiscoveredIds | None:
    return DiscoveredIds(mydramalist_id=match.group("id"))


def _build_dramaboxdb(match: re.Match[str], host: str) -> DiscoveredIds | None:
    slug = match.group("slug") or ""
    return DiscoveredIds(dramaboxdb_id=match.group("id"), dramaboxdb_slug=slug or None)


#: The table. Adding a site is an entry here and a builder above — nothing else in this module
#: knows the list.
_SITES: tuple[_Site, ...] = (
    _Site("tmdb", ("themoviedb.org",), _TMDB_PATH, _build_tmdb),
    _Site("imdb", ("imdb.com",), _IMDB_PATH, _build_imdb),
    _Site("wikipedia", ("wikipedia.org",), _WIKIPEDIA_PATH, _build_wikipedia),
    _Site("wikidata", ("wikidata.org",), _WIKIDATA_PATH, _build_wikidata),
    _Site("mydramalist", ("mydramalist.com",), _MYDRAMALIST_PATH, _build_mydramalist),
    _Site("dramaboxdb", ("dramaboxdb.com",), _DRAMABOXDB_PATH, _build_dramaboxdb),
)


def _host_owns(host: str, domains: tuple[str, ...]) -> bool:
    """True for the domain itself and for any subdomain of it, and for nothing else.

    The suffix check carries its own dot on purpose. Without it `notthemoviedb.org` matches,
    and a domain that merely *contains* a trusted name is the cheapest way to get a wrong id
    attached to a series.
    """
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def extract_ids(url: str) -> DiscoveredIds | None:
    """Every identifier a result URL carries, or None when it carries none.

    Pure: no network, no database, no settings. Case-insensitive, indifferent to `www`, to a
    trailing slash, to a query string, to a fragment, and to the language prefixes these sites
    use. A single URL can only carry one site's id; a *set* of results carries many, which is
    the point — the admin's answer is usually on the second or third row.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        # A malformed authority (an unbalanced IPv6 bracket, say). Not a link to anything.
        return None
    if parts.scheme.lower() not in _SCHEMES or not host:
        return None

    path = parts.path or "/"
    for site in _SITES:
        if not _host_owns(host, site.domains):
            continue
        match = site.path.match(path)
        if match is None:
            continue
        return site.build(match, host)
    return None


def is_resolvable(ids: DiscoveredIds | None) -> bool:
    """True when dramatarr can turn these ids into metadata *today*.

    TMDB and IMDb both land on `GET /api/library/tmdb/lookup`, so a result carrying either is
    one click from a real match. Wikipedia, Wikidata and MyDramaList ids are recorded and shown
    because they tell the admin they have found the right show, but nothing here resolves them.
    """
    return ids is not None and (ids.tmdb_id is not None or bool(ids.imdb_id))


def dedupe_keys(ids: DiscoveredIds | None) -> tuple[tuple[str, str], ...]:
    """Stable `(kind, id)` pairs for `ids` — the identity a result is deduplicated on.

    Five links to the same TMDB page (the canonical URL, a slugged one, a mobile one and two
    aggregators quoting it) are one answer, and a picker that lists it five times is worse than
    one that lists it once.
    """
    if ids is None:
        return ()
    keys: list[tuple[str, str]] = []
    if ids.tmdb_id is not None:
        keys.append(("tmdb", f"{ids.tmdb_kind or 'tv'}/{ids.tmdb_id}"))
    if ids.imdb_id:
        keys.append(("imdb", ids.imdb_id))
    if ids.wikipedia_title:
        keys.append(("wikipedia", f"{ids.wikipedia_lang or ''}:{ids.wikipedia_title}"))
    if ids.wikidata_id:
        keys.append(("wikidata", ids.wikidata_id))
    if ids.mydramalist_id:
        keys.append(("mydramalist", ids.mydramalist_id))
    return tuple(keys)


# ==========================================================================================
# The search itself
# ==========================================================================================


def search_query(query: str, year: int | None = None) -> str:
    """What actually goes to the instance: the admin's words, plus the year when it helps.

    The year is appended rather than sent as a filter — SearXNG has no year parameter, and a
    bare `2024` next to a title is what makes a search engine separate a drama from its remake.
    It is skipped when the query already says it, so the series-scoped route can default the
    query from a title that already ends in the year.
    """
    text = " ".join((query or "").split())
    if year is None:
        return text
    stamp = str(year)
    if not text:
        return stamp
    return text if stamp in text.split() else f"{text} {stamp}"


def _params(config: SearxngConfig, query: str) -> dict[str, str]:
    """The general-web query.

    `engines` is deliberately **not** sent, even though the configuration has a list. Those are
    the admin's *image* engines, chosen on the artwork screen; naming `bing images` in a general
    search asks SearXNG for a general-category engine that does not exist and every result is
    filtered away. The instance's own general engines are the right set here, and they are what
    it uses when the parameter is absent.
    """
    params = {
        "q": query,
        "format": "json",
        "categories": DISCOVER_CATEGORY,
        "safesearch": str(config.safe_search),
    }
    if config.language:
        params["language"] = config.language
    return params


def _text(value: Any, limit: int) -> str:
    """Collapse whitespace and bound the length. Engines emit newlines and runs of spaces."""
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[:limit].rstrip()}…"


def _source_domain(url: str) -> str:
    """`https://www.themoviedb.org/tv/1396` → `themoviedb.org`.

    Shown next to every snippet. An extract with no visible provenance is the part of a web
    search an admin cannot judge, and this is the cheapest way to give them that judgement.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_http_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _result(item: Any) -> DiscoveryResult | None:
    """One SearXNG row as a candidate, or None when there is nothing to show or click."""
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if not isinstance(url, str) or not _is_http_url(url):
        return None

    ids = extract_ids(url) or DiscoveredIds()
    return DiscoveryResult(
        title=_text(item.get("title"), TITLE_MAX_CHARS),
        url=url,
        # Display only. This is an extract of a page dramatarr does not control and it never
        # becomes `series.overview` — see the module docstring.
        snippet=_text(item.get("content"), SNIPPET_MAX_CHARS),
        engine=_text(item.get("engine"), 64),
        source_domain=_source_domain(url),
        ids=ids,
        resolvable=is_resolvable(ids),
    )


def _unresponsive(payload: dict[str, Any]) -> list[str]:
    """The engines the instance says failed, as plain names.

    SearXNG reports these as `[[name, reason], …]`. Carrying them is what lets an empty result
    list be attributed correctly, and this household proved why that matters: of fourteen
    general engines on their instance, only two actually answered — Google was returning HTTP
    200 with a JavaScript interstitial, which SearXNG parses as zero results and reports as no
    error at all. Without this field an empty answer looks like "the web has nothing about
    this" when the truth is "the engines that could have answered did not".

    The silent-zero case is *not* covered by this and cannot be: an engine that returns a
    valid empty page is indistinguishable from one with nothing to say. What this does cover
    is every failure the instance itself noticed, which is the difference between a screen that
    misattributes and one that is merely incomplete.
    """
    rows = payload.get("unresponsive_engines")
    if not isinstance(rows, list):
        return []
    names: list[str] = []
    for row in rows:
        if isinstance(row, str):
            names.append(row)
        elif isinstance(row, (list, tuple)) and row:
            names.append(str(row[0]))
    return sorted(dict.fromkeys(name for name in names if name))


def rank(results: Iterable[DiscoveryResult]) -> list[DiscoveryResult]:
    """Deduplicate by identifier, then float the actionable rows to the top.

    The sort is stable and has exactly two tiers, so everything that is not one click from a
    match keeps the order the instance's engines put it in. Results carrying no id at all are
    deduplicated by URL instead, which collapses the same page reported by three engines.
    """
    seen_ids: set[tuple[str, str]] = set()
    seen_urls: set[str] = set()
    kept: list[DiscoveryResult] = []

    for result in results:
        keys = dedupe_keys(result.ids)
        if keys:
            if any(key in seen_ids for key in keys):
                continue
            seen_ids.update(keys)
        else:
            normalized = result.url.rstrip("/")
            if normalized in seen_urls:
                continue
            seen_urls.add(normalized)
        kept.append(result)

    kept.sort(key=lambda result: 0 if result.resolvable else 1)
    return kept


async def discover(
    session: AsyncSession,
    query: str,
    *,
    year: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> DiscoveryResults:
    """Search the household's SearXNG instance for pages that identify a title.

    Raises `SEARCH_NOT_CONFIGURED` when there is no instance to ask — the same code, from the
    same helper, that the artwork screen's diagnostics use, so an admin who has not set SearXNG
    up gets one message about it rather than two.

    No result page is fetched. What comes back is what the instance already said, plus whatever
    `extract_ids` could read out of each URL.
    """
    return await discover_with(
        await _configured_searxng(session), query, year=year, limit=limit
    )


#: How many domains go into one `site:` clause. Engines truncate long queries, and past a
#: handful the filter costs more results than it saves.
MAX_SITE_FILTERS = 10


def scoped_query(query: str, domains: Sequence[str]) -> str:
    """`ruta de cristal (site:dramabox.com OR site:reelshort.com)`.

    Asks the web index for the platforms' *own* pages, which is what a person does by hand when
    TMDB has never heard of the show. Nothing fetches those pages — a result stays a title and
    a link the admin opens themselves, exactly as the module docstring describes.
    """
    clause = " OR ".join(f"site:{domain}" for domain in domains[:MAX_SITE_FILTERS])
    return f"{query} ({clause})" if clause else query


def _on_a_platform(url: str, domains: Sequence[str]) -> bool:
    """True when a result's host is one of the configured domains, or a subdomain of one.

    The leading dot on the suffix test is deliberate: without it `notdramabox.com` would be
    read as DramaBox, which is the same lookalike hole `vocab.provider_for_domain` closes.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


async def discover_scoped(
    session: AsyncSession,
    query: str,
    *,
    year: int | None = None,
    limit: int = DEFAULT_LIMIT,
    domains: Sequence[str] = (),
) -> DiscoveryResults:
    """`discover`, genuinely restricted to the household's platforms.

    Two corrections live here, both of them mistakes this module shipped and then measured.

    **`site:` is a request, not a guarantee.** Asked for `site:dramabox.com`, this household's
    SearXNG returns Dailymotion and YouTube — its engines drop the operator and search the
    words. So the filter is applied *here*, against the hosts that actually came back. The
    `site:` clause is still sent, because engines that honour it return better candidates, but
    nothing depends on them honouring it. Before this, results were labelled `narrowed` while
    being entirely unfiltered, which is a worse failure than not filtering at all: it puts a
    true-sounding label on a false claim.

    **An empty answer stays empty.** This used to widen to the whole web whenever the narrow
    search found nothing, which meant a household whose platforms are thinly indexed always
    got a generic search and no sign their filter had been dropped. Widening is the caller's
    own action now.
    """
    results = await discover_with(
        await _configured_searxng(session),
        scoped_query(query, domains) if domains else query,
        year=year,
        # Filtering happens after the fact, so ask for more than the caller wants — otherwise
        # one page of mostly-irrelevant hits leaves nothing behind.
        limit=limit if not domains else min(MAX_LIMIT, limit * 3),
    )
    if domains:
        results.results = [
            row for row in results.results if _on_a_platform(row.url, domains)
        ][:limit]
        results.resolvable_count = sum(1 for row in results.results if row.resolvable)
        results.narrowed = True
    return results


async def discover_with(
    config: SearxngConfig,
    query: str,
    *,
    year: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> DiscoveryResults:
    """The same search, against a configuration the caller already resolved.

    Split out of `discover` for one reason: a caller that wants to run this *concurrently*
    with another provider cannot hold the database session while it does. An `AsyncSession` is
    not safe for concurrent use, so two `gather`ed coroutines that each read settings from the
    same session corrupt each other — quietly, and in a way that surfaces as an unrelated
    error somewhere else. Resolving the configuration first, then racing only the network
    calls, is what makes that safe. See `app.services.requests.search`.
    """
    text = search_query(query, year)
    limit = max(1, min(limit, MAX_LIMIT))

    if not text:
        return DiscoveryResults(query="", provider=PROVIDER)

    payload = await _searxng_get(config, SEARXNG_SEARCH_PATH, _params(config, text))
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise AppError(
            "SEARXNG_BAD_RESPONSE",
            "SearXNG's answer had no results list",
            502,
            {"keys": sorted(payload)[:10]},
        )

    candidates = [candidate for item in rows if (candidate := _result(item)) is not None]
    ranked = rank(candidates)
    return DiscoveryResults(
        query=text,
        provider=PROVIDER,
        results=ranked[:limit],
        resolvable_count=sum(1 for result in ranked[:limit] if result.resolvable),
        unresponsive_engines=_unresponsive(payload),
    )


__all__ = [
    "DEFAULT_LIMIT",
    "DISCOVER_CATEGORY",
    "MAX_LIMIT",
    "PROVIDER",
    "SNIPPET_MAX_CHARS",
    "dedupe_keys",
    "discover",
    "discover_scoped",
    "discover_with",
    "extract_ids",
    "is_resolvable",
    "rank",
    "search_query",
]
