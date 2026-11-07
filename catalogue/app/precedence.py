"""Field precedence: how the canonical Title is computed from its sources.

Every source writes into `title_source`; this module decides what the merged
`title` row says. The rule is explicit per field rather than last-writer-wins,
because last-writer-wins makes the answer depend on sync order -- so the same
data would resolve differently depending on which job happened to run second.

Precedence is expressed over source KINDS, not source names, so adding a source
does not mean editing this table.

  PLATFORM  the app a title actually lives on (DramaBox, ReelShort). Authoritative
            for its own catalogue: title, synopsis, episode count, availability.
  ENRICHER  a third-party metadata source (IMDb). Authoritative for nothing the
            platform publishes, but fills what platforms structurally lack.

Notably absent: `rating`. Ratings are never resolved onto Title at all. They
live per-source in the `rating` table flagged `is_editorial`, because both
platforms' scores are promotional filler (DramaBox floor-clamps to 7.1-10,
ReelShort sits at 4.5-4.9 catalogue-wide). Keeping them off Title is what makes
it structurally impossible for them to leak into sorting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

PLATFORM = "platform"
ENRICHER = "enricher"

#: Field -> kinds allowed to supply it, in order of precedence.
#: A field absent from a kind's tuple can never be written by that kind.
FIELD_PRECEDENCE: dict[str, tuple[str, ...]] = {
    # The platform's own catalogue is authoritative for its own title.
    "primary_title": (PLATFORM, ENRICHER),
    # Platform release date = when it actually appeared; IMDb startYear is
    # first air. Prefer the platform, fall back to the enricher.
    "year": (PLATFORM, ENRICHER),
    # IMDb ships no synopsis at all, and the platform copy is what users saw.
    "synopsis": (PLATFORM,),
    # Authoritative on the platform; IMDb episode rows are only ever used as a
    # match confirmer, never as the count.
    "episode_count": (PLATFORM,),
    "first_paid_episode": (PLATFORM,),
    # ReelShort tag namespace 1015 is the only source of this anywhere.
    "audience_rating": (PLATFORM, ENRICHER),
    "completed": (PLATFORM,),
}

RESOLVED_FIELDS = tuple(FIELD_PRECEDENCE)


@dataclass(frozen=True)
class Contribution:
    """One source's view of a title."""

    source_key: str
    kind: str
    first_seen: datetime | None
    values: dict[str, Any]


def _age_key(contribution: Contribution) -> tuple[datetime, str]:
    """Sort key for "oldest link wins", safe against mixed tzinfo.

    This tiebreak only ever runs when a title has two contributions of the same
    kind -- which, before deduplication existed, never happened. It does now, and
    the naive comparison it replaced raised TypeError: SQLite hands back naive
    datetimes for a row read from disk while a row created in the same session
    still carries the aware value from its Python default, so a merge could mix
    the two. Postgres would not have shown this.
    """
    stamp = contribution.first_seen
    if stamp is None:
        stamp = datetime.max.replace(tzinfo=UTC)
    elif stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (stamp, contribution.source_key)


def resolve(contributions: list[Contribution]) -> dict[str, Any]:
    """Compute the canonical field values for a title.

    Deterministic by construction: for each field we walk the kinds in
    precedence order and, among sources of that kind offering a non-null value,
    take the one linked longest (earliest `first_seen`). Sync order never
    affects the outcome.
    """
    resolved: dict[str, Any] = {}

    for field, kinds in FIELD_PRECEDENCE.items():
        for kind in kinds:
            candidates = [
                c
                for c in contributions
                if c.kind == kind and c.values.get(field) is not None
            ]
            if not candidates:
                continue
            # Stable tiebreak between two platforms: oldest link wins, with the
            # source key as a final deterministic fallback.
            winner = min(candidates, key=_age_key)
            resolved[field] = winner.values[field]
            break

    return resolved


def kind_of(source_key: str, registry: dict[str, str] | None = None) -> str:
    """Look up a source's kind, defaulting to PLATFORM.

    Defaulting to PLATFORM is the conservative choice: a new source is assumed
    to be describing its own catalogue until it says otherwise.
    """
    return (registry or {}).get(source_key, PLATFORM)
