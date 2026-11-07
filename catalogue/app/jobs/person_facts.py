"""Structured facts about people, read from a source's own performer pages.

This exists because a recorded conclusion was wrong. The project had measured
that DramaBox publishes no biographical data, on the evidence that
`bookInfo.performerList` carries exactly performerId, performerName,
performerFormatName, performerAvatar and videoCount. That is true of
`performerList`, and false of the source: `/name/{performerId}` is a page in its
own right, and it is keyed on the SAME id already stored as
`PersonSource.external_id`. So this walks ids we hold rather than probing a
19,000-wide id space, and never matches on a name.

What arrives is Chinese prose, and only in the zhHans locale -- `/en/name/{id}`
returns the identical structure with `introduction` empty. This index's reader
does not read Chinese, so the prose is NOT stored. It is mined for the facts
inside it, which are language-neutral once mapped: gender, height, weight and
star sign. Measured on a random sample of 70 people missing a bio: 48 have an
introduction, of which 45 give gender, 26 height, 22 weight and 22 a star sign.

The Han name IS kept, as an alias. It never becomes `person.name`, because the
romanised form is the only one this index's reader can type and search on.
"""

from __future__ import annotations

import logging
from types import ModuleType

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Person, PersonAlias, PersonSource, Source
from app.normalize import normalize_name

log = logging.getLogger(__name__)

#: Which fields this job is allowed to write onto `person`. Kept explicit so a
#: parser gaining a new key cannot silently start populating columns.
FACTS = ("gender", "height_cm", "weight_kg", "zodiac")


def run_person_facts(
    module: ModuleType, session: Session, ctx, *, limit: int = 0
) -> dict:
    """Fetch performer pages for people known to `module`, and store the facts."""
    stats = {
        "considered": 0, "fetched": 0, "failed": 0,
        "facts_set": 0, "people_enriched": 0, "aliases_added": 0, "avatars": 0,
    }

    parse = getattr(module, "parse_performer", None)
    fetch = getattr(module, "fetch_performer", None)
    if parse is None or fetch is None:
        stats["note"] = f"{module.KEY} publishes no performer detail pages"
        return stats

    # DramaBox addresses its JSON through a date-stamped buildId that rotates on
    # every deploy, and only `preflight` reads it. `app/jobs/catalog.py` is the
    # sole other caller, so without this a facts run outside a catalogue sync
    # dies on a missing ctx.state["build_id"] -- or worse, uses a stale one and
    # 404s on every page while reporting them as ordinary failures.
    preflight = getattr(module, "preflight", None)
    if preflight is not None:
        preflight(ctx)

    source = session.scalar(select(Source).where(Source.key == module.KEY))
    if source is None:
        stats["note"] = f"{module.KEY} is not a registered source"
        return stats

    # Only people who are still missing at least one fact, so a re-run is cheap
    # and converges rather than re-fetching the whole roster every night.
    pending = session.execute(
        select(PersonSource.external_id, Person)
        .join(Person, Person.id == PersonSource.person_id)
        .where(
            PersonSource.source_id == source.id,
            Person.merged_into_id.is_(None),
        )
    ).all()
    pending = [
        (external_id, person)
        for external_id, person in pending
        if any(getattr(person, field) is None for field in FACTS)
    ]
    if limit:
        pending = pending[:limit]
    stats["considered"] = len(pending)

    for index, (external_id, person) in enumerate(pending, start=1):
        if ctx.cancelled():
            break
        ctx.progress(index, len(pending), "facts")
        try:
            parsed = parse(fetch(ctx, external_id))
        except Exception as exc:  # noqa: BLE001 - one bad page is not fatal
            log.warning("performer page failed for %s: %s", external_id, exc)
            stats["failed"] += 1
            continue
        stats["fetched"] += 1

        gained = 0
        for field in FACTS:
            value = parsed.get(field)
            # Never overwrite: a fact already recorded came from somewhere, and
            # a later page that omits the field must not blank it.
            if value is not None and getattr(person, field) is None:
                setattr(person, field, value)
                gained += 1
        if gained:
            stats["facts_set"] += gained
            stats["people_enriched"] += 1

        if _record_alias(session, person.id, source.id, parsed.get("han_name")):
            stats["aliases_added"] += 1

        if index % 20 == 0:
            session.commit()

    session.commit()
    return stats


def _record_alias(
    session: Session, person_id: int, source_id: int, alias: str | None
) -> bool:
    """Store an alternate name for display. Never as the searchable name.

    Tagged `zh-Hans` rather than left unlabelled so a consumer can decide what
    to render instead of sniffing the characters.
    """
    if not alias:
        return False
    normalized = normalize_name(alias)
    if not normalized:
        return False
    exists = session.scalar(
        select(PersonAlias).where(
            PersonAlias.person_id == person_id,
            PersonAlias.normalized_alias == normalized,
            PersonAlias.source_id == source_id,
        )
    )
    if exists is not None:
        return False
    session.add(
        PersonAlias(
            person_id=person_id,
            alias=alias,
            normalized_alias=normalized,
            lang="zh-Hans",
            source_id=source_id,
        )
    )
    return True
