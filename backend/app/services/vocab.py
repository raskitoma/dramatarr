"""Canonical spellings for the three fields humans type into: genres, rating, provider.

These columns are free text and are written from four directions — TMDB's genre list, an
admin typing into the editor, a filename the scanner parsed, and an import plan — so without a
single normalizer the same value arrives four ways. This household's live data shows exactly
that: `["Mecha", "Revenge", "son-in-law", "secret-identity", "ai"]` in one row, and `Dramabox`
and `Reelshort` as network names for platforms that spell themselves DramaBox and ReelShort.

Everything here is applied **on write**, never on read. A column that is normalized at render
time still stores the mess, still sorts wrong, and still shows the mess to the next tool that
reads the database.

Title casing is real title casing, not `str.title()`:

* `str.title()` breaks on apostrophes (`"don't"` → `"Don'T"`) and on anything already correct
  (`"DramaBox"` → `"Dramabox"`), which is how the live data got the way it is.
* Hyphens are **kept**, and each part is cased on its own. `Sci-Fi & Fantasy` is one of TMDB's
  own genre names, so turning hyphens into spaces would corrupt canonical values to fix
  slug-shaped ones.
* Minor words stay lowercase unless they lead or close the value, which is what makes
  `son-in-law` come out as `Son-in-Law` rather than `Son-In-Law`.
* A short acronym list survives intact, because `ai` → `Ai` is not an improvement.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Lowercase inside a title, capitalized when first or last. English plus the Spanish
#: particles this household's titles actually contain.
_MINOR_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor",
        "of", "on", "onto", "or", "over", "the", "to", "up", "via", "vs", "with",
        "con", "de", "del", "el", "en", "la", "las", "los", "para", "por", "un", "una", "y",
    }
)

#: Values that are not words and must not be sentence-cased. Keyed by their lowercase form.
_ACRONYMS: dict[str, str] = {
    "ai": "AI",
    "bl": "BL",
    "gl": "GL",
    "tv": "TV",
    "us": "US",
    "uk": "UK",
    "usa": "USA",
    "diy": "DIY",
    "ceo": "CEO",
    "mma": "MMA",
    "sci": "Sci",  # so `sci-fi` cases as `Sci-Fi` rather than hitting the minor-word list
}

#: Splits a value into words while keeping the separators, so hyphens survive the rebuild.
#: The dashes are escaped rather than written literally: an en dash and an em dash are
#: indistinguishable from a hyphen in most editors, and all three are real separators here.
_TOKENS = re.compile("([^\\s\\-\u2013\u2014/]+)")


def _word(word: str, *, first: bool, last: bool) -> str:
    lowered = word.lower()
    acronym = _ACRONYMS.get(lowered)
    if acronym is not None:
        return acronym
    if not first and not last and lowered in _MINOR_WORDS:
        return lowered
    # Only the first character is forced; the rest is left alone so an intentional inner
    # capital (`DramaBox`, `ReelShort`, `McQueen`) is not destroyed on the way through.
    return word[0].upper() + word[1:] if word else word


def title_case(value: str) -> str:
    """`son-in-law` → `Son-in-Law`, `ai` → `AI`, `Sci-Fi & Fantasy` unchanged."""
    text = " ".join((value or "").replace("_", " ").split())
    if not text:
        return ""

    parts = _TOKENS.split(text)
    # `re.split` with a capture group alternates separator, word, separator, ... so the odd
    # indices are the words and the even ones are whatever sat between them.
    words = [index for index, part in enumerate(parts) if index % 2 == 1 and part]
    if not words:
        return text

    first, last = words[0], words[-1]
    for index in words:
        parts[index] = _word(parts[index], first=index == first, last=index == last)
    return "".join(parts)


def normalize_genres(values: Iterable[str] | None) -> list[str]:
    """Title-case every tag, drop blanks, and de-duplicate case-insensitively.

    Order is the caller's, first occurrence wins: an admin who arranged their tags deliberately
    keeps that arrangement, and `["Drama", "drama"]` collapses to one rather than rendering as
    two chips that look identical and cannot both be removed.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        tag = title_case(str(raw))
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


# --- Content ratings ---------------------------------------------------------------------------

#: The standard age ratings, in ascending order of restriction, grouped as the UI shows them.
#: Film first because short-form drama is rated that way far more often than it is rated as TV.
CONTENT_RATINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("film", ("G", "PG", "PG-13", "R", "NC-17")),
    ("tv", ("TV-Y", "TV-Y7", "TV-G", "TV-PG", "TV-14", "TV-MA")),
)

