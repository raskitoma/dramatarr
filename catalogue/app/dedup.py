"""Deciding that two `title` rows are the same series.

Two platforms list the same show, and one platform sometimes lists it twice. Until
this module existed, `ingest_record` keyed identity on `(source_id, external_id)`
alone, so every listing became its own `Title`: 763 of 10,141 rows in the live
catalogue were duplicates (7.5%).

The discipline is the one `app/sources/imdb.py` already uses for IMDb: exact match
on a normalised key, never fuzzy, with secondary evidence allowed only to CONFIRM
a title match and never to establish one. Similarity matching is specifically
forbidden here -- the catalogue is generated from a small template grammar, so
"Fated to My Cruel CEO", "Fated to My Forbidden Alpha" and "Fated to My Neighbor
Boss" are three *different* shows whose titles differ by one word. Any rule that
merges on closeness merges those.

Three keys, measured against the full catalogue:

`match_key`      `normalize_title` with the separators deleted too. The pair that
                 prompted this module differs only by a dropped space -- DramaBox
                 published "Orange LightRedemption. 1" where ReelShort published
                 "Orange Light Redemption.1" -- and `normalize_title`, which
                 collapses punctuation to a space, keeps them apart. Squashing
                 costs little: of 318 groups it finds, only 3 need the extra
                 aggression, and all 3 are true duplicates.

`dubbed_key`     additionally strips a `(DUBBED)` marker. DramaBox publishes 494
                 titles with it; 485 pair with a non-dubbed twin at an identical
                 episode count and identical poster bytes. A dubbed release is the
                 same series with another audio track, so it merges.

`poster_key`     identical poster BYTES, and the only key that never auto-merges.
                 It corroborates just 14 of 318 title-key groups, so it is nearly
                 useless across platforms -- but it is what exposed the `(DUBBED)`
                 class. Because it ignores the title, it carries no independent
                 evidence, so everything it finds alone goes to a human.

Notably absent: perceptual hash. `image.phash` exists and was built expecting to
serve here, but it does not. Platforms commission different key art, so the
prompting pair sits at Hamming distance 19 -- equal to the *minimum* distance
between two unrelated posters in this catalogue. There is no threshold that
separates signal from noise, so phash is not consulted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from app.normalize import normalize_title

#: Every way the three platforms mark a dubbed re-release.
#:
#: DramaBox appends `（DUBBED）` (fullwidth parens, sometimes ASCII); GoodShort
#: PREFIXES `[ENG DUB]`. The prefix form matters more than it looks: it lands at
#: the front of the title, so it is baked into `identity_key`
#: ('engdubsheseemedsoangelic') and a dubbed variant never even becomes a
#: candidate for its plain twin until it is stripped.
#:
#: The bracket requirement is what keeps this safe -- a bare "dub" inside a title
#: is not a marker. Verified against the catalogue: of 845 titles containing
#: "dub", all 845 are markers and none is an ordinary word.
_DUBBED = re.compile(
    r"[（(\[]\s*(?:eng(?:lish)?[\s\-]*)?dub(?:bed)?\s*[）)\]]"
    r"|\bdubbed\b",
    re.IGNORECASE,
)
#: What `normalize_title` leaves between words. Deleting only whitespace
#: keeps non-Latin characters, which an [^a-z0-9] class would erase.
_SEPARATORS = re.compile(r"\s+")
_DIGIT_RUN = re.compile(r"\d+")

#: How far episode counts may drift and still confirm.
#:
#: The measured distribution over the 273 cross-source candidate pairs is bimodal
#: with an empty gap: 173 pairs at delta 0, 77 at 1, 4 at 2, then NOTHING until 10,
#: after which they run to 64. Any threshold from 3 to 9 selects an identical set,
#: so the cut point is not a judgement call -- the data chose it. Set to 1 rather
#: than 2 only because the four pairs at delta 2 are worth a human glance, and
#: costing four reviews is cheaper than one wrong merge.
#:
#: Wide deltas are where distinct shows with generic titles hide: "Crown of
#: Vengeance" is 100 episodes on DramaBox and 36 on ReelShort. Those go to review.
EPISODE_TOLERANCE = 1

#: A title released either side of New Year is dated differently by each platform.
YEAR_TOLERANCE = 1

#: Above this, a shared poster is house-style or placeholder artwork rather than
#: evidence of identity. Measured: 466 posters are shared by exactly 2 titles and
#: one by 3, so the cut only ever excludes that outlier today -- it is here for the
#: default-artwork case that would otherwise fuse a whole bucket at once.
POSTER_BUCKET_MAX = 2


class Verdict(str, Enum):
    AUTO = "auto"      #: title key matches and the evidence confirms it
    QUEUE = "queue"    #: title key matches, evidence disagrees -- an operator decides
    REJECT = "reject"  #: not a candidate at all


def match_key(value: str) -> str:
    """`normalize_title`, then delete the separators it left behind.

    'Orange LightRedemption. 1' and 'Orange Light Redemption.1' both become
    'orangelightredemption1'.

    Built on `normalize_title` rather than on the raw string, which matters more
    than it looks. `normalize_title` folds accents through NFKD first, so 'Café'
    and 'Cafe' converge; squashing the raw text with an ASCII class instead
    *deletes* the accented character ('cafromance' against 'caferomance') and the
    pair can never match. It also erases any non-Latin title to the empty string,
    which would give every one of them the same key.
    """
    if not value:
        return ""
    return _SEPARATORS.sub("", normalize_title(value))


def dubbed_key(value: str) -> str:
    """`match_key` with a `(DUBBED)` marker removed."""
    if not value:
        return ""
    return match_key(_DUBBED.sub(" ", value))


def is_dubbed(value: str) -> bool:
    return bool(value) and _DUBBED.search(value) is not None


def digit_runs(value: str) -> list[str]:
    """Ordered digit groups of a title, as a guard on the squashed key.

    Deleting separators can fuse digits that were apart: "Part 1 2" and "Part 12"
    both squash to 'part12'. Comparing digit runs -- ['1','2'] against ['12'] --
    keeps them apart.

    Catches nothing in the catalogue as it stands (0 of 318 groups disagree on
    digit runs), which is exactly why it is worth stating: 194 titles contain a
    digit, so the vector is live even though it has not fired, and a part-number
    collision is the kind that silently fuses a sequel into its predecessor.
    """
    return _DIGIT_RUN.findall(normalize_title(value or ""))


@dataclass(frozen=True)
class Candidate:
    """One `title` row, reduced to what identity resolution looks at."""

    title_id: int
    primary_title: str
    year: int | None = None
    episode_count: int | None = None
    source_keys: frozenset[str] = frozenset()
    poster_sha256: str | None = None


@dataclass
class Group:
    """A set of candidates sharing a key, with the verdict and its reasoning."""

    key: str
    key_kind: str
    members: list[Candidate]
    verdict: Verdict
    reason: str
    evidence: dict = field(default_factory=dict)

    @property
    def survivor(self) -> Candidate:
        """The lowest id wins.

        Arbitrary but deliberate: it is stable across re-runs, and it is almost
        always the row a consumer saw first and therefore already stores. Nothing
        is lost by the choice -- the merge unions every source, alias, episode and
        credit onto the survivor regardless of which row it is.
        """
        return min(self.members, key=lambda c: c.title_id)

    @property
    def absorbed(self) -> list[Candidate]:
        keep = self.survivor.title_id
        return [c for c in self.members if c.title_id != keep]


def _shares_a_source(members: list[Candidate]) -> bool:
    """Do any two members come from the same platform?

    Pairwise on purpose. Asking whether the whole group spans two platforms lets a
    three-row group -- two from one platform, one from another -- pass a
    "cross-source" test and auto-merge the same-platform pair inside it.
    """
    seen: set[str] = set()
    for member in members:
        for key in member.source_keys:
            if key in seen:
                return True
            seen.add(key)
    return False


def _dubbed_explains(members: list[Candidate]) -> bool:
    """True when a `(DUBBED)` marker accounts for the duplicate listing.

    DramaBox publishes 485 shows twice, once with the marker. That is a known and
    benign reason for one catalogue to hold two entries, so it auto-merges; any
    other same-platform pair does not.
    """
    marked = [is_dubbed(m.primary_title) for m in members]
    return any(marked) and not all(marked)


def _spread(values: list[int | None]) -> int | None:
    """Max minus min, or None if anything is missing. Absence proves nothing."""
    if not values or any(v is None for v in values):
        return None
    return max(values) - min(values)


def _known_spread(values: list[int | None]) -> tuple[int | None, int]:
    """Spread over the values that EXIST, plus how many that was.

    "Absence proves nothing" was the rule `_spread` states and the opposite of
    what it did: one missing value made the whole signal None, and a None signal
    sent the group to review. That is absence proving something.

    It only began to matter with a source that never publishes the field at all.
    NetShort has no usable date anywhere -- its `datePublished` is a series-level
    constant identical across episodes and even across episodes that do not
    exist -- so the gap is structural and permanent, not a hole that a later
    sync fills. Under the old rule every NetShort title queued for review
    forever, and a show carried by both ReelShort and NetShort stayed two
    catalogue entries rather than one title with two `studios`.

    The count is returned because a spread over fewer than two values is not a
    measurement. The caller decides what to do with that.
    """
    known = [v for v in values if v is not None]
    if len(known) < 2:
        return None, len(known)
    return max(known) - min(known), len(known)


def classify(members: list[Candidate], *, key: str, key_kind: str) -> Group:
    """Decide what to do with a set of candidates sharing a title key.

    Evidence can only confirm. `(year, episode_count)` is close to entropy-free
    in this catalogue -- 678 titles share (2026, 60) -- so it is never consulted
    unless a key already matched.
    """
    if len(members) < 2:
        return Group(key, key_kind, members, Verdict.REJECT, "not a duplicate set")

    episodes = _spread([m.episode_count for m in members])
    years, years_known = _known_spread([m.year for m in members])
    evidence = {
        "episode_counts": [m.episode_count for m in members],
        "years": [m.year for m in members],
        "episode_spread": episodes,
        "year_spread": years,
        "years_known": years_known,
        "sources": sorted({k for m in members for k in m.source_keys}),
    }

    # A digit-run disagreement is a hard stop, not a queue: it means the squashed
    # key fused two different numbers, so the pair is very likely a sequel and its
    # predecessor rather than one show listed twice.
    runs = {tuple(digit_runs(m.primary_title)) for m in members}
    if len(runs) > 1:
        evidence["digit_runs"] = [list(r) for r in sorted(runs)]
        return Group(key, key_kind, members, Verdict.REJECT,
                     "digit runs disagree -- likely different parts of a series",
                     evidence)

    # The poster branch is settled BEFORE the same-source gate below, and the
    # order matters: every poster bucket in the live catalogue is same-source
    # (0 of 467 shared hashes span two platforms, because each platform serves its
    # own CDN bytes), so gating first would hide every one of them behind the
    # wrong reason and make the placeholder cap unreachable.
    if key_kind == "poster":
        # Never auto-merges, however well the evidence agrees. `image.sha256` is
        # globally unique and app/images.py reuses the row for identical bytes, so
        # a platform's placeholder or house-style artwork gives one image_id to N
        # unrelated shows -- and identical episode counts are cheap in a catalogue
        # where 678 titles run exactly 60. This key IGNORES the title, so it has
        # no independent evidence at all and a human has to look.
        if len(members) > POSTER_BUCKET_MAX:
            return Group(key, key_kind, members, Verdict.REJECT,
                         f"{len(members)} titles share this poster -- placeholder art",
                         evidence)
        return Group(key, key_kind, members, Verdict.QUEUE,
                     "identical poster bytes, but the titles differ", evidence)

    # Two members from the SAME platform auto-merge only when a `(DUBBED)` marker
    # explains why one catalogue lists the show twice. Everything else goes to a
    # human, because a platform listing one name twice is at least as likely to be
    # two different shows: the live bucket 'returnoftheracingking' holds one
    # DramaBox row and TWO ReelShort rows at 52 and 53 episodes, and a group-wide
    # "is this cross-source?" test passes it straight through to an auto-merge.
    # Checked pairwise for that reason -- one cross-source member must not license
    # merging the same-source pair beside it.
    if _shares_a_source(members) and not _dubbed_explains(members):
        return Group(key, key_kind, members, Verdict.QUEUE,
                     "two titles from the same platform with no dubbed marker"
                     " to explain the pair", evidence)

    if episodes is None:
        return Group(key, key_kind, members, Verdict.QUEUE,
                     "title matches but an episode count is missing", evidence)
    if episodes > EPISODE_TOLERANCE:
        return Group(key, key_kind, members, Verdict.QUEUE,
                     f"episode counts differ by {episodes}"
                     f" (tolerance {EPISODE_TOLERANCE})", evidence)

    if years_known >= 2:
        if years > YEAR_TOLERANCE:
            return Group(key, key_kind, members, Verdict.QUEUE,
                         f"years differ by {years} (tolerance {YEAR_TOLERANCE})",
                         evidence)
        return Group(key, key_kind, members, Verdict.AUTO,
                     "exact title key with confirming episode count and year",
                     evidence)

    # Fewer than two members publish a year, so the year cannot confirm or deny.
    # Rather than queue forever -- which is what a permanently year-less source
    # would mean -- the remaining confirmer is held to a HIGHER standard: the
    # episode counts must be IDENTICAL, not merely within tolerance. One fewer
    # signal, so no slack in the one that is left.
    #
    # Over-merging is worse than leaving duplicates, and (year, episode_count) is
    # already close to entropy-free here -- 678 titles run exactly (2026, 60) --
    # so episode count alone cannot carry an auto-merge with any looseness. An
    # exact tie plus an exact title key, cross-platform (same-source pairs were
    # gated above), is the narrowest place this can safely land.
    if episodes == 0:
        return Group(key, key_kind, members, Verdict.AUTO,
                     "exact title key and identical episode counts; no year"
                     " published by at least two members", evidence)
    return Group(key, key_kind, members, Verdict.QUEUE,
                 f"no year to confirm with and episode counts differ by {episodes}",
                 evidence)


def group_candidates(candidates: list[Candidate]) -> list[Group]:
    """Bucket candidates by each key in turn and classify every bucket.

    Keys are applied in order of decreasing directness, and a title already
    placed by a stronger key is not re-bucketed by a weaker one -- otherwise the
    same pair is reported three times.
    """
    keyed: list[tuple[str, callable]] = [
        ("title", lambda c: match_key(c.primary_title)),
        ("dubbed", lambda c: dubbed_key(c.primary_title)),
        ("poster", lambda c: c.poster_sha256),
    ]

    groups: list[Group] = []
    seen_pairs: set[tuple[int, int]] = set()
    for kind, keyfn in keyed:
        buckets: dict[str, list[Candidate]] = {}
        for c in candidates:
            k = keyfn(c)
            if k:
                buckets.setdefault(k, []).append(c)
        for k, members in buckets.items():
            if len(members) < 2:
                continue
            ids = sorted(m.title_id for m in members)
            pairs = {(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]}
            if pairs <= seen_pairs:
                continue  # already covered by a stronger key
            seen_pairs |= pairs
            group = classify(members, key=k, key_kind=kind)
            # A rejected group is not returned, but its pairs stay in seen_pairs
            # so a weaker key cannot re-propose what a stronger one refused. A
            # digit-run mismatch means "different parts of a series", and
            # identical poster art does not make that less true.
            if group.verdict is not Verdict.REJECT:
                groups.append(group)
    return groups
