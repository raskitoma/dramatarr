"""Write parsed source records into the database.

The governing requirement is idempotency: running a sync twice must write
nothing the second time. That is why every child table has a uniqueness rule
and why `Title` fields are compared before assignment -- an unconditional
assignment would fire `onupdate` and churn `updated_at` on every run, which
would in turn make `?updated_since=` report the entire catalogue as changed
every day.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.precedence import PLATFORM, RESOLVED_FIELDS, Contribution, resolve

from app.models import (
    Episode,
    Person,
    PersonSource,
    Rating,
    Source,
    Studio,
    Tag,
    Title,
    TitleAlias,
    TitleChange,
    TitlePerson,
    TitleSource,
    TitleTag,
)
from app.dedup import Candidate, Verdict, classify, match_key
from app.normalize import normalize_name, normalize_title
from app.sources.base import SourceRecord

def ensure_source(
    session: Session,
    key: str,
    studio_name: str,
    base_url: str,
    watch_url_template: str,
    kind: str = PLATFORM,
) -> tuple[Source, Studio]:
    config = json.dumps({"kind": kind})
    source = session.scalar(select(Source).where(Source.key == key))
    if source is None:
        source = Source(
            key=key,
            base_url=base_url,
            watch_url_template=watch_url_template,
            config_json=config,
        )
        session.add(source)
        session.flush()
    else:
        if source.watch_url_template != watch_url_template:
            source.watch_url_template = watch_url_template
        if source.config_json != config:
            source.config_json = config

    studio = session.scalar(select(Studio).where(Studio.source_id == source.id))
    if studio is None:
        studio = Studio(
            source_id=source.id,
            name=studio_name,
            slug=key,
            homepage_url=base_url,
        )
        session.add(studio)
        session.flush()
    return source, studio


def _get_or_create_tag(session: Session, namespace: str, value: str) -> Tag:
    tag = session.scalar(
        select(Tag).where(Tag.namespace == namespace, Tag.value == value)
    )
    if tag is None:
        tag = Tag(namespace=namespace, value=value)
        session.add(tag)
        session.flush()
    return tag


def record_values(record: SourceRecord) -> dict:
    """The scalar fields precedence resolves. Relations live in their own
    tables and are unioned rather than resolved."""
    return {
        "primary_title": record.title or None,
        "year": record.year,
        "synopsis": record.synopsis,
        "episode_count": record.episode_count,
        "first_paid_episode": record.first_paid_episode,
        "audience_rating": record.audience_rating,
        "completed": record.completed,
        # Not a resolved field -- carried so IMDb matching can skip entirely
        # where a platform already told us the answer. ReelShort publishes
        # actor_info.imdb_id on some titles.
        "imdb_tconst": record.imdb_tconst,
    }


def _resolve_title(session: Session, title: Title, source_id: int) -> bool:
    """Recompute the canonical Title from ALL of its sources.

    Deliberately not "apply this record's values". Reading every contribution
    each time makes the result independent of sync order -- otherwise the same
    data would resolve differently depending on which source synced last.
    """
    contributions: list[Contribution] = []
    rows = session.execute(
        select(TitleSource, Source)
        .join(Source, TitleSource.source_id == Source.id)
        .where(TitleSource.title_id == title.id)
    ).all()
    for link, source in rows:
        try:
            values = json.loads(link.parsed_json or "{}")
        except json.JSONDecodeError:
            values = {}
        contributions.append(
            Contribution(
                source_key=source.key,
                kind=json.loads(source.config_json or "{}").get("kind", PLATFORM),
                first_seen=link.first_seen,
                values=values,
            )
        )

    resolved = resolve(contributions)

    changed = False
    for field_name in RESOLVED_FIELDS:
        new = resolved.get(field_name)
        if new is None:
            continue  # absent in every source is not the same as cleared
        old = getattr(title, field_name)
        if old == new:
            continue
        setattr(title, field_name, new)
        changed = True
        if old is not None:
            session.add(
                TitleChange(
                    title_id=title.id,
                    source_id=source_id,
                    field=field_name,
                    old_value=str(old),
                    new_value=str(new),
                )
            )

    if changed and title.primary_title:
        title.normalized_title = normalize_title(title.primary_title)
        # Must move with normalized_title or the identity index goes stale and the
        # next sync stops recognising a title it already merged.
        title.identity_key = match_key(title.primary_title)
    return changed


def _sync_aliases(session: Session, title: Title, record: SourceRecord, source_id: int) -> None:
    existing = {
        a.normalized_alias
        for a in session.scalars(
            select(TitleAlias).where(
                TitleAlias.title_id == title.id, TitleAlias.source_id == source_id
            )
        )
    }
    title_norm = normalize_title(record.title)
    for alias in record.aliases:
        norm = normalize_title(alias)
        if not norm or norm == title_norm or norm in existing:
            continue
        existing.add(norm)
        session.add(
            TitleAlias(
                title_id=title.id,
                alias=alias,
                normalized_alias=norm,
                source_id=source_id,
            )
        )


def _sync_tags(session: Session, title: Title, record: SourceRecord, source_id: int) -> None:
    existing = {
        tt.tag_id
        for tt in session.scalars(
            select(TitleTag).where(
                TitleTag.title_id == title.id, TitleTag.source_id == source_id
            )
        )
    }
    for namespace, value in record.tags:
        if not value:
            continue
        tag = _get_or_create_tag(session, namespace, value)
        if tag.id in existing:
            continue
        existing.add(tag.id)
        session.add(TitleTag(title_id=title.id, tag_id=tag.id, source_id=source_id))


def _sync_episodes(
    session: Session, title: Title, record: SourceRecord, link: TitleSource
) -> None:
    """Write this EDITION's episodes.

    Takes the link rather than a bare `source_id` because one platform can list
    the same show twice -- a dubbed twin, a republish, a Spanish catalogue -- and
    those rows share both a title_id and a source_id. Only the link says which
    listing an episode came from, and language is a property of the link.
    """
    if not record.episodes:
        return
    source_id = link.source_id
    existing = {
        e.external_id: e
        for e in session.scalars(
            select(Episode).where(
                Episode.source_id == source_id, Episode.title_id == title.id
            )
        )
    }
    for ep in record.episodes:
        row = existing.get(ep.external_id)
        if row is None:
            session.add(
                Episode(
                    title_id=title.id,
                    source_id=source_id,
                    title_source_id=link.id,
                    external_id=ep.external_id,
                    number=ep.number,
                    name=ep.name,
                    duration_ms=ep.duration_ms,
                    is_free=ep.is_free,
                    published_at=ep.published_at,
                )
            )
            continue
        # Backfill the edition on rows written before the column existed. Only
        # when unset: this external_id came from THIS link's payload, so the
        # attribution is the source's own. An id already pointing at a different
        # link means two editions published the same episode id and the unique
        # constraint collapsed them into one row -- leave that alone rather than
        # letting the last sync to run decide whose episode it is.
        if row.title_source_id is None:
            row.title_source_id = link.id
        # Episodes can flip from free to paid as a title ages off promotion.
        if row.is_free != ep.is_free:
            row.is_free = ep.is_free
        if ep.duration_ms and row.duration_ms != ep.duration_ms:
            row.duration_ms = ep.duration_ms
        # `number` was previously never updated, so a parser that had numbered
        # a source's episodes wrongly could not be repaired by re-ingesting --
        # the corrected value was computed and then discarded. A source can also
        # legitimately renumber when it inserts or withdraws an episode.
        if ep.number is not None and row.number != ep.number:
            row.number = ep.number


def _sync_rating(session: Session, title: Title, record: SourceRecord, source_id: int) -> None:
    if record.rating is None:
        return
    row = session.scalar(
        select(Rating).where(Rating.title_id == title.id, Rating.source_id == source_id)
    )
    if row is None:
        session.add(
            Rating(
                title_id=title.id,
                source_id=source_id,
                value=record.rating,
                votes=record.rating_votes,
                is_editorial=record.rating_is_editorial,
            )
        )
        return

    # `uq_rating_title_source` allows exactly one row per (title, source), but a
    # title can hold TWO links from one source once same-platform duplicates are
    # merged -- DramaBox's `(DUBBED)` twins are the common case. Both records then
    # write here, and an unconditional overwrite would flip `value` according to
    # whichever synced last, every single night. Keep the better-established
    # figure instead, which is the same rule the merge itself applies, so the
    # outcome does not depend on sync order.
    incoming_votes = record.rating_votes or 0
    if incoming_votes < (row.votes or 0):
        return
    if row.value != record.rating:
        row.value = record.rating
    if record.rating_votes is not None and row.votes != record.rating_votes:
        row.votes = record.rating_votes


def _avatar_key(url: str | None) -> str | None:
    """An avatar URL with its cache-buster removed.

    DramaBox appends `?time=<epoch-ms>` to every headshot, regenerated per
    response: 1,956 of the archived URLs carry one, and the 8 performers that
    ever showed two URLs collapse to a single one once it is dropped. Comparing
    the raw string would read as "the photo changed" on every sync, clear
    `avatar_image_id` and re-download the entire roster nightly.

    Verified safe to drop rather than merely ignore: the bare URL and the
    timestamped one return byte-identical images, so the stored form stays
    fetchable. A source that ever signs its image URLs would need this revisited
    -- a signature is not a cache-buster.
    """
    if not url:
        return None
    return url.split("?", 1)[0] or None


def _sync_cast(session: Session, title: Title, record: SourceRecord, source_id: int) -> None:
    for credit in record.cast:
        link = session.scalar(
            select(PersonSource).where(
                PersonSource.source_id == source_id,
                PersonSource.external_id == credit.external_id,
            )
        )
        if link is None:
            person = Person(name=credit.name, normalized_name=normalize_name(credit.name))
            session.add(person)
            session.flush()
            link = PersonSource(
                person_id=person.id,
                source_id=source_id,
                external_id=credit.external_id,
                avatar_url=_avatar_key(credit.avatar_url),
            )
            session.add(link)
            session.flush()
        elif credit.avatar_url:
            incoming = _avatar_key(credit.avatar_url)
            if _avatar_key(link.avatar_url) != incoming:
                # Compared before assigning, like every other field here: an
                # unconditional write churns the row on every sync. A genuinely
                # different photo means the stored image is stale, so it is
                # dropped and avatar_fetch picks the new one up.
                link.avatar_url = incoming
                link.avatar_image_id = None

        exists = session.scalar(
            select(TitlePerson).where(
                TitlePerson.title_id == title.id,
                TitlePerson.person_id == link.person_id,
                TitlePerson.role == credit.role,
                TitlePerson.source_id == source_id,
            )
        )
        if exists is None:
            session.add(
                TitlePerson(
                    title_id=title.id,
                    person_id=link.person_id,
                    role=credit.role,
                    billing_order=credit.billing_order,
                    source_id=source_id,
                )
            )


def _sync_localized_title(
    session: Session, title: Title, record: SourceRecord, source_id: int
) -> None:
    """Record a localised edition's own title as an alternate title.

    The canonical `primary_title` stays whatever precedence picks -- in practice
    the edition seen first, which for an existing catalogue is the English one.
    The other languages become alternate titles rather than competing for the
    headline, so a reader searching "Atada por amor" finds the show while the
    entry itself keeps reading as one show rather than three.

    `kind` and `region` on `title_alias` existed unused until now and are exactly
    the right shape for this, so no new table is involved.
    """
    if not record.lang or not record.title:
        return
    normalized = normalize_title(record.title)
    if not normalized or normalized == normalize_title(title.primary_title or ""):
        return
    exists = session.scalar(
        select(TitleAlias).where(
            TitleAlias.title_id == title.id,
            TitleAlias.normalized_alias == normalized,
            TitleAlias.source_id == source_id,
        )
    )
    if exists is not None:
        # A row may predate the columns, or the edition may have been relabelled.
        if exists.kind != "localized":
            exists.kind = "localized"
        if exists.region != record.lang:
            exists.region = record.lang
        return
    session.add(
        TitleAlias(
            title_id=title.id,
            alias=record.title,
            normalized_alias=normalized,
            kind="localized",
            region=record.lang,
            source_id=source_id,
        )
    )


def _find_locale_sibling(
    session: Session, record: SourceRecord, source_id: int
) -> Title | None:
    """The same show in another language, joined on a key the SOURCE publishes.

    ReelShort runs a separate catalogue per language and gives one show a
    different `book_id` in each, so a Spanish edition looks like an unrelated
    title. `t_book_id` carries the edition prefix and a shared serial, and that
    serial is what `SourceRecord.locale_key` holds.

    Deliberately an exact equality on an indexed column and nothing else. The
    titles genuinely differ between editions -- "Bound by Love" and "Atada por
    amor" share no words -- so nothing here could be recovered by comparing
    names, which is exactly why the source's own identifier is the only
    acceptable basis.
    """
    if not record.locale_key:
        return None
    link = session.scalar(
        select(TitleSource)
        .join(Title, Title.id == TitleSource.title_id)
        .where(
            TitleSource.source_id == source_id,
            TitleSource.locale_key == record.locale_key,
            TitleSource.external_id != record.external_id,
            Title.merged_into_id.is_(None),
        )
        .order_by(TitleSource.first_seen, TitleSource.id)
    )
    return session.get(Title, link.title_id) if link is not None else None


def _find_same_series(
    session: Session, record: SourceRecord, source_id: int
) -> Title | None:
    """Has another source already given us this show?

    The prevention half of deduplication. Without it a merge lasts exactly until
    the next sync: this function is the only thing standing between a merged
    catalogue and 763 duplicates reappearing overnight.

    Only ever consults an exact indexed key plus confirming evidence, the same
    discipline as `app/dedup.py` -- which owns the rule, so the two cannot drift.
    A `None` return is always safe: it creates a separate title that the nightly
    `dedup` job will find and queue for review.
    """
    key = match_key(record.title)
    if not key:
        return None

    rows = session.scalars(
        select(Title).where(
            Title.identity_key == key, Title.merged_into_id.is_(None)
        )
    ).all()
    if not rows:
        return None

    incoming = Candidate(
        title_id=0,
        primary_title=record.title,
        year=record.year,
        episode_count=record.episode_count,
        source_keys=frozenset(),
    )
    for existing in rows:
        # A title already carrying a link from THIS source is a different show
        # that happens to share a name -- not this record's twin. Merging into it
        # would fuse two of one platform's own titles on nothing but the name.
        if session.scalar(
            select(TitleSource.id).where(
                TitleSource.title_id == existing.id,
                TitleSource.source_id == source_id,
            )
        ):
            continue
        candidate = Candidate(
            title_id=existing.id,
            primary_title=existing.primary_title,
            year=existing.year,
            episode_count=existing.episode_count,
            source_keys=frozenset(),
        )
        if classify([incoming, candidate], key=key, key_kind="title").verdict is Verdict.AUTO:
            return existing
    return None


def ingest_record(
    session: Session, source: Source, studio: Studio, record: SourceRecord
) -> bool:
    """Upsert one record. Returns True if a new Title was created."""
    now = datetime.now(UTC)
    link = session.scalar(
        select(TitleSource).where(
            TitleSource.source_id == source.id,
            TitleSource.external_id == record.external_id,
        )
    )

    created = False
    if link is None:
        # Before minting a new Title, ask whether another source already has this
        # show. Attaching a second link to the existing row is what makes one
        # series one title, and it is order-independent: whichever platform syncs
        # first, the second one joins it.
        # A localised edition first: it is an exact, source-published key, so it
        # is both cheaper and safer than the title-derived one below.
        title = _find_locale_sibling(session, record, source.id)
        if title is None:
            title = _find_same_series(session, record, source.id)
        if title is None:
            title = Title(
                primary_title=record.title,
                normalized_title=normalize_title(record.title),
                identity_key=match_key(record.title),
            )
            session.add(title)
            session.flush()
            created = True
        link = TitleSource(
            title_id=title.id,
            source_id=source.id,
            studio_id=studio.id,
            external_id=record.external_id,
            locale_key=record.locale_key,
            lang=record.lang,
        )
        session.add(link)
    else:
        title = session.get(Title, link.title_id)
        # Backfill on rows that predate these columns, and follow a source that
        # relabels an edition.
        if record.locale_key and link.locale_key != record.locale_key:
            link.locale_key = record.locale_key
        if record.lang and link.lang != record.lang:
            link.lang = record.lang

    # Record this source's view first, then resolve the canonical row from all
    # of them. Writing before resolving is what makes the result independent of
    # which source synced last.
    values = json.dumps(record_values(record), default=str)
    if link.parsed_json != values:
        link.parsed_json = values
    session.flush()
    _resolve_title(session, title, source.id)

    # last_seen moves every run -- that is how delisting is detected -- but it
    # lives on TitleSource, so it does not disturb Title.updated_at.
    link.last_seen = now
    link.delisted_at = None
    if record.url and link.url != record.url:
        link.url = record.url
    if record.share_url and link.share_url != record.share_url:
        link.share_url = record.share_url
    if record.raw_sha256 and link.raw_sha256 != record.raw_sha256:
        link.raw_sha256 = record.raw_sha256
    if record.poster_url and link.poster_url != record.poster_url:
        link.poster_url = record.poster_url

    _sync_aliases(session, title, record, source.id)
    _sync_localized_title(session, title, record, source.id)
    _sync_tags(session, title, record, source.id)
    _sync_episodes(session, title, record, link)
    _sync_rating(session, title, record, source.id)
    _sync_cast(session, title, record, source.id)
    return created
