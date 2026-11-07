"""REST API."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.schemas import (
    EditionOut,
    EpisodeOut,
    Page,
    PersonAliasOut,
    PersonBrief,
    PersonDetail,
    RatingOut,
    RunOut,
    StudioLink,
    StudioOut,
    TagOut,
    TitleBrief,
    TitleDetail,
)
from app.config import settings
from app.db import get_session
from app.jobs import scheduler
from app.people import resolve_person
from app.jobs.scheduler import JobRejected
from app.models import (
    Episode,
    MatchCandidate,
    MergeState,
    Person,
    PersonAlias,
    PersonSource,
    Rating,
    Source,
    Studio,
    Tag,
    Title,
    TitleAlias,
    TitleImage,
    TitleMerge,
    TitlePerson,
    TitleSource,
    TitleTag,
    Image,
    SyncProgress,
    SyncRun,
)
from app.merge import resolve_survivor
from app.search import search_ids
from app.sources.base import watch_url

router = APIRouter(dependencies=[Depends(require_api_key)])


# --- cursor helpers ----------------------------------------------------------


def encode_cursor(updated_at: datetime, title_id: int) -> str:
    raw = json.dumps([updated_at.isoformat(), title_id])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp, tolerating an unencoded '+' offset.

    A client that pastes `2026-08-16T12:00:00+00:00` straight into a query
    string gets the '+' decoded as a space, and a strict parser answers 422.
    There is no valid ISO 8601 string with a space in that position, so the
    repair is unambiguous -- and this is the endpoint the downstream app polls,
    which is the worst place for a confusing rejection.
    """
    candidate = value.strip()
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    repaired = re.sub(r"\s(\d{2}:\d{2})$", r"+\1", candidate)
    try:
        return datetime.fromisoformat(repaired)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid timestamp {value!r}"
        ) from exc


def encode_search_cursor(offset: int) -> str:
    """Search paginates by position in the ranked list.

    Keyset on (updated_at, id) is right for browsing, where the order is the
    data's own; it is wrong for search, where the order IS the answer.
    """
    return base64.urlsafe_b64encode(json.dumps(["s", offset]).encode()).decode()


