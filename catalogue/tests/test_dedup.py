"""Cross-source deduplication: deciding two title rows are the same series.

Every fixture here is a real row from the live catalogue, because the bug this
module fixes was invisible to synthetic data -- the reported pair differs by a
single dropped space, which nobody would think to invent.

The governing property is the same as the IMDb matcher's: REFUSE rather than
guess. A wrong merge fuses two different dramas and shows users the wrong
synopsis, which is far harder to notice later than a duplicate row.
"""

from __future__ import annotations

from app.dedup import (
    EPISODE_TOLERANCE,
    Candidate,
    Verdict,
    classify,
    digit_runs,
    dubbed_key,
    group_candidates,
    is_dubbed,
    match_key,
)
from app.normalize import normalize_title

# --- the pair that prompted the module ---------------------------------------

#: Real rows: DramaBox dropped the space in "LightRedemption", ReelShort dropped
#: the one before ".1". Same 20 episodes, same year, same story.
FRUIT_DRAMABOX = Candidate(
    title_id=720,
    primary_title="Fruit Dimension: Orange LightRedemption. 1",
    year=2026,
    episode_count=20,
    source_keys=frozenset({"dramabox"}),
    poster_sha256="1433f4356ec3d8c3",
)
FRUIT_REELSHORT = Candidate(
    title_id=9776,
    primary_title="Fruit Dimension: Orange Light Redemption.1",
    year=2026,
    episode_count=20,
    source_keys=frozenset({"reelshort"}),
    poster_sha256="50c52e209d683c9f",
)


def test_the_reported_pair_matches() -> None:
    assert match_key(FRUIT_DRAMABOX.primary_title) == match_key(FRUIT_REELSHORT.primary_title)


def test_normalize_title_alone_would_have_missed_it() -> None:
    """Why the key squashes whitespace instead of collapsing it. normalize_title
    turns punctuation into a space, which keeps this pair apart forever."""
    assert normalize_title(FRUIT_DRAMABOX.primary_title) != normalize_title(
        FRUIT_REELSHORT.primary_title
    )


def test_the_reported_pair_auto_merges() -> None:
    group = classify([FRUIT_DRAMABOX, FRUIT_REELSHORT], key="k", key_kind="title")
    assert group.verdict is Verdict.AUTO
    assert group.survivor.title_id == 720
    assert [c.title_id for c in group.absorbed] == [9776]


def test_the_pair_is_not_confirmed_by_its_posters() -> None:
    """Platforms commission different key art. Recorded as a test because
    poster identity looks like it should work here and does not."""
    assert FRUIT_DRAMABOX.poster_sha256 != FRUIT_REELSHORT.poster_sha256


# --- the negative control ----------------------------------------------------

#: Real rows. Six DIFFERENT shows whose titles differ by one word. This family is
#: the negative control for every matching decision in this codebase; the IMDb
#: matcher has its own copy in tests/test_matcher.py.
FATED_FAMILY = [
    Candidate(6074, "Fated to My Cruel CEO", 2024, 57, frozenset({"dramabox"})),
    Candidate(7021, "Fated to My Forbidden Alpha", 2023, 61, frozenset({"reelshort"})),
    Candidate(177, "Fated to My Forbidden Vampire", 2024, 76, frozenset({"reelshort"})),
    Candidate(442, "Fated to My Homeless Billionaire Alpha", 2025, 64, frozenset({"reelshort"})),
    Candidate(3926, "Fated to My Neighbor Boss", 2025, 57, frozenset({"dramabox"})),
    Candidate(2702, "Fated to My Ruthless Alpha", 2026, 60, frozenset({"dramabox"})),
]


def test_the_fated_family_never_groups() -> None:
    """The test this module exists to pass. Six near-identical titles, several
    with agreeing episode counts -- a similarity-based rule merges them all."""
    assert len({match_key(c.primary_title) for c in FATED_FAMILY}) == len(FATED_FAMILY)
    assert group_candidates(FATED_FAMILY) == []


def test_two_fated_titles_with_identical_episode_counts_still_do_not_group() -> None:
    """6074 and 3926 both run 57 episodes. Evidence may only CONFIRM a title
    match, never establish one."""
    same_shape = [c for c in FATED_FAMILY if c.episode_count == 57]
    assert len(same_shape) == 2
    assert group_candidates(same_shape) == []


# --- episode drift ------------------------------------------------------------


