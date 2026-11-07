"""M8: IMDb matching.

The governing property is that the matcher REFUSES rather than guesses.
Vertical drama titles come from a small template grammar, so string similarity
carries almost no signal -- a matcher that leans on it produces confident wrong
answers, and a wrong link silently fuses two different dramas in a way that is
much harder to notice later than a missing one.
"""

from __future__ import annotations

import pytest

from app.sources.imdb import Candidate, decide

#: Six DIFFERENT shows, real tconsts from the IMDb datasets. Pairwise trigram
#: similarity is above 0.8. This family is the negative control for the whole
#: matching design.
FATED_FAMILY = [
    Candidate("tt28777411", "Fated to My Forbidden Alpha", "", "tvSeries", 2023, "primaryTitle", 60),
    Candidate("tt37618257", "Fated to My Forbidden Guardian", "", "tvMiniSeries", 2025, "primaryTitle", 58),
    Candidate("tt31512078", "Fated to My Forbidden Vampire", "", "tvMiniSeries", 2024, "primaryTitle", 62),
    Candidate("tt34384391", "Fated to My Bad Boy Alpha", "", "tvMiniSeries", 2024, "primaryTitle", 61),
    Candidate("tt33372745", "Fated to My Swapped Alpha", "", "tvMiniSeries", 2024, "primaryTitle", 59),
    Candidate("tt36956577", "Fated to My Homeless Billionaire Alpha", "", "tvMiniSeries", 2025, "primaryTitle", 63),
]


# --- the negative control ----------------------------------------------------


def test_a_near_miss_against_the_fated_family_auto_accepts_nothing() -> None:
    """The test this whole module exists to pass.

    Our title matches NONE of the six exactly, but is a hair away from all of
    them and every episode count agrees. A fuzzy matcher would happily pick one.
    """
    decision = decide(
        our_title="Fated to My Forbidden Billionaire",
        our_year=2024,
        our_episodes=60,
        candidates=FATED_FAMILY,
    )
    assert decision.auto is False, (
        "a near-miss must never auto-accept: these are six different shows"
    )


def test_an_ambiguous_exact_match_is_queued_not_guessed() -> None:
    """Several candidates matched exactly and the episode shape agrees with one.
    That is still ambiguity, and ambiguity goes to a human."""
    decision = decide("Fated to My Forbidden Alpha", 2023, 60, FATED_FAMILY)

    assert decision.auto is False
    assert decision.method == "ambiguous"
    assert len(decision.evidence["candidates"]) > 1


def test_evidence_records_why_so_a_review_takes_seconds() -> None:
    decision = decide("Fated to My Forbidden Alpha", 2023, 60, FATED_FAMILY)
    evidence = decision.evidence

    assert evidence["our_title"] == "Fated to My Forbidden Alpha"
    assert evidence["our_episodes"] == 60
    assert evidence["candidates"], "the alternatives considered must be shown"
    assert "episode_fingerprint" in evidence


@pytest.mark.parametrize("candidate", FATED_FAMILY)
def test_no_single_member_of_the_family_ever_auto_matches_another(candidate) -> None:
    """Cross-check every pairing: no member may auto-accept as a different one."""
    others = [c for c in FATED_FAMILY if c.tconst != candidate.tconst]
    decision = decide(candidate.primary_title, candidate.start_year, candidate.episode_count, others)

    assert not (decision.auto and decision.tconst != candidate.tconst)


# --- what DOES auto-accept ---------------------------------------------------


def test_a_unique_exact_match_with_a_confirming_shape_auto_accepts() -> None:
    """The one safe case: nothing else matched, and the episode fingerprint --
    50-80 episodes of 1-3 minutes, a shape no reality show shares -- agrees."""
    only = [Candidate("tt29605443", "The Double Life of My Billionaire Husband", "", "tvMiniSeries", 2023, "primaryTitle", 49)]
    decision = decide("The Double Life of My Billionaire Husband", 2023, 50, only)

    assert decision.auto is True
    assert decision.tconst == "tt29605443"
    assert decision.method == "exact_title+episode_shape"


def test_a_unique_match_with_no_episode_rows_is_queued_not_rejected() -> None:
    """Some IMDb entries are series-level stubs with no episode rows. The
    fingerprint is a strong positive confirmer, but its ABSENCE proves nothing,
    so it falls to review rather than being thrown away."""
    stub = [Candidate("tt31066180", "Never Divorce a Secret Billionaire Heiress", "", "tvMiniSeries", 2023, "primaryTitle", None)]
    decision = decide("Never Divorce a Secret Billionaire Heiress", 2023, 56, stub)

    assert decision.auto is False
    assert decision.method == "unconfirmed"
    assert decision.tconst == "tt31066180"


def test_a_wildly_different_episode_count_does_not_auto_accept() -> None:
    """'Married at First Sight' resolves to a reality franchise as well as a
    vertical. The shape is what tells them apart."""
    reality = [Candidate("tt15254812", "Married at First Sight", "", "tvSeries", 2016, "primaryTitle", 400)]
    decision = decide("Married at First Sight", 2023, 60, reality)

    assert decision.auto is False
    assert decision.method == "shape_mismatch"


