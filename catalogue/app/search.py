"""Full-text search.

The only dialect-specific module besides `db.py`. Callers use `search_titles`
and never see SQL, so the Postgres switch is a second branch here rather than a
rewrite. Nothing depends on a Postgres-only extension such as pg_trgm.

The index is rebuilt after a sync rather than maintained by triggers: syncs are
batch jobs, a rebuild over ~10k rows takes well under a second, and it removes
a whole class of "the trigger missed a row" bugs.

RESULTS ARE TIERED, because a hard AND across every term made a longer query
return FEWER results -- so pasting a series' exact title could return nothing
while a fragment of it returned matches. That is backwards for the main use of
an indexer, where the caller usually already knows the name.

    1. exact match on the normalised title or an alias
    2. FTS with every term required  (precise)
    3. FTS with any term matching, ranked  (broad)

Ties are resolved by tier, then by FTS rank. Tier 1 is what guarantees that
searching an exact title returns THAT title first even when five near-identical
siblings share most of its words.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.normalize import normalize_title

FTS_TABLE = "title_fts"
#: Postgres equivalent. Like the FTS5 table it is created and populated here,
#: not by the ORM, and alembic is told to ignore it.
PG_TABLE = "title_search"


def _is_sqlite(session: Session) -> bool:
    return session.get_bind().dialect.name == "sqlite"


def ensure_index(session: Session) -> None:
    if not _is_sqlite(session):
        _ensure_pg_index(session)
        return
    session.execute(
        text(
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE}
                USING fts5(title, aliases, synopsis, people,
                           content='', tokenize='unicode61')"""
        )
    )
    session.commit()


def _ensure_pg_index(session: Session) -> None:
    session.execute(
        text(
            f"""CREATE TABLE IF NOT EXISTS {PG_TABLE} (
                    title_id integer PRIMARY KEY
                        REFERENCES title(id) ON DELETE CASCADE,
                    vector   tsvector NOT NULL
                )"""
        )
    )
    session.execute(
        text(f"CREATE INDEX IF NOT EXISTS ix_{PG_TABLE}_vector"
             f" ON {PG_TABLE} USING GIN (vector)")
    )
    session.commit()


#: Field weights: a hit in the title outranks one in the synopsis. A is the
#: title, B aliases, C cast, D synopsis.
_PG_VECTOR = """
    setweight(to_tsvector('simple', coalesce(t.primary_title, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce((
        SELECT string_agg(a.alias, ' ') FROM title_alias a WHERE a.title_id = t.id
    ), '')), 'B') ||
    setweight(to_tsvector('simple', coalesce((
        SELECT string_agg(p.name, ' ') FROM title_person tp
        JOIN person p ON p.id = tp.person_id WHERE tp.title_id = t.id
    ), '')), 'C') ||
    setweight(to_tsvector('simple', coalesce(t.synopsis, '')), 'D')
"""


def _reindex_pg(session: Session) -> int:
    """Rebuild the Postgres index in one statement.

    'simple' rather than 'english': these titles are template-generated and
    stemming "Heiress" to "heiress" is all the normalisation wanted -- English
    stemming would also collapse distinctions the catalogue actually uses.
    """
    _ensure_pg_index(session)
    session.execute(text(f"TRUNCATE {PG_TABLE}"))
    result = session.execute(
        text(
            f"INSERT INTO {PG_TABLE} (title_id, vector)"
            f" SELECT t.id, {_PG_VECTOR} FROM title t"
            f" WHERE t.merged_into_id IS NULL"
        )
    )
    session.commit()
    return result.rowcount or 0


