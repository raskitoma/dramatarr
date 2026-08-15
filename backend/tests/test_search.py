"""Multilingual search.

The acceptance case from the contract: one series must be reachable from its native script,
every romanization of it, and its translated titles.
"""

from __future__ import annotations

import pytest

from app.services.search.normalize import detect_script, normalize, romanize, search_keys

pytestmark = pytest.mark.anyio


# --- Normalization -------------------------------------------------------------------------


def test_normalize_strips_diacritics_and_case() -> None:
    assert normalize("Ruta de Cristal") == normalize("RUTA DE CRISTAL")
    assert normalize("Corazón") == normalize("corazon")


def test_normalize_collapses_punctuation_and_whitespace() -> None:
    assert normalize("Show:   The  Return!") == normalize("show the return")


def test_normalize_preserves_cjk() -> None:
    """Stripping non-ASCII here would erase the title entirely."""
    assert "迷情" in normalize("迷情")


@pytest.mark.parametrize(
    ("text", "script"),
    [("迷情", "han"), ("왕의 귀향", "hangul"), ("Ruta de Cristal", "latin")],
)
def test_detect_script(text: str, script: str) -> None:
    assert detect_script(text) == script


# --- Romanization --------------------------------------------------------------------------


def test_romanize_han_produces_spaced_joined_and_initials() -> None:
    variants = {v.lower() for v in romanize("迷情")}
    assert "mi qing" in variants
    assert "miqing" in variants
    assert "mq" in variants


def test_romanize_hangul() -> None:
    variants = {v.lower() for v in romanize("왕의 귀향")}
    assert variants, "Hangul romanization produced nothing"
    # Revised Romanization of 왕 is "wang" under every implementation we tolerate.
    assert any("wang" in v for v in variants)


def test_search_keys_includes_normalized_and_romanized() -> None:
    keys = {k.lower() for k in search_keys("迷情")}
    assert "迷情" in keys
    assert "mi qing" in keys
    assert "miqing" in keys


def test_search_keys_deduplicates_and_drops_empties() -> None:
    keys = search_keys("Ruta de Cristal")
    assert len(keys) == len(set(keys))
    assert all(k.strip() for k in keys)


# --- End-to-end index + query --------------------------------------------------------------


@pytest.fixture
async def indexed_series(db):
    """The contract's acceptance fixture: a Chinese drama with an English alternate title."""
    from app.models import Series, SeriesTitle, TitleKind, TitleSource
    from app.services.search.index import ensure_index, reindex_series

    await ensure_index(db)

    series = Series(
        title="迷情",
        slug="mi-qing",
        sort_title="迷情",
        library_path="迷情 (2023)",
        year=2023,
        original_language="zh",
    )
    db.add(series)
    await db.flush()

    for language, title, kind in [
        ("zh", "迷情", TitleKind.NATIVE),
        ("en", "Infatuation", TitleKind.OFFICIAL),
    ]:
        db.add(
            SeriesTitle(
                series_id=series.id,
                language=language,
                title=title,
                kind=kind,
                source=TitleSource.TMDB,
                normalized=normalize(title),
            )
        )
        # Romanizations are materialized as their own rows — that is what makes the query
        # path a single FTS MATCH instead of per-row transliteration at search time.
        for variant in romanize(title):
            db.add(
                SeriesTitle(
                    series_id=series.id,
                    language=language,
                    title=variant,
                    kind=TitleKind.ROMANIZATION,
                    source=TitleSource.TMDB,
                    normalized=normalize(variant),
                )
            )

    await db.commit()
    await reindex_series(db, series.id)
    await db.commit()
    return series


@pytest.mark.parametrize("query", ["迷情", "mi qing", "miqing", "Infatuation", "infat"])
async def test_series_found_across_languages(db, indexed_series, query: str) -> None:
    from app.services.search.service import search_library

    results = await search_library(db, query)
    found = {hit.series.id for hit in results.series}
    assert indexed_series.id in found, f"{query!r} did not find the series"


async def test_result_reports_which_title_matched(db, indexed_series) -> None:
    """A cross-language hit looks like a bug unless the UI can explain it."""
    from app.services.search.service import search_library

    results = await search_library(db, "miqing")
    hit = next(h for h in results.series if h.series.id == indexed_series.id)
    assert hit.matched_title


async def test_unrelated_query_returns_nothing(db, indexed_series) -> None:
    from app.services.search.service import search_library

    results = await search_library(db, "completely unrelated zzzz")
    assert indexed_series.id not in {hit.series.id for hit in results.series}
