"""Canonical spellings for genres, networks and ratings.

Every case below is either a value that is actually in this household's database or a value
whose corruption would be caused by the obvious wrong implementation. The two that matter most:

* **`str.title()` is the wrong tool**, and it is the tool that produced the mess. It lowercases
  inner capitals, so `DramaBox` becomes `Dramabox` — which is precisely what the live rows
  show. Anything that round-trips a correct value into a wrong one is the bug.
* **Hyphens are kept.** `Sci-Fi & Fantasy` is one of TMDB's own genre names. An implementation
  that turns hyphens into spaces to tidy up `son-in-law` corrupts a canonical value to fix a
  cosmetic one, which is a bad trade.
"""

from __future__ import annotations

import pytest

from app.services.vocab import (
    METADATA_SITES,
    PROVIDERS,
    Provider,
    canonical_content_rating,
    canonical_network,
    clean_domain,
    normalize_genres,
    parse_providers,
    provider_domains,
    provider_for_domain,
    provider_names,
    title_case,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # --- The live data ---
        ("son-in-law", "Son-in-Law"),
        ("secret-identity", "Secret-Identity"),
        ("ai", "AI"),
        ("Mecha", "Mecha"),
        ("Revenge", "Revenge"),
        # --- Minor words: lowercase inside, capitalized at either end ---
        ("the king of the hill", "The King of the Hill"),
        ("a day in the life", "A Day in the Life"),
        ("of mice and men", "Of Mice and Men"),
        # `the` closes the value, so it is capitalized rather than left lowercase.
        ("all about the", "All About The"),
        # --- Values that are already right must survive untouched ---
        ("Sci-Fi & Fantasy", "Sci-Fi & Fantasy"),
        ("Action & Adventure", "Action & Adventure"),
        ("War & Politics", "War & Politics"),
        ("DramaBox", "DramaBox"),
        ("McQueen", "McQueen"),
        # --- Shapes that break `str.title()` ---
        ("don't look up", "Don't Look Up"),
        ("rock 'n' roll", "Rock 'n' Roll"),
        # --- Whitespace and separators ---
        ("  extra   spaces  ", "Extra Spaces"),
        ("under_score_tags", "Under Score Tags"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_title_case(raw: str, expected: str) -> None:
    assert title_case(raw) == expected


def test_title_case_is_idempotent() -> None:
    """It runs on every write, so applying it twice must not drift."""
    for value in ("son-in-law", "Sci-Fi & Fantasy", "ai", "the king of the hill", "DramaBox"):
        once = title_case(value)
        assert title_case(once) == once


def test_genres_are_deduplicated_case_insensitively_keeping_the_first() -> None:
    """Two chips that render identically cannot both be removed — so there is only ever one."""
    assert normalize_genres(["Drama", "drama", "DRAMA"]) == ["Drama"]
    # Order is the admin's; first occurrence wins.
    assert normalize_genres(["Revenge", "Mecha", "revenge"]) == ["Revenge", "Mecha"]


def test_genres_drop_blanks_but_keep_everything_else() -> None:
    assert normalize_genres(["Mecha", "", "   ", "ai"]) == ["Mecha", "AI"]
    assert normalize_genres(None) == []
    assert normalize_genres([]) == []


def test_the_live_genre_row_comes_out_clean() -> None:
    """The exact array in this household's database, and what it should have been."""
    assert normalize_genres(
        ["Mecha", "Revenge", "son-in-law", "secret-identity", "ai"]
    ) == ["Mecha", "Revenge", "Son-in-Law", "Secret-Identity", "AI"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The live rows, and the spellings the platforms use for themselves.
        ("Dramabox", "DramaBox"),
        ("dramabox", "DramaBox"),
        ("DRAMABOX", "DramaBox"),
        ("drama box", "DramaBox"),
        ("Reelshort", "ReelShort"),
        ("reelshort", "ReelShort"),
        ("ReelShort", "ReelShort"),
        ("shortmax", "ShortMax"),
        ("kalostv", "KalosTV"),
        # Not a known platform: still cased consistently, so `netflix` and `Netflix` do not
        # become two entries in a filter list.
        ("netflix", "Netflix"),
        ("hbo max", "Hbo Max"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_canonical_network(raw: str | None, expected: str | None) -> None:
    assert canonical_network(raw) == expected


def test_every_listed_provider_is_its_own_canonical_form() -> None:
    """A list entry that does not survive its own lookup would silently rename itself."""
    for name in PROVIDERS:
        assert canonical_network(name) == name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pg13", "PG-13"),
        ("PG-13", "PG-13"),
        ("pg 13", "PG-13"),
        ("tv14", "TV-14"),
        ("TV-MA", "TV-MA"),
        ("nc17", "NC-17"),
        ("r", "R"),
        # Not in the standard set: preserved exactly. TMDB returns per-country certifications
        # this list does not have, and replacing real data with nothing is worse than an
        # unfamiliar string.
        ("R15+", "R15+"),
        ("12", "12"),
        ("청소년관람불가", "청소년관람불가"),
        ("", None),
        (None, None),
    ],
)
def test_canonical_content_rating(raw: str | None, expected: str | None) -> None:
    assert canonical_content_rating(raw) == expected


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("dramabox.com", "DramaBox"),
        ("www.dramabox.com", "DramaBox"),
        ("m.dramabox.com", "DramaBox"),
        ("dramaboxdb.com", "DramaBox"),
        ("reelshort.com", "ReelShort"),
        ("es.reelshort.com", "ReelShort"),
        # A lookalike must not be read as the real thing — the domain is the only evidence
        # here, so a suffix match that ignored the dot would attribute someone else's page.
        ("notdramabox.com", None),
        ("dramabox.com.evil.example", None),
        ("example.com", None),
        ("", None),
        (None, None),
    ],
)
def test_provider_for_domain(domain: str | None, expected: str | None) -> None:
    assert provider_for_domain(domain) == expected


