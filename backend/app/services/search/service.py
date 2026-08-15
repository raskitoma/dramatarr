"""Query path for multilingual search.

FTS5 narrows the library to candidates cheaply; rapidfuzz then decides the order and, more
importantly, records *which* stored variant won, so the UI can explain the match.

Hits are serialized with the same `SeriesSummary`/`EpisodeSummary` models the rest of
`/api/library` uses, so the search page reuses the browse grid's poster card unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Episode, Series, SeriesTitle, TitleKind
from app.schemas.search import EpisodeHit, MatchField, SearchResults, SeriesHit
from app.services import library as library_service
from app.services.search.index import (
    INDEX_TABLE,
    KIND_EPISODE_TITLE,
    KIND_SERIES_OVERVIEW,
    KIND_SERIES_TITLE,
    ensure_index,
)
from app.services.search.normalize import normalize, search_keys
from app.services.watch import progress_for_episodes

log = logging.getLogger(__name__)

# The trigram tokenizer cannot match fewer than three characters; below that the only
# workable path is a prefix scan of the pre-normalized titles.
MIN_TRIGRAM_LEN = 3

MIN_SCORE = 50.0
OVERVIEW_WEIGHT = 0.7
EPISODE_WEIGHT = 0.9
MAX_EPISODES_PER_SERIES = 20

_MATCH_SQL = sql(
    f"SELECT kind, ref_id, series_id FROM {INDEX_TABLE} "
    f"WHERE {INDEX_TABLE} MATCH :expr ORDER BY rank LIMIT :cap"
)

#: `series_titles.kind` values that mean "a machine-generated Latin rendering".
_ROMANIZED_KINDS = frozenset({TitleKind.ROMANIZATION, TitleKind.ACRONYM})


def _quote(term: str) -> str:
    """FTS5 string literal. Doubling `"` is the only escape the syntax defines, and quoting
    the whole term is what neutralizes `*`, `-`, `:`, `(`, `^` and the boolean keywords."""
    return '"' + term.replace('"', '""') + '"'


def _long_terms(key: str) -> list[str]:
    return [term for term in key.split(" ") if len(term) >= MIN_TRIGRAM_LEN]


def _fts_expression(keys: list[str], *, broad: bool) -> str | None:
    """One OR of phrase clauses, restricted to the `text` column.

    The column filter matters: `kind` is an indexed column in the contracted DDL, so an
    unfiltered MATCH on "series_title" would otherwise return the entire library.
    """
    clauses: list[str] = []
    seen: set[str] = set()

    def add(clause: str) -> None:
        if clause not in seen:
            seen.add(clause)
            clauses.append(clause)

    for key in keys:
        if len(key) >= MIN_TRIGRAM_LEN:
            add(_quote(key))

    # FTS5 AND is per-row, so this only catches words that share one indexed field.
    terms = _long_terms(keys[0]) if keys else []
    if len(terms) > 1:
        add("(" + " AND ".join(_quote(term) for term in terms) + ")")
        if broad:
            for term in terms:
                add(_quote(term))

    if not clauses:
        return None
    return "{text} : (" + " OR ".join(clauses) + ")"


async def _fts_candidates(
    session: AsyncSession, keys: list[str], cap: int, *, broad: bool = False
) -> list[tuple[str, str, str]]:
    expr = _fts_expression(keys, broad=broad)
    if expr is None:
        return []
    result = await session.execute(_MATCH_SQL, {"expr": expr, "cap": cap})
    return [(row[0], row[1], row[2]) for row in result.all()]


async def _prefix_candidates(session: AsyncSession, keys: list[str], cap: int) -> list[str]:
    """Contracted fallback for queries under three characters: prefix LIKE on
    `series_titles.normalized`. Every romanization is a row there, so "mq" lands."""
    if not keys:
        return []
    conditions = [SeriesTitle.normalized.startswith(key, autoescape=True) for key in keys]
    stmt = select(SeriesTitle.series_id).where(or_(*conditions)).distinct().limit(cap)
    return list((await session.execute(stmt)).scalars())


def _score(query: str, candidate: str) -> float:
    if not candidate or not query:
        return 0.0
    if candidate == query:
        return 100.0
    ratio = float(fuzz.WRatio(query, candidate))
    if candidate.startswith(query):
        return max(ratio, 94.0)
    if query in candidate:
        return max(ratio, 86.0)
    return ratio


class _Match:
    __slots__ = ("field", "kind", "language", "score", "text")

    def __init__(
        self,
        score: float,
        text: str,
        field: MatchField,
        language: str = "",
        kind: str = "",
    ) -> None:
        self.score = score
        self.text = text
        self.field = field
        self.language = language
        self.kind = kind


def _title_field(kind: str | None) -> MatchField:
    if kind is None:
        return MatchField.TITLE
    if kind in _ROMANIZED_KINDS:
        return MatchField.ROMANIZATION
    return MatchField.ALTERNATE


def _best_title_match(query: str, series: Series) -> _Match:
    best = _Match(0.0, series.title, MatchField.TITLE, series.original_language or "")
    seen: set[str] = set()

    def consider(display: str, normalized: str, kind: str | None, language: str | None) -> None:
        nonlocal best
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        score = _score(query, normalized)
        if score > best.score:
            best = _Match(score, display, _title_field(kind), language or "", kind or "")

    # The canonical title goes first so a `series_titles` row that merely repeats it is
    # deduped away and the hit is reported as `title`, not `alternate`.
    consider(series.title, normalize(series.title), None, series.original_language)
    if series.original_title:
        consider(series.original_title, normalize(series.original_title), None, None)
    for title in series.titles:
        body = title.normalized or normalize(title.title)
        consider(title.title, body, title.kind, title.language)
    return best


def _overview_match(query: str, series: Series) -> _Match | None:
    body = normalize(series.overview)
    if not body:
        return None
    score = float(fuzz.partial_ratio(query, body)) * OVERVIEW_WEIGHT
    return _Match(score, series.overview or "", MatchField.OVERVIEW)


def _best_episode_text(query: str, episode: Episode) -> tuple[float, str]:
    best_score = 0.0
    best_text = episode.title or ""
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []
    if episode.title:
        candidates.append((episode.title, normalize(episode.title)))
    for title in episode.titles:
        candidates.append((title.title, title.normalized or normalize(title.title)))
    for display, body in candidates:
        if not body or body in seen:
            continue
        seen.add(body)
        score = _score(query, body)
        if score > best_score:
            best_score, best_text = score, display
    return best_score, best_text


async def search_library(
    session: AsyncSession,
    query: str,
    *,
    limit: int = 30,
    user_id: str | None = None,
) -> SearchResults:
    """Multilingual search over titles, romanizations, overviews and episode titles.

    `user_id` is optional so the function stays callable from maintenance code; pass it from
    the API so hits carry the caller's watched/progress counters like every other list does.
    """
    started = time.perf_counter()
    raw = (query or "").strip()
    normalized = normalize(raw)

    def _empty() -> SearchResults:
        return SearchResults(
            query=raw,
            normalized_query=normalized,
            total=0,
            took_ms=round((time.perf_counter() - started) * 1000, 3),
            series=[],
            episodes=[],
        )

    if not normalized:
        return _empty()

    await ensure_index(session)

    # Romanized forms of the *query* are searched too, so a Han query still finds a series
    # whose index only carries pinyin, and vice versa. pypinyin/pykakasi are synchronous and
    # load sizeable dictionaries on first use, so they never run on the event loop.
    keys = await asyncio.to_thread(search_keys, raw) or [normalized]
    if keys[0] != normalized:
        keys = [normalized, *[k for k in keys if k != normalized]]

    # A 10x pool is ample for re-ranking `limit` results, and it bounds the worst case when
    # a common word appears in every overview in the library.
    cap = min(max(limit * 10, 150), 500)
    series_ids: set[str] = set()
    episode_ids: set[str] = set()
    overview_hits: set[str] = set()

    rows: list[tuple[str, str, str]] = []
    if len(normalized) >= MIN_TRIGRAM_LEN:
        rows = await _fts_candidates(session, keys, cap)
        if not rows and len(_long_terms(normalized)) > 1:
            # Nothing shares a field with every word; loosen to any-word before giving up.
            rows = await _fts_candidates(session, keys, cap, broad=True)

    for kind, ref_id, series_id in rows:
        series_ids.add(series_id)
        if kind == KIND_EPISODE_TITLE:
            episode_ids.add(ref_id)
        elif kind == KIND_SERIES_OVERVIEW:
            overview_hits.add(series_id)
        elif kind != KIND_SERIES_TITLE:  # forward-compatible with row kinds added later
            log.debug("Unknown search_index kind %r", kind)

    if not series_ids:
        series_ids.update(await _prefix_candidates(session, keys, cap))

    if not series_ids:
        return _empty()

    series_rows = list(
        (
            await session.execute(
                select(Series).where(Series.id.in_(series_ids)).options(selectinload(Series.titles))
            )
        ).scalars()
    )

    episodes: list[Episode] = []
    if episode_ids:
        episodes = list(
            (
                await session.execute(
                    select(Episode)
                    .where(Episode.id.in_(episode_ids))
                    .options(selectinload(Episode.titles), selectinload(Episode.media_files))
                )
            ).scalars()
        )

    # One aggregate query for every candidate series and one for every candidate episode's
    # progress — the per-hit serializers below must not touch the database.
    counts = await library_service.series_counts(
        session, [s.id for s in series_rows], user_id or ""
    )
    progress = (
        await progress_for_episodes(session, user_id, [ep.id for ep in episodes])
        if user_id and episodes
        else {}
    )

    by_series: dict[str, list[tuple[float, str, Episode]]] = defaultdict(list)
    for episode in episodes:
        score, matched_text = _best_episode_text(normalized, episode)
        if score >= MIN_SCORE:
            by_series[episode.series_id].append((score, matched_text, episode))

    hits: list[tuple[float, str, SeriesHit]] = []
    for series in series_rows:
        best = _best_title_match(normalized, series)
        if series.id in overview_hits:
            overview = _overview_match(normalized, series)
            if overview is not None and overview.score > best.score:
                best = overview

        episode_matches = sorted(by_series.get(series.id, []), key=lambda item: -item[0])
        if episode_matches and episode_matches[0][0] * EPISODE_WEIGHT > best.score:
            top = episode_matches[0]
            best = _Match(top[0] * EPISODE_WEIGHT, top[1], MatchField.EPISODE)

        if best.score < MIN_SCORE:
            continue

        nested = [
            EpisodeHit(
                episode=library_service.episode_summary(episode, progress.get(episode.id)),
                series_id=series.id,
                series_slug=series.slug,
                series_title=series.title,
                score=round(score, 2),
                matched_title=matched_text,
            )
            for score, matched_text, episode in episode_matches[:MAX_EPISODES_PER_SERIES]
        ]
        nested.sort(key=lambda hit: (hit.episode.season_number, hit.episode.episode_number))

        hits.append(
            (
                best.score,
                normalize(series.title),
                SeriesHit(
                    series=library_service.series_summary(series, counts.get(series.id)),
                    score=round(best.score, 2),
                    matched_title=best.text,
                    matched_language=best.language,
                    matched_field=best.field,
                    matched_title_kind=best.kind,
                    episodes=nested,
                ),
            )
        )

    hits.sort(key=lambda item: (-item[0], item[1]))
    top_hits = [hit for _, _, hit in hits[:limit]]

    flat: list[EpisodeHit] = [ep for hit in top_hits for ep in hit.episodes]
    flat.sort(key=lambda hit: -hit.score)

    return SearchResults(
        query=raw,
        normalized_query=normalized,
        total=len(hits),
        took_ms=round((time.perf_counter() - started) * 1000, 3),
        series=top_hits,
        episodes=flat[:limit],
    )


__all__ = ["MIN_SCORE", "MIN_TRIGRAM_LEN", "search_library"]
