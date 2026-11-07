"""IMDb non-commercial datasets.

An ENRICHER, not a platform: authoritative for nothing the apps publish, but it
supplies the one thing they cannot -- a real rating. Both platforms' scores are
promotional filler (DramaBox floor-clamps to 7.1-10, ReelShort sits at 4.5-4.9
catalogue-wide), so IMDb is the only genuine rating source in this project.

Licence: personal, non-commercial use only. IMDb-derived rows carry `source_id`
so they can be excluded from an export or dropped wholesale.
Attribution: "Information courtesy of IMDb (https://www.imdb.com). Used with
permission."

MATCHING IS THE HARD PART, and fuzzy matching actively hurts here. Vertical
drama titles are generated from a small template grammar, so string similarity
carries almost no signal -- these are six DIFFERENT shows:

    Fated to My Forbidden Alpha      Fated to My Bad Boy Alpha
    Fated to My Forbidden Guardian   Fated to My Swapped Alpha
    Fated to My Forbidden Vampire    Fated to My Homeless Billionaire Alpha

Pairwise trigram similarity is above 0.8. Any ordinary fuzzy matcher links them
confidently and wrongly. So: exact-on-normalised only, confirmed by episode
shape, with everything else routed to a human. Recon measured exact-on-
normalised at 27/29 -- fuzzy adds nothing but false positives.
"""

from __future__ import annotations

import csv
import gzip
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.normalize import normalize_title

log = logging.getLogger(__name__)

KEY = "imdb"
STUDIO_NAME = "IMDb"
BASE_URL = "https://datasets.imdbws.com"
#: IMDb is not a place to watch anything, so it contributes no watch URL.
WATCH_URL_TEMPLATE = ""
KIND = "enricher"

ATTRIBUTION = (
    "Information courtesy of IMDb (https://www.imdb.com). Used with permission."
)

#: Only what matching and enrichment actually need. `title.principals` and
#: `name.basics` add another ~1 GB for cast we mostly already have from the
#: platforms, and ReelShort hands us IMDb person ids directly.
DATASETS = {
    "basics": "title.basics.tsv.gz",
    "akas": "title.akas.tsv.gz",
    "ratings": "title.ratings.tsv.gz",
    "episode": "title.episode.tsv.gz",
}

NULL = r"\N"

#: Vertical dramas run 50-80 episodes of 1-3 minutes. No reality show shares
#: that shape, which makes it a far stronger identity signal than the title.
MAX_EPISODE_RUNTIME_MIN = 5
MIN_VERTICAL_EPISODES = 20
#: How far an episode count may drift and still confirm. Platforms add episodes
#: after IMDb is first populated.
EPISODE_COUNT_TOLERANCE = 0.25


@dataclass
class Candidate:
    tconst: str
    primary_title: str
    original_title: str
    title_type: str
    start_year: int | None
    matched_on: str  # primaryTitle | originalTitle | aka
    episode_count: int | None = None
    mean_runtime: float | None = None
    rating: float | None = None
    votes: int | None = None
    akas: list[str] = field(default_factory=list)


# --- download ----------------------------------------------------------------


def dataset_url(name: str) -> str:
    return f"{BASE_URL}/{DATASETS[name]}"


def download(name: str, into: Path, timeout: float = 600.0) -> Path:
    """Fetch one dataset, revalidating with If-None-Match / If-Modified-Since.

    The files refresh daily and land in two waves, so a naive "download all
    seven now" can straddle a refresh boundary. Each is snapshotted separately
    with its own validators.
    """
    into.mkdir(parents=True, exist_ok=True)
    target = into / DATASETS[name]
    meta = target.with_suffix(".meta")

    headers = {"User-Agent": "DramadexBot/0.1 (self-hosted metadata indexer)"}
    if target.exists() and meta.exists():
        for line in meta.read_text().splitlines():
            key, _, value = line.partition("=")
            if key == "etag" and value:
                headers["If-None-Match"] = value
            elif key == "last_modified" and value:
                headers["If-Modified-Since"] = value

    with httpx.stream(
        "GET", dataset_url(name), headers=headers, timeout=timeout, follow_redirects=True
    ) as response:
        if response.status_code == 304:
            log.info("imdb %s unchanged", name)
            return target
        response.raise_for_status()
        tmp = target.with_suffix(".part")
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
        tmp.replace(target)
        meta.write_text(
            f"etag={response.headers.get('etag', '')}\n"
            f"last_modified={response.headers.get('last-modified', '')}\n"
        )
    log.info("imdb %s downloaded (%s bytes)", name, target.stat().st_size)
    return target


# --- streaming ---------------------------------------------------------------


def stream(path: Path) -> Iterator[dict[str, str]]:
    """Yield rows from a gzipped IMDb TSV.

    QUOTE_NONE matters: IMDb does not quote fields, and titles legitimately
    contain double quotes.
    """
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)
        yield from reader


def _clean(value: str | None) -> str | None:
    return None if value in (None, "", NULL) else value


def _int(value: str | None) -> int | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


# --- candidate discovery -----------------------------------------------------


