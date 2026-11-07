"""Operator console.

Server-rendered, no build step, no bundler. This is a console for watching
syncs and eyeballing records -- not a product surface.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

import secrets

from app.api.auth import COOKIE_NAME, require_api_key
from app.config import settings
from app.db import get_session
from app.jobs import scheduler
from app.jobs.scheduler import JobRejected
from app.models import (
    RunState,
    Episode,
    MatchCandidate,
    ParseQuarantine,
    Person,
    Rating,
    Source,
    SourceState,
    SyncProgress,
    SyncRun,
    Tag,
    Title,
    TitleAlias,
    TitlePerson,
    TitleSource,
    TitleTag,
)
from app.sources.base import watch_url

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
router = APIRouter(prefix="/admin", dependencies=[Depends(require_api_key)])

#: Login lives on its own unauthenticated router -- guarding it with the very
#: credential it exists to collect would make the console unreachable.
public = APIRouter(prefix="/admin")


@public.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@public.post("/login")
def login_submit(key: str = Form(...)):
    if not settings.api_key or not secrets.compare_digest(key, settings.api_key):
        return RedirectResponse("/admin/login?error=That+key+was+not+accepted.", status_code=303)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        key,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@public.post("/logout")
def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


templates.env.filters["eta"] = _fmt_eta


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    sources = []
    for source in session.scalars(select(Source).order_by(Source.key)):
        state = session.query(SourceState).filter_by(source_id=source.id).one_or_none()
        last = session.scalar(
            select(SyncRun)
            .where(SyncRun.source_id == source.id)
            .order_by(SyncRun.id.desc())
            .limit(1)
        )
        # Shown beside the last run because they answer different questions.
        # "Last run failed" is not the same as "this source is broken": the CLI
        # does not record run state at all, so a source crawled by hand can show
        # an old failure as its most recent run while being perfectly healthy --
        # which is exactly how GoodShort looked after 2,596 titles were indexed.
        last_success = session.scalar(
            select(SyncRun)
            .where(
                SyncRun.source_id == source.id,
                SyncRun.state == RunState.SUCCEEDED,
            )
            .order_by(SyncRun.id.desc())
            .limit(1)
        )
        sources.append(
            {
                "key": source.key,
                "enabled": source.enabled,
                "titles": session.scalar(
                    select(func.count())
                    .select_from(TitleSource)
                    .where(TitleSource.source_id == source.id)
                ),
                "watermark": state.watermark if state else None,
                "breaker": state.breaker_state if state else "closed",
                "locked_by": state.lock_pid if state else None,
                "last": last,
                "last_success": last_success,
            }
        )

    counts = {
        "titles": session.scalar(select(func.count()).select_from(Title)),
        "episodes": session.scalar(select(func.count()).select_from(Episode)),
        # Live people only, matching /stats. A tombstone is a forwarding pointer,
        # not someone in the catalogue.
        "people": session.scalar(
            select(func.count())
            .select_from(Person)
            .where(Person.merged_into_id.is_(None))
        ),
        "tags": session.scalar(select(func.count()).select_from(Tag)),
        "quarantine": session.scalar(select(func.count()).select_from(ParseQuarantine)),
    }
    active = list(
        session.scalars(
            select(SyncRun).where(SyncRun.state == "running").order_by(SyncRun.id.desc())
        )
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"sources": sources, "counts": counts, "active": active, "progress": _progress_map(session)},
    )


def _progress_map(session: Session) -> dict:
    return {
        p.run_id: p for p in session.scalars(select(SyncProgress))
    }


@router.post("/sync")
def trigger(
    source: str = Form(...),
    job_type: str = Form("incremental"),
    session: Session = Depends(get_session),
):
    try:
        run = scheduler.create_run(session, source, job_type, manual=True)
        scheduler.spawn(run.id)
        message = f"started {source}/{job_type} as run {run.id}"
    except JobRejected as exc:
        message = f"rejected: {exc}"
    return RedirectResponse(f"/admin/runs?message={message}", status_code=303)


def superseded_failures(session: Session, runs) -> set[int]:
    """Failed runs that a later run of the SAME job has since succeeded past.

    A failure stays in `sync_run` forever, because that table is history. But a
    red row for a bug fixed two releases ago reads exactly like one from this
    morning, and the operator has to open each to tell them apart -- which is how
    real failures get lost among dead ones. Three of the failures on this box were
    of that kind: a SQLite-only backup, a "database is locked" from before the
    Postgres move, and a GoodShort crawl that failed on code fixed minutes later.

    "Superseded" is defined by the data rather than by timestamps or versions: the
    same source and job_type ran again afterwards and succeeded, so whatever broke
    is demonstrably no longer breaking.
    """
    failed = [r for r in runs if r.state == RunState.FAILED]
    if not failed:
        return set()

    latest_success: dict[tuple[int, str], int] = {}
    for source_id, job_type, run_id in session.execute(
        select(SyncRun.source_id, SyncRun.job_type, func.max(SyncRun.id))
        .where(SyncRun.state == RunState.SUCCEEDED)
        .group_by(SyncRun.source_id, SyncRun.job_type)
    ).all():
        latest_success[(source_id, job_type)] = run_id

    return {
        r.id
        for r in failed
        if latest_success.get((r.source_id, r.job_type), 0) > r.id
    }


@router.get("/runs", response_class=HTMLResponse)
def runs(
    request: Request, message: str | None = None, session: Session = Depends(get_session)
):
    rows = list(
        session.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(40))
    )
    sources = {s.id: s.key for s in session.scalars(select(Source))}
    stale = superseded_failures(session, rows)
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "runs": rows,
            "sources": sources,
            "progress": _progress_map(session),
            "message": message,
            "stats": {r.id: _stats(r) for r in rows},
            "stale": stale,
        },
    )


def _stats(run: SyncRun) -> dict:
    try:
        return json.loads(run.stats_json or "{}")
    except json.JSONDecodeError:
        return {}


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: int, session: Session = Depends(get_session)):
    ok = scheduler.request_cancel(session, run_id)
    msg = f"cancel requested for run {run_id}" if ok else f"run {run_id} is not cancellable"
    return RedirectResponse(f"/admin/runs?message={msg}", status_code=303)


@router.get("/titles", response_class=HTMLResponse)
def titles(
    request: Request,
    q: str | None = None,
    limit: int = 50,
    session: Session = Depends(get_session),
):
    from app.search import search_ids

    stmt = select(Title).order_by(Title.updated_at.desc()).limit(limit)
    if q:
        ids = search_ids(session, q)
        stmt = select(Title).where(Title.id.in_(ids)).limit(limit) if ids else None

    rows = list(session.scalars(stmt)) if stmt is not None else []
    return templates.TemplateResponse(
        request, "titles.html", {"titles": rows, "q": q or "", "links": {t.id: _links(session, t.id) for t in rows}}
    )


def _links(session: Session, title_id: int) -> list[dict]:
    out = []
    for link, source in session.execute(
        select(TitleSource, Source)
        .join(Source, TitleSource.source_id == Source.id)
        .where(TitleSource.title_id == title_id)
    ).all():
        out.append(
            {
                "key": source.key,
                "watch_url": watch_url(source.watch_url_template or "", link.external_id)
                if source.watch_url_template
                else None,
            }
        )
    return out


@router.get("/titles/{title_id}", response_class=HTMLResponse)
def title_detail(
    request: Request, title_id: int, session: Session = Depends(get_session)
):
    title = session.get(Title, title_id)
    tags = session.execute(
        select(Tag.namespace, Tag.value)
        .join(TitleTag, TitleTag.tag_id == Tag.id)
        .where(TitleTag.title_id == title_id)
    ).all()
    cast = session.execute(
        select(Person.name, TitlePerson.role)
        .join(TitlePerson, TitlePerson.person_id == Person.id)
        .where(TitlePerson.title_id == title_id)
        .order_by(TitlePerson.billing_order)
    ).all()
    ratings = session.execute(
        select(Source.key, Rating.value, Rating.is_editorial)
        .join(Source, Rating.source_id == Source.id)
        .where(Rating.title_id == title_id)
    ).all()
    aliases = list(
        session.scalars(select(TitleAlias.alias).where(TitleAlias.title_id == title_id))
    )
    episodes = session.scalar(
        select(func.count()).select_from(Episode).where(Episode.title_id == title_id)
    )
    sources = session.execute(
        select(TitleSource, Source)
        .join(Source, TitleSource.source_id == Source.id)
        .where(TitleSource.title_id == title_id)
    ).all()

    return templates.TemplateResponse(
        request,
        "title_detail.html",
        {
            "title": title,
            "tags": tags,
            "cast": cast,
            "ratings": ratings,
            "aliases": aliases,
            "episode_count": episodes,
            "sources": [
                {
                    "key": s.key,
                    "external_id": ts.external_id,
                    "watch_url": watch_url(s.watch_url_template or "", ts.external_id)
                    if s.watch_url_template
                    else None,
                    "url": ts.url,
                    "last_seen": ts.last_seen,
                    "parsed": json.loads(ts.parsed_json or "{}"),
                }
                for ts, s in sources
            ],
        },
    )


@router.get("/matches", response_class=HTMLResponse)
def matches(
    request: Request,
    message: str | None = None,
    session: Session = Depends(get_session),
):
    rows = session.execute(
        select(MatchCandidate, Title)
        .join(Title, MatchCandidate.title_id == Title.id)
        .where(MatchCandidate.state == "proposed")
        .order_by(MatchCandidate.score.desc())
        .limit(100)
    ).all()
    items = []
    for match, title in rows:
        try:
            evidence = json.loads(match.evidence_json or "{}")
        except json.JSONDecodeError:
            evidence = {}
        items.append({"m": match, "t": title, "e": evidence})
    return templates.TemplateResponse(
        request, "matches.html", {"items": items, "message": message}
    )


@router.post("/matches/{match_id}/{action}")
def decide_match(
    match_id: int,
    action: str,
    tconst: str = Form(default=""),
    session: Session = Depends(get_session),
):
    from app.jobs.imdb_sync import confirm_match, enrich_one, reject_match
    from app.models import MatchCandidate as MC

    if action == "confirm":
        chosen = (tconst or "").strip()
        ok = confirm_match(session, match_id, tconst=chosen or None)
        if ok:
            row = session.get(MC, match_id)
            added = enrich_one(session, row.title_id, row.target_external_id, settings.data_dir)
            msg = f"linked to {row.target_external_id}"
            if added.get("ratings"):
                msg += " (rating pulled)"
        else:
            msg = f"could not confirm match {match_id} -- check the tconst"
    else:
        ok = reject_match(session, match_id)
        msg = (
            f"match {match_id} rejected; it will not be proposed again"
            if ok
            else f"match {match_id} was not pending"
        )
    return RedirectResponse(f"/admin/matches?message={msg}", status_code=303)


def _merge_rank(a: Title, b: Title) -> int:
    """Sort key for the review queue: 0 = look at this one first.

    A RANKING, deliberately not a rule. It reorders what a human sees and hides
    nothing, so the worst case is an unhelpful order -- unlike a `Verdict.REJECT`,
    which is never returned to the queue at all and would make a missed duplicate
    permanently invisible.

    The signal, hand-adjudicated by the dramatarr session over 11 confirmed
    cross-platform duplicates (4 of them unanimous across three independent
    readers) against 26 mere title collisions: where exactly one member of a pair
    publishes a year, the YEAR-LESS member having MORE episodes went with a
    genuine duplicate 9 times in 11 (82%), versus 27% among collisions. It is not
    an artefact of one source running longer -- the only year-less source is the
    SHORTEST of the four by median episode count -- and the likely mechanism is
    that it segments the same show more finely.

    It is emphatically not a merge criterion. Two unanimous counterexamples exist
    (`18209/2660`, `18855/12857`): real duplicates where the year-less side has
    FEWER episodes. Used to REJECT candidates, those two would have vanished
    silently. Used to ORDER them, they simply sort later and a human still sees
    them.
    """
    years = [t.year for t in (a, b)]
    counts = [t.episode_count for t in (a, b)]
    if None in counts or len([y for y in years if y is not None]) != 1:
        return 1
    yearless, dated = (a, b) if a.year is None else (b, a)
    return 0 if (yearless.episode_count or 0) > (dated.episode_count or 0) else 1


@router.get("/merges", response_class=HTMLResponse)
def merges(
    request: Request,
    message: str | None = None,
    session: Session = Depends(get_session),
):
    """Duplicate pairs the rule would not merge on its own.

    Everything here matched on an exact key but failed to be CONFIRMED by the
    evidence -- usually a wide episode-count gap, which is exactly where two
    different shows with a generic name hide.
    """
    from app.models import MergeState, TitleMerge

    loser = aliased(Title)
    survivor = aliased(Title)
    # Fetch every proposal, THEN rank, THEN cut to the page. Ordering by
    # `TitleMerge.id` under a LIMIT did not merely show them in an arbitrary
    # order -- with 439 proposed and a cap of 200 it decided which 239 a human
    # would never see at all.
    rows = session.execute(
        select(TitleMerge, survivor, loser)
        .join(survivor, TitleMerge.survivor_id == survivor.id)
        .join(loser, TitleMerge.loser_id == loser.id)
        .where(TitleMerge.state == MergeState.PROPOSED)
        .order_by(TitleMerge.id)
    ).all()
    total = len(rows)
    ranked = sorted(rows, key=lambda r: (_merge_rank(r[1], r[2]), r[0].id))

    items = []
    for row, keep, drop in ranked[:200]:
        try:
            evidence = json.loads(row.evidence_json or "{}")
        except json.JSONDecodeError:
            evidence = {}
        items.append({"m": row, "keep": keep, "drop": drop, "e": evidence,
                      "likely": _merge_rank(keep, drop) == 0})
    return templates.TemplateResponse(
        request, "merges.html",
        {"items": items, "message": message, "total": total, "shown": len(items)},
    )


@router.post("/merges/{merge_id}/{action}")
def decide_merge(
    merge_id: int,
    action: str,
    session: Session = Depends(get_session),
):
    from datetime import UTC, datetime

    from app.merge import MergeError, merge_titles
    from app.models import MergeState, TitleMerge
    from app.search import reindex

    row = session.get(TitleMerge, merge_id)
    if row is None or row.state != MergeState.PROPOSED:
        return RedirectResponse(
            f"/admin/merges?message=merge {merge_id} is not pending", status_code=303
        )

    if action == "confirm":
        try:
            merge_titles(
                session, row.survivor_id, row.loser_id,
                reason=row.reason or "confirmed by operator",
                key_kind=row.key_kind, merged_by="operator",
            )
            session.commit()
            # The survivor's aliases and cast just changed, and the loser has to
            # leave the index; both need a rebuild to be reflected in search.
            reindex(session)
            msg = f"merged {row.loser_id} into {row.survivor_id}"
        except MergeError as exc:
            session.rollback()
            msg = f"could not merge: {exc}"
    else:
        row.state = MergeState.REJECTED
        row.decided_at = datetime.now(UTC)
        row.merged_by = "operator"
        session.commit()
        msg = f"kept {row.loser_id} and {row.survivor_id} separate; not proposed again"
    return RedirectResponse(f"/admin/merges?message={msg}", status_code=303)


@router.get("/quarantine", response_class=HTMLResponse)
def quarantine(request: Request, session: Session = Depends(get_session)):
    rows = list(
        session.scalars(
            select(ParseQuarantine).order_by(ParseQuarantine.id.desc()).limit(100)
        )
    )
    sources = {s.id: s.key for s in session.scalars(select(Source))}
    return templates.TemplateResponse(
        request, "quarantine.html", {"rows": rows, "sources": sources}
    )