def test_a_one_episode_drift_still_merges() -> None:
    """Real pair: platforms add episodes at different times."""
    group = classify(
        [
            Candidate(2803, "The Scent of  My Fated Luna", 2025, 46, frozenset({"dramabox"})),
            Candidate(8679, "The Scent of My Fated Luna", 2025, 47, frozenset({"reelshort"})),
        ],
        key="k",
        key_kind="title",
    )
    assert group.verdict is Verdict.AUTO


def test_a_wide_episode_delta_queues_instead_of_merging() -> None:
    """Real pair, and the reason the tolerance is small. Same title, 100 episodes
    against 36 -- either a different cut or a different show. An operator decides."""
    group = classify(
        [
            Candidate(5030, "Crown of Vengeance", 2024, 100, frozenset({"dramabox"})),
            Candidate(7231, "Crown of Vengeance", 2024, 36, frozenset({"reelshort"})),
        ],
        key="k",
        key_kind="title",
    )
    assert group.verdict is Verdict.QUEUE
    assert "64" in group.reason


def test_the_tolerance_boundary_is_inclusive() -> None:
    def pair(delta: int):
        return classify(
            [Candidate(1, "Same Show", 2026, 50, frozenset({"dramabox"})),
             Candidate(2, "Same Show", 2026, 50 + delta, frozenset({"reelshort"}))],
            key="k", key_kind="title")

    assert pair(EPISODE_TOLERANCE).verdict is Verdict.AUTO
    assert pair(EPISODE_TOLERANCE + 1).verdict is Verdict.QUEUE


def test_a_missing_episode_count_queues() -> None:
    """Absence proves nothing -- it must not read as agreement."""
    group = classify(
        [
            Candidate(1649, "My Secret Billionaire Husband ", 2026, 50, frozenset({"dramabox"})),
            Candidate(9070, "My Secret Billionaire Husband", None, 80, frozenset({"reelshort"})),
        ],
        key="k", key_kind="title",
    )
    assert group.verdict is Verdict.QUEUE


# --- the digit guard ----------------------------------------------------------


def test_squashing_would_fuse_two_different_part_numbers() -> None:
    """The vector the digit guard exists for: deleting separators pulls digits
    together, so these two collide on the key itself."""
    assert match_key("Redemption Part 1 2") == match_key("Redemption Part 12")


def test_a_digit_run_disagreement_is_rejected_outright() -> None:
    """Rejected rather than queued -- a fused part number means sequel-and-
    predecessor, not one show listed twice, so there is nothing to review."""
    group = classify(
        [
            Candidate(1, "Redemption Part 1 2", 2026, 60, frozenset({"dramabox"})),
            Candidate(2, "Redemption Part 12", 2026, 60, frozenset({"reelshort"})),
        ],
        key="k", key_kind="title",
    )
    assert group.verdict is Verdict.REJECT
    assert "digit" in group.reason


def test_the_reported_pair_agrees_on_its_digit_run() -> None:
    """Both spellings end in a '1', so the guard does not block the real merge."""
    assert digit_runs(FRUIT_DRAMABOX.primary_title) == digit_runs(
        FRUIT_REELSHORT.primary_title
    ) == ["1"]


def test_titles_without_digits_are_unaffected() -> None:
    assert digit_runs("Chained Wings") == []


# --- the DUBBED class ---------------------------------------------------------


def test_dubbed_markers_are_recognised_in_both_paren_styles() -> None:
    """DramaBox uses fullwidth parens on most of them and ASCII on some."""
    assert is_dubbed("How the Tables Turned（DUBBED）")
    assert is_dubbed("The One That Got Away(DUBBED)")
    assert not is_dubbed("How the Tables Turned")


def test_goodshorts_prefix_form_is_recognised() -> None:
    """GoodShort marks dubs at the FRONT: '[ENG DUB] She Seemed So Angelic'."""
    assert is_dubbed("[ENG DUB] She Seemed So Angelic")
    assert is_dubbed("[ENGLISH DUB] Bonus Backfire")
    assert is_dubbed("[eng dub] lowercase counts too")


def test_the_prefix_form_is_why_it_had_to_be_stripped_from_the_key() -> None:
    """A suffix marker leaves the key's head intact; a prefix does not. Until
    this was handled, a GoodShort dub keyed as 'engdubsheseemedsoangelic' and
    could never become a candidate for its own plain twin."""
    assert match_key("[ENG DUB] She Seemed So Angelic") != match_key(
        "She Seemed So Angelic"
    )
    assert dubbed_key("[ENG DUB] She Seemed So Angelic") == dubbed_key(
        "She Seemed So Angelic"
    )


