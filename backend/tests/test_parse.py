"""Filename parsing. These are the shapes real libraries actually contain."""

from __future__ import annotations

import pytest

from app.services.scanner.parse import (
    guess_subtitle_language,
    parse_episode,
    parse_series_folder,
)


@pytest.mark.parametrize(
    ("filename", "season", "episode"),
    [
        ("Ruta de Cristal - S01E03 - Titulo.mkv", 1, 3),
        ("ruta.de.cristal.s02e11.1080p.web-dl.mkv", 2, 11),
        ("Show S1E5.mp4", 1, 5),
        ("Show - Season 2 Episode 7.mp4", 2, 7),
        ("Longevity Record - 4.mp4", 1, 4),
        ("Show E09.mkv", 1, 9),
        ("Show EP12.mkv", 1, 12),
        ("Show [08].mkv", 1, 8),
        ("Some Drama 23.mp4", 1, 23),
        # CJK episode markers — the case that makes an Asian-drama library work at all.
        ("迷情 第3集.mp4", 1, 3),
        ("迷情 第12集.mp4", 1, 12),
        ("ドラマ 第5話.mkv", 1, 5),
    ],
)
def test_parse_episode_numbers(filename: str, season: int, episode: int) -> None:
    parsed = parse_episode(filename)
    assert parsed is not None, f"failed to parse {filename!r}"
    assert parsed.season == season
    assert parsed.episode == episode


def test_parse_episode_prefers_sxxeyy_over_trailing_number() -> None:
    """A release tag full of digits must not beat an explicit SxxEyy."""
    parsed = parse_episode("Show - S03E04 - 1080p x265 10bit - Group [2024].mkv")
    assert parsed is not None
    assert (parsed.season, parsed.episode) == (3, 4)


def test_parse_episode_ignores_resolution_as_episode() -> None:
    parsed = parse_episode("Show - S01E02 - 720p.mkv")
    assert parsed is not None
    assert parsed.episode == 2


def test_parse_episode_returns_none_when_unparseable() -> None:
    assert parse_episode("random-clip-no-episode-info.mp4") is None


@pytest.mark.parametrize(
    ("folder", "title", "year"),
    [
        ("Ruta de Cristal (2024)", "Ruta de Cristal", 2024),
        ("Longevity Record", "Longevity Record", None),
        ("迷情 (2023)", "迷情", 2023),
        ("Some Show [2019]", "Some Show", 2019),
        ("왕의 귀향", "왕의 귀향", None),
    ],
)
def test_parse_series_folder(folder: str, title: str, year: int | None) -> None:
    parsed_title, parsed_year = parse_series_folder(folder)
    assert parsed_title == title
    assert parsed_year == year


@pytest.mark.parametrize(
    ("filename", "language"),
    [
        ("Show - S01E01.es.srt", "es"),
        ("Show - S01E01.spa.srt", "es"),
        ("Show - S01E01.en.srt", "en"),
        ("Show - S01E01.pt-BR.srt", "pt-BR"),
        ("Show - S01E01.srt", None),
    ],
)
def test_guess_subtitle_language(filename: str, language: str | None) -> None:
    assert guess_subtitle_language(filename) == language
