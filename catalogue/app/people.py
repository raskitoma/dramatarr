"""Merging duplicate `person` rows.

Deliberately separate from `app/merge.py`, which merges titles: a person is a
different identity problem and must not be folded in as a side effect. What they
share is the discipline -- an exact key, never name similarity. In a catalogue
where several actors genuinely share a name, matching on names would fuse real
people, and unlike a bad title merge there is no watch link to notice it by.

The duplicates this exists to clean up came from one character. ReelShort has no
numeric person id, so the fandom URL is the key, and it arrived by two routes
that disagreed about a trailing slash: the roster stripped it, the detail-page
cast did not. 118 of 2,306 people were the same actor twice -- the roster row
carrying the bio, the cast row carrying the photo, so a cast list showed one name
twice, each half-populated. (502 rows shared a normalised NAME, which is a
different and much looser question: it sweeps in actors who genuinely share one,
and the same person on two platforms. Merging on it would fuse real people.)

`app/sources/reelshort.person_key` stops it recurring; this repairs what exists.

A merge leaves a tombstone rather than deleting, so an id a consumer already
stored keeps resolving -- see `resolve_person`.
"""

from __future__ import annotations

import logging

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Person, PersonSource

log = logging.getLogger(__name__)


class PersonMergeError(RuntimeError):
    pass


def duplicate_groups(session: Session) -> list[list[int]]:
    """People that are the same person, keyed on an EXACT normalised source id.

    Two rows group only when a source published the same identifier for both --
    the same fandom URL modulo its trailing slash. Names are never compared, so
    two different actors called the same thing stay two people.
    """
    # Tombstones are excluded: a merged row keeps its id but owns nothing, and
    # letting one back into a group would propose merging a person into the
    # pointer that already forwards to them.
    rows = session.execute(
        select(PersonSource.person_id, PersonSource.source_id, PersonSource.external_id)
        .join(Person, Person.id == PersonSource.person_id)
        .where(Person.merged_into_id.is_(None))
    ).all()
    return group_links(rows)


def group_links(rows: Iterable[tuple[int, int, str | None]]) -> list[list[int]]:
    """The grouping itself, over raw `(person_id, source_id, external_id)` rows.

    Split out from `duplicate_groups` so the same code can run against a backup,
    which is how `restore_tombstones` recovers which survivor absorbed an id that
    no longer exists. A second implementation would be a guaranteed drift, and a
    drift there silently points a restored tombstone at the wrong person -- the
    identical trap the dedup migration hit by reimplementing `match_key`.

    Groups are returned sorted, so the survivor is always the lowest id: the
    original merge picked it that way and a restore has to agree.
    """
    by_key: dict[tuple[int, str], set[int]] = {}
    for person_id, source_id, external_id in rows:
        key = (source_id, (external_id or "").strip().rstrip("/"))
        if not key[1]:
            continue
        by_key.setdefault(key, set()).add(person_id)

    # Union people that share any key, so a three-way duplicate collapses once.
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for people in by_key.values():
        ids = sorted(people)
        for other in ids[1:]:
            union(ids[0], other)

    groups: dict[int, set[int]] = {}
    for person_id in list(parent):
        groups.setdefault(find(person_id), set()).add(person_id)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def resolve_person(session: Session, person_id: int) -> int:
    """Follow a tombstone chain to the live person.

    Merging into an already-merged row is legal and happens whenever a third
    duplicate turns up after the first pair was folded together, so a single hop
    would strand the consumer one short of the survivor.
    """
    seen: set[int] = set()
    current = person_id
    while True:
        if current in seen:
            raise PersonMergeError(f"merge cycle through person {current}")
        seen.add(current)
        nxt = session.execute(
            text("SELECT merged_into_id FROM person WHERE id = :p"), {"p": current}
        ).scalar()
        if nxt is None:
            return current
        current = nxt


