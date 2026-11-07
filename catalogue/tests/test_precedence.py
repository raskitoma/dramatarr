"""M4: field precedence.

The property under test is order-independence. Last-writer-wins would make the
same data resolve differently depending on which sync happened to run second,
which is a bug you only notice months later when a field flips for no reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.precedence import (
    ENRICHER,
    FIELD_PRECEDENCE,
    PLATFORM,
    Contribution,
    resolve,
)

EARLY = datetime(2026, 1, 1, tzinfo=UTC)
LATE = EARLY + timedelta(days=30)


def platform(key: str, first_seen: datetime = EARLY, **values) -> Contribution:
    return Contribution(key, PLATFORM, first_seen, values)


def enricher(key: str = "imdb", first_seen: datetime = EARLY, **values) -> Contribution:
    return Contribution(key, ENRICHER, first_seen, values)


# --- platform beats enricher -------------------------------------------------


def test_platform_title_wins_over_the_enricher() -> None:
    """IMDb retitles verticals between markets; the app's own catalogue is
    authoritative for its own title."""
    resolved = resolve(
        [
            platform("reelshort", primary_title="Bound by Love"),
            enricher(primary_title="Love in the Mist"),
        ]
    )
    assert resolved["primary_title"] == "Bound by Love"


def test_platform_year_wins_but_the_enricher_fills_a_gap() -> None:
    both = resolve([platform("dramabox", year=2026), enricher(year=2023)])
    assert both["year"] == 2026

    only_imdb = resolve([platform("dramabox", year=None), enricher(year=2023)])
    assert only_imdb["year"] == 2023


def test_enricher_can_never_supply_platform_only_fields() -> None:
    """IMDb ships no synopsis at all, and its episode rows are a match
    confirmer -- never the count. Encoded as policy, not convention."""
    resolved = resolve([enricher(synopsis="from imdb", episode_count=1)])
    assert "synopsis" not in resolved
    assert "episode_count" not in resolved


def test_platform_only_policy_matches_the_declared_table() -> None:
    for field in ("synopsis", "episode_count", "first_paid_episode", "completed"):
        assert FIELD_PRECEDENCE[field] == (PLATFORM,)


# --- order independence ------------------------------------------------------


def test_resolution_does_not_depend_on_contribution_order() -> None:
    """The core property. If this fails, the answer depends on sync order."""
    a = platform("dramabox", EARLY, primary_title="A", year=2025, episode_count=60)
    b = platform("reelshort", LATE, primary_title="B", year=2026, episode_count=80)

    assert resolve([a, b]) == resolve([b, a])


def test_between_two_platforms_the_older_link_wins() -> None:
    """A deterministic tiebreak, rather than an arbitrary preference for one
    app over the other."""
    older = platform("reelshort", EARLY, primary_title="First seen here")
    newer = platform("dramabox", LATE, primary_title="Seen later")

    assert resolve([older, newer])["primary_title"] == "First seen here"
    assert resolve([newer, older])["primary_title"] == "First seen here"


def test_a_missing_value_does_not_beat_a_present_one() -> None:
    """Absent in this payload is not the same as cleared -- a source that omits
    a field must not blank it."""
    resolved = resolve(
        [
            platform("dramabox", EARLY, audience_rating=None),
            platform("reelshort", LATE, audience_rating="All Ages"),
        ]
    )
    assert resolved["audience_rating"] == "All Ages"


def test_no_contributions_resolves_to_nothing() -> None:
    assert resolve([]) == {}


# --- ratings are structurally excluded ---------------------------------------


def test_rating_is_not_a_resolvable_field() -> None:
    """Both platforms' scores are promotional filler: DramaBox floor-clamps to
    7.1-10, ReelShort sits at 4.5-4.9 catalogue-wide. Keeping rating off the
    resolution table -- and off Title entirely -- is what makes it impossible
    for them to reach sorting.
    """
    assert "rating" not in FIELD_PRECEDENCE

    resolved = resolve([platform("dramabox", rating=9.9)])
    assert "rating" not in resolved


def test_title_has_no_rating_column_at_all() -> None:
    from app.models import Title

    columns = {c.name for c in Title.__table__.columns}
    assert "rating" not in columns
    assert "score" not in columns


@pytest.mark.parametrize("field", sorted(FIELD_PRECEDENCE))
def test_every_policy_lists_kinds_in_a_known_vocabulary(field: str) -> None:
    assert FIELD_PRECEDENCE[field], f"{field} has an empty precedence tuple"
    assert set(FIELD_PRECEDENCE[field]) <= {PLATFORM, ENRICHER}
