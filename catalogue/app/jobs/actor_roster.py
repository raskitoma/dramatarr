"""Actor roster ingestion.

Cast scraped from title detail pages is patchy -- roughly 20% of ReelShort
titles carry credits at all -- so the curated roster is the other half of the
picture: it names people the detail pages never mention, and its per-actor
filmographies link them back to titles we already hold.

The join is deliberate rather than fuzzy. A person's `external_id` for this
source is their fandom URL, which is exactly what `actor_info.actors[].
inside_url` contains on detail pages. So roster rows and cast rows merge on an
identifier both sides publish, instead of on name similarity -- which would be
its own disaster in a catalogue where several actors share a name.

DramaBox has no equivalent: it publishes no actor index, and its `/name/{id}`
space would need roughly 19,000 probes to enumerate. Its roster comes free from
`performerList` on detail records instead, during the ordinary sync.
"""

from __future__ import annotations

import logging
from types import ModuleType

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest import ensure_source
from app.models import Person, PersonSource, TitlePerson, TitleSource
from app.normalize import normalize_name

log = logging.getLogger(__name__)


def run_actor_roster(
    module: ModuleType, session: Session, ctx, *, fetch_profiles: bool = True
) -> dict:
    stats = {
        "listed": 0,
        "people_created": 0,
        "people_linked": 0,
        "profiles_fetched": 0,
        "bios": 0,
        "avatars": 0,
        "credits_created": 0,
        "unmatched_titles": 0,
    }

    # An adapter signals "no roster" either by omitting the attribute or by
    # returning None from it. Accept both rather than making every adapter
    # express it the same way.
    roster = getattr(module, "roster", None)
    refs = list(roster(ctx) or []) if roster is not None else []
    if not refs:
        stats["note"] = f"{module.KEY} publishes no actor roster"
        return stats

    source, _ = ensure_source(
        session,
        module.KEY,
        module.STUDIO_NAME,
        module.BASE_URL,
        module.WATCH_URL_TEMPLATE,
    )

    stats["listed"] = len(refs)

    # book_id -> our title id, so a filmography can be linked without guessing.
    known = {
        link.external_id: link.title_id
        for link in session.scalars(
            select(TitleSource).where(TitleSource.source_id == source.id)
        )
    }

    for index, ref in enumerate(refs, start=1):
        if ctx.cancelled():
            break
        ctx.progress(index, len(refs), "roster")

        person, created = _upsert_person(session, source.id, ref)
        if created:
            stats["people_created"] += 1
        else:
            stats["people_linked"] += 1

        if not fetch_profiles:
            continue

        try:
            doc = module.fetch_actor(ctx, ref)
            profile = module.parse_actor_page(doc)
        except Exception as exc:  # noqa: BLE001 - one bad profile is not fatal
            log.warning("actor page failed for %s: %s", ref.external_id, exc)
            continue

        stats["profiles_fetched"] += 1
        if profile.get("bio") and not person.bio:
            person.bio = profile["bio"]
            stats["bios"] += 1
        if _record_avatar(session, source.id, ref.external_id, profile.get("avatar_url")):
            stats["avatars"] += 1

        for book_id in profile.get("book_ids", []):
            title_id = known.get(book_id)
            if title_id is None:
                # A title we have not ingested yet. Not an error: the roster
                # covers the whole catalogue and we may be capped.
                stats["unmatched_titles"] += 1
                continue
            if _link_credit(session, title_id, person.id, source.id):
                stats["credits_created"] += 1

        if index % 20 == 0:
            session.commit()

    session.commit()
    stats.update(_backfill_missing_bios(module, session, ctx, source.id))
    session.commit()
    return stats