def test_no_candidates_yields_no_match() -> None:
    decision = decide("Something Not On IMDb", 2026, 50, [])
    assert decision.tconst is None and decision.auto is False


# --- year is a tiebreaker, never a filter ------------------------------------


def test_a_year_disagreement_does_not_block_a_confirmed_match() -> None:
    """Apps re-release, and IMDb's startYear is first air rather than app
    availability. Filtering on year would eliminate true matches."""
    candidate = [Candidate("tt32982109", "The Divorced Billionaire Heiress", "", "movie", 2023, "primaryTitle", 75)]
    decision = decide("The Divorced Billionaire Heiress", 2026, 75, candidate)

    assert decision.auto is True


def test_title_type_is_never_used_to_filter() -> None:
    """IMDb files the same kind of object inconsistently -- siblings are
    tvSeries and tvMiniSeries, and TMDB even calls one a movie."""
    for title_type in ("tvSeries", "tvMiniSeries", "movie", "video"):
        candidate = [Candidate("tt1", "A Vertical Drama", "", title_type, 2025, "primaryTitle", 60)]
        assert decide("A Vertical Drama", 2025, 60, candidate).auto is True


def test_episode_tolerance_absorbs_ongoing_series() -> None:
    """Platforms add episodes after IMDb is first populated."""
    candidate = [Candidate("tt1", "Some Drama", "", "tvMiniSeries", 2025, "primaryTitle", 60)]
    assert decide("Some Drama", 2025, 66, candidate).auto is True
    assert decide("Some Drama", 2025, 200, candidate).auto is False


# --- operator choice ---------------------------------------------------------


def _queued_session(tmp_path):
    """A queued ambiguous match, as the real queue holds one."""
    import json

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.models import Base, MatchCandidate, MatchState, Source, Title

    engine = create_engine(f"sqlite:///{tmp_path}/m.db", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine, future=True)
    session.add(Source(id=1, key="imdb", base_url="https://www.imdb.com"))
    session.add(
        Title(id=1, primary_title="Married at First Sight", normalized_title="married at first sight", episode_count=90)
    )
    session.add(
        MatchCandidate(
            id=1,
            title_id=1,
            target_source_id=1,
            target_external_id="tt30523849",  # the matcher's guess
            method="ambiguous",
            score=0.6,
            evidence_json=json.dumps(
                {"candidates": [{"tconst": "tt30523849"}, {"tconst": "tt3868860"}]}
            ),
            state=MatchState.PROPOSED,
        )
    )
    session.commit()
    return session


def test_an_operator_can_choose_a_candidate_other_than_the_guess(tmp_path) -> None:
    """The queue exists precisely because the matcher was unsure, so offering
    only "accept my guess" or "reject everything" is useless when the operator
    can see which of five rows is right."""
    from app.jobs.imdb_sync import confirm_match
    from app.models import MatchCandidate, TitleSource

    session = _queued_session(tmp_path)
    assert confirm_match(session, 1, tconst="tt3868860") is True

    match = session.get(MatchCandidate, 1)
    assert match.target_external_id == "tt3868860"
    link = session.query(TitleSource).filter_by(source_id=1).one()
    assert link.external_id == "tt3868860"


def test_choosing_a_different_candidate_records_both(tmp_path) -> None:
    """The decision has to stay auditable: what was proposed, and what was
    actually chosen."""
    import json

    from app.jobs.imdb_sync import confirm_match
    from app.models import MatchCandidate

    session = _queued_session(tmp_path)
    confirm_match(session, 1, tconst="tt3868860")

    evidence = json.loads(session.get(MatchCandidate, 1).evidence_json)
    assert evidence["matcher_proposed"] == "tt30523849"
    assert evidence["operator_chose"] == "tt3868860"


def test_confirming_without_a_choice_still_takes_the_guess(tmp_path) -> None:
    from app.jobs.imdb_sync import confirm_match
    from app.models import MatchCandidate

    session = _queued_session(tmp_path)
    assert confirm_match(session, 1) is True
    assert session.get(MatchCandidate, 1).target_external_id == "tt30523849"


def test_a_malformed_tconst_is_refused(tmp_path) -> None:
    from app.jobs.imdb_sync import confirm_match
    from app.models import MatchCandidate, MatchState

    session = _queued_session(tmp_path)
    assert confirm_match(session, 1, tconst="not-an-imdb-id") is False
    assert session.get(MatchCandidate, 1).state == MatchState.PROPOSED


def test_a_rejected_target_is_never_proposed_again(tmp_path) -> None:
    """Otherwise every IMDb run re-asks a question already answered."""
    from app.jobs.imdb_sync import reject_match
    from app.models import MatchCandidate, MatchState

    session = _queued_session(tmp_path)
    assert reject_match(session, 1) is True
    assert session.get(MatchCandidate, 1).state == MatchState.REJECTED
