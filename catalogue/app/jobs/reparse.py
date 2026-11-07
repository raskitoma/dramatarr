"""Re-run parsers over the raw archive. No network at all.

This is the payoff for archiving every response before parsing it. When a
parser is wrong -- and it will be, because these payloads have undocumented
edge cases -- the fix costs zero requests instead of a full re-crawl.

Listing and detail responses are BOTH replayed and merged per title, in that
order. Replaying only details would silently drop every listing-only field:
DramaBox publishes ratings, status and its free flag on the browse page and
nowhere else.
"""

from __future__ import annotations

import logging
from types import ModuleType

from sqlalchemy.orm import Session

from app.fetch.archive import Archive
from app.ingest import ensure_source, ingest_record
from app.models import ParseQuarantine
from app.search import reindex
from app.sources.base import SourceRecord, merge_records

log = logging.getLogger(__name__)


def run_reparse(
    module: ModuleType, session: Session, archive: Archive, ctx=None
) -> dict:
    stats = {
        "scanned": 0,
        "listings": 0,
        "details": 0,
        "reparsed": 0,
        "created": 0,
        "updated": 0,
        "failed": 0,
    }

    source, studio = ensure_source(
        session,
        module.KEY,
        module.STUDIO_NAME,
        module.BASE_URL,
        module.WATCH_URL_TEMPLATE,
    )

    records: dict[str, SourceRecord] = {}
    urls = archive.iter_urls()
    stats["scanned"] = len(urls)

    def quarantine(url: str, sha: str | None, exc: Exception) -> None:
        log.warning("reparse failed for %s: %s", url, exc)
        session.add(
            ParseQuarantine(
                source_id=source.id,
                raw_sha256=sha,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        stats["failed"] += 1

    # Listings first: they seed the record, including fields details lack.
    for url in urls:
        if not getattr(module, "is_listing_url", lambda _u: False)(url):
            continue
        doc = archive.get(url)
        if doc is None:
            continue
        try:
            parsed = module.parse_listing(doc)
        except Exception as exc:  # noqa: BLE001
            quarantine(url, doc.sha256, exc)
            continue
        stats["listings"] += 1
        for record in parsed:
            records[record.external_id] = record

    # Details overlay, contributing cast and episodes without blanking the rest.
    for url in urls:
        if not module.is_detail_url(url):
            continue
        doc = archive.get(url)
        if doc is None:
            continue
        try:
            detail = module.parse_detail(doc)
        except Exception as exc:  # noqa: BLE001
            quarantine(url, doc.sha256, exc)
            continue
        stats["details"] += 1
        existing = records.get(detail.external_id)
        records[detail.external_id] = (
            merge_records(existing, detail) if existing else detail
        )

    total = len(records)
    for record in records.values():
        # Reparse is fast but not instant over a full archive; without this it
        # ignored cancellation entirely and reported zero progress throughout.
        if ctx is not None and ctx.cancelled():
            break
        stats["reparsed"] += 1
        if ctx is not None:
            ctx.progress(stats["reparsed"], total, "reparse")
        if ingest_record(session, source, studio, record):
            stats["created"] += 1
        else:
            stats["updated"] += 1

    stats["indexed"] = reindex(session)
    session.commit()
    return stats