def test_a_bare_dub_inside_a_title_is_not_a_marker() -> None:
    """The brackets are what make the widened pattern safe. Nothing in the live
    catalogue trips this, but 'Dublin' must never read as a dubbed release."""
    assert not is_dubbed("A Night in Dublin")
    assert not is_dubbed("Double Contract Love")
    assert not is_dubbed("The Dubious Heir")
    assert dubbed_key("A Night in Dublin") == match_key("A Night in Dublin")


def test_a_dubbed_title_pairs_with_its_twin() -> None:
    assert dubbed_key("They Drove Her to Death（DUBBED）") == dubbed_key(
        "They Drove Her to Death"
    )


def test_the_dubbed_key_does_not_reach_past_the_marker() -> None:
    """Stripping the marker must not make two different shows collide."""
    assert dubbed_key("How the Tables Turned（DUBBED）") != dubbed_key(
        "How the Tables Turn"
    )


# --- the poster key -----------------------------------------------------------


def test_the_poster_key_never_auto_merges() -> None:
    """It ignores the title, so it has no independent evidence -- and identical
    episode counts are cheap when 678 titles run exactly 60. Even a perfect-looking
    match goes to a human."""
    perfect = classify(
        [Candidate(211, "A（DUBBED）", 2026, 99, frozenset({"dramabox"}), "deadbeef"),
         Candidate(1054, "A", 2026, 99, frozenset({"dramabox"}), "deadbeef")],
        key="deadbeef", key_kind="poster")
    assert perfect.verdict is Verdict.QUEUE


def test_a_poster_shared_by_too_many_titles_is_rejected() -> None:
    """Placeholder or house-style artwork. Without the cap, one default poster
    fuses its whole bucket in a single pass."""
    # Digit-free titles on purpose: the digit guard is checked first, and it would
    # reject "Show 1" vs "Show 2" for a different reason than the one under test.
    bucket = [
        Candidate(i, name, 2026, 60, frozenset({"dramabox"}), "sharedart")
        for i, name in enumerate(
            ["Chained Wings", "Doting Snake Lord", "Never Fall Alone", "The Destined One"],
            start=1,
        )
    ]
    # Same platform on purpose: every real poster bucket is, so this also pins the
    # guard order -- the placeholder cap must be reached before the same-source gate.
    group = classify(bucket, key="sharedart", key_kind="poster")
    assert group.verdict is Verdict.REJECT
    assert "placeholder" in group.reason


# --- grouping -----------------------------------------------------------------


def test_a_pair_is_reported_once_even_when_several_keys_find_it() -> None:
    """A DUBBED pair matches on the dubbed key AND on identical poster bytes.
    Reporting it twice would double-count the merge and confuse the review queue."""
    pair = [
        Candidate(211, "They Drove Her to Death（DUBBED）", 2026, 99,
                  frozenset({"dramabox"}), "samehash"),
        Candidate(1054, "They Drove Her to Death", 2026, 99,
                  frozenset({"dramabox"}), "samehash"),
    ]
    groups = group_candidates(pair)
    assert len(groups) == 1
    assert groups[0].key_kind == "dubbed"


def test_the_survivor_is_the_lowest_id() -> None:
    """Stable across re-runs, and it is the row a consumer most likely stores."""
    group = classify(
        [Candidate(8679, "The Scent of My Fated Luna", 2025, 47, frozenset({"reelshort"})),
         Candidate(2803, "The Scent of  My Fated Luna", 2025, 46, frozenset({"dramabox"}))],
        key="k", key_kind="title")
    assert group.verdict is Verdict.AUTO
    assert group.survivor.title_id == 2803
    assert [c.title_id for c in group.absorbed] == [8679]


def test_one_platform_listing_a_name_four_times_is_queued_not_merged() -> None:
    """Real cluster: four DramaBox rows, all 51 episodes, none marked (DUBBED).

    A platform listing one name repeatedly is at least as likely to be several
    different shows, so no marker means no auto-merge.
    """
    cluster = [
        Candidate(3392, "Divorced at the Wedding Day", 2025, 51, frozenset({"dramabox"})),
        Candidate(2265, "Divorced at the Wedding Day", 2026, 51, frozenset({"dramabox"})),
        Candidate(2733, "Divorced At the Wedding Day", 2026, 51, frozenset({"dramabox"})),
    ]
    group = classify(cluster, key="k", key_kind="title")
    assert group.verdict is Verdict.QUEUE
    assert "same platform" in group.reason
    assert group.survivor.title_id == 2265


