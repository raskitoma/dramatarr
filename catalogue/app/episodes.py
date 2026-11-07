"""One-off repair of ReelShort episode numbering.

Two parser bugs put wrong rows in the database, and neither could be fixed by
re-ingesting, because `_sync_episodes` never updated `number` on an existing row
-- the corrected value was computed and then thrown away.

  1. A trailer was ingested as an episode. `online_base` mixes real episodes
     (chapter_type 1, 1-based serial_number) with one preview (chapter_type 2,
     is_preview 1, serial_number 0). 755 of 3,558 archived titles carry one, and
     the payload's own `total` counts it -- so `episode_count` claimed one
     episode more than exists.
  2. Every episode was numbered one too high, because the parser added 1 to a
     serial_number that was already 1-based.

Together these told a viewer who had finished a show that its last episode was
still missing. A downstream consumer noticed: twelve of nineteen series each
short exactly one episode, never a middle one, and every one of them
ReelShort-only.

The preview is identifiable in the database without re-reading the archive: the
old off-by-one stored it as `number = 1`, while real episodes started at 2. So a
ReelShort title has a preview row IFF it holds an episode numbered 1 -- verified
against the archive, 755 rows across 754 titles (one merged title holds two
ReelShort links, hence two previews).

Order matters. The previews must be deleted BEFORE renumbering, or the preview
at 1 and the real first episode at 2 both become 1 and collide.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Source, Title, TitleSource

log = logging.getLogger(__name__)

SOURCE_KEY = "reelshort"


def repair_reelshort_numbering(session: Session, *, dry_run: bool = True) -> dict:
    """Delete preview rows, renumber the rest, and correct `episode_count`."""
    from app.ingest import _resolve_title

    source_id = session.scalar(
        text("SELECT id FROM source WHERE key = :k"), {"k": SOURCE_KEY}
    )
    stats: dict[str, object] = {"dry_run": dry_run, "source_id": source_id}
    if source_id is None:
        stats["note"] = "reelshort is not a registered source"
        return stats

    # HARD GUARD. This function is NOT idempotent and must never run twice.
    # Before it runs, a preview sits at number 1 and real episodes start at 2;
    # after it, REAL first episodes sit at number 1. A second run would read
    # those as previews and delete the opening episode of every ReelShort title.
    # The tell is unambiguous: beforehand 2,799 titles start at 2, afterwards
    # none do, so "no title starts at 2" means the work is already done.
    starts_at_two = session.scalar(
        text(
            "SELECT count(*) FROM (SELECT title_id FROM episode"
            "  WHERE source_id = :s GROUP BY title_id HAVING min(number) = 2) x"
        ),
        {"s": source_id},
    )
    if not starts_at_two:
        stats["note"] = (
            "already repaired: no ReelShort title starts at episode 2, so any row "
            "at number 1 is a real episode. Refusing to run."
        )
        return stats

    preview_titles = [
        row[0]
        for row in session.execute(
            text(
                "SELECT DISTINCT title_id FROM episode"
                " WHERE source_id = :s AND number = 1"
            ),
            {"s": source_id},
        ).all()
    ]
    stats["preview_rows"] = session.scalar(
        text("SELECT count(*) FROM episode WHERE source_id = :s AND number = 1"),
        {"s": source_id},
    )
    stats["preview_titles"] = len(preview_titles)
    stats["episodes_total"] = session.scalar(
        text("SELECT count(*) FROM episode WHERE source_id = :s"), {"s": source_id}
    )

    if dry_run:
        stats["sample_titles"] = preview_titles[:10]
        return stats

    # 1. The trailers. Deleted before renumbering, or they would collide with
    #    the real first episode.
    stats["deleted"] = session.execute(
        text("DELETE FROM episode WHERE source_id = :s AND number = 1"),
        {"s": source_id},
    ).rowcount

    # 2. Every remaining ReelShort episode is one too high, on every title --
    #    not only the ones that carried a preview.
    stats["renumbered"] = session.execute(
        text("UPDATE episode SET number = number - 1 WHERE source_id = :s"),
        {"s": source_id},
    ).rowcount

    # 3. `episode_count` came from the payload's `total`, which counted the
    #    preview. Only titles that HAD one are wrong. The stored per-source value
    #    is corrected and the canonical Title recomputed through the ordinary
    #    precedence path, so a title also carried by another platform resolves by
    #    the same rules as any sync rather than by arithmetic invented here.
    stats["counts_note"] = (
        "run resync_episode_counts to correct episode_count -- a merged title "
        "carries several ReelShort links and each needs its own value"
    )
    session.commit()
    return stats


def resync_episode_counts(session: Session, archive, module) -> dict:
    """Recompute every ReelShort link's `episode_count` from its own payload.

    Deliberately NOT arithmetic. The first version of this decremented one link
    per title, which is wrong twice over: 187 titles carry more than one
    ReelShort link -- a merged `（DUBBED）` twin contributes its own -- and
    `session.scalar` returned only the first, so the other kept its inflated
    count and precedence went on picking it. Subtracting one also assumes a
    preview existed, which is only true for 755 of 3,558.

    Re-parsing each link's archived detail answers exactly instead of
    approximately, costs no requests, and is idempotent: a link whose value is
    already right is left alone.
    """
    from app.ingest import _resolve_title

    source_id = session.scalar(
        text("SELECT id FROM source WHERE key = :k"), {"k": SOURCE_KEY}
    )
    stats = {"links": 0, "no_archive": 0, "unchanged": 0,
             "corrected": 0, "locale_keys": 0, "titles_resolved": 0, "failed": 0}
    if source_id is None:
        return stats

    links = session.execute(
        text(
            "SELECT id, title_id, url, external_id FROM title_source"
            " WHERE source_id = :s ORDER BY id"
        ),
        {"s": source_id},
    ).all()
    touched: set[int] = set()

    for link_id, title_id, url, external_id in links:
        stats["links"] += 1
        doc = None
        for candidate in (url, f"{module.BASE_URL}/movie/{external_id}"):
            if candidate:
                doc = archive.get(candidate) or archive.get(candidate.rstrip("/"))
                if doc is not None:
                    break
        if doc is None:
            stats["no_archive"] += 1
            continue
        try:
            record = module.parse_detail(doc)
        except Exception as exc:  # noqa: BLE001 - one bad payload is not fatal
            log.warning("reparse of %s failed: %s", url or external_id, exc)
            stats["failed"] += 1
            continue
        if record.episode_count is None:
            stats["unchanged"] += 1
            continue

        row = session.get(TitleSource, link_id)

        # Backfill the cross-locale key onto rows that predate it. This MUST run
        # before any Spanish or Portuguese crawl: `_find_locale_sibling` matches
        # an incoming edition against these keys, so with them all NULL every
        # localised title would land as a brand new one and the catalogue would
        # gain thousands of duplicates instead of alternate titles.
        if record.locale_key and row.locale_key != record.locale_key:
            row.locale_key = record.locale_key
            stats["locale_keys"] = stats.get("locale_keys", 0) + 1
        if record.lang and row.lang != record.lang:
            row.lang = record.lang

        try:
            values = json.loads(row.parsed_json or "{}")
        except json.JSONDecodeError:
            values = {}
        if values.get("episode_count") == record.episode_count:
            stats["unchanged"] += 1
            continue
        values["episode_count"] = record.episode_count
        row.parsed_json = json.dumps(values)
        stats["corrected"] += 1
        touched.add(title_id)

    for title_id in touched:
        title = session.get(Title, title_id)
        if title is not None and _resolve_title(session, title, source_id):
            stats["titles_resolved"] += 1

    session.commit()
    return stats


def tag_episode_editions(
    session: Session,
    archive=None,
    modules: dict | None = None,
    *,
    dry_run: bool = True,
) -> dict:
    """Point every episode at the `title_source` link that published it.

    Two phases, because they have very different certainty:

    1. **Arithmetic that cannot be wrong.** Where a (title, source) pair holds
       exactly ONE link, that link published every episode row under it. 588,699
       of 680,725 rows (86.5%) resolve this way, with no archive and no adapter.
    2. **The archive, for the rest.** 1,080 pairs hold more than one link -- 501
       DramaBox, 391 GoodShort, 188 ReelShort -- covering 92,026 rows. Only the
       payload each link was parsed from says which episode ids it published, so
       this re-parses the archived detail and assigns by external_id. Exact, and
       free of network cost.

    Anything phase 2 cannot reach stays NULL. That is deliberate: an unattributed
    episode is still served, and is simply never matched by a `?lang=` filter.
    Guessing -- splitting rows evenly, or handing them all to the oldest link --
    would put a Spanish episode under an English edition, which is worse than
    admitting the edition is unknown.

    Idempotent: every write is guarded on `title_source_id IS NULL`, so a second
    run assigns only what the first could not.
    """
    stats: dict[str, object] = {"dry_run": dry_run}

    stats["unassigned_before"] = session.scalar(
        text("SELECT count(*) FROM episode WHERE title_source_id IS NULL")
    )

    # --- phase 1: the unambiguous majority ---------------------------------
    # Written as correlated subqueries rather than UPDATE..FROM so it runs
    # identically on Postgres and on the SQLite used by tests and backups.
    # `ix_title_source_title` is what keeps this from being 680,725 sequential
    # scans; before that index existed this phase did not finish.
    solo = text(
        "SELECT count(*) FROM episode e WHERE e.title_source_id IS NULL"
        " AND (SELECT count(*) FROM title_source ts"
        "      WHERE ts.title_id = e.title_id AND ts.source_id = e.source_id) = 1"
    )
    stats["phase1_eligible"] = session.scalar(solo)

    if not dry_run:
        stats["phase1_assigned"] = session.execute(
            text(
                "UPDATE episode SET title_source_id = ("
                "  SELECT ts.id FROM title_source ts"
                "   WHERE ts.title_id = episode.title_id"
                "     AND ts.source_id = episode.source_id)"
                " WHERE title_source_id IS NULL"
                "   AND (SELECT count(*) FROM title_source ts2"
                "         WHERE ts2.title_id = episode.title_id"
                "           AND ts2.source_id = episode.source_id) = 1"
            )
        ).rowcount
        session.commit()

    # --- phase 2: the contested pairs, settled by the archive ---------------
    groups = session.execute(
        text(
            "SELECT e.title_id, e.source_id, s.key, count(*) AS rows"
            "  FROM episode e JOIN source s ON s.id = e.source_id"
            " WHERE e.title_source_id IS NULL"
            " GROUP BY e.title_id, e.source_id, s.key"
        )
    ).all()
    stats["phase2_groups"] = len(groups)
    stats["phase2_rows"] = sum(g.rows for g in groups)

    assigned = no_archive = failed = no_module = contested = 0
    per_source_missing: dict[str, int] = {}
    indexes: dict[str, dict[str, str]] = {}
    if archive is not None and modules and not dry_run:
        for title_id, source_id, source_key, _rows in groups:
            module = modules.get(source_key)
            if module is None:
                no_module += 1
                continue
            if source_key not in indexes:
                indexes[source_key] = build_archive_index(archive, module)
            index = indexes[source_key]
            links = session.execute(
                text(
                    "SELECT id, url, external_id FROM title_source"
                    " WHERE title_id = :t AND source_id = :s ORDER BY id"
                ),
                {"t": title_id, "s": source_id},
            ).all()
            # Read every link's payload FIRST, so an id published by more than
            # one edition can be recognised as evidence-free before anything is
            # written. Assigning in link order instead let the lowest link id
            # win a contested id, which is ordering, not evidence -- and
            # `app/merge.py` decouples id order from edition order, so the
            # lowest id is not even reliably the original edition.
            per_link: list[tuple[int, list[str]]] = []
            for link_id, url, external_id in links:
                doc = None
                for candidate in (url, index.get(str(external_id))):
                    if not candidate:
                        continue
                    doc = archive.get(candidate) or archive.get(candidate.rstrip("/"))
                    if doc is not None:
                        break
                if doc is None:
                    no_archive += 1
                    per_source_missing[source_key] = per_source_missing.get(source_key, 0) + 1
                    continue
                try:
                    record = module.parse_detail(doc)
                except Exception as exc:  # noqa: BLE001 - one bad payload is not fatal
                    log.warning("reparse of %s failed: %s", url or external_id, exc)
                    failed += 1
                    continue
                per_link.append(
                    (link_id, [e.external_id for e in record.episodes if e.external_id])
                )

            counts: dict[str, int] = {}
            for _link_id, ids in per_link:
                for value in set(ids):
                    counts[value] = counts.get(value, 0) + 1
            contested += sum(1 for value in counts if counts[value] > 1)

            for link_id, ids in per_link:
                # Only ids exactly one edition claims. A shared id is evidence
                # of nothing, and the unique constraint has already collapsed
                # such episodes into a single row, so no pointer would be right.
                ids = [value for value in ids if counts.get(value) == 1]
                if not ids:
                    continue
                # Chunked: a link can publish hundreds of episodes and some
                # drivers cap bound parameters well below that.
                for start in range(0, len(ids), 200):
                    chunk = ids[start : start + 200]
                    params = {f"e{i}": v for i, v in enumerate(chunk)}
                    placeholders = ", ".join(f":e{i}" for i in range(len(chunk)))
                    assigned += session.execute(
                        text(
                            "UPDATE episode SET title_source_id = :link"
                            " WHERE title_id = :t AND source_id = :s"
                            "   AND title_source_id IS NULL"
                            f"   AND external_id IN ({placeholders})"
                        ),
                        {"link": link_id, "t": title_id, "s": source_id, **params},
                    ).rowcount
        session.commit()

    stats["phase2_assigned"] = assigned
    stats["phase2_no_archive"] = no_archive
    # Per source, because an aggregate hid that DramaBox resolved NOTHING while
    # the total still looked like progress.
    stats["phase2_no_archive_by_source"] = per_source_missing
    # An episode id published by two editions is not evidence of either, so it
    # is left NULL and counted rather than awarded to whichever link sorts first.
    stats["phase2_contested_ids"] = contested
    stats["phase2_failed"] = failed
    stats["phase2_no_module"] = no_module
    stats["unassigned_after"] = session.scalar(
        text("SELECT count(*) FROM episode WHERE title_source_id IS NULL")
    )
    return stats


def build_archive_index(archive, module) -> dict[str, str]:
    """external_id -> the archived URL holding that link's detail payload.

    Built by scanning the archive once, because the key CANNOT be reconstructed
    from what we store. DramaBox archives under
    `/_next/data/<buildId>/drama/<id>/<slug>.json` while `title_source.url`
    holds the pretty page, and the buildId rotates -- so the two strings can
    never coincide and no amount of guessing recovers it. The previous helper
    guessed, appending ReelShort's `/movie/{id}` for every adapter, which meant
    it resolved 0 of 60 DramaBox links against 55 of 60 ReelShort ones. DramaBox
    is 69% of the contested rows, so the backfill silently did almost nothing.

    Two extraction routes, because the four adapters key differently:
      * `external_id_from_url` where the adapter publishes one (goodshort,
        netshort -- netshort's id is a numeric tail inside a slug segment, so
        segment matching alone would miss it);
      * otherwise every path segment, which covers DramaBox's numeric id and
        ReelShort's 24-character hex book_id without special-casing either.
    """
    index: dict[str, str] = {}
    is_detail = getattr(module, "is_detail_url", None)
    extract = getattr(module, "external_id_from_url", None)
    for url in archive.iter_urls():
        if callable(is_detail) and not is_detail(url):
            continue
        keys: list[str] = []
        if callable(extract):
            try:
                found = extract(url)
            except Exception:  # noqa: BLE001 - a parser that dislikes this URL
                found = None
            if found:
                keys.append(str(found))
        path = url.split("?")[0].split("#")[0]
        for segment in path.split("/"):
            segment = segment.removesuffix(".json")
            if segment:
                keys.append(segment)
        for key in keys:
            index.setdefault(key, url)
    return index
