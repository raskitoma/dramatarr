"""Group import inference.

The engine is pure, so these tests build `UnmatchedFile` lists directly — no database, no
mounted library, no ffprobe. Filenames are the shapes real libraries actually produce: CDN
renditions, camera exports, date-stamped captures, and half-tidy scene releases.

Nothing here writes to disk, and nothing in `app.services.importer` could: a plan is a
proposal about which episode a file *is*, never an instruction to rename it.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.importer import (
    SINGLE_FILE_CONFIDENCE_CAP,
    STRATEGY_DATE,
    STRATEGY_EMBEDDED_EPISODE,
    STRATEGY_MTIME,
    STRATEGY_NAMES,
    STRATEGY_NATURAL_NAME,
    STRATEGY_SHARED_PATTERN_NUMBER,
    ImportPlan,
    UnmatchedFile,
    available_strategies,
    infer_plan,
    parse_filename_date,
)

# --- Fixtures in the plain sense: builders, not pytest fixtures ------------------------------


def make_files(
    *names: str,
    durations: list[float | None] | None = None,
    mtimes: list[float] | None = None,
) -> list[UnmatchedFile]:
    """One `UnmatchedFile` per name. Ids are stable and derived from position so a failing
    assertion points at a specific file."""
    return [
        UnmatchedFile(
            id=f"mf{index:03d}",
            name=name,
            path=f"Folder/{name}",
            size_bytes=1_000_000 + index,
            mtime=mtimes[index] if mtimes else float(1_700_000_000 + index * 3600),
            duration_seconds=durations[index] if durations else None,
        )
        for index, name in enumerate(names)
    ]


def production_files() -> list[UnmatchedFile]:
    """The real folder from the bug report: 58 CDN renditions, a contiguous integer run
    wrapped in a shared prefix-free / suffix-heavy form."""
    return make_files(
        *(f"{number}.720p.narrowv3.mp4" for number in range(701151231, 701151289))
    )


def numbers_of(plan: ImportPlan) -> list[int]:
    return [episode.episode_number for episode in plan.episodes]


def names_in_order(plan: ImportPlan) -> list[str]:
    return [episode.current_name for episode in plan.episodes]


# --- The production case --------------------------------------------------------------------


def test_production_58_renditions_map_to_episodes_1_to_58() -> None:
    """`Tempest The Last Mecha`: 58 opaque ids must become episodes 1..58 in numeric order."""
    files = production_files()
    assert len(files) == 58

    plan = infer_plan("Tempest The Last Mecha", files)

    assert plan.strategy == STRATEGY_SHARED_PATTERN_NUMBER
    assert plan.confidence == 0.95
    assert numbers_of(plan) == list(range(1, 59))
    assert names_in_order(plan)[0] == "701151231.720p.narrowv3.mp4"
    assert names_in_order(plan)[-1] == "701151288.720p.narrowv3.mp4"
    assert plan.warnings == []


def test_production_case_explanation_states_the_real_numbers_and_range() -> None:
    plan = infer_plan("Tempest The Last Mecha", production_files())
    assert "58 files" in plan.explanation
    assert "701151231–701151288" in plan.explanation  # noqa: RUF001 - en dash is the range mark
    assert "contiguous" in plan.explanation
    assert "N.720p.narrowv3.mp4" in plan.explanation


def test_production_case_survives_shuffled_input_order() -> None:
    """Directory listings arrive in whatever order the filesystem feels like."""
    files = production_files()
    shuffled = files[29:] + files[:29]

    plan = infer_plan("Tempest The Last Mecha", shuffled)

    assert numbers_of(plan) == list(range(1, 59))
    assert names_in_order(plan)[0] == "701151231.720p.narrowv3.mp4"


def test_production_case_suggests_series_title_from_the_folder() -> None:
    plan = infer_plan("Tempest The Last Mecha (2019)", production_files())
    assert plan.suggested_series_title == "Tempest The Last Mecha"
    assert plan.suggested_year == 2019


def test_every_file_appears_exactly_once_in_the_plan() -> None:
    files = production_files()
    plan = infer_plan("Tempest The Last Mecha", files)
    assert sorted(episode.media_file_id for episode in plan.episodes) == sorted(
        file.id for file in files
    )


# --- Strategy 1: shared_pattern_number -------------------------------------------------------


def test_contiguous_run_is_confident_and_gapless() -> None:
    files = make_files(*(f"{number}.720p.narrowv3.mp4" for number in range(701151231, 701151236)))
    plan = infer_plan("Show", files)
    assert plan.strategy == STRATEGY_SHARED_PATTERN_NUMBER
    assert plan.confidence == 0.95
    assert numbers_of(plan) == [1, 2, 3, 4, 5]
    assert plan.warnings == []


def test_gapped_run_drops_confidence_and_counts_what_is_missing() -> None:
    """701151233 and 701151234 are absent — the admin must be told before approving."""
    present = [701151231, 701151232, 701151235, 701151236]
    files = make_files(*(f"{number}.720p.narrowv3.mp4" for number in present))

    plan = infer_plan("Show", files)

    assert plan.strategy == STRATEGY_SHARED_PATTERN_NUMBER
    assert plan.confidence == 0.75
    assert numbers_of(plan) == [1, 2, 3, 4]
    assert any("2 number(s) missing" in warning for warning in plan.warnings)
    assert "2 missing" in plan.explanation


def test_numeric_common_prefix_does_not_eat_the_digits_that_matter() -> None:
    """`10001..10004` share `1000`; reporting the range as 1-4 would lose the identity of the
    files and make the explanation a lie."""
    plan = infer_plan("Show", make_files("10001.mp4", "10002.mp4", "10003.mp4", "10004.mp4"))

    assert plan.strategy == STRATEGY_SHARED_PATTERN_NUMBER
    assert plan.confidence == 0.95
    assert "10001–10004" in plan.explanation  # noqa: RUF001 - en dash is the range mark
    assert numbers_of(plan) == [1, 2, 3, 4]


def test_numeric_common_suffix_is_pushed_off_the_digits_too() -> None:
    """`10/20/30` with a trailing `0` in the common suffix must read as 10,20,30 — gapped and
    flagged — rather than as a falsely confident 1,2,3."""
    plan = infer_plan(
        "Show",
        make_files(
            "10.720p.narrowv3.mp4", "20.720p.narrowv3.mp4", "30.720p.narrowv3.mp4"
        ),
    )

    assert plan.strategy == STRATEGY_SHARED_PATTERN_NUMBER
    assert plan.confidence == 0.75
    assert "10–30" in plan.explanation  # noqa: RUF001 - en dash is the range mark
    assert numbers_of(plan) == [1, 2, 3]


def test_mixed_extensions_still_share_a_pattern() -> None:
    """A common suffix cannot span `.mp4` and `.mkv`, so the stems are tried next."""
    plan = infer_plan(
        "Show", make_files("701151231.mp4", "701151232.mkv", "701151233.mp4")
    )

    assert plan.strategy == STRATEGY_SHARED_PATTERN_NUMBER
    assert plan.confidence == 0.95
    assert names_in_order(plan) == ["701151231.mp4", "701151232.mkv", "701151233.mp4"]
    assert ".*" in plan.explanation


def test_shared_pattern_declines_when_the_variable_part_is_not_a_number() -> None:
    files = make_files("rendition_hi.mp4", "rendition_lo.mp4", "rendition_sd.mp4")
    assert STRATEGY_SHARED_PATTERN_NUMBER not in dict(available_strategies(files))


def test_shared_pattern_needs_at_least_two_files() -> None:
    files = make_files("701151231.720p.narrowv3.mp4")
    assert STRATEGY_SHARED_PATTERN_NUMBER not in dict(available_strategies(files))


# --- Strategy 2: embedded_episode ------------------------------------------------------------


def test_embedded_episode_numbers_are_used_as_they_stand() -> None:
    files = make_files("Ruta S01E01.mkv", "Ruta S01E02.mkv", "Ruta S01E03.mkv")
    plan = infer_plan("Ruta de Cristal", files)

    assert plan.strategy == STRATEGY_EMBEDDED_EPISODE
    assert plan.confidence == 0.98
    assert numbers_of(plan) == [1, 2, 3]


def test_embedded_episode_beats_a_shared_numeric_pattern() -> None:
    """These names satisfy both strategies; the one that read a real episode number wins."""
    files = make_files("Ruta S01E05.mkv", "Ruta S01E06.mkv", "Ruta S01E07.mkv")
    ranked = available_strategies(files)

    assert ranked[0] == (STRATEGY_EMBEDDED_EPISODE, 0.98)
    assert (STRATEGY_SHARED_PATTERN_NUMBER, 0.95) in ranked
    assert numbers_of(infer_plan("Ruta", files)) == [5, 6, 7]


def test_camera_exports_are_a_sequence_not_episodes_12_to_14() -> None:
    """`IMG_0012/0013/0014.MP4` are a camera's three clips. `parse_episode` will happily read
    the trailing digits as episodes 12-14, because a bare number is its last-resort pattern —
    but the folder shares a rigid form, and that group evidence is the stronger reading."""
    files = make_files("IMG_0012.MP4", "IMG_0013.MP4", "IMG_0014.MP4")
    plan = infer_plan("Cindy's Tempest (2019)", files)

    assert plan.strategy == STRATEGY_SHARED_PATTERN_NUMBER
    assert numbers_of(plan) == [1, 2, 3]
    assert names_in_order(plan) == ["IMG_0012.MP4", "IMG_0013.MP4", "IMG_0014.MP4"]


def test_a_marker_free_number_scores_below_a_shared_form_and_above_a_name_sort() -> None:
    files = make_files("IMG_0012.MP4", "IMG_0013.MP4", "IMG_0014.MP4")
    ranked = dict(available_strategies(files))

    assert ranked[STRATEGY_NATURAL_NAME] < ranked[STRATEGY_EMBEDDED_EPISODE]
    assert ranked[STRATEGY_EMBEDDED_EPISODE] < ranked[STRATEGY_SHARED_PATTERN_NUMBER]


def test_an_explicit_episode_marker_still_outranks_a_shared_form() -> None:
    """The distinction is the marker, not the digits: `S01E05` means what it says."""
    files = make_files("Ruta S01E05.mkv", "Ruta S01E06.mkv", "Ruta S01E07.mkv")
    ranked = dict(available_strategies(files))

    assert ranked[STRATEGY_EMBEDDED_EPISODE] == 0.98
    assert ranked[STRATEGY_EMBEDDED_EPISODE] > ranked[STRATEGY_SHARED_PATTERN_NUMBER]
    assert numbers_of(infer_plan("Ruta", files)) == [5, 6, 7]


def test_marker_free_numbers_still_win_when_no_shared_form_exists() -> None:
    """Dropping to 0.6 must not throw the reading away — with nothing better available these
    numbers are still the best signal in the folder."""
    files = make_files("Longevity Record - 4.mp4", "Some Other Cut - 2.mp4")
    plan = infer_plan("Longevity Record", files)

    assert plan.strategy == STRATEGY_EMBEDDED_EPISODE
    assert numbers_of(plan) == [2, 4]


def test_embedded_episode_does_not_renumber_a_run_that_starts_late() -> None:
    """`E05..E07` are episodes 5..7. Rebasing them to 1..3 would be silent data loss."""
    plan = infer_plan("Ruta", make_files("Ruta S01E05.mkv", "Ruta S01E06.mkv", "Ruta S01E07.mkv"))
    assert numbers_of(plan) == [5, 6, 7]


def test_half_the_names_parse_and_the_rest_are_reported() -> None:
    files = make_files(
        "Ruta S01E01.mkv",
        "Ruta S01E02.mkv",
        "Ruta S01E03.mkv",
        "opening.mkv",
        "closing.mkv",
        "interlude.mkv",
    )

    plan = infer_plan("Ruta", files)

    assert plan.strategy == STRATEGY_EMBEDDED_EPISODE
    # Half coverage is a hint, not an answer: still ahead of a blind name sort, well short of
    # the 0.98 a fully-parsed folder earns.
    assert 0.5 < plan.confidence < 0.98
    assert numbers_of(plan) == [1, 2, 3, 4, 5, 6]
    assert names_in_order(plan)[:3] == ["Ruta S01E01.mkv", "Ruta S01E02.mkv", "Ruta S01E03.mkv"]
    # The leftovers land after the parsed run, in natural name order, in the next free slots.
    assert names_in_order(plan)[3:] == ["closing.mkv", "interlude.mkv", "opening.mkv"]
    assert any("carry no episode number" in warning for warning in plan.warnings)


def test_unparsed_files_continue_above_the_parsed_run_instead_of_restarting() -> None:
    """One known episode 4 plus two anonymous files: the anonymous ones are 5 and 6. Numbering
    them 1 and 2 would both guess worse and hand the admin a jumbled table to review."""
    files = make_files("Ruta S01E04.mkv", "opening.mkv", "closing.mkv")
    plan = infer_plan("Ruta", files)

    assert names_in_order(plan) == ["Ruta S01E04.mkv", "closing.mkv", "opening.mkv"]
    assert numbers_of(plan) == [4, 5, 6]


def test_partially_parsed_files_carry_lower_per_episode_confidence() -> None:
    files = make_files("Ruta S01E01.mkv", "Ruta S01E02.mkv", "opening.mkv")
    plan = infer_plan("Ruta", files)
    by_name = {episode.current_name: episode for episode in plan.episodes}
    assert by_name["opening.mkv"].confidence < by_name["Ruta S01E01.mkv"].confidence


def test_embedded_season_disagreement_is_flagged_not_silently_applied() -> None:
    files = make_files("Show S02E01.mkv", "Show S02E02.mkv", "Show S02E03.mkv")
    plan = infer_plan("Show", files, season=1)

    assert all(episode.season_number == 1 for episode in plan.episodes)
    assert any("season 2" in warning for warning in plan.warnings)


def test_no_season_warning_when_the_admin_agrees_with_the_filenames() -> None:
    files = make_files("Show S02E01.mkv", "Show S02E02.mkv", "Show S02E03.mkv")
    plan = infer_plan("Show", files, season=2)
    assert not any("season" in warning for warning in plan.warnings)


# --- Strategy 3: date ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("2024-01-05 vlog.mp4", date(2024, 1, 5)),
        ("show.2024.01.05.mp4", date(2024, 1, 5)),
        ("show_2024_01_05.mp4", date(2024, 1, 5)),
        ("20240105.mkv", date(2024, 1, 5)),
        ("cam_20240105_1030.mp4", date(2024, 1, 5)),
        ("05-01-2024 vlog.mp4", date(2024, 1, 5)),
        ("vlog.31.12.2023.mp4", date(2023, 12, 31)),
        # Not dates: an impossible calendar day, and a plain long id.
        ("2024-13-05 vlog.mp4", None),
        ("20240230.mkv", None),
        ("701151231.720p.narrowv3.mp4", None),
        ("no numbers here.mkv", None),
    ],
)
def test_parse_filename_date_formats(filename: str, expected: date | None) -> None:
    assert parse_filename_date(filename) == expected


def test_date_strategy_orders_iso_dates() -> None:
    files = make_files(
        "2024-03-20 vlog.mp4", "2024-01-05 vlog.mp4", "2024-02-11 vlog.mp4"
    )
    plan = infer_plan("Vlog", files)

    assert plan.strategy == STRATEGY_DATE
    assert plan.confidence == 0.8
    assert names_in_order(plan) == [
        "2024-01-05 vlog.mp4",
        "2024-02-11 vlog.mp4",
        "2024-03-20 vlog.mp4",
    ]
    assert numbers_of(plan) == [1, 2, 3]


def test_date_strategy_explanation_reports_the_range() -> None:
    files = make_files("2024-03-20 vlog.mp4", "2024-01-05 vlog.mp4")
    plan = infer_plan("Vlog", files)
    assert "2024-01-05" in plan.explanation
    assert "2024-03-20" in plan.explanation


def test_date_strategy_orders_compact_dates_and_outranks_the_numeric_run() -> None:
    """`20240105` is a valid integer run *and* a date; the date reading is the true one, and
    its 0.8 has to beat the 0.75 a gap-ridden numeric run earns."""
    files = make_files("20240320.mkv", "20240105.mkv", "20240211.mkv")
    plan = infer_plan("Vlog", files)

    assert plan.strategy == STRATEGY_DATE
    assert names_in_order(plan) == ["20240105.mkv", "20240211.mkv", "20240320.mkv"]


def test_date_strategy_orders_day_first_dates() -> None:
    files = make_files("20-03-2024 vlog.mp4", "05-01-2024 vlog.mp4", "11-02-2024 vlog.mp4")
    plan = infer_plan("Vlog", files)

    assert plan.strategy == STRATEGY_DATE
    assert names_in_order(plan) == [
        "05-01-2024 vlog.mp4",
        "11-02-2024 vlog.mp4",
        "20-03-2024 vlog.mp4",
    ]


def test_date_strategy_declines_when_one_file_has_no_date() -> None:
    files = make_files("2024-01-05 vlog.mp4", "2024-02-11 vlog.mp4", "bloopers.mp4")
    assert STRATEGY_DATE not in dict(available_strategies(files))


def test_date_strategy_declines_when_every_file_shares_one_date() -> None:
    """Identical dates order nothing; claiming 0.8 for what is really a name sort is worse
    than admitting the name sort."""
    files = make_files("2024-01-05 part one.mp4", "2024-01-05 part two.mp4")
    assert STRATEGY_DATE not in dict(available_strategies(files))


# --- Strategy 4: natural_name ----------------------------------------------------------------


def test_natural_sort_puts_ep2_before_ep10() -> None:
    files = make_files(
        "trailer scene10 final.mov", "trailer scene2 rough.mov", "trailer scene3 draft.mov"
    )
    plan = infer_plan("Show", files)

    assert plan.strategy == STRATEGY_NATURAL_NAME
    assert plan.confidence == 0.5
    assert names_in_order(plan) == [
        "trailer scene2 rough.mov",
        "trailer scene3 draft.mov",
        "trailer scene10 final.mov",
    ]


def test_natural_sort_is_the_floor_for_names_with_no_signal_at_all() -> None:
    files = make_files("beta.mkv", "alpha.mkv", "gamma.mkv")
    plan = infer_plan("Show", files)

    assert plan.strategy == STRATEGY_NATURAL_NAME
    assert names_in_order(plan) == ["alpha.mkv", "beta.mkv", "gamma.mkv"]


def test_a_plan_always_has_a_strategy() -> None:
    plan = infer_plan("Show", make_files("beta.mkv", "alpha.mkv"))
    assert plan.strategy in STRATEGY_NAMES


# --- Strategy 5: mtime -----------------------------------------------------------------------


def test_mtime_strategy_orders_by_modification_time_and_admits_it_is_guessing() -> None:
    files = make_files(
        "beta.mkv", "alpha.mkv", "gamma.mkv", mtimes=[300.0, 100.0, 200.0]
    )
    plan = infer_plan("Show", files, strategy=STRATEGY_MTIME)

    assert plan.strategy == STRATEGY_MTIME
    assert plan.confidence == 0.3
    assert names_in_order(plan) == ["alpha.mkv", "gamma.mkv", "beta.mkv"]
    assert any("guess" in warning for warning in plan.warnings)


def test_mtime_never_wins_on_its_own() -> None:
    """It is always available and always last, because natural_name is never worse."""
    files = make_files("beta.mkv", "alpha.mkv")
    ranked = available_strategies(files)

    assert ranked[-1] == (STRATEGY_MTIME, 0.3)
    assert infer_plan("Show", files).strategy == STRATEGY_NATURAL_NAME


# --- Strategy selection ----------------------------------------------------------------------


def test_available_strategies_are_ranked_best_first() -> None:
    files = production_files()
    ranked = available_strategies(files)

    assert ranked == sorted(ranked, key=lambda item: -item[1])
    assert ranked[0][0] == STRATEGY_SHARED_PATTERN_NUMBER


def test_available_strategies_is_empty_for_an_empty_folder() -> None:
    assert available_strategies([]) == []


def test_an_explicit_strategy_overrides_the_ranking() -> None:
    files = production_files()
    plan = infer_plan("Show", files, strategy=STRATEGY_NATURAL_NAME)

    assert plan.strategy == STRATEGY_NATURAL_NAME
    assert plan.confidence == 0.5


def test_an_inapplicable_strategy_falls_back_and_says_so() -> None:
    files = make_files("beta.mkv", "alpha.mkv")
    plan = infer_plan("Show", files, strategy=STRATEGY_SHARED_PATTERN_NUMBER)

    assert plan.strategy == STRATEGY_NATURAL_NAME
    assert any(STRATEGY_SHARED_PATTERN_NUMBER in warning for warning in plan.warnings)


def test_an_unknown_strategy_name_is_a_caller_bug() -> None:
    with pytest.raises(ValueError, match="unknown import strategy"):
        infer_plan("Show", make_files("alpha.mkv"), strategy="vibes")


# --- season / start_episode shifting ----------------------------------------------------------


def test_season_applies_to_every_proposed_episode() -> None:
    plan = infer_plan("Show", production_files(), season=2)
    assert plan.season_number == 2
    assert {episode.season_number for episode in plan.episodes} == {2}


def test_start_episode_shifts_a_ranked_run() -> None:
    """The back half of a split release: same files, episodes 13 onwards."""
    files = make_files(*(f"{number}.720p.narrowv3.mp4" for number in range(701151231, 701151236)))
    plan = infer_plan("Show", files, season=2, start_episode=13)

    assert numbers_of(plan) == [13, 14, 15, 16, 17]
    assert {episode.season_number for episode in plan.episodes} == {2}


def test_start_episode_rebases_parsed_numbers_when_asked() -> None:
    files = make_files("Ruta S01E01.mkv", "Ruta S01E02.mkv", "Ruta S01E03.mkv")
    plan = infer_plan("Ruta", files, start_episode=13)
    assert numbers_of(plan) == [13, 14, 15]


def test_start_episode_defaults_leave_parsed_numbers_alone() -> None:
    files = make_files("Ruta S01E11.mkv", "Ruta S01E12.mkv")
    assert numbers_of(infer_plan("Ruta", files)) == [11, 12]


def test_start_episode_shifts_a_date_ordered_run() -> None:
    files = make_files("2024-02-11 vlog.mp4", "2024-01-05 vlog.mp4")
    plan = infer_plan("Vlog", files, start_episode=7)
    assert numbers_of(plan) == [7, 8]


# --- Warnings ---------------------------------------------------------------------------------


def test_warns_when_a_duration_exceeds_three_times_the_median() -> None:
    files = make_files(
        "701151231.720p.narrowv3.mp4",
        "701151232.720p.narrowv3.mp4",
        "701151233.720p.narrowv3.mp4",
        "701151234.720p.narrowv3.mp4",
        durations=[2400.0, 2400.0, 2400.0, 9000.0],
    )
    plan = infer_plan("Show", files)

    assert any("median duration" in warning for warning in plan.warnings)
    assert any("701151234.720p.narrowv3.mp4" in warning for warning in plan.warnings)


def test_no_duration_warning_when_episodes_are_merely_uneven() -> None:
    files = make_files(
        "701151231.720p.narrowv3.mp4",
        "701151232.720p.narrowv3.mp4",
        "701151233.720p.narrowv3.mp4",
        durations=[2400.0, 2700.0, 3000.0],
    )
    assert infer_plan("Show", files).warnings == []


def test_warns_about_a_file_shorter_than_a_minute() -> None:
    files = make_files(
        "701151231.720p.narrowv3.mp4",
        "701151232.720p.narrowv3.mp4",
        "701151233.720p.narrowv3.mp4",
        durations=[2400.0, 2400.0, 45.0],
    )
    plan = infer_plan("Show", files)

    assert any("trailers or samples" in warning for warning in plan.warnings)
    assert any("701151233.720p.narrowv3.mp4" in warning for warning in plan.warnings)


def test_warns_about_a_clip_far_shorter_than_its_neighbours() -> None:
    """The absolute floor is not enough on its own.

    A 3-minute recap among 45-minute episodes is nowhere near 60s, so only a comparison with
    the rest of the folder can catch it — and it is exactly the file an admin must not import
    as episode 4 without looking.
    """
    files = make_files(
        "701151231.720p.narrowv3.mp4",
        "701151232.720p.narrowv3.mp4",
        "701151233.720p.narrowv3.mp4",
        "701151234.720p.narrowv3.mp4",
        durations=[2700.0, 2700.0, 2700.0, 180.0],
    )
    plan = infer_plan("Show", files)
    flagged = [warning for warning in plan.warnings if "median duration" in warning]

    assert len(flagged) == 1
    assert "701151234.720p.narrowv3.mp4" in flagged[0]
    assert "701151231.720p.narrowv3.mp4" not in flagged[0]


def test_a_uniformly_short_folder_is_described_as_a_folder_not_as_a_list() -> None:
    """Every file being short says something about the folder, not about three of its files.

    Naming three of them and adding "and 55 more" is the shape of a warning that gets
    scrolled past; it also implies the other 55 are fine, which is the opposite of the truth.
    """
    files = make_files(
        *(f"{number}.720p.narrowv3.mp4" for number in range(701151231, 701151235)),
        durations=[8.0, 8.0, 8.0, 8.0],
    )
    warnings = infer_plan("Show", files).warnings

    assert any("All 4 files are shorter than 60s" in warning for warning in warnings)
    assert not any("and 1 more" in warning for warning in warnings)


def test_a_single_short_file_is_not_reported_in_the_plural() -> None:
    warnings = infer_plan("Show", make_files("clip.mkv", durations=[9.0])).warnings
    assert any(warning.startswith("This file is shorter than 60s") for warning in warnings)


def test_missing_durations_never_trigger_a_duration_warning() -> None:
    files = make_files(
        "701151231.720p.narrowv3.mp4",
        "701151232.720p.narrowv3.mp4",
        durations=[None, None],
    )
    assert infer_plan("Show", files).warnings == []


def test_warns_about_duplicate_proposed_episode_numbers() -> None:
    files = make_files("Show S01E01 part a.mkv", "Show S01E01 part b.mkv")
    plan = infer_plan("Show", files)

    assert numbers_of(plan) == [1, 1]
    assert any("Duplicate episode numbers" in warning for warning in plan.warnings)


def test_warns_and_caps_confidence_for_a_single_file() -> None:
    plan = infer_plan("Show", make_files("Ruta S01E07.mkv"))

    assert plan.confidence <= SINGLE_FILE_CONFIDENCE_CAP
    assert numbers_of(plan) == [7]
    assert any("single file is not a sequence" in warning for warning in plan.warnings)
    # The review table renders the per-row figure, so the cap has to reach it too — a 0.4 plan
    # made of 0.98 rows invites the admin to trust the rows.
    assert all(episode.confidence <= SINGLE_FILE_CONFIDENCE_CAP for episode in plan.episodes)


def test_a_one_file_folder_is_not_described_as_having_a_first_and_a_last() -> None:
    plan = infer_plan("Show", make_files("only-one-file.mp4"))

    assert plan.strategy == STRATEGY_NATURAL_NAME
    assert plan.explanation == (
        '1 file ordered by natural filename sort (digit-aware): "only-one-file.mp4"'
    )


def test_empty_folder_yields_an_empty_plan_rather_than_an_error() -> None:
    plan = infer_plan("Show", [])

    assert plan.episodes == []
    assert plan.confidence == 0.0
    assert plan.strategy == STRATEGY_NATURAL_NAME
    assert plan.warnings != []


def test_warns_when_the_numeric_run_has_gaps() -> None:
    present = [701151231, 701151232, 701151233, 701151240]
    plan = infer_plan("Show", make_files(*(f"{n}.720p.narrowv3.mp4" for n in present)))

    assert any("6 number(s) missing" in warning for warning in plan.warnings)


def test_several_warnings_stack_on_one_bad_folder() -> None:
    """Warnings are independent checks, not a first-match-wins chain."""
    files = make_files(
        "Show S01E01 part a.mkv",
        "Show S01E01 part b.mkv",
        "Show S01E09.mkv",
        durations=[2400.0, 30.0, 20000.0],
    )
    plan = infer_plan("Show", files, season=3)
    joined = " | ".join(plan.warnings)

    assert "Duplicate episode numbers" in joined
    assert "trailers or samples" in joined
    assert "median duration" in joined
    assert "season 1" in joined


# --- Plan shape --------------------------------------------------------------------------------


def test_every_proposal_carries_a_reason() -> None:
    plan = infer_plan("Show", production_files())
    assert all(episode.reason for episode in plan.episodes)
    assert "701151231" in plan.episodes[0].reason


def test_plan_keeps_the_folder_it_was_given() -> None:
    plan = infer_plan("Shows/Tempest The Last Mecha", production_files())
    assert plan.folder == "Shows/Tempest The Last Mecha"
    assert plan.suggested_series_title == "Tempest The Last Mecha"


def test_blank_folder_has_no_suggested_title() -> None:
    plan = infer_plan("", make_files("alpha.mkv", "beta.mkv"))
    assert plan.suggested_series_title == ""
    assert plan.suggested_year is None


@pytest.mark.parametrize(
    "names",
    [
        ("Ruta S01E03.mkv", "Ruta S01E01.mkv", "opening.mkv", "Ruta S01E02.mkv"),
        ("Ruta S01E12.mkv", "bonus.mkv", "Ruta S01E11.mkv"),
        ("701151233.720p.narrowv3.mp4", "701151231.720p.narrowv3.mp4"),
        ("2024-03-20 vlog.mp4", "2024-01-05 vlog.mp4"),
        ("gamma.mkv", "alpha.mkv"),
    ],
)
def test_episodes_come_back_in_ascending_episode_order(names: tuple[str, ...]) -> None:
    """Whatever the strategy, the plan is a table the admin reads top to bottom."""
    plan = infer_plan("Ruta", make_files(*names))
    assert numbers_of(plan) == sorted(numbers_of(plan))


def test_inference_does_not_mutate_the_input_sequence() -> None:
    files = production_files()
    original = [file.name for file in files]
    infer_plan("Show", files)
    assert [file.name for file in files] == original
