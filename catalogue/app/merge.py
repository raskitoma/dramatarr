"""Merging one `title` row into another.

Nine tables carry a FK to `title.id`, and two more hold title ids outside the ORM
entirely (`title_search` on Postgres, `title_fts` on SQLite -- both created in raw
SQL by app/search.py and both hidden from Alembic). All eleven have to be handled
or the merge loses data.

The asymmetry that governs the whole module: five of the unique constraints
include a source column, so they CANNOT collide when the two rows come from
different platforms, and DO collide when they come from the same one. Intra-source
duplicates are not the edge case here -- DramaBox's 485 `(DUBBED)` twins are all
intra-source, so the colliding path is the common path and every repoint is written
conflict-aware.

Two rules the whole design rests on:

1. **Repoint `title_source`, never delete it.** `ingest_record` finds an existing
   title only through `(source_id, external_id)`. Delete the losing link and the
   next sync of that source takes the `link is None` branch and recreates the
   duplicate. The repointed link IS the permanent record of the merge.

2. **Tombstone the loser, never delete the row.** A downstream consumer already
   stores these ids; a 404 tells it nothing about where the title went. The row
   stays with `merged_into_id` set, which also makes the merge reversible.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.ingest import _resolve_title
from app.models import MergeState, Title, TitleMerge
from app.normalize import normalize_title

log = logging.getLogger(__name__)


class MergeError(RuntimeError):
    pass


def _eq_or_both_null(alias_a: str, alias_b: str, column: str) -> str:
    """Portable NULL-safe equality.

    Both engines treat NULLs as DISTINCT in a unique constraint, so a nullable
    column cannot be compared with `=` when deciding whether a row would collide.
    Postgres has IS NOT DISTINCT FROM and SQLite has IS; neither is in both.
    """
    return (
        f"({alias_a}.{column} = {alias_b}.{column}"
        f" OR ({alias_a}.{column} IS NULL AND {alias_b}.{column} IS NULL))"
    )


def _repoint(session: Session, table: str, survivor: int, loser: int,
             conflict_columns: tuple[str, ...]) -> tuple[int, int]:
    """Move `table` rows from loser to survivor, dropping any that would collide.

    Returns (moved, dropped). The colliding row is deleted rather than kept
    because the survivor already holds the same fact.
    """
    if conflict_columns:
        predicate = " AND ".join(
            _eq_or_both_null("keep", "dupe", column) for column in conflict_columns
        )
        dropped = session.execute(
            text(
                f"DELETE FROM {table} WHERE id IN ("
                f"  SELECT dupe.id FROM {table} dupe"
                f"  WHERE dupe.title_id = :loser AND EXISTS ("
                f"    SELECT 1 FROM {table} keep"
                f"    WHERE keep.title_id = :survivor AND {predicate}"
                f"  )"
                f")"
            ),
            {"survivor": survivor, "loser": loser},
        ).rowcount or 0
    else:
        dropped = 0

    moved = session.execute(
        text(f"UPDATE {table} SET title_id = :survivor WHERE title_id = :loser"),
        {"survivor": survivor, "loser": loser},
    ).rowcount or 0
    return moved, dropped


def _keep_best_billing(session: Session, survivor: int, loser: int) -> None:
    """`billing_order` sits outside uq_title_person, so a plain conflict-drop can
    silently discard the better billing. Lift it onto the surviving row first."""
    session.execute(
        text(
            "UPDATE title_person SET billing_order = ("
            "  SELECT MIN(dupe.billing_order) FROM title_person dupe"
            "  WHERE dupe.title_id = :loser"
            "    AND dupe.person_id = title_person.person_id"
            "    AND dupe.billing_order IS NOT NULL"
            ") WHERE title_id = :survivor AND ("
            "  billing_order IS NULL OR billing_order > ("
            "    SELECT MIN(dupe.billing_order) FROM title_person dupe"
            "    WHERE dupe.title_id = :loser"
            "      AND dupe.person_id = title_person.person_id"
            "      AND dupe.billing_order IS NOT NULL"
            "  )"
            ")"
        ),
        {"survivor": survivor, "loser": loser},
    )


def _resolve_ratings(session: Session, survivor: int, loser: int) -> int:
    """uq_rating_title_source is exactly (title_id, source_id), so any source the
    two rows share is an immediate IntegrityError. Keep the better-established
    row -- higher vote count -- rather than whichever happens to be first.

    Deletes the WEAKER row on whichever side it sits. Only dropping from the
    loser's side would look right and be wrong: the repoint that follows drops any
    row that still conflicts, so a better loser row would be discarded anyway and
    the survivor's inferior rating would silently win.
    """
    dropped = session.execute(
        text(
            "DELETE FROM rating WHERE title_id = :survivor AND source_id IN ("
            "  SELECT source_id FROM rating WHERE title_id = :loser"
            ") AND COALESCE(votes, 0) < COALESCE(("
            "  SELECT COALESCE(other.votes, 0) FROM rating other"
            "  WHERE other.title_id = :loser AND other.source_id = rating.source_id"
            "), 0)"
        ),
        {"survivor": survivor, "loser": loser},
    ).rowcount or 0
    return dropped


def _resolve_match_candidates(session: Session, survivor: int, loser: int) -> int:
    """Two near-identically titled duplicates are exactly the ambiguous input
    imdb.decide() refuses, so both rows often hold the SAME proposed tconst.

    Retention precedence is REJECTED > CONFIRMED > PROPOSED, not row order: the
    IMDb job rebuilds its veto set from (title_id, target_external_id), so losing
    a REJECTED row would let a human's rejection be re-proposed forever.

    As with ratings, the weaker row is dropped on whichever side it sits -- the
    repoint that follows would otherwise discard a stronger loser-side state.
    """
    return session.execute(
        text(
            "DELETE FROM match_candidate WHERE title_id = :survivor"
            " AND EXISTS ("
            "   SELECT 1 FROM match_candidate other"
            "   WHERE other.title_id = :loser"
            "     AND other.target_source_id = match_candidate.target_source_id"
            "     AND other.target_external_id = match_candidate.target_external_id"
            "     AND CASE other.state WHEN 'rejected' THEN 3"
            "                          WHEN 'confirmed' THEN 2 ELSE 1 END"
            "       > CASE match_candidate.state WHEN 'rejected' THEN 3"
            "                          WHEN 'confirmed' THEN 2 ELSE 1 END"
            " )"
        ),
        {"survivor": survivor, "loser": loser},
    ).rowcount or 0


def _preserve_losing_title_as_alias(session: Session, survivor: Title,
                                    loser: Title) -> bool:
    """Keep the loser's spelling searchable.

    `_sync_aliases` suppresses an alias equal to its own record title, so the
    loser's `primary_title` exists nowhere as an alias. Without this the DramaBox
    spelling "Fruit Dimension: Orange LightRedemption. 1" becomes unfindable the
    moment it is merged away -- a user-visible regression caused by the fix.
    """
    normalized = normalize_title(loser.primary_title)
    if not normalized or normalized == normalize_title(survivor.primary_title):
        return False

    source_id = session.execute(
        text("SELECT source_id FROM title_source WHERE title_id = :t LIMIT 1"),
        {"t": survivor.id},
    ).scalar()
    exists = session.execute(
        text(
            "SELECT 1 FROM title_alias WHERE title_id = :t"
            " AND normalized_alias = :n LIMIT 1"
        ),
        {"t": survivor.id, "n": normalized},
    ).first()
    if exists:
        return False

    session.execute(
        text(
            "INSERT INTO title_alias (title_id, alias, normalized_alias, source_id)"
            " VALUES (:t, :a, :n, :s)"
        ),
        {"t": survivor.id, "a": loser.primary_title, "n": normalized, "s": source_id},
    )
    return True


def _drop_from_search_index(session: Session, loser: int) -> None:
    """Postgres keys `title_search` by title_id (PRIMARY KEY), so repointing is a
    guaranteed violation -- delete instead. SQLite's contentless fts5 cannot be
    row-deleted without the original column values, so it is left to the caller's
    reindex; leaving it is what puts a dead id in search results."""
    if session.bind.dialect.name == "postgresql":
        session.execute(
            text("DELETE FROM title_search WHERE title_id = :t"), {"t": loser}
        )


def resolve_survivor(session: Session, title_id: int) -> int:
    """Follow a tombstone chain to the live title. Merging into an already-merged
    row would otherwise strand the consumer one hop short."""
    seen: set[int] = set()
    current = title_id
    while True:
        if current in seen:
            raise MergeError(f"merge cycle through title {current}")
        seen.add(current)
        nxt = session.execute(
            text("SELECT merged_into_id FROM title WHERE id = :t"), {"t": current}
        ).scalar()
        if nxt is None:
            return current
        current = nxt


def merge_titles(session: Session, survivor_id: int, loser_id: int, *,
                 reason: str = "", key_kind: str | None = None,
                 merged_by: str | None = None) -> dict:
    """Fold `loser_id` into `survivor_id`. Returns a per-table report.

    Caller owns the transaction: every step is a statement against `session`, so
    a failure anywhere rolls the whole merge back rather than leaving a title
    half-absorbed.
    """
    if survivor_id == loser_id:
        raise MergeError("cannot merge a title into itself")

    survivor_id = resolve_survivor(session, survivor_id)
    if survivor_id == loser_id:
        raise MergeError(f"title {loser_id} is already the survivor of this chain")

    survivor = session.get(Title, survivor_id)
    loser = session.get(Title, loser_id)
    if survivor is None or loser is None:
        raise MergeError(f"missing title: survivor={survivor_id} loser={loser_id}")
    if loser.merged_into_id is not None:
        raise MergeError(f"title {loser_id} is already merged into {loser.merged_into_id}")

    # An operator's rejection outranks the rule. Without this the pair is re-merged
    # on the next run, because the evidence that produced the verdict has not
    # changed -- only the human judgement about it has.
    rejected = session.scalars(
        select(TitleMerge).where(
            TitleMerge.loser_id == loser_id,
            TitleMerge.survivor_id == survivor_id,
            TitleMerge.state == MergeState.REJECTED,
        )
    ).first()
    if rejected is not None:
        raise MergeError(
            f"merge {loser_id} -> {survivor_id} was rejected by"
            f" {rejected.merged_by or 'an operator'}; re-merging needs that row cleared"
        )

    report: dict[str, object] = {"survivor": survivor_id, "loser": loser_id}

    # Capture the loser's links BEFORE repointing them -- they are the identity
    # anchors, and after the UPDATE there is no way to tell which of the
    # survivor's links used to belong to the loser.
    report["links"] = [
        {"title_source_id": row[0], "source_id": row[1], "external_id": row[2]}
        for row in session.execute(
            text(
                "SELECT id, source_id, external_id FROM title_source"
                " WHERE title_id = :t ORDER BY id"
            ),
            {"t": loser_id},
        ).all()
    ]

    # Conflict-resolving tables first: they decide WHICH row survives on their own
    # terms, and a blind repoint would raise before that judgement can be applied.
    # These count rows dropped from the SURVIVOR because the loser's were better.
    report["rating_superseded"] = _resolve_ratings(session, survivor_id, loser_id)
    report["match_candidate_superseded"] = _resolve_match_candidates(
        session, survivor_id, loser_id
    )
    _keep_best_billing(session, survivor_id, loser_id)

    # uq_title_image has NO source_id, so it is the one table that collides just as
    # readily across platforms as within one. Only the link is ever deleted: an
    # `image` row is content-addressed and shared, so deleting it would strip art
    # from unrelated titles.
    for table, conflict in (
        ("rating", ("source_id",)),
        ("title_image", ("image_id", "kind")),
        ("match_candidate", ("target_source_id", "target_external_id")),
        ("episode", ("source_id", "external_id")),
        ("title_person", ("person_id", "role", "source_id")),
        ("title_tag", ("tag_id", "source_id")),
        ("title_alias", ("normalized_alias", "source_id")),
        # No uniqueness at all -- history is append-only, so everything moves.
        ("title_change", ()),
        # Must move, must never be dropped. See the module docstring.
        ("title_source", ()),
    ):
        moved, dropped = _repoint(session, table, survivor_id, loser_id, conflict)
        report[table] = {"moved": moved, "dropped": dropped}

    report["alias_added"] = _preserve_losing_title_as_alias(session, survivor, loser)
    _drop_from_search_index(session, loser_id)

    now = datetime.now(UTC)
    loser.merged_into_id = survivor_id
    loser.merged_at = now

    # Flatten any chain that already pointed at the loser, rather than leaving
    # `resolve_survivor` to walk it forever. `title.merged_into_id` and
    # `title_merge.survivor_id` are both FKs to `title.id` that the repoint loop
    # above does not touch, so without this an operator merging a row that is
    # itself the survivor of an earlier merge leaves tombstones two hops from the
    # live title.
    report["chain_flattened"] = session.execute(
        text(
            "UPDATE title SET merged_into_id = :survivor, updated_at = :now"
            " WHERE merged_into_id = :loser"
        ),
        {"survivor": survivor_id, "loser": loser_id, "now": now},
    ).rowcount or 0
    session.execute(
        text("UPDATE title_merge SET survivor_id = :survivor WHERE survivor_id = :loser"),
        {"survivor": survivor_id, "loser": loser_id},
    )
    # The tombstone must look CHANGED to a ?updated_since= poll or the consumer
    # never learns where the title went -- it simply stops appearing.
    loser.updated_at = now

    # Recompute the survivor from ALL of its sources now that it has more of them.
    # This is what makes the merged row order-independent: the same end state
    # whichever platform synced first.
    #
    # source_id=None on purpose. _resolve_title stamps it onto any TitleChange it
    # writes, and a merge is not attributable to one source -- the change came from
    # the union. Passing `survivor_id` here (they are both ints, so nothing
    # complains) wrote title ids into title_change.source_id and produced four
    # foreign-key violations against `source` on the real catalogue.
    session.flush()
    _resolve_title(session, survivor, None)
    survivor.updated_at = now

    # A pair may already be here as a PROPOSED row from a dry run; promote it
    # rather than inserting a second row and tripping uq_title_merge_pair.
    audit = session.scalars(
        select(TitleMerge).where(
            TitleMerge.loser_id == loser_id, TitleMerge.survivor_id == survivor_id
        )
    ).first()
    if audit is None:
        audit = TitleMerge(loser_id=loser_id, survivor_id=survivor_id)
        session.add(audit)
    audit.state = MergeState.APPLIED
    audit.key_kind = key_kind or audit.key_kind
    audit.reason = reason or audit.reason
    audit.report_json = json.dumps(report, default=str)
    audit.merged_by = merged_by
    audit.decided_at = now
    audit.reverted_at = None

    log.info("merged title %s into %s (%s)", loser_id, survivor_id, reason or "no reason given")
    report["reason"] = reason
    return report


def unmerge(session: Session, loser_id: int) -> dict:
    """Undo a merge, so a wrong auto-merge does not need a database restore.

    Replays the audit row's captured links back onto the loser and clears the
    tombstone. Honest about its limits: child rows other than `title_source` stay
    with the survivor. That is enough, because the returned link is what
    `ingest_record` keys on -- the next sync of that source finds the loser again
    and repopulates its episodes, tags and cast from the archive.

    Without the link handed back, clearing the tombstone alone would leave a title
    that no sync can ever reach.
    """
    loser = session.get(Title, loser_id)
    if loser is None:
        raise MergeError(f"no such title: {loser_id}")
    if loser.merged_into_id is None:
        raise MergeError(f"title {loser_id} is not merged")

    survivor_id = loser.merged_into_id
    audit = session.scalars(
        select(TitleMerge)
        .where(TitleMerge.loser_id == loser_id, TitleMerge.state == MergeState.APPLIED)
        .order_by(TitleMerge.id.desc())
    ).first()

    returned = 0
    if audit is not None:
        try:
            links = json.loads(audit.report_json or "{}").get("links", [])
        except json.JSONDecodeError:
            links = []
        for link in links:
            returned += session.execute(
                text("UPDATE title_source SET title_id = :loser WHERE id = :id"),
                {"loser": loser_id, "id": link.get("title_source_id")},
            ).rowcount or 0
        audit.reverted_at = datetime.now(UTC)
        # REJECTED, not REVERTED, because the state has to act as negative memory.
        # Classification is a pure function of the stored keys, so tonight's dedup
        # run would reach exactly the same verdict and merge the pair straight back
        # -- an operator's undo has to be permanent, or it is theatre.
        audit.state = MergeState.REJECTED

    loser.merged_into_id = None
    loser.merged_at = None
    loser.updated_at = datetime.now(UTC)

    survivor = session.get(Title, survivor_id)
    if survivor is not None and returned:
        # The survivor just lost sources, so its canonical fields must be
        # recomputed from what is left.
        session.flush()
        _resolve_title(session, survivor, None)  # not attributable to one source
        survivor.updated_at = datetime.now(UTC)

    return {
        "title": loser_id,
        "was_merged_into": survivor_id,
        "links_returned": returned,
        "audit_found": audit is not None,
    }