def merge_people(session: Session, survivor_id: int, loser_id: int) -> dict:
    """Fold one person into another. Caller owns the transaction."""
    if survivor_id == loser_id:
        raise PersonMergeError("cannot merge a person into themselves")
    survivor = session.get(Person, survivor_id)
    loser = session.get(Person, loser_id)
    if survivor is None or loser is None:
        raise PersonMergeError(f"missing person: {survivor_id} / {loser_id}")
    # Absorbing into a tombstone would build a chain that resolves correctly but
    # points readers at a row holding nothing, and re-absorbing one that already
    # merged would overwrite a good pointer with a worse one.
    if survivor.merged_into_id is not None:
        raise PersonMergeError(
            f"person {survivor_id} is a tombstone for {survivor.merged_into_id}"
        )
    if loser.merged_into_id is not None:
        raise PersonMergeError(f"person {loser_id} is already merged")

    report: dict[str, object] = {"survivor": survivor_id, "loser": loser_id}

    # The halves are complementary by construction: the roster row has the bio,
    # the cast row has the photo. Take whatever the survivor is missing.
    if not (survivor.bio or "").strip() and (loser.bio or "").strip():
        survivor.bio = loser.bio
        report["bio_taken"] = True
    if not (survivor.name or "").strip() and loser.name:
        survivor.name = loser.name

    # uq_person_source_external is (source_id, external_id), and after key
    # normalisation the two rows collide by definition -- that IS the duplicate.
    # Keep whichever row carries more: a stored image first, then a URL to fetch.
    report["links_dropped"] = session.execute(
        text(
            "DELETE FROM person_source WHERE id IN ("
            "  SELECT dupe.id FROM person_source dupe"
            "  WHERE dupe.person_id = :loser AND EXISTS ("
            "    SELECT 1 FROM person_source keep"
            "    WHERE keep.person_id = :survivor"
            "      AND keep.source_id = dupe.source_id"
            "      AND rtrim(keep.external_id, '/') = rtrim(dupe.external_id, '/')"
            "      AND (keep.avatar_image_id IS NOT NULL"
            "           OR dupe.avatar_image_id IS NULL)"
            "  )"
            ")"
        ),
        {"survivor": survivor_id, "loser": loser_id},
    ).rowcount or 0

    # Anything the survivor did NOT already have moves across, including the
    # better-populated half of a colliding pair.
    session.execute(
        text(
            "DELETE FROM person_source WHERE id IN ("
            "  SELECT keep.id FROM person_source keep"
            "  WHERE keep.person_id = :survivor AND EXISTS ("
            "    SELECT 1 FROM person_source dupe"
            "    WHERE dupe.person_id = :loser"
            "      AND dupe.source_id = keep.source_id"
            "      AND rtrim(keep.external_id, '/') = rtrim(dupe.external_id, '/')"
            "  )"
            ")"
        ),
        {"survivor": survivor_id, "loser": loser_id},
    )
    report["links_moved"] = session.execute(
        text("UPDATE person_source SET person_id = :survivor WHERE person_id = :loser"),
        {"survivor": survivor_id, "loser": loser_id},
    ).rowcount or 0

    # billing_order sits outside uq_title_person, so a plain conflict-drop would
    # silently discard the better billing.
    session.execute(
        text(
            "UPDATE title_person SET billing_order = ("
            "  SELECT MIN(dupe.billing_order) FROM title_person dupe"
            "  WHERE dupe.person_id = :loser AND dupe.title_id = title_person.title_id"
            "    AND dupe.billing_order IS NOT NULL"
            ") WHERE person_id = :survivor AND ("
            "  billing_order IS NULL OR billing_order > ("
            "    SELECT MIN(dupe.billing_order) FROM title_person dupe"
            "    WHERE dupe.person_id = :loser AND dupe.title_id = title_person.title_id"
            "      AND dupe.billing_order IS NOT NULL))"
        ),
        {"survivor": survivor_id, "loser": loser_id},
    )
    report["credits_dropped"] = session.execute(
        text(
            "DELETE FROM title_person WHERE id IN ("
            "  SELECT dupe.id FROM title_person dupe"
            "  WHERE dupe.person_id = :loser AND EXISTS ("
            "    SELECT 1 FROM title_person keep"
            "    WHERE keep.person_id = :survivor"
            "      AND keep.title_id = dupe.title_id"
            "      AND (keep.role = dupe.role"
            "           OR (keep.role IS NULL AND dupe.role IS NULL))"
            "      AND (keep.source_id = dupe.source_id"
            "           OR (keep.source_id IS NULL AND dupe.source_id IS NULL))"
            "  )"
            ")"
        ),
        {"survivor": survivor_id, "loser": loser_id},
    ).rowcount or 0
    report["credits_moved"] = session.execute(
        text("UPDATE title_person SET person_id = :survivor WHERE person_id = :loser"),
        {"survivor": survivor_id, "loser": loser_id},
    ).rowcount or 0

    # Tombstone, never delete. Nothing in the schema points at `person` beyond
    # the two tables just repointed, which is what made deleting look free -- but
    # a consumer holds person ids too, and deleting turned 118 of them into 404s
    # with nothing to follow. The row is kept whole rather than blanked: its bio
    # may differ from the survivor's, and a tombstone is the only remaining copy.
    # That makes filtering `merged_into_id IS NULL` mandatory in every listing
    # and count, which `resolve_person` and the API dependency handle on reads.
    loser.merged_into_id = survivor_id
    loser.merged_at = datetime.now(UTC)
    report["tombstoned"] = loser_id
    return report