#: Flat lookup, keyed by a squashed form so `pg13`, `pg-13` and `PG 13` all land on `PG-13`.
_RATING_BY_KEY: dict[str, str] = {
    re.sub(r"[^a-z0-9]", "", rating.lower()): rating
    for _group, ratings in CONTENT_RATINGS
    for rating in ratings
}

VALID_CONTENT_RATINGS: frozenset[str] = frozenset(_RATING_BY_KEY.values())


def canonical_content_rating(value: str | None) -> str | None:
    """A standard rating in its canonical spelling, or the admin's own text left untouched.

    Deliberately **not** a whitelist. TMDB returns per-country certifications this list does
    not have (`12`, `15`, `R15+`, `청소년관람불가`), and refusing them would replace real data
    with nothing. The select offers the standard set; anything else is preserved as typed.
    """
    text = (value or "").strip()
    if not text:
        return None
    return _RATING_BY_KEY.get(re.sub(r"[^a-z0-9]", "", text.lower()), text)


# --- Providers ---------------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class Provider:
    """A vertical-drama platform, and the domain its own pages live on.

    `domain` is optional and frequently empty. A platform with no domain is still a perfectly
    good `network` value — it just cannot narrow a web search, which is the only thing the
    domain is used for. Guessing one would be worse than leaving it blank: a `site:` filter
    with a wrong domain does not fail, it returns nothing, silently.
    """

    name: str
    domain: str = ""


#: The platforms this library is filled from, as the household named them.
#:
#: A **seed**, not a fixed list. The real list lives in the `library.providers` setting and is
#: editable in the admin UI, because it changes faster than this code does — new platforms
#: appear constantly and only the household knows which ones it actually uses. Domains are
#: filled in only where they are known; the rest are for the admin to complete.
DEFAULT_PROVIDERS: tuple[Provider, ...] = (
    Provider("DramaBox", "dramabox.com"),
    Provider("GoodShort", "goodshort.com"),
    Provider("NetShort", "netshort.com"),
    Provider("FlickReels"),
    Provider("StarDustTV"),
    Provider("DramaWave"),
    Provider("ShortMax", "shortmax.com"),
    Provider("ReelShort", "reelshort.com"),
    Provider("iDrama"),
    Provider("FlexTV", "flextv.cc"),
    Provider("StarShort"),
    Provider("KalosTV", "kalostv.com"),
    Provider("DramaBite"),
    Provider("ShotShort"),
    Provider("DramaPops"),
    Provider("MicroDrama"),
    Provider("ShortWave"),
    Provider("TvSeries"),
    Provider("MoboReels", "moboreels.com"),
    Provider("ReelFren"),
)

#: Setting key holding the household's own list. JSON: `[{"name": ..., "domain": ...}]`.
PROVIDERS_SETTING = "library.providers"

#: Reference databases that are searched **alongside** the household's platforms, always.
#:
#: A platform's own page tells you the show exists and what it is called. These tell you the
#: year, the cast and a written synopsis — IMDb in particular carries a release year for titles
#: that have nothing else, which is the field the editor most often cannot fill. They are
#: reference works rather than distributors, so including them costs nothing and they are
#: already what `extract_ids` knows how to read an identifier out of.
#:
#: Deliberately not configurable. The household's platform list changes constantly; this does
#: not, and making it a setting would invite someone to remove IMDb and then wonder why the
#: year stopped appearing.
METADATA_SITES: tuple[str, ...] = (
    "imdb.com",
    "themoviedb.org",
    "mydramalist.com",
    "dramaboxdb.com",
)

#: Names only, for the places that just need the vocabulary (casing, the picker's fallback).
PROVIDERS: tuple[str, ...] = tuple(provider.name for provider in DEFAULT_PROVIDERS)

_PROVIDER_BY_KEY: dict[str, str] = {
    re.sub(r"[^a-z0-9]", "", name.lower()): name for name in PROVIDERS
}


def _squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def clean_domain(value: str) -> str:
    """`https://www.DramaBox.com/es/` → `dramabox.com`. Empty when there is nothing usable.

    Admins paste whatever is in the address bar, so a scheme, a `www.`, a path and a trailing
    slash all have to come off. What is kept is a bare host, because that is the only shape a
    `site:` filter and a domain comparison both accept.
    """
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@")[-1].split(":")[0]
    if text.startswith("www."):
        text = text[4:]
    # A bare word is not a domain; accepting one would put `dramabox` into a `site:` filter
    # and quietly return nothing forever.
    return text if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", text) else ""