def _backfill_missing_bios(module: ModuleType, session: Session, ctx, source_id: int) -> dict:
    """Fetch profiles for people the ROSTER never lists.

    The curated A-Z page names ~229 actors, but detail pages credit more: cast
    rows carry `actor_info.actors[].inside_url`, which is a fandom URL and is
    stored as the person's external_id. Those people were created by the
    catalogue sync and never had a profile fetched, so they had no bio -- 137 of
    them, against 220 who did.

    Same join as everywhere else here: the fandom URL IS the identifier, so this
    walks stored ids rather than guessing at slugs (which are irregular enough
    that synthesising them was ruled out in recon).
    """
    from app.sources.base import PersonRef

    pending = session.execute(
        select(PersonSource.external_id, Person)
        .join(Person, Person.id == PersonSource.person_id)
        .where(
            PersonSource.source_id == source_id,
            PersonSource.external_id.like("http%"),
        )
    ).all()
    # A person who already has a bio can still be missing a face -- that is the
    # normal case here, because the bio came from this page and the headshot on
    # it was never read. Fetching only the bio-less ones would leave them.
    have_avatar = {
        row[0]
        for row in session.execute(
            select(PersonSource.external_id).where(
                PersonSource.source_id == source_id,
                PersonSource.avatar_url.isnot(None),
            )
        ).all()
    }
    pending = [
        (ext, person)
        for ext, person in pending
        if not (person.bio or "").strip() or ext not in have_avatar
    ]

    out = {"bio_backfill_considered": len(pending), "bio_backfill_bios": 0,
           "bio_backfill_avatars": 0, "bio_backfill_failed": 0}
    for index, (external_id, person) in enumerate(pending, start=1):
        if ctx.cancelled():
            break
        ctx.progress(index, len(pending), "bios")
        try:
            doc = module.fetch_actor(ctx, PersonRef(external_id=external_id,
                                                    name=person.name,
                                                    url=external_id))
            profile = module.parse_actor_page(doc)
        except Exception as exc:  # noqa: BLE001 - one bad profile is not fatal
            log.warning("bio backfill failed for %s: %s", external_id, exc)
            out["bio_backfill_failed"] += 1
            continue
        if profile.get("bio") and not (person.bio or "").strip():
            person.bio = profile["bio"]
            out["bio_backfill_bios"] += 1
        if _record_avatar(session, source_id, external_id, profile.get("avatar_url")):
            out["bio_backfill_avatars"] += 1
        if index % 20 == 0:
            session.commit()
    return out


def _record_avatar(
    session: Session, source_id: int, external_id: str, avatar_url: str | None
) -> bool:
    """Remember where this person's headshot lives, for `avatar_fetch` to collect.

    Only the URL is stored here. Downloading during a roster walk would put an
    image CDN on the critical path of the crawl, which is the same reason
    `TitleSource.poster_url` and DramaBox avatars are captured then fetched later.
    """
    if not avatar_url:
        return False
    link = session.scalar(
        select(PersonSource).where(
            PersonSource.source_id == source_id,
            PersonSource.external_id == external_id,
        )
    )
    # Never overwrite: a stored image was already fetched from some URL, and
    # rewriting the pointer would make the next run re-download the same face.
    if link is None or link.avatar_url or link.avatar_image_id is not None:
        return False
    link.avatar_url = avatar_url
    return True


def _upsert_person(session: Session, source_id: int, ref) -> tuple[Person, bool]:
    """Find or create the person behind a roster entry.

    Matching is on the fandom URL, which detail-page cast rows already use as
    their external_id -- so an actor scraped from a title and the same actor
    listed on the roster resolve to one row without name matching.
    """
    link = session.scalar(
        select(PersonSource).where(
            PersonSource.source_id == source_id,
            PersonSource.external_id == ref.external_id,
        )
    )
    if link is not None:
        person = session.get(Person, link.person_id)
        if ref.name and person.name != ref.name:
            person.name = ref.name
            person.normalized_name = normalize_name(ref.name)
        if ref.url and not link.url:
            link.url = ref.url
        return person, False

    person = Person(name=ref.name or "", normalized_name=normalize_name(ref.name or ""))
    session.add(person)
    session.flush()
    session.add(
        PersonSource(
            person_id=person.id,
            source_id=source_id,
            external_id=ref.external_id,
            url=ref.url,
        )
    )
    session.flush()
    return person, True


def _link_credit(session: Session, title_id: int, person_id: int, source_id: int) -> bool:
    exists = session.scalar(
        select(TitlePerson).where(
            TitlePerson.title_id == title_id,
            TitlePerson.person_id == person_id,
            TitlePerson.role == "actor",
            TitlePerson.source_id == source_id,
        )
    )
    if exists is not None:
        return False
    session.add(
        TitlePerson(
            title_id=title_id, person_id=person_id, role="actor", source_id=source_id
        )
    )
    return True
