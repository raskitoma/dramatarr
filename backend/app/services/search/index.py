"""The FTS5 index behind multilingual search.

The `trigram` tokenizer is the whole point: it treats CJK and Latin identically and gives
substring matching for free, so "INFAT" and "miqing" both hit without a language-aware
tokenizer. Its one constraint is that a MATCH needs at least three characters — queries
below that are served by the LIKE fallback in `service.py`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Episode, Series, SeriesTitle, TitleKind, TitleSource
from app.services.search.normalize import normalize, search_keys

log = logging.getLogger(__name__)

INDEX_TABLE = "search_index"

KIND_SERIES_TITLE = "series_title"
KIND_SERIES_OVERVIEW = "series_overview"
KIND_EPISODE_TITLE = "episode_title"

_CREATE_INDEX = sql(
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {INDEX_TABLE} USING fts5("
    "kind, ref_id UNINDEXED, series_id UNINDEXED, text, tokenize='trigram')"
)
_TABLE_EXISTS = sql("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = :name")
_DELETE_SERIES = sql(f"DELETE FROM {INDEX_TABLE} WHERE series_id = :series_id")
_DELETE_ALL = sql(f"DELETE FROM {INDEX_TABLE}")
_INSERT_ROW = sql(
    f"INSERT INTO {INDEX_TABLE} (kind, ref_id, series_id, text) "
    "VALUES (:kind, :ref_id, :series_id, :text)"
)

_REBUILD_BATCH = 100


async def ensure_index(session: AsyncSession) -> None:
    """Idempotent.

    Commits only when it actually created the table *and* the caller had nothing else in
    flight. `reindex_series` promises the caller owns the transaction, and it is reached
    from the admin PATCH route with the edited series still pending — committing there would
    silently harden a half-finished request. When the session is clean (the query path, the
    lifespan hook) the DDL is committed so it does not sit inside an open transaction.
    """
    exists = await session.scalar(_TABLE_EXISTS, {"name": INDEX_TABLE})
    if exists:
        return
    await session.execute(_CREATE_INDEX)
    if not (session.new or session.dirty or session.deleted):
        await session.commit()


def _romanization_sources(
    series: Series | None, titles: list[SeriesTitle]
) -> list[tuple[str, str, str]]:
    """(language, text, source) triples worth romanizing.

    `series.title` and `series.original_title` are in here, not just the `series_titles`
    rows: an admin renaming a series through `PATCH /series/{id}` changes the column without
    writing a title row, and a series can reach this function before anything has written
    one at all. Without them a rename to a non-Latin title would be findable only in its own
    script.
    """
    sources: list[tuple[str, str, str]] = []
    if series is not None:
        sources.append(("", series.title, TitleSource.MANUAL))
        if series.original_title:
            sources.append(
                (series.original_language or "", series.original_title, TitleSource.MANUAL)
            )
    sources.extend(
        (t.language, t.title, t.source or TitleSource.MANUAL)
        for t in titles
        if t.kind != TitleKind.ROMANIZATION
    )
    return [(lang, text, src) for lang, text, src in sources if text and text.strip()]


async def ensure_title_variants(session: AsyncSession, series_id: str) -> int:
    """Materialize every romanization of a series' titles as its own `series_titles` row.

    §7 requires the variants to live in the table rather than be computed at query time —
    the short-query LIKE fallback reads `series_titles.normalized` directly, so "mq" is only
    findable if the acronym exists as a row. Doing it here means any writer of titles gets
    the variants for free by calling `reindex_series`.
    """
    titles = list(
        (
            await session.execute(select(SeriesTitle).where(SeriesTitle.series_id == series_id))
        ).scalars()
    )
    series = await session.get(Series, series_id)
    # Deduped on the normalized form alone, the same rule `metadata._TitleCollector` uses: a
    # second row with an identical `normalized` adds nothing to the index and would only be
    # noise in the admin's title editor.
    existing = {normalize(t.title) for t in titles}
    sources = _romanization_sources(series, titles)

    # pypinyin/pykakasi are synchronous CPU work; batch the whole series off the loop.
    texts = list(dict.fromkeys(text for _, text, _ in sources))
    variants = await asyncio.to_thread(lambda: {text: search_keys(text) for text in texts})

    added = 0
    for _language, text, source in sources:
        for key in variants.get(text, ()):
            if key in existing:
                continue
            existing.add(key)
            session.add(
                SeriesTitle(
                    series_id=series_id,
                    language="",
                    title=key,
                    kind=TitleKind.ROMANIZATION,
                    source=source,
                    normalized=key,
                )
            )
            added += 1

    # Backfill `normalized` on rows a writer left blank; the fallback query depends on it.
    for title in titles:
        expected = normalize(title.title)
        if title.normalized != expected:
            title.normalized = expected

    if added or titles:
        await session.flush()
    return added


def _series_rows(series: Series) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, ref_id: str, value: str | None) -> None:
        body = normalize(value)
        # ref_id only discriminates for episodes; two series titles with the same normalized
        # text are one index row no matter which SeriesTitle row they came from.
        key = (kind, ref_id if kind == KIND_EPISODE_TITLE else "", body)
        if not body or key in seen:
            return
        seen.add(key)
        rows.append({"kind": kind, "ref_id": ref_id, "series_id": series.id, "text": body})

    add(KIND_SERIES_TITLE, series.id, series.title)
    add(KIND_SERIES_TITLE, series.id, series.original_title)
    for title in series.titles:
        add(KIND_SERIES_TITLE, title.id, title.normalized or title.title)
    add(KIND_SERIES_OVERVIEW, series.id, series.overview)

    for episode in series.episodes:
        # Episode.title is indexed alongside the EpisodeTitle rows because a scan-created
        # episode often has the former and none of the latter.
        add(KIND_EPISODE_TITLE, episode.id, episode.title)
        for episode_title in episode.titles:
            add(KIND_EPISODE_TITLE, episode.id, episode_title.normalized or episode_title.title)

    return rows


async def _load_series(session: AsyncSession, series_id: str) -> Series | None:
    # populate_existing is load-bearing: the caller has usually just edited the series (the
    # PATCH route holds it in the identity map) and `ensure_title_variants` has just inserted
    # rows into `series_titles`. Without it SQLAlchemy keeps the already-loaded collections
    # and the new romanizations never reach the index.
    stmt = (
        select(Series)
        .where(Series.id == series_id)
        .options(
            selectinload(Series.titles),
            selectinload(Series.episodes).selectinload(Episode.titles),
        )
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def reindex_series(session: AsyncSession, series_id: str) -> None:
    """Replace this series' slice of the index. Transaction control stays with the caller."""
    await ensure_index(session)
    await ensure_title_variants(session, series_id)

    series = await _load_series(session, series_id)
    await session.execute(_DELETE_SERIES, {"series_id": series_id})
    if series is None:
        return

    rows = _series_rows(series)
    if rows:
        await session.execute(_INSERT_ROW, rows)


async def rebuild_index(session: AsyncSession) -> None:
    """Wipe and redo everything, committing per batch so a large library does not build one
    enormous transaction."""
    await ensure_index(session)
    await session.execute(_DELETE_ALL)
    await session.commit()

    ids = list((await session.execute(select(Series.id).order_by(Series.id))).scalars())
    log.info("Rebuilding search index over %d series", len(ids))

    for start in range(0, len(ids), _REBUILD_BATCH):
        for series_id in ids[start : start + _REBUILD_BATCH]:
            await reindex_series(session, series_id)
        await session.commit()


async def drop_index(session: AsyncSession) -> None:
    """Only used by maintenance and tests; a schema change to the virtual table needs it."""
    await session.execute(sql(f"DROP TABLE IF EXISTS {INDEX_TABLE}"))
    await session.commit()


__all__ = [
    "INDEX_TABLE",
    "KIND_EPISODE_TITLE",
    "KIND_SERIES_OVERVIEW",
    "KIND_SERIES_TITLE",
    "drop_index",
    "ensure_index",
    "ensure_title_variants",
    "rebuild_index",
    "reindex_series",
]
