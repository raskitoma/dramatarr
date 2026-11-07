"""The `dedup` job: find duplicate titles, merge the confident ones, queue the rest.

Dry run is the default. A merge rewrites eleven tables and removes ids a deployed
consumer already stores, so the job that does it has to be inspectable before it
runs -- `--apply` is a deliberate act, not an omission.

Ordering matters and is not obvious. Candidates are unioned into clusters BEFORE
any merge happens, because three keys can each find a different pair of the same
cluster: "Divorced at the Wedding Day" is four DramaBox rows, and merging them
pairwise in discovery order would try to fold a row into a title that was itself
already folded away. Everything in a cluster merges into that cluster's single
lowest id.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.dedup import Candidate, Verdict, group_candidates
from app.merge import merge_titles
from app.models import MergeState, Title, TitleMerge

log = logging.getLogger(__name__)

#: Job identity for the scheduler and the once-daily partial unique index.
JOB_TYPE = "dedup"


def load_candidates(session: Session) -> list[Candidate]:
    """Every live title, reduced to the fields identity resolution reads.

    Tombstones are excluded: a merged row must never be a candidate again, or the
    job would keep proposing merges for rows it already absorbed.
    """
    rows = session.execute(
        text(
            """
            SELECT t.id, t.primary_title, t.year, t.episode_count,
                   (SELECT string_agg(DISTINCT s.key, ',')
                      FROM title_source ts JOIN source s ON s.id = ts.source_id
                     WHERE ts.title_id = t.id) AS srcs,
                   (SELECT i.sha256
                      FROM title_image ti JOIN image i ON i.id = ti.image_id
                     WHERE ti.title_id = t.id AND ti.kind = 'poster'
                     ORDER BY ti.id LIMIT 1) AS poster
              FROM title t
             WHERE t.merged_into_id IS NULL
            """
            if session.bind.dialect.name == "postgresql"
            else """
            SELECT t.id, t.primary_title, t.year, t.episode_count,
                   (SELECT group_concat(DISTINCT s.key)
                      FROM title_source ts JOIN source s ON s.id = ts.source_id
                     WHERE ts.title_id = t.id) AS srcs,
                   (SELECT i.sha256
                      FROM title_image ti JOIN image i ON i.id = ti.image_id
                     WHERE ti.title_id = t.id AND ti.kind = 'poster'
                     ORDER BY ti.id LIMIT 1) AS poster
              FROM title t
             WHERE t.merged_into_id IS NULL
            """
        )
    ).all()
    return [
        Candidate(
            title_id=row[0],
            primary_title=row[1] or "",
            year=row[2],
            episode_count=row[3],
            source_keys=frozenset((row[4] or "").split(",")) - {""},
            poster_sha256=row[5],
        )
        for row in rows
    ]


def _cluster(groups) -> dict[int, dict]:
    """Union overlapping groups so each title belongs to exactly one cluster.

    Without this, a cluster found by two different keys merges pairwise in
    discovery order and the second merge targets an already-absorbed row.
    """
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

    meta: dict[int, dict] = {}
    for group in groups:
        ids = [c.title_id for c in group.members]
        for other in ids[1:]:
            union(ids[0], other)
        for tid in ids:
            meta[tid] = {
                "key_kind": group.key_kind,
                "verdict": group.verdict,
                "reason": group.reason,
                "evidence": group.evidence,
            }

    clusters: dict[int, dict] = {}
    for tid in list(parent):
        root = find(tid)
        entry = clusters.setdefault(
            root, {"members": set(), "verdicts": set(), "key_kinds": set(),
                   "reasons": [], "evidence": {}}
        )
        entry["members"].add(tid)
        info = meta.get(tid)
        if info:
            entry["verdicts"].add(info["verdict"])
            entry["key_kinds"].add(info["key_kind"])
            if info["reason"] not in entry["reasons"]:
                entry["reasons"].append(info["reason"])
            entry["evidence"] = entry["evidence"] or info["evidence"]
    return clusters


def _queue(session: Session, survivor: int, loser: int, cluster: dict) -> bool:
    """Record a pair an operator has to judge. Idempotent across runs.

    A pair an operator already REJECTED stays rejected -- re-proposing it every
    night would make the queue unusable, which is the failure mode that makes
    people stop reading queues.
    """
    existing = session.scalars(
        select(TitleMerge).where(
            TitleMerge.loser_id == loser, TitleMerge.survivor_id == survivor
        )
    ).first()
    if existing is not None:
        return False
    session.add(
        TitleMerge(
            loser_id=loser,
            survivor_id=survivor,
            state=MergeState.PROPOSED,
            key_kind=",".join(sorted(k for k in cluster["key_kinds"] if k)),
            reason="; ".join(cluster["reasons"]),
            evidence_json=json.dumps(cluster["evidence"], default=str),
        )
    )
    return True


def run_dedup(
    session: Session,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    progress=None,
) -> dict:
    """Find and (optionally) apply title merges.

    Returns a report suitable for printing or storing in `sync_run.stats_json`.
    """
    candidates = load_candidates(session)
    groups = group_candidates(candidates)
    clusters = _cluster(groups)

    stats = {
        "titles_scanned": len(candidates),
        "groups_found": len(groups),
        "clusters": len(clusters),
        "auto": 0,
        "queued": 0,
        "merged": 0,
        "absorbed": 0,
        "failed": 0,
        "dry_run": dry_run,
    }
    plan: list[dict] = []
    by_id = {c.title_id: c for c in candidates}

    ordered = sorted(clusters.items())
    if limit:
        ordered = ordered[:limit]

    for index, (survivor, cluster) in enumerate(ordered):
        members = sorted(cluster["members"])
        losers = [m for m in members if m != survivor]
        # A cluster auto-merges only if EVERY group that formed it auto-merged.
        # One disagreeing pair taints the cluster: the rows are transitively
        # linked, so merging the confident pairs would still fold in the doubtful
        # row by transitivity.
        auto = cluster["verdicts"] == {Verdict.AUTO}
        entry = {
            "survivor": survivor,
            "survivor_title": by_id[survivor].primary_title if survivor in by_id else None,
            "losers": losers,
            "loser_titles": [by_id[m].primary_title for m in losers if m in by_id],
            "key_kinds": sorted(k for k in cluster["key_kinds"] if k),
            "verdict": "auto" if auto else "queue",
            "reason": "; ".join(cluster["reasons"]),
            "evidence": cluster["evidence"],
        }
        plan.append(entry)

        if auto:
            stats["auto"] += 1
        else:
            stats["queued"] += 1

        if dry_run:
            continue

        for loser in losers:
            if auto:
                # Commit per PAIR, not per cluster. A cluster can have several
                # losers, and a rollback undoes everything uncommitted in the
                # session -- so one failing pair would silently discard the
                # successful merges beside it while `stats` still counted them,
                # reporting merges that are not in the database.
                try:
                    merge_titles(
                        session,
                        survivor,
                        loser,
                        reason=entry["reason"],
                        key_kind=",".join(entry["key_kinds"]),
                        merged_by="dedup",
                    )
                    session.commit()
                    stats["merged"] += 1
                    stats["absorbed"] += 1
                except Exception as exc:  # one bad pair must not kill the run
                    session.rollback()
                    stats["failed"] += 1
                    log.warning("merge %s -> %s failed: %s", loser, survivor, exc)
            else:
                try:
                    _queue(session, survivor, loser, cluster)
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    log.warning("queueing %s -> %s failed: %s", loser, survivor, exc)
        if progress:
            progress(index + 1, len(ordered))

    if not dry_run and (stats["merged"] or stats["queued"]):
        # The search index still holds the tombstoned ids. On Postgres the
        # merge deleted each loser's row, but the survivor's vector is stale
        # because its aliases and cast just changed; on SQLite a contentless
        # fts5 row cannot be deleted without the original values at all. Either
        # way a full rebuild is the honest fix, and it is cheap at this scale.
        from app.search import reindex

        stats["reindexed"] = reindex(session)

    stats["plan"] = plan
    return stats


def run(session: Session, **kwargs) -> dict:
    """Runner entry point. Defaults to dry run; the scheduler never merges
    unattended without `apply=True` being set explicitly."""
    return run_dedup(
        session,
        dry_run=not kwargs.get("apply", False),
        limit=kwargs.get("limit"),
        progress=kwargs.get("progress"),
    )