def test_a_mixed_cluster_does_not_let_one_platform_license_the_other() -> None:
    """The real 'returnoftheracingking' bucket: one DramaBox row and TWO ReelShort
    rows. A group-wide "does this span two platforms?" test passes, and the two
    ReelShort rows get auto-merged on nothing but a shared name. The check has to
    be pairwise."""
    group = classify(
        [Candidate(3143, "Return of the Racing King", 2026, 52, frozenset({"dramabox"})),
         Candidate(7215, "Return of the racing king", 2025, 53, frozenset({"reelshort"})),
         Candidate(7280, "Return of the Racing King", 2026, 52, frozenset({"reelshort"}))],
        key="returnoftheracingking", key_kind="title")
    assert group.verdict is Verdict.QUEUE


def test_a_dubbed_twin_still_auto_merges_within_one_platform() -> None:
    """The marker is the licence: it is a known, benign reason for DramaBox to list
    a show twice, and 485 pairs depend on this path."""
    group = classify(
        [Candidate(211, "They Drove Her to Death（DUBBED）", 2026, 99, frozenset({"dramabox"})),
         Candidate(1054, "They Drove Her to Death", 2026, 99, frozenset({"dramabox"}))],
        key="k", key_kind="dubbed")
    assert group.verdict is Verdict.AUTO


def test_a_lone_title_is_never_a_group() -> None:
    assert group_candidates([FRUIT_DRAMABOX]) == []
    assert classify([FRUIT_DRAMABOX], key="k", key_kind="title").verdict is Verdict.REJECT


# --- a source that never publishes a year ------------------------------------


def test_a_missing_year_no_longer_blocks_a_confirmed_match() -> None:
    """NetShort publishes no usable date anywhere, so the gap is structural and
    permanent rather than a hole a later sync fills. Under the old rule -- one
    missing value made the whole signal None, and a None signal queued -- every
    NetShort title queued for review forever, and a show carried by both
    ReelShort and NetShort stayed TWO catalogue entries instead of one title with
    two studios. That is absence proving something, which `_spread`'s own
    docstring says it must not."""
    members = [
        Candidate(1, "Wrong Choice", 2026, 100, frozenset({"reelshort"})),
        Candidate(2, "Wrong Choice", None, 100, frozenset({"netshort"})),
    ]
    group = classify(members, key="wrongchoice", key_kind="title")
    assert group.verdict is Verdict.AUTO
    assert group.evidence["years_known"] == 1


def test_without_a_year_the_episode_counts_must_be_identical() -> None:
    """One fewer confirmer, so no slack in the one that is left. Within
    tolerance is enough when a year agrees; it is not enough alone, because
    (year, episode_count) is already close to entropy-free here -- 678 titles run
    exactly (2026, 60)."""
    members = [
        Candidate(1, "Wrong Choice", 2026, 100, frozenset({"reelshort"})),
        Candidate(2, "Wrong Choice", None, 101, frozenset({"netshort"})),
    ]
    group = classify(members, key="wrongchoice", key_kind="title")
    assert group.verdict is Verdict.QUEUE
    assert "no year to confirm with" in group.reason


def test_two_known_years_are_still_compared() -> None:
    """The ordinary path is untouched: where two members publish a year, a
    disagreement beyond tolerance still queues."""
    members = [
        Candidate(1, "Wrong Choice", 2020, 100, frozenset({"reelshort"})),
        Candidate(2, "Wrong Choice", 2026, 100, frozenset({"dramabox"})),
    ]
    group = classify(members, key="wrongchoice", key_kind="title")
    assert group.verdict is Verdict.QUEUE
    assert "years differ" in group.reason


def test_a_year_less_pair_from_ONE_platform_still_queues() -> None:
    """The same-source gate runs first and is not weakened: one platform listing
    a name twice is at least as likely to be two different shows."""
    members = [
        Candidate(1, "Wrong Choice", None, 100, frozenset({"netshort"})),
        Candidate(2, "Wrong Choice", None, 100, frozenset({"netshort"})),
    ]
    group = classify(members, key="wrongchoice", key_kind="title")
    assert group.verdict is Verdict.QUEUE
