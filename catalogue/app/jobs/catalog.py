"""Catalogue sync: discover -> detail -> merge -> ingest."""

from __future__ import annotations

import logging
from types import ModuleType

from sqlalchemy.orm import Session

from app.ingest import ensure_source, ingest_record
from app.jobs.context import RunContext
from app.models import ParseQuarantine
from app.search import reindex
from app.sources.base import Mode, merge_records

log = logging.getLogger(__name__)


def run_catalog_sync(
    module: ModuleType,
    session: Session,
    ctx: RunContext,
    mode: Mode = Mode.FULL,
    *,
    fetch_details: bool = True,
) -> dict:
    """Run one catalogue pass for a source. Returns run statistics."""
    stats = {
        "discovered": 0,
        "created": 0,
        "updated": 0,
        "details_fetched": 0,
        "quarantined": 0,
    }

    module.preflight(ctx)
    source, studio = ensure_source(
        session,
        module.KEY,
        module.STUDIO_NAME,
        module.BASE_URL,
        module.WATCH_URL_TEMPLATE,
    )

    for ref in module.discover(ctx, mode):
        if ctx.cancelled():
            break
        stats["discovered"] += 1

        record = module.record_from_hint(ref.hint) if ref.hint else None

        if fetch_details:
            try:
                doc = module.fetch_detail(ctx, ref)
                detail = module.parse_detail(doc)
                stats["details_fetched"] += 1
                record = merge_records(record, detail) if record else detail
            except Exception as exc:
                # A bad record must not kill the run. Quarantine it with its
                # hash; fix the parser, then reparse over the archive.
                log.warning("detail parse failed for %s: %s", ref.external_id, exc)
                session.add(
                    ParseQuarantine(
                        source_id=source.id,
                        external_id=ref.external_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                stats["quarantined"] += 1

        if record is None:
            continue

        try:
            if ingest_record(session, source, studio, record):
                stats["created"] += 1
            else:
                stats["updated"] += 1
        except Exception as exc:  # noqa: BLE001 - one title must not end the run
            # The parse quarantine above already covered "the payload would not
            # parse". This covers "the payload parsed and the WRITE failed",
            # which was fatal until a real crawl found it: NetShort publishes
            # some titles with episode 1 listed twice, the second INSERT violated
            # uq_episode_external, and the exception unwound past the loop and
            # killed a 15,000-title run 2,251 titles in. The rows already
            # committed survived, but every remaining title was abandoned and a
            # retry would die on the same one.
            #
            # A failed write leaves the session poisoned, so it must be rolled
            # back before anything else can be recorded -- including the
            # quarantine row itself.
            session.rollback()
            log.warning("ingest failed for %s: %s", ref.external_id, exc)
            session.add(
                ParseQuarantine(
                    source_id=source.id,
                    external_id=ref.external_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            stats["quarantined"] += 1
            session.commit()
            continue

        # Only a capped FULL crawl knows its size up front. An incremental run
        # discovers as it goes, and reporting max_items as its total produced
        # nonsense like "131 / 50" with no ETA.
        if mode is Mode.FULL:
            # A cap is an exact total; otherwise fall back to the adapter's
            # estimate so an uncapped crawl still has a denominator.
            total = ctx.max_items or ctx.state.get("catalogue_estimate")
        else:
            total = None
        ctx.progress(stats["discovered"], total, phase="ingest")
        # Commit per item, NOT in batches of 25. Batching looks cheaper but the
        # detail fetch happens inside the loop, so a batched transaction holds
        # SQLite's single write lock across 25 network round-trips -- tens of
        # seconds. That starved a concurrent poster job into
        # "database is locked". Under WAL with synchronous=NORMAL these commits
        # are cheap; a held lock is not.
        session.commit()

    session.commit()
    # Rebuilt after the batch rather than maintained by triggers: it takes well
    # under a second at this scale and removes a class of "the trigger missed a
    # row" bugs.
    stats["indexed"] = reindex(session)
    # Verdict tally per run. A non-zero `challenge` count on ReelShort means
    # pacing is wrong, and is the thing to watch rather than wall-clock time.
    stats["verdicts"] = dict(ctx.fetcher.verdicts)

    # A FULL crawl that discovered nothing is broken, not finished. There is no
    # legitimate reading of "walk the entire catalogue" that ends at zero: an
    # empty INCREMENTAL means "nothing new since the watermark", but an empty
    # FULL means discovery never reached the catalogue at all.
    #
    # This exists because the silent version cost three separate incidents in two
    # days, each one a correct feature sitting inert behind no data, and each
    # invisible because the run reported `succeeded`. The last was NetShort
    # renaming its sitemaps out from under a substring filter: discovery matched
    # nothing, yielded nothing, and the dashboard showed a clean green run.
    # Failing loudly is what makes "the site changed shape" look different from
    # "there was nothing new".
    if mode is Mode.FULL and not stats["discovered"]:
        raise RuntimeError(
            "full_catalog discovered 0 refs -- discovery is blind, not the "
            f"catalogue empty (verdicts: {stats['verdicts']})"
        )
    return stats