def _search_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        kind, offset = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if kind != "s":
            raise ValueError("not a search cursor")
        return int(offset)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        stamp, title_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(stamp), int(title_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


# --- resolving a superseded id -----------------------------------------------


def canonical_title_id(
    title_id: int,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> int:
    """Map a requested title id to the live row, following a merge tombstone.

    A dependency rather than a call in each handler, because doing it per route
    means remembering it per route -- and both times it was forgotten the failure
    was silent. `/titles/{id}/episodes` answered 200 with an empty list once its
    episode rows had moved to the survivor, and this route 404'd for a poster that
    exists. Anything keyed on a path title id gets this by construction now.

    Sets `Content-Location` to the same route under the surviving id, derived from
    the request path so each route advertises its own canonical URL.
    """
    canonical = resolve_survivor(session, title_id)
    if canonical != title_id:
        response.headers["Content-Location"] = request.url.path.replace(
            f"/{title_id}", f"/{canonical}", 1
        )
        response.headers["Deprecation"] = "true"
    return canonical


def canonical_person_id(
    person_id: int,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> int:
    """Map a requested person id to the live row, following a merge tombstone.

    The person counterpart of `canonical_title_id`, and a dependency for the same
    reason: `merge_people` moves every credit and link to the survivor, so a
    superseded id owns nothing and each route that forgot to resolve would fail
    differently -- 404 here, an empty photo there. One dependency, applied by
    construction.
    """
    canonical = resolve_person(session, person_id)
    if canonical != person_id:
        response.headers["Content-Location"] = request.url.path.replace(
            f"/{person_id}", f"/{canonical}", 1
        )
        response.headers["Deprecation"] = "true"
    return canonical


# --- serialisation -----------------------------------------------------------


def _editions(session: Session, title_id: int, primary_title: str | None) -> list[EditionOut]:
    """Every language edition of this title, from the links that published them.

    Read from each link's own `parsed_json` rather than from `title_alias`,
    because the alias carries only the NAME. The synopsis a Spanish reader
    actually reads is stored per link and was not exposed anywhere -- a consumer
    could learn the Spanish title existed and still had only the English blurb.

    Only links that carry a `lang` appear. A title held in one language has no
    editions to choose between, and returning a single self-referential entry
    for it would make consumers filter out noise on every title in the
    catalogue.
    """
    rows = session.execute(
        select(TitleSource, Source)
        .join(Source, TitleSource.source_id == Source.id)
        .where(TitleSource.title_id == title_id, TitleSource.lang.is_not(None))
        .order_by(TitleSource.first_seen, Source.key, TitleSource.id)
    ).all()
    if len(rows) < 2:
        return []

    editions: list[EditionOut] = []
    for link, source in rows:
        try:
            values = json.loads(link.parsed_json or "{}")
        except json.JSONDecodeError:
            values = {}
        title_text = values.get("primary_title")
        editions.append(
            EditionOut(
                lang=link.lang,
                title=title_text,
                synopsis=values.get("synopsis"),
                source=source.key,
                watch_url=(
                    watch_url(source.watch_url_template, link.external_id)
                    if source.watch_url_template else None
                ),
                is_primary=bool(title_text and title_text == primary_title),
            )
        )
    return editions


def _studio_links(session: Session, title_id: int) -> list[StudioLink]:
    """Every platform this title is available on.

    Ordered, and the order is contract. Before deduplication this list always had
    one element, so a consumer reading `studios[0]` was safe by accident; a merged
    title has two, and with no ORDER BY the database is free to return them in
    whichever order it likes -- which in practice flipped with whichever sync had
    run last, flipping the platform label and Watch button of a merged show from
    day to day.
    `first_seen` first, so index 0 is the link that has been known longest -- the
    same link `precedence.resolve` takes `primary_title` and `episode_count` from,
    so the label agrees with the metadata beside it.
    """
    rows = session.execute(
        select(TitleSource, Source, Studio)
        .join(Source, TitleSource.source_id == Source.id)
        .outerjoin(Studio, TitleSource.studio_id == Studio.id)
        .where(TitleSource.title_id == title_id)
        .order_by(TitleSource.first_seen, Source.key, TitleSource.id)
    ).all()
    links = []
    for link, source, studio in rows:
        if not source.watch_url_template:
            continue
        links.append(
            StudioLink(
                key=source.key,
                name=studio.name if studio else source.key,
                external_id=link.external_id,
                watch_url=watch_url(source.watch_url_template, link.external_id),
                url=link.url,
                share_url=link.share_url,
                delisted_at=link.delisted_at,
            )
        )
    return links


def _poster_url(session: Session, title_id: int) -> str | None:
    """Point at our own endpoint, not the origin CDN.

    Source URLs rot when a platform rotates its CDN, and hotlinking would leak
    a downstream page's viewers to a third-party host on every render.
    """
    stored = session.execute(
        select(Image.id)
        .join(TitleImage, TitleImage.image_id == Image.id)
        .where(TitleImage.title_id == title_id)
        .limit(1)
    ).first()
    return f"/titles/{title_id}/poster" if stored else None


def _photo_url(session: Session, person_id: int) -> str | None:
    """Point at our own endpoint, not the source CDN.

    Same reasoning as `_poster_url`: source URLs rot when a platform rotates its
    CDN, and hotlinking would leak a downstream page's viewers to a third party
    on every render. None is the common case -- the platforms publish a headshot
    for only some of the cast.
    """
    stored = session.execute(
        select(PersonSource.avatar_image_id)
        .where(
            PersonSource.person_id == person_id,
            PersonSource.avatar_image_id.isnot(None),
        )
        .limit(1)
    ).first()
    return f"/people/{person_id}/photo" if stored else None


def _brief(session: Session, title: Title) -> TitleBrief:
    """Serialise a title, resolving through a tombstone for the derived fields.

    The resolution has to happen here and not only in `get_title`. A merge moves
    every `title_source` and `title_image` row to the survivor, so a tombstone's
    own `studios` is `[]` and its `poster_url` is `None` -- and a tombstone is
    exactly what a `?updated_since=` poll returns, because that feed is the only
    way a consumer learns its stored id was superseded.

    A consumer that does not yet read `merged_into` would otherwise upsert its
    record for the old id and overwrite a working `watch_url` with an empty list,
    turning a metadata change into data loss in the consumer. Echoing the
    requested `id` while serving the survivor's links keeps that consumer working
    unchanged, and `merged_into` lets an updated one converge.
    """
    requested_id = title.id
    merged_into = None
    if title.merged_into_id is not None:
        merged_into = resolve_survivor(session, title.id)
        survivor = session.get(Title, merged_into)
        if survivor is not None:
            title = survivor  # every derived field now comes from the survivor

    return TitleBrief(
        id=requested_id,
        primary_title=title.primary_title,
        year=title.year,
        episode_count=title.episode_count,
        audience_rating=title.audience_rating,
        poster_url=_poster_url(session, title.id),
        studios=_studio_links(session, title.id),
        updated_at=title.updated_at,
        merged_into=merged_into,
    )


# --- titles ------------------------------------------------------------------


@router.get("/titles", response_model=Page, tags=["catalogue"])
def list_titles(
    session: Session = Depends(get_session),
    q: str | None = Query(None, description="free-text search"),
    studio: str | None = Query(None, description="source key, e.g. reelshort"),
    genre: str | None = None,
    year: int | None = None,
    audience: str | None = None,
    person: str | None = None,
    updated_since: str | None = Query(
        None,
        description="ISO timestamp; returns everything changed since. "
        "A '+00:00' offset may be sent unencoded.",
    ),
    include_delisted: bool = False,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
) -> Page:
    stmt = select(Title)

    ranked: list[int] | None = None
    if q:
        ranked = search_ids(session, q)
        if not ranked:
            return Page(items=[], next_cursor=None, count=0)
        stmt = stmt.where(Title.id.in_(ranked))
    # EXISTS, never JOIN. A JOIN emits one row per matching child, and the
    # keyset page below applies LIMIT to ROWS while `.unique()` dedupes
    # afterwards -- so a window of `limit + 1` rows holding any duplicate
    # collapses to `<= limit` titles, the "is there another page?" test reads
    # that as the end of the scan, and every remaining title is silently
    # dropped. Measured on the live catalogue: `?studio=netshort` returned 5,925
    # of 6,223 titles, losing 298 (4.8%) with a clean exhausted cursor and no
    # error. 399 NetShort titles carry two links, which is all it took.
    #
    # The same shape was latent on `genre` and `person`: a title credited to two
    # actors both matching one `?person=` substring duplicates identically.
    if studio:
        stmt = stmt.where(
            select(1)
            .select_from(TitleSource)
            .join(Source, TitleSource.source_id == Source.id)
            .where(TitleSource.title_id == Title.id, Source.key == studio)
            .exists()
        )
    if genre:
        stmt = stmt.where(
            select(1)
            .select_from(TitleTag)
            .join(Tag, TitleTag.tag_id == Tag.id)
            .where(TitleTag.title_id == Title.id, Tag.value == genre)
            .exists()
        )
    if person:
        stmt = stmt.where(
            select(1)
            .select_from(TitlePerson)
            .join(Person, TitlePerson.person_id == Person.id)
            .where(
                TitlePerson.title_id == Title.id,
                Person.name.ilike(f"%{person}%"),
                Person.merged_into_id.is_(None),
            )
            .exists()
        )
    if year:
        stmt = stmt.where(Title.year == year)
    if audience:
        stmt = stmt.where(Title.audience_rating == audience)
    if not include_delisted:
        stmt = stmt.where(Title.delisted_at.is_(None))
    if updated_since:
        stmt = stmt.where(Title.updated_at >= parse_timestamp(updated_since))
    else:
        # Tombstones are hidden while browsing but MUST survive a ?updated_since=
        # poll, which is the only way a consumer storing a merged id ever learns
        # where it went. Filtering them here unconditionally -- the way
        # include_delisted does above -- would make merged titles simply stop
        # appearing, and dramatarr would keep a dead id forever.
        stmt = stmt.where(Title.merged_into_id.is_(None))

    # Keyset, not offset: syncs write while clients poll, and offset would skip
    # or repeat rows mid-scan.
    if cursor:
        stamp, last_id = decode_cursor(cursor)
        stmt = stmt.where(
            (Title.updated_at > stamp)
            | ((Title.updated_at == stamp) & (Title.id > last_id))
        )

    if ranked is not None:
        # Search results come back in relevance order -- exact title first.
        # Re-sorting them by updated_at, as keyset pagination does for browsing,
        # silently threw that ranking away: an exact-title query returned the
        # right row somewhere on page one rather than at the top.
        offset = _search_offset(cursor)
        window = ranked[offset : offset + limit + 1]
        found = {t.id: t for t in session.scalars(stmt.where(Title.id.in_(window)))}
        titles = [found[i] for i in window if i in found]
        next_cursor = None
        if len(titles) > limit:
            titles = titles[:limit]
            next_cursor = encode_search_cursor(offset + limit)
        return Page(
            items=[_brief(session, t) for t in titles],
            next_cursor=next_cursor,
            count=len(titles),
        )

    stmt = stmt.order_by(Title.updated_at, Title.id).limit(limit + 1)
    titles = list(session.scalars(stmt).unique())

    next_cursor = None
    if len(titles) > limit:
        titles = titles[:limit]
        last = titles[-1]
        next_cursor = encode_cursor(last.updated_at, last.id)

    return Page(
        items=[_brief(session, t) for t in titles],
        next_cursor=next_cursor,
        count=len(titles),
    )


@router.get("/titles/{title_id}", response_model=TitleDetail, tags=["catalogue"])
def get_title(
    title_id: int = Depends(canonical_title_id),
    session: Session = Depends(get_session),
) -> TitleDetail:
    """A title, following a merge tombstone to the row that replaced it.

    A consumer holding a superseded id gets the surviving title rather than a 404,
    plus a `Content-Location` header saying where it went. Serving the content
    instead of a 301 is deliberate: a redirect only helps a client that follows
    them, and the deployed consumer was written before merges existed.
    """
    title = session.get(Title, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail="no such title")

    aliases = list(
        session.scalars(select(TitleAlias.alias).where(TitleAlias.title_id == title_id))
    )
    tags = [
        TagOut(namespace=ns, value=v)
        for ns, v in session.execute(
            select(Tag.namespace, Tag.value)
            .join(TitleTag, TitleTag.tag_id == Tag.id)
            .where(TitleTag.title_id == title_id)
        ).all()
    ]
    cast = [
        PersonBrief(
            id=p.id,
            name=p.name,
            role=tp.role,
            billing_order=tp.billing_order,
            photo_url=_photo_url(session, p.id),
        )
        for tp, p in session.execute(
            select(TitlePerson, Person)
            .join(Person, TitlePerson.person_id == Person.id)
            .where(TitlePerson.title_id == title_id)
            .order_by(TitlePerson.billing_order)
        ).all()
    ]
    ratings = [
        RatingOut(
            source=src.key,
            value=r.value,
            votes=r.votes,
            scale=r.scale,
            is_editorial=r.is_editorial,
        )
        for r, src in session.execute(
            select(Rating, Source)
            .join(Source, Rating.source_id == Source.id)
            .where(Rating.title_id == title_id)
        ).all()
    ]

    brief = _brief(session, title)
    return TitleDetail(
        **brief.model_dump(),
        synopsis=title.synopsis,
        first_paid_episode=title.first_paid_episode,
        completed=title.completed,
        delisted_at=title.delisted_at,
        aliases=aliases,
        editions=_editions(session, title_id, title.primary_title),
        tags=tags,
        cast=cast,
        ratings=ratings,
    )


@router.get("/titles/{title_id}/poster", tags=["catalogue"])
def get_poster(
    response: Response,
    title_id: int = Depends(canonical_title_id),
    session: Session = Depends(get_session),
):
    """Serve key art from the local store.

    Served by us rather than hotlinked so the index keeps working when a source
    rotates its CDN, and so a downstream page does not leak its viewers' IPs to
    a third-party host on every render.
    """
    row = session.execute(
        select(Image)
        .join(TitleImage, TitleImage.image_id == Image.id)
        .where(TitleImage.title_id == title_id)
        # A merged title can hold two primary posters -- nothing in the schema
        # forbids it and `attach_poster` always writes is_primary=True -- so the
        # id is needed as a tiebreak or the served art flips between requests.
        .order_by(TitleImage.is_primary.desc(), TitleImage.id)
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no poster stored for this title")

    path = settings.image_dir / row.local_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="poster missing from the store")
    return FileResponse(
        path,
        media_type="image/webp",
        # Carry over whatever `canonical_title_id` set. FastAPI merges the
        # injected Response's headers only when the handler returns a model; a
        # Response returned directly bypasses that, so a superseded id would have
        # served the right image with nothing saying the id had moved.
        headers={"Cache-Control": "public, max-age=86400", **dict(response.headers)},
    )


@router.get(
    "/titles/{title_id}/episodes", response_model=list[EpisodeOut], tags=["catalogue"]
)
def get_episodes(
    response: Response,
    canonical: int = Depends(canonical_title_id),
    source: str | None = Query(
        None,
        description="Return only this platform's episodes, e.g. `reelshort`. "
        "Omit for every platform's, grouped by source.",
    ),
    lang: str | None = Query(
        None,
        description="Return only this language edition's episodes, e.g. `es`. "
        "A platform that publishes per-language catalogues lists the same show "
        "once per language, so `?source=` alone cannot separate them. Episodes "
        "whose edition is unknown are never matched by this filter.",
    ),
    session: Session = Depends(get_session),
) -> list[EpisodeOut]:
    """Episodes for a title, optionally from one platform.

    A merged title holds the episode rows of every platform it is listed on, so
    this can return two runs of the same show -- 20 DramaBox rows and 20 ReelShort
    rows for the same 20 episodes. That is not double-counting: they are separate
    published listings, with their own numbering and their own free/paid split.
    `episode_count` on the title stays a single platform's figure, resolved by
    precedence, so it deliberately does NOT match the length of this list.

    Filtering is opt-in rather than a default. Picking a platform silently would
    hide rows a consumer had already fetched -- for "The Scent of My Fated Luna",
    DramaBox publishes 46 episodes and ReelShort 47, and defaulting to either
    makes an episode disappear with nothing in the response to say why.

    The same show can also appear twice under ONE platform, which `?source=`
    cannot separate: ReelShort publishes a separate catalogue per language, so a
    localised title answers with two runs of 1..N in two languages. `?lang=`
    separates those, and `X-Episode-Languages` advertises what there is to pick
    from. A row whose edition is unknown carries `lang: null` and is returned
    unfiltered, because dropping it would hide an episode that genuinely exists.
    """
    # `canonical` comes from the dependency, which also sets Content-Location.
    # Every episode row moves to the survivor during a merge, so resolving here is
    # what stops a superseded id returning an empty list instead of the show.
    available = list(
        session.scalars(
            select(Source.key)
            .join(Episode, Episode.source_id == Source.id)
            .where(Episode.title_id == canonical)
            .distinct()
            .order_by(Source.key)
        )
    )
    # Advertised so a consumer can offer a platform switch without having to
    # fetch every episode first and infer the options from them.
    response.headers["X-Episode-Sources"] = ",".join(available)

    # Advertised for the same reason as the sources: a consumer offering a
    # language switch should not have to fetch every episode and infer the
    # options. Only labelled editions count -- an untagged row is not evidence
    # that a language exists.
    languages = list(
        session.scalars(
            select(TitleSource.lang)
            .join(Episode, Episode.title_source_id == TitleSource.id)
            .where(Episode.title_id == canonical, TitleSource.lang.is_not(None))
            .distinct()
            .order_by(TitleSource.lang)
        )
    )
    response.headers["X-Episode-Languages"] = ",".join(languages)

    stmt = (
        select(Episode, Source, TitleSource.lang)
        .join(Source, Episode.source_id == Source.id)
        # OUTER: an episode whose edition could not be settled from the archive
        # keeps a NULL pointer, and an inner join would silently drop it -- the
        # exact failure this endpoint's docstring exists to prevent.
        .outerjoin(TitleSource, Episode.title_source_id == TitleSource.id)
        .where(Episode.title_id == canonical)
        # By source, then by edition, then by number. Ordering on `number` alone
        # interleaves the platforms (1 dramabox, 2 dramabox, 2 reelshort...),
        # which reads as corrupt data; with per-language catalogues it would
        # interleave the languages too, so a reader would see episode 1 twice
        # before episode 2.
        #
        # Grouped on the EDITION ID rather than on `lang`. Sorting on `lang`
        # sorts on a column that is NULL for every untagged row, and NULL
        # placement is dialect-dependent -- SQLite first, Postgres last -- so a
        # partially tagged title came back 4,5,6,1,2,3, differently in
        # production than in the tests, which is precisely the corrupt-looking
        # output this clause exists to prevent. `title_source_id` is stable,
        # non-null for tagged rows, and coalesced so untagged rows form one
        # group instead of scattering.
        .order_by(
            Source.key,
            func.coalesce(Episode.title_source_id, 0),
            Episode.number,
            Episode.id,
        )
    )
    if source:
        if source not in available:
            raise HTTPException(
                status_code=404,
                detail=f"no episodes from {source!r} for this title;"
                f" available: {available or 'none'}",
            )
        stmt = stmt.where(Source.key == source)
    if lang:
        if lang not in languages:
            raise HTTPException(
                status_code=404,
                detail=f"no episodes in {lang!r} for this title;"
                f" available: {languages or 'none'}",
            )
        stmt = stmt.where(TitleSource.lang == lang)

    return [
        EpisodeOut(
            number=e.number,
            name=e.name,
            duration_ms=e.duration_ms,
            is_free=e.is_free,
            source=s.key,
            lang=row_lang,
        )
        for e, s, row_lang in session.execute(stmt).all()
    ]


# --- people, studios, genres -------------------------------------------------


@router.get("/people", response_model=list[PersonBrief], tags=["catalogue"])
def list_people(
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[PersonBrief]:
    # Tombstones carry a name but no credits, links or photo, so listing one
    # would offer a dead-looking entry that a search result should never surface.
    stmt = (
        select(Person)
        .where(Person.merged_into_id.is_(None))
        .order_by(Person.name)
        .limit(limit)
    )
    if q:
        stmt = stmt.where(Person.name.ilike(f"%{q}%"))
    return [PersonBrief(id=p.id, name=p.name) for p in session.scalars(stmt)]


@router.get("/people/{person_id}", response_model=PersonDetail, tags=["catalogue"])
def get_person(
    person_id: int = Depends(canonical_person_id),
    session: Session = Depends(get_session),
) -> PersonDetail:
    """A person, following a merge tombstone to whoever absorbed them.

    `person_id` is already the surviving id -- the dependency resolved it and set
    `Content-Location` when it moved -- so the body below must never reach for
    the path parameter again, or a superseded id would answer 200 with an empty
    person.
    """
    person = session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="no such person")

    externals = {
        src.key: link.external_id
        for link, src in session.execute(
            select(PersonSource, Source)
            .join(Source, PersonSource.source_id == Source.id)
            .where(PersonSource.person_id == person_id)
        ).all()
    }
    titles = list(
        session.scalars(
            select(Title)
            .join(TitlePerson, TitlePerson.title_id == Title.id)
            .where(TitlePerson.person_id == person_id)
        ).unique()
    )
    aliases = [
        PersonAliasOut(alias=row.alias, lang=row.lang, source=key)
        for row, key in session.execute(
            select(PersonAlias, Source.key)
            .outerjoin(Source, PersonAlias.source_id == Source.id)
            .where(PersonAlias.person_id == person_id)
            .order_by(PersonAlias.id)
        ).all()
    ]
    return PersonDetail(
        id=person.id,
        name=person.name,
        bio=person.bio,
        photo_url=_photo_url(session, person.id),
        gender=person.gender,
        height_cm=person.height_cm,
        weight_kg=person.weight_kg,
        zodiac=person.zodiac,
        aliases=aliases,
        external_ids=externals,
        titles=[_brief(session, t) for t in titles],
    )


@router.get("/people/{person_id}/photo", tags=["catalogue"])
def get_person_photo(
    response: Response,
    person_id: int = Depends(canonical_person_id),
    session: Session = Depends(get_session),
):
    """Serve an actor headshot from the local store.

    Served by us rather than hotlinked, for the same two reasons as the poster
    route: the source CDN rotates and would break the link, and proxying keeps a
    downstream page's viewers off a third-party host.
    """
    row = session.execute(
        select(Image)
        .join(PersonSource, PersonSource.avatar_image_id == Image.id)
        .where(PersonSource.person_id == person_id)
        # Deterministic when a person is known to several sources and each
        # published its own photo -- otherwise the served face could change
        # between requests.
        .order_by(PersonSource.source_id, Image.id)
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no photo stored for this person")

    path = settings.image_dir / row.local_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="photo missing from the store")
    return FileResponse(
        path,
        media_type="image/webp",
        # Same trap as the poster route: FastAPI merges the injected Response's
        # headers only when the handler returns a model, so a Response returned
        # directly would serve the right face with nothing saying the id moved.
        headers={"Cache-Control": "public, max-age=86400", **dict(response.headers)},
    )


@router.get("/studios", response_model=list[StudioOut], tags=["catalogue"])
def list_studios(session: Session = Depends(get_session)) -> list[StudioOut]:
    rows = session.execute(
        select(Studio, Source, func.count(TitleSource.id))
        .join(Source, Studio.source_id == Source.id)
        .outerjoin(TitleSource, TitleSource.studio_id == Studio.id)
        # BOTH entities, not just Studio. Grouping by `studio.id` alone while
        # selecting `source.*` is a hard 500 on PostgreSQL
        # ("column source.id must appear in the GROUP BY clause"); SQLite accepts
        # it under its bare-columns extension when the grouping column is a
        # primary key, so the endpoint passed its tests and had never once
        # succeeded in production. Found by the dramatarr session enumerating the
        # API, not by a user, because nothing it consumes calls this route.
        .group_by(Studio.id, Source.id)
    ).all()
    return [
        StudioOut(
            key=source.key,
            name=studio.name,
            homepage_url=studio.homepage_url,
            title_count=count,
        )
        for studio, source, count in rows
    ]


@router.get("/genres", response_model=list[TagOut], tags=["catalogue"])
def list_genres(
    namespace: str | None = None, session: Session = Depends(get_session)
) -> list[TagOut]:
    stmt = select(Tag).order_by(Tag.namespace, Tag.value)
    if namespace:
        stmt = stmt.where(Tag.namespace == namespace)
    return [TagOut(namespace=t.namespace, value=t.value) for t in session.scalars(stmt)]


# --- sync --------------------------------------------------------------------


def _run_out(session: Session, run: SyncRun) -> RunOut:
    source = session.get(Source, run.source_id)
    progress = session.query(SyncProgress).filter_by(run_id=run.id).one_or_none()
    try:
        stats = json.loads(run.stats_json or "{}")
    except json.JSONDecodeError:
        stats = {}
    return RunOut(
        id=run.id,
        source=source.key if source else "?",
        job_type=run.job_type,
        run_date=str(run.run_date),
        state=run.state,
        manual=run.manual,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        items_done=progress.items_done if progress else None,
        items_total=progress.items_total if progress else None,
        eta_seconds=progress.eta_seconds if progress else None,
        rate_per_sec=progress.rate_ewma if progress else None,
        stats=stats,
    )


@router.get("/sync/runs", response_model=list[RunOut], tags=["sync"])
def list_runs(
    source: str | None = None,
    state: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[RunOut]:
    stmt = select(SyncRun).order_by(SyncRun.id.desc()).limit(limit)
    if source:
        stmt = stmt.join(Source, SyncRun.source_id == Source.id).where(
            Source.key == source
        )
    if state:
        stmt = stmt.where(SyncRun.state == state)
    return [_run_out(session, r) for r in session.scalars(stmt)]


@router.get("/sync/runs/{run_id}", response_model=RunOut, tags=["sync"])
def get_run(run_id: int, session: Session = Depends(get_session)) -> RunOut:
    run = session.get(SyncRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    return _run_out(session, run)


@router.post("/sync/runs", response_model=RunOut, status_code=202, tags=["sync"])
def trigger_run(
    source: str,
    job_type: str = "incremental",
    session: Session = Depends(get_session),
) -> RunOut:
    """Manually trigger a sync.

    Always `manual=True`: the daily cap exists to stop *scheduled* runs piling
    up, and the brief says operator-triggered runs are the exception.
    """
    try:
        run = scheduler.create_run(session, source, job_type, manual=True)
    except JobRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    scheduler.spawn(run.id)
    return _run_out(session, run)


@router.post("/sync/runs/{run_id}/cancel", tags=["sync"])
def cancel_run(run_id: int, session: Session = Depends(get_session)) -> dict:
    if not scheduler.request_cancel(session, run_id):
        raise HTTPException(status_code=409, detail="run is not cancellable")
    return {"cancelled": run_id}


# --- match review queue ------------------------------------------------------


@router.get("/matches", tags=["matching"])
def list_matches(
    state: str = "proposed",
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[dict]:
    """Proposed IMDb links awaiting a human.

    Anything the matcher was not certain about lands here rather than being
    guessed: a wrong link silently fuses two different dramas, which is far
    harder to notice later than a missing one.
    """
    rows = session.execute(
        select(MatchCandidate, Title)
        .join(Title, MatchCandidate.title_id == Title.id)
        .where(MatchCandidate.state == state)
        .order_by(MatchCandidate.score.desc())
        .limit(limit)
    ).all()
    out = []
    for match, title in rows:
        try:
            evidence = json.loads(match.evidence_json or "{}")
        except json.JSONDecodeError:
            evidence = {}
        out.append(
            {
                "id": match.id,
                "title_id": title.id,
                "title": title.primary_title,
                "year": title.year,
                "episode_count": title.episode_count,
                "target": match.target_external_id,
                "target_url": f"https://www.imdb.com/title/{match.target_external_id}/",
                "method": match.method,
                "score": match.score,
                "state": match.state,
                "evidence": evidence,
            }
        )
    return out


@router.post("/matches/{match_id}/confirm", tags=["matching"])
def confirm(
    match_id: int,
    tconst: str | None = Query(
        None, description="Choose a specific candidate instead of the matcher's guess"
    ),
    session: Session = Depends(get_session),
) -> dict:
    from app.jobs.imdb_sync import confirm_match

    if not confirm_match(session, match_id, tconst=tconst):
        raise HTTPException(
            status_code=409, detail="not a pending match, or the tconst is invalid"
        )
    match = session.get(MatchCandidate, match_id)
    return {"confirmed": match_id, "linked_to": match.target_external_id}


@router.post("/matches/{match_id}/reject", tags=["matching"])
def reject(match_id: int, session: Session = Depends(get_session)) -> dict:
    from app.jobs.imdb_sync import reject_match

    if not reject_match(session, match_id):
        raise HTTPException(status_code=409, detail="not a pending match")
    return {"rejected": match_id}


# --- stats -------------------------------------------------------------------


@router.get("/stats", tags=["ops"])
def stats(session: Session = Depends(get_session)) -> dict:
    """Coverage and footprint.

    Field coverage is the number worth watching: cast is genuinely absent on
    many titles, so knowing the real rate is what stops sparse data being
    mistaken for a bug.
    """
    # Tombstones are excluded from every count here. Counting them inflated
    # `titles` by 7.5% before deduplication existed, and a coverage percentage
    # whose denominator includes rows nobody can fetch is not a coverage figure.
    live = Title.merged_into_id.is_(None)
    total = (
        session.scalar(select(func.count()).select_from(Title).where(live)) or 0
    )

    def covered(column) -> int:
        return (
            session.scalar(
                select(func.count())
                .select_from(Title)
                .where(column.isnot(None), live)
            )
            or 0
        )

    coverage = {
        name: {"count": covered(col), "pct": round(100 * covered(col) / total, 1) if total else 0.0}
        for name, col in (
            ("synopsis", Title.synopsis),
            ("year", Title.year),
            ("episode_count", Title.episode_count),
            ("audience_rating", Title.audience_rating),
            ("first_paid_episode", Title.first_paid_episode),
        )
    }
    for label, stmt in (
        ("cast", select(func.count(func.distinct(TitlePerson.title_id)))),
        ("episodes", select(func.count(func.distinct(Episode.title_id)))),
        ("tags", select(func.count(func.distinct(TitleTag.title_id)))),
        ("aliases", select(func.count(func.distinct(TitleAlias.title_id)))),
    ):
        n = session.scalar(stmt) or 0
        coverage[label] = {
            "count": n,
            "pct": round(100 * n / total, 1) if total else 0.0,
        }

    return {
        "titles": total,
        "episodes": session.scalar(select(func.count()).select_from(Episode)),
        "people": session.scalar(
            select(func.count())
            .select_from(Person)
            .where(Person.merged_into_id.is_(None))
        ),
        "tags": session.scalar(select(func.count()).select_from(Tag)),
        "delisted": session.scalar(
            select(func.count())
            .select_from(Title)
            .where(Title.delisted_at.isnot(None), live)
        ),
        "merged": session.scalar(
            select(func.count())
            .select_from(Title)
            .where(Title.merged_into_id.isnot(None))
        ),
        "merges_pending_review": session.scalar(
            select(func.count())
            .select_from(TitleMerge)
            .where(TitleMerge.state == MergeState.PROPOSED)
        ),
        "coverage": coverage,
        "by_studio": {
            key: count
            for key, count in session.execute(
                select(Source.key, func.count(TitleSource.id))
                .join(TitleSource, TitleSource.source_id == Source.id)
                .group_by(Source.key)
            ).all()
        },
    }