def find_candidates(
    basics_path: Path, wanted: dict[str, set[int]], akas_path: Path | None = None
) -> dict[str, Candidate]:
    """Single pass over title.basics, keeping only rows whose normalised title
    matches something we hold.

    Streaming with a set lookup rather than loading 12.7M rows: we only ever
    care about the handful that could be ours.
    """
    found: dict[str, Candidate] = {}

    for row in stream(basics_path):
        primary = _clean(row.get("primaryTitle"))
        original = _clean(row.get("originalTitle"))

        matched_on = None
        if primary and normalize_title(primary) in wanted:
            matched_on = "primaryTitle"
        elif original and normalize_title(original) in wanted:
            matched_on = "originalTitle"
        if matched_on is None:
            continue

        tconst = row["tconst"]
        found[tconst] = Candidate(
            tconst=tconst,
            primary_title=primary or "",
            original_title=original or "",
            title_type=row.get("titleType", ""),
            start_year=_int(row.get("startYear")),
            matched_on=matched_on,
        )

    if akas_path is not None and akas_path.exists():
        _add_aka_matches(akas_path, basics_path, wanted, found)

    return found


def _add_aka_matches(
    akas_path: Path, basics_path: Path, wanted: dict[str, set[int]], found: dict[str, Candidate]
) -> None:
    """Titles get retitled between markets constantly, so akas are extra match
    keys -- and the only genuine alternate titles this project can get."""
    extra: dict[str, str] = {}
    for row in stream(akas_path):
        title = _clean(row.get("title"))
        if not title:
            continue
        # NOTE the join key here is `titleId`, not `tconst`. Easy schema bug.
        tconst = row.get("titleId", "")
        if tconst in found:
            # Collect EVERY aka for a title we matched, not only the one that
            # happened to match. These are the genuine alternate titles the
            # project cannot get anywhere else: DramaBox's are slug variants of
            # the primary title and ReelShort's are sparse.
            found[tconst].akas.append(title)
            continue
        if normalize_title(title) in wanted:
            extra[tconst] = title

    if not extra:
        return
    for row in stream(basics_path):
        tconst = row["tconst"]
        if tconst not in extra:
            continue
        found[tconst] = Candidate(
            tconst=tconst,
            primary_title=_clean(row.get("primaryTitle")) or "",
            original_title=_clean(row.get("originalTitle")) or "",
            title_type=row.get("titleType", ""),
            start_year=_int(row.get("startYear")),
            matched_on="aka",
            akas=[extra[tconst]],
        )


def attach_episode_shape(episode_path: Path, candidates: dict[str, Candidate]) -> None:
    """Count episodes per candidate parent."""
    counts: dict[str, int] = {}
    for row in stream(episode_path):
        parent = row.get("parentTconst")
        if parent in candidates:
            counts[parent] = counts.get(parent, 0) + 1
    for tconst, count in counts.items():
        candidates[tconst].episode_count = count


def attach_ratings(ratings_path: Path, candidates: dict[str, Candidate]) -> None:
    for row in stream(ratings_path):
        tconst = row["tconst"]
        if tconst in candidates:
            candidates[tconst].rating = float(row["averageRating"])
            candidates[tconst].votes = _int(row.get("numVotes"))


# --- the matching rules ------------------------------------------------------


@dataclass
class Decision:
    tconst: str | None
    auto: bool
    method: str
    score: float
    evidence: dict


def decide(
    our_title: str,
    our_year: int | None,
    our_episodes: int | None,
    candidates: list[Candidate],
) -> Decision:
    """Pick a match, or refuse to.

    Deliberately has no fuzzy branch. Adding one would make the "Fated to My …"
    family cross-link, which is the failure this whole design exists to avoid.
    """
    evidence: dict = {
        "our_title": our_title,
        "our_year": our_year,
        "our_episodes": our_episodes,
        "candidates": [
            {
                "tconst": c.tconst,
                "title": c.primary_title,
                "year": c.start_year,
                "episodes": c.episode_count,
                "type": c.title_type,
                "matched_on": c.matched_on,
            }
            for c in candidates[:10]
        ],
    }

    if not candidates:
        return Decision(None, False, "no_candidates", 0.0, evidence)

    # Year is a TIEBREAKER, never a filter: apps re-release, and IMDb's
    # startYear is first air rather than app availability.
    ranked = sorted(
        candidates,
        key=lambda c: (
            0 if _episodes_agree(our_episodes, c.episode_count) else 1,
            abs((c.start_year or 0) - (our_year or 0)) if our_year and c.start_year else 99,
        ),
    )
    best = ranked[0]
    confirmed = _episodes_agree(our_episodes, best.episode_count)
    unique = len(candidates) == 1

    evidence["episode_fingerprint"] = {
        "ours": our_episodes,
        "imdb": best.episode_count,
        "agrees": confirmed,
    }

    # Auto-accept needs an exact normalised title match AND the episode shape to
    # confirm AND no ambiguity. Absence of episode data proves nothing, so it
    # falls to review rather than being rejected.
    if unique and confirmed:
        return Decision(best.tconst, True, "exact_title+episode_shape", 1.0, evidence)
    if confirmed and not unique:
        evidence["note"] = "episode shape agrees but several titles matched exactly"
        return Decision(best.tconst, False, "ambiguous", 0.6, evidence)
    if unique and best.episode_count is None:
        evidence["note"] = "no episode rows on IMDb; cannot confirm"
        return Decision(best.tconst, False, "unconfirmed", 0.5, evidence)

    evidence["note"] = "exact title match but episode shape disagrees"
    return Decision(best.tconst, False, "shape_mismatch", 0.3, evidence)


def _episodes_agree(ours: int | None, theirs: int | None) -> bool:
    if not ours or not theirs:
        return False
    tolerance = max(3, int(ours * EPISODE_COUNT_TOLERANCE))
    return abs(ours - theirs) <= tolerance