def merge_duplicate_people(session: Session, *, dry_run: bool = True) -> dict:
    """Find and (optionally) merge every duplicate person."""
    groups = duplicate_groups(session)
    stats = {
        "groups": len(groups),
        "people_absorbed": sum(len(g) - 1 for g in groups),
        "merged": 0,
        "failed": 0,
        "bios_gained": 0,
        "dry_run": dry_run,
    }
    if dry_run:
        stats["sample"] = [g[:4] for g in groups[:10]]
        return stats

    for group in groups:
        survivor, *losers = group
        for loser in losers:
            try:
                report = merge_people(session, survivor, loser)
                session.commit()
                stats["merged"] += 1
                if report.get("bio_taken"):
                    stats["bios_gained"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad group is not fatal
                session.rollback()
                stats["failed"] += 1
                log.warning("person merge %s -> %s failed: %s", loser, survivor, exc)
    return stats


def restore_tombstones(
    session: Session, backup_path: str, *, dry_run: bool = True
) -> dict:
    """Re-create person rows an earlier merge deleted, as tombstones.

    Before merges left tombstones they deleted the absorbed row outright, so the
    ids simply vanished and a consumer holding one got a 404 with nothing to
    follow. The mapping is not lost, though: `group_links` is deterministic and
    the survivor is always a group's lowest id, so replaying it over a pre-merge
    backup reproduces exactly which id was folded into which.

    Only ids that are missing from the live database are restored, which makes
    this re-runnable and safe against a backup that predates other work. Every
    survivor is resolved through the live chain first, so a person who has since
    been merged again lands on the row that actually exists.
    """
    import sqlite3

    connection = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        links = connection.execute(
            "SELECT person_id, source_id, external_id FROM person_source"
        ).fetchall()
        names = {
            row[0]: (row[1], row[2], row[3])
            for row in connection.execute(
                "SELECT id, name, normalized_name, bio FROM person"
            )
        }
    finally:
        connection.close()

    live = {
        row[0]
        for row in session.execute(text("SELECT id FROM person")).all()
    }
    stats: dict[str, object] = {
        "groups": 0,
        "already_present": 0,
        "restored": 0,
        "unknown_survivor": 0,
        "missing_name": 0,
        "dry_run": dry_run,
    }
    restored: list[tuple[int, int]] = []

    for group in group_links(links):
        stats["groups"] += 1
        survivor, *losers = group
        for loser in losers:
            if loser in live:
                stats["already_present"] += 1
                continue
            if survivor not in live:
                # The backup's survivor was itself removed by something other
                # than this merge. Restoring a pointer to a row that does not
                # exist would trade a 404 for a broken chain, so skip and report.
                stats["unknown_survivor"] += 1
                continue
            if loser not in names:
                stats["missing_name"] += 1
                continue
            restored.append((loser, resolve_person(session, survivor)))

    stats["restored"] = len(restored)
    if dry_run:
        stats["sample"] = restored[:10]
        return stats

    now = datetime.now(UTC)
    for loser, survivor in restored:
        name, normalized, bio = names[loser]
        session.execute(
            text(
                "INSERT INTO person (id, name, normalized_name, bio,"
                "                    merged_into_id, merged_at)"
                " VALUES (:id, :name, :normalized, :bio, :survivor, :merged_at)"
            ),
            {
                "id": loser,
                "name": name,
                "normalized": normalized,
                # Restored whole, the same as a tombstone a live merge writes.
                # One rule for tombstones -- keep the row, filter it everywhere --
                # is easier to hold than two, and every listing and count now does.
                "bio": bio,
                "survivor": survivor,
                "merged_at": now,
            },
        )
    # Explicit ids do not advance Postgres's identity sequence, so a restored id
    # above the sequence would collide with the next natural insert. GREATEST
    # keeps this one-directional: plain setval(MAX(id)) would move the sequence
    # BACKWARDS whenever the highest rows were deleted rather than restored, and
    # hand out ids all over again. SQLite derives the next rowid itself.
    if restored and session.bind.dialect.name == "postgresql":
        session.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('person', 'id'),"
                "              GREATEST("
                "                (SELECT MAX(id) FROM person),"
                "                (SELECT last_value FROM person_id_seq)))"
            )
        )
    session.commit()
    return stats
