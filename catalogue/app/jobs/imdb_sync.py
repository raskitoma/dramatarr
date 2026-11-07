"""IMDb enrichment: download, match, enrich.

Nothing here auto-accepts a fuzzy match. A wrong link silently fuses two
different dramas and is far harder to notice afterwards than a missing one, so
anything short of certain goes to a human.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest import ensure_source
from app.models import (
    MatchState,
    Rating,
    Source,
    Title,
    TitleAlias,
    TitleSource,
    MatchCandidate,
)
from app.normalize import normalize_title
from app.precedence import ENRICHER
from app.sources import imdb

log = logging.getLogger(__name__)


def imdb_source(session: Session):
    source, studio = ensure_source(
        session,
        imdb.KEY,
        imdb.STUDIO_NAME,
        imdb.BASE_URL,
        imdb.WATCH_URL_TEMPLATE,
        kind=ENRICHER,
    )
    return source, studio


def wanted_titles(session: Session) -> dict[str, set[int]]:
    """Normalised title -> our title ids, over primary titles AND aliases."""
    wanted: dict[str, set[int]] = {}
    for title_id, norm in session.execute(
        select(Title.id, Title.normalized_title)
    ).all():
        if norm:
            wanted.setdefault(norm, set()).add(title_id)
    for title_id, norm in session.execute(
        select(TitleAlias.title_id, TitleAlias.normalized_alias)
    ).all():
        if norm:
            wanted.setdefault(norm, set()).add(title_id)
    return wanted


def explicit_tconsts(session: Session) -> dict[int, str]:
    """Titles where a platform handed us an IMDb id outright.

    ReelShort publishes `actor_info.imdb_id`. Where it exists there is nothing
    to match: no fuzzy matching, no review queue, no ambiguity.
    """
    out: dict[int, str] = {}
    for link in session.scalars(select(TitleSource)):
        try:
            parsed = json.loads(link.parsed_json or "{}")
        except json.JSONDecodeError:
            continue
        tconst = parsed.get("imdb_tconst")
        if tconst:
            out[link.title_id] = tconst
    return out


def run_imdb_refresh(
    session: Session,
    data_dir: Path,
    *,
    datasets: tuple[str, ...] = ("basics", "ratings", "episode", "akas"),
    download: bool = True,
    ctx=None,
) -> dict:
    stats = {
        "wanted": 0,
        "candidates": 0,
        "auto_matched": 0,
        "queued": 0,
        "ratings": 0,
        "aliases": 0,
        "explicit": 0,
    }

    imdb_dir = data_dir / "imdb"
    paths: dict[str, Path] = {}
    for name in datasets:
        if download:
            paths[name] = imdb.download(name, imdb_dir)
        else:
            paths[name] = imdb_dir / imdb.DATASETS[name]
        if ctx is not None:
            ctx.progress(len(paths), len(datasets), "download")

    source, _ = imdb_source(session)
    wanted = wanted_titles(session)
    stats["wanted"] = len(wanted)

    if ctx is not None:
        ctx.progress(0, None, "scanning title.basics")
    candidates = imdb.find_candidates(
        paths["basics"], wanted, paths.get("akas")
    )
    stats["candidates"] = len(candidates)

    if "episode" in paths:
        if ctx is not None:
            ctx.progress(0, None, "episode fingerprints")
        imdb.attach_episode_shape(paths["episode"], candidates)
    if "ratings" in paths:
        imdb.attach_ratings(paths["ratings"], candidates)

    # Group candidates by the normalised title they matched.
    by_norm: dict[str, list[imdb.Candidate]] = {}
    for cand in candidates.values():
        for label in filter(None, [cand.primary_title, cand.original_title, *cand.akas]):
            norm = normalize_title(label)
            if norm in wanted:
                by_norm.setdefault(norm, []).append(cand)

    explicit = explicit_tconsts(session)
    stats["explicit"] = len(explicit)

    # Titles already linked to IMDb, by any route. Re-proposing these would
    # throw away every decision an operator has made.
    already_linked = {
        link.title_id: link.external_id
        for link in session.scalars(
            select(TitleSource).where(TitleSource.source_id == source.id)
        )
    }
    # Targets an operator has explicitly said no to.
    rejected = {
        (m.title_id, m.target_external_id)
        for m in session.scalars(
            select(MatchCandidate).where(MatchCandidate.state == MatchState.REJECTED)
        )
    }

    titles = list(session.scalars(select(Title)))
    for index, title in enumerate(titles, start=1):
        if ctx is not None:
            if ctx.cancelled():
                break
            ctx.progress(index, len(titles), "matching")

        tconst = already_linked.get(title.id) or explicit.get(title.id)
        if already_linked.get(title.id):
            stats["already_linked"] = stats.get("already_linked", 0) + 1
        if tconst:
            _link(session, source.id, title.id, tconst, method="platform_provided")
            stats["auto_matched"] += 1
        else:
            pool = {
                c.tconst: c for c in by_norm.get(title.normalized_title or "", [])
            }
            for alias in session.scalars(
                select(TitleAlias.normalized_alias).where(TitleAlias.title_id == title.id)
            ):
                for c in by_norm.get(alias or "", []):
                    pool[c.tconst] = c

            # Never re-propose something already turned down.
            for dead in [t for t in pool if (title.id, t) in rejected]:
                pool.pop(dead, None)

            decision = imdb.decide(
                title.primary_title, title.year, title.episode_count, list(pool.values())
            )
            if decision.auto and decision.tconst:
                _link(session, source.id, title.id, decision.tconst, method=decision.method)
                stats["auto_matched"] += 1
                tconst = decision.tconst
            elif decision.tconst:
                _queue(session, source.id, title.id, decision)
                stats["queued"] += 1
                continue
            else:
                continue

        cand = candidates.get(tconst)
        if cand is None:
            continue
        if cand.rating is not None:
            _upsert_rating(session, title.id, source.id, cand)
            stats["ratings"] += 1
        stats["aliases"] += _add_aliases(session, title, source.id, cand)

    session.commit()
    return stats


def _link(session: Session, source_id: int, title_id: int, tconst: str, *, method: str) -> None:
    existing = session.scalar(
        select(TitleSource).where(
            TitleSource.source_id == source_id, TitleSource.external_id == tconst
        )
    )
    if existing is not None:
        existing.last_seen = datetime.now(UTC)
        return
    session.add(
        TitleSource(
            title_id=title_id,
            source_id=source_id,
            external_id=tconst,
            url=f"https://www.imdb.com/title/{tconst}/",
            parsed_json=json.dumps({"match_method": method}),
        )
    )
    session.flush()


def _queue(session: Session, source_id: int, title_id: int, decision) -> None:
    existing = session.scalar(
        select(MatchCandidate).where(
            MatchCandidate.title_id == title_id,
            MatchCandidate.target_source_id == source_id,
            MatchCandidate.target_external_id == decision.tconst,
        )
    )
    if existing is not None:
        return
    session.add(
        MatchCandidate(
            title_id=title_id,
            target_source_id=source_id,
            target_external_id=decision.tconst,
            method=decision.method,
            score=decision.score,
            evidence_json=json.dumps(decision.evidence, default=str),
            state=MatchState.PROPOSED,
        )
    )


def _upsert_rating(session: Session, title_id: int, source_id: int, cand) -> None:
    row = session.scalar(
        select(Rating).where(Rating.title_id == title_id, Rating.source_id == source_id)
    )
    if row is None:
        session.add(
            Rating(
                title_id=title_id,
                source_id=source_id,
                value=cand.rating,
                votes=cand.votes,
                scale=10.0,
                # The point of the whole exercise: a rating that is not filler.
                is_editorial=False,
            )
        )
        return
    row.value = cand.rating
    row.votes = cand.votes


def _add_aliases(session: Session, title: Title, source_id: int, cand) -> int:
    """IMDb akas are the only genuine alternate titles available here --
    DramaBox's are slug variants and ReelShort's are sparse."""
    existing = {
        a
        for a in session.scalars(
            select(TitleAlias.normalized_alias).where(TitleAlias.title_id == title.id)
        )
    }
    added = 0
    for label in {cand.primary_title, cand.original_title, *cand.akas}:
        if not label:
            continue
        norm = normalize_title(label)
        if not norm or norm == title.normalized_title or norm in existing:
            continue
        existing.add(norm)
        session.add(
            TitleAlias(
                title_id=title.id,
                alias=label,
                normalized_alias=norm,
                kind="imdb_aka",
                source_id=source_id,
            )
        )
        added += 1
    return added


def confirm_match(
    session: Session,
    match_id: int,
    *,
    tconst: str | None = None,
    by: str = "operator",
) -> bool:
    """Accept a match, optionally choosing a DIFFERENT candidate.

    The queue row stores the matcher's best guess, but the whole reason a row
    is here is that the matcher was not sure. Offering only "accept my guess"
    or "reject everything" is useless when the evidence lists five candidates
    and the operator can see which one is right -- so `tconst` overrides.
    """
    match = session.get(MatchCandidate, match_id)
    if match is None or match.state != MatchState.PROPOSED:
        return False

    chosen = (tconst or match.target_external_id or "").strip()
    if not chosen.startswith("tt"):
        return False

    if chosen != match.target_external_id:
        # Record what was actually chosen, so the decision is auditable rather
        # than silently diverging from the row.
        try:
            evidence = json.loads(match.evidence_json or "{}")
        except json.JSONDecodeError:
            evidence = {}
        evidence["operator_chose"] = chosen
        evidence["matcher_proposed"] = match.target_external_id
        match.evidence_json = json.dumps(evidence, default=str)
        match.target_external_id = chosen

    match.state = MatchState.CONFIRMED
    match.decided_at = datetime.now(UTC)
    match.decided_by = by
    _link(
        session,
        match.target_source_id,
        match.title_id,
        chosen,
        method="operator_confirmed",
    )
    session.commit()
    return True


def enrich_one(session: Session, title_id: int, tconst: str, data_dir) -> dict:
    """Pull rating and akas for a single confirmed match.

    Without this a decision shows no effect until the next full IMDb run, which
    reads as "nothing happened".
    """
    from pathlib import Path

    source, _ = imdb_source(session)
    title = session.get(Title, title_id)
    if title is None:
        return {"ratings": 0, "aliases": 0}

    ratings_path = Path(data_dir) / "imdb" / imdb.DATASETS["ratings"]
    added = {"ratings": 0, "aliases": 0}
    if not ratings_path.exists():
        return added

    cand = imdb.Candidate(tconst, title.primary_title, "", "", title.year, "operator")
    imdb.attach_ratings(ratings_path, {tconst: cand})
    if cand.rating is not None:
        _upsert_rating(session, title.id, source.id, cand)
        added["ratings"] = 1
    session.commit()
    return added


def reject_match(session: Session, match_id: int, *, by: str = "operator") -> bool:
    match = session.get(MatchCandidate, match_id)
    if match is None or match.state != MatchState.PROPOSED:
        return False
    match.state = MatchState.REJECTED
    match.decided_at = datetime.now(UTC)
    match.decided_by = by
    session.commit()
    return True