def reindex(session: Session) -> int:
    """Rebuild the search index. Returns the number of titles indexed."""
    if not _is_sqlite(session):
        return _reindex_pg(session)
    ensure_index(session)
    # A contentless FTS5 table rejects a plain DELETE once it holds rows -- and
    # only then, so the first reindex on a fresh database succeeds and the
    # second fails. The documented reset is this command form.
    session.execute(text(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('delete-all')"))

    rows = session.execute(
        text(
            """
            SELECT t.id,
                   t.primary_title,
                   COALESCE((SELECT group_concat(a.alias, ' ')
                             FROM title_alias a WHERE a.title_id = t.id), ''),
                   COALESCE(t.synopsis, ''),
                   COALESCE((SELECT group_concat(p.name, ' ')
                             FROM title_person tp JOIN person p ON p.id = tp.person_id
                             WHERE tp.title_id = t.id), '')
            FROM title t
            WHERE t.merged_into_id IS NULL
            """
        )
    ).all()

    for row in rows:
        session.execute(
            text(
                f"INSERT INTO {FTS_TABLE}(rowid, title, aliases, synopsis, people)"
                " VALUES (:id, :title, :aliases, :synopsis, :people)"
            ),
            {
                "id": row[0],
                "title": row[1],
                "aliases": row[2],
                "synopsis": row[3],
                "people": row[4],
            },
        )
    session.commit()
    return len(rows)


def _terms(query: str) -> list[str]:
    """FTS5 treats plenty of punctuation as syntax, so each term is quoted.

    Without this, searching `billionaire's` is a syntax error rather than a
    search.
    """
    return [t for t in query.replace('"', " ").split() if t]


def _match_all(terms: list[str]) -> str:
    return " ".join(f'"{t}"' for t in terms)


def _match_any(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms)


def exact_ids(session: Session, query: str) -> list[int]:
    """Titles whose normalised title or alias equals the query exactly.

    This is the tier that makes "paste the full name" work, and it is also what
    keeps a near-identical sibling from outranking the title actually asked for.
    """
    normalised = normalize_title(query)
    if not normalised:
        return []
    # Tombstones must be excluded HERE as well as in the index. This tier is a
    # live query against `title` and `title_alias`, so `reindex` skipping merged
    # rows does nothing for it -- and almost every merged pair shares a
    # normalized_title, which is how the pair was found. Leaving them in returns
    # a superseded id at rank 1 and, worse, silently shortens the page: the API
    # slices `limit + 1` ranked ids and then drops the tombstones, so a full page
    # comes back short with no next_cursor and the rest of the results vanish.
    rows = session.execute(
        text(
            """
            SELECT id FROM title
             WHERE normalized_title = :q AND merged_into_id IS NULL
            UNION
            SELECT a.title_id FROM title_alias a
              JOIN title t ON t.id = a.title_id
             WHERE a.normalized_alias = :q AND t.merged_into_id IS NULL
            """
        ),
        {"q": normalised},
    ).all()
    return [r[0] for r in rows]


def _fts_ids(session: Session, expression: str, limit: int) -> list[int]:
    try:
        rows = session.execute(
            text(
                f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :q"
                " ORDER BY rank LIMIT :limit"
            ),
            {"q": expression, "limit": limit},
        ).all()
    except Exception:
        # A malformed FTS expression is a bad query, not a server fault.
        session.rollback()
        return []
    return [r[0] for r in rows]


def search_ids(session: Session, query: str, limit: int = 200) -> list[int]:
    """Title ids matching a free-text query, best match first."""
    if not query or not query.strip():
        return []

    if not _is_sqlite(session):
        return _search_pg(session, query, limit)

    ensure_index(session)
    terms = _terms(query)
    if not terms:
        return []

    ordered: list[int] = []
    seen: set[int] = set()

    def extend(ids: list[int]) -> None:
        for title_id in ids:
            if title_id not in seen:
                seen.add(title_id)
                ordered.append(title_id)
            if len(ordered) >= limit:
                return

    # 1. Exact title or alias.
    extend(exact_ids(session, query))
    # 2. Every term present -- precise, and the old behaviour.
    if len(ordered) < limit:
        extend(_fts_ids(session, _match_all(terms), limit))
    # 3. Any term, ranked -- ONLY when the precise tiers found nothing.
    #    Padding a good result set with weak matches makes "one exact hit" and
    #    "no hit at all" look identical to a caller: both return a full page.
    if not ordered and len(terms) > 1:
        extend(_fts_ids(session, _match_any(terms), limit))

    return ordered[:limit]


def _pg_ids(session: Session, tsquery_sql: str, query: str, limit: int) -> list[int]:
    try:
        rows = session.execute(
            text(
                f"SELECT title_id FROM {PG_TABLE}"
                f" WHERE vector @@ {tsquery_sql}"
                f" ORDER BY ts_rank(vector, {tsquery_sql}) DESC, title_id"
                " LIMIT :limit"
            ),
            {"q": query, "limit": limit},
        ).all()
    except Exception:
        session.rollback()
        return []
    return [r[0] for r in rows]


def _search_pg(session: Session, query: str, limit: int) -> list[int]:
    """Same tiering as SQLite: exact, then all terms, then any terms.

    The tiers matter more than the engine -- they are what makes pasting a full
    title return that title rather than a page of near-misses.
    """
    _ensure_pg_index(session)
    terms = _terms(query)
    if not terms:
        return []

    ordered: list[int] = []
    seen: set[int] = set()

    def extend(ids: list[int]) -> None:
        for title_id in ids:
            if title_id not in seen:
                seen.add(title_id)
                ordered.append(title_id)

    extend(exact_ids(session, query))
    if len(ordered) < limit:
        # plainto_tsquery ANDs the terms.
        extend(_pg_ids(session, "plainto_tsquery('simple', :q)", query, limit))
    if not ordered and len(terms) > 1:
        # websearch_to_tsquery understands OR between quoted terms.
        any_terms = " or ".join(terms)
        extend(_pg_ids(session, "websearch_to_tsquery('simple', :q)", any_terms, limit))

    return ordered[:limit]