# --- The configurable list -----------------------------------------------------------------
#
# The platform list is the household's, not this code's: it changes faster than the code does.
# What matters here is that a bad entry is dropped rather than half-kept, because a malformed
# domain does not fail loudly — it silently narrows a search to nothing.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.DramaBox.com/es/", "dramabox.com"),
        ("dramabox.com", "dramabox.com"),
        ("http://x.co:8080/a?b=c", "x.co"),
        ("WWW.ReelShort.COM", "reelshort.com"),
        # A bare word is not a domain. Accepting one would put `site:dramabox` into a query
        # and return nothing, forever, with no error anywhere.
        ("dramabox", ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_clean_domain(raw: str, expected: str) -> None:
    assert clean_domain(raw) == expected


def test_parse_providers_drops_what_it_cannot_use() -> None:
    parsed = parse_providers(
        [
            {"name": "DramaBox", "domain": "https://www.dramabox.com/"},
            {"name": "  ", "domain": "orphan.com"},   # no name: not a platform
            "ReelFren",                                 # bare string: name only
            {"name": "dramabox", "domain": "other.com"},  # duplicate, first wins
            42,
        ]
    )
    assert parsed == [
        Provider(name="DramaBox", domain="dramabox.com"),
        Provider(name="ReelFren", domain=""),
    ]


def test_parse_providers_keeps_the_admins_order() -> None:
    """Somebody who put the three they actually use at the top meant that."""
    parsed = parse_providers([{"name": "ReelShort"}, {"name": "DramaBox"}])
    assert provider_names(parsed) == ("ReelShort", "DramaBox")


def test_a_platform_with_no_domain_is_still_a_platform() -> None:
    """It just cannot narrow a search — which is the only thing the domain is for."""
    parsed = parse_providers(
        [{"name": "StarShort"}, {"name": "DramaBox", "domain": "dramabox.com"}]
    )
    assert provider_names(parsed) == ("StarShort", "DramaBox")
    assert provider_domains(parsed) == ("dramabox.com",)


def test_the_configured_list_wins_over_the_builtin_table() -> None:
    """Otherwise an admin correcting a domain would be overruled by the seed."""
    mine = [Provider(name="Mi Plataforma", domain="ejemplo.com")]
    assert provider_for_domain("ejemplo.com", mine) == "Mi Plataforma"
    # And a built-in domain is *not* recognized once the household has its own list.
    assert provider_for_domain("dramabox.com", mine) is None


def test_the_reference_databases_ride_along_with_the_platforms() -> None:
    """IMDb is what supplies the year — the field the editor most often cannot fill.

    A platform's own page says the show exists and what it is called. The reference databases
    say when it came out and what it is about, so a metadata search wants both sets. They are
    not configurable on purpose: the platform list churns constantly, this does not, and a
    household that removed IMDb would lose the year with no visible cause.
    """
    mine = [Provider("ReelShort", "reelshort.com"), Provider("NetShort", "netshort.com")]

    assert provider_domains(mine) == ("reelshort.com", "netshort.com")

    both = provider_domains(mine, with_metadata_sites=True)
    assert both[:2] == ("reelshort.com", "netshort.com")
    assert "imdb.com" in both
    assert set(METADATA_SITES) <= set(both)


def test_a_platform_that_is_also_a_reference_site_is_listed_once() -> None:
    """A duplicate would spend one of the ten `site:` slots saying the same thing
    twice."""
    both = provider_domains([Provider("IMDb", "imdb.com")], with_metadata_sites=True)
    assert both.count("imdb.com") == 1