def parse_providers(raw: Any) -> list[Provider]:
    """The stored list, cleaned. Anything unusable is dropped rather than half-kept.

    Deduplicated by name so the picker cannot show the same platform twice, and order is
    preserved — an admin who put the three they actually use at the top meant that.
    """
    if not isinstance(raw, list):
        return []
    out: list[Provider] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            name, domain = entry, ""
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "")
            domain = str(entry.get("domain") or "")
        else:
            continue
        name = " ".join(name.split())
        if not name:
            continue
        key = _squash(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(Provider(name=name, domain=clean_domain(domain)))
    return out


def provider_names(providers: Sequence[Provider]) -> tuple[str, ...]:
    return tuple(provider.name for provider in providers)


def provider_domains(
    providers: Sequence[Provider], *, with_metadata_sites: bool = False
) -> tuple[str, ...]:
    """The domains that can narrow a search.

    `with_metadata_sites` adds the reference databases. Callers looking for *metadata* want
    them; a caller asking purely "which platform is this on" does not, so it is opt-in rather
    than baked in.
    """
    own = tuple(provider.domain for provider in providers if provider.domain)
    if not with_metadata_sites:
        return own
    # Deduplicated, in case a household lists one of these as a platform of its own.
    return tuple(dict.fromkeys(own + METADATA_SITES))


async def configured_providers(settings: Any) -> list[Provider]:
    """The household's list, or the seed when it has not been edited yet.

    Falling back to the seed rather than to an empty list matters: an install that has never
    opened the settings screen still gets a usable platform picker and a usable short-drama
    search. Once the admin saves anything, their list is the whole truth — including the
    removals, which is why an empty saved list is *not* treated as "unconfigured".
    """
    return parse_providers(await settings.get(PROVIDERS_SETTING, None))


def canonical_network(value: str | None) -> str | None:
    """`dramabox`, `Dramabox`, `DRAMA BOX` → `DramaBox`. Anything unknown is title-cased.

    The known platforms are matched on a squashed key because their names arrive spelled every
    possible way — from a filename, from a menu, from memory. An unrecognized value still gets
    consistent casing rather than being left exactly as typed, so `netflix` and `Netflix` do
    not become two different networks in the filter list.
    """
    text = (value or "").strip()
    if not text:
        return None
    known = _PROVIDER_BY_KEY.get(re.sub(r"[^a-z0-9]", "", text.lower()))
    return known if known is not None else title_case(text)


#: Built-in domain → platform, used when the household has configured nothing yet. The
#: configured list wins over this; see `provider_for_domain`.
PROVIDER_DOMAINS: dict[str, str] = {
    provider.domain: provider.name for provider in DEFAULT_PROVIDERS if provider.domain
} | {"dramaboxdb.com": "DramaBox"}


def provider_for_domain(
    domain: str | None, providers: Sequence[Provider] | None = None
) -> str | None:
    """The platform a result domain belongs to, or None when the domain says nothing.

    A hit on `reelshort.com` names the platform even when the page carries no metadata id at
    all, which for this library is most of them — so this is what puts a provider on a card
    that would otherwise be an anonymous link.
    """
    host = (domain or "").strip().lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]

    table = (
        {p.domain: p.name for p in providers if p.domain}
        if providers
        else PROVIDER_DOMAINS
    )
    if host in table:
        return table[host]
    # `m.dramabox.com`, `es.reelshort.com` — a subdomain of a known platform is that platform.
    # Matched with the leading dot so `notdramabox.com` is not read as DramaBox.
    for known, name in table.items():
        if host.endswith(f".{known}"):
            return name
    return None


__all__ = [
    "CONTENT_RATINGS",
    "DEFAULT_PROVIDERS",
    "METADATA_SITES",
    "PROVIDERS",
    "PROVIDERS_SETTING",
    "PROVIDER_DOMAINS",
    "VALID_CONTENT_RATINGS",
    "Provider",
    "canonical_content_rating",
    "canonical_network",
    "clean_domain",
    "normalize_genres",
    "parse_providers",
    "provider_domains",
    "provider_for_domain",
    "provider_names",
    "title_case",
]
