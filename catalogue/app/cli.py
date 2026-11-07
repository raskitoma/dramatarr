"""Command line interface.

Sync commands land with the job engine in M5; for now this covers setup and
inspection.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import settings


def cmd_info(_args: argparse.Namespace) -> int:
    from sqlalchemy import inspect

    from app.db import engine

    print(f"database   {settings.db_url}")
    print(f"data dir   {settings.data_dir.resolve()}")
    print(f"timezone   {settings.tz}  (run_date today = {settings.today()})")
    print(f"max items  {settings.max_items or 'uncapped'}")
    print(f"api key    {'set' if settings.api_key else 'NOT SET'}")
    try:
        tables = sorted(inspect(engine).get_table_names())
        print(f"tables     {len(tables)}: {', '.join(tables)}")
    except Exception as exc:  # pragma: no cover - diagnostic path
        print(f"tables     unavailable ({exc})")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    from app.fetch.archive import Archive

    archive = Archive(settings.archive_dir, settings.archive_keep_versions)
    urls = archive.iter_urls()
    print(f"{len(urls)} archived responses in {archive.root}")
    for url in urls[: args.limit]:
        print(f"  {url}")
    return 0


from app.sources.registry import PLATFORM_KEYS

SOURCES = {k: f"app.sources.{k}" for k in PLATFORM_KEYS}


def cmd_sync(args: argparse.Namespace) -> int:
    import importlib
    import time

    from app.db import session_scope
    from app.fetch.archive import Archive
    from app.fetch.breaker import CircuitBreaker
    from app.fetch.client import Fetcher
    from app.fetch.ratelimit import RateLimiterRegistry
    from app.fetch.robots import RobotsCache
    from app.jobs.catalog import run_catalog_sync
    from app.jobs.context import RunContext
    from app.sources.base import Mode

    module = importlib.import_module(SOURCES[args.source])
    archive = Archive(settings.archive_dir, settings.archive_keep_versions)
    limiters = RateLimiterRegistry(module.RATE)
    robots = RobotsCache(settings.user_agent)
    breaker = CircuitBreaker(module.KEY)

    max_items = args.limit if args.limit is not None else settings.max_items
    started = time.monotonic()

    def show(done: int, total: int | None, phase: str | None) -> None:
        cap = total or "?"
        print(f"\r  {phase or 'working'}: {done}/{cap}", end="", flush=True)

    with Fetcher(settings.user_agent, archive, limiters, robots, breaker) as fetcher:
        ctx = RunContext(
            fetcher=fetcher, max_items=max_items, watermark=args.since, on_progress=show
        )
        with session_scope() as session:
            stats = run_catalog_sync(
                module,
                session,
                ctx,
                Mode.INCREMENTAL if args.incremental else Mode.FULL,
                fetch_details=not args.no_details,
            )

    elapsed = time.monotonic() - started
    print(f"\r{' ' * 40}\r{args.source} sync finished in {elapsed:.1f}s")
    for key, value in stats.items():
        print(f"  {key:18} {value}")
    if ctx.new_watermark:
        print(f"  {'watermark':18} {ctx.new_watermark}")
    return 0


def cmd_reparse(args: argparse.Namespace) -> int:
    import importlib

    from app.db import session_scope
    from app.fetch.archive import Archive
    from app.jobs.reparse import run_reparse

    module = importlib.import_module(SOURCES[args.source])
    archive = Archive(settings.archive_dir, settings.archive_keep_versions)
    with session_scope() as session:
        stats = run_reparse(module, session, archive)
    for key, value in stats.items():
        print(f"  {key:12} {value}")
    return 0


def cmd_roster(args: argparse.Namespace) -> int:
    import importlib

    from app.db import session_scope
    from app.fetch.archive import Archive
    from app.fetch.client import Fetcher
    from app.fetch.ratelimit import RateLimiterRegistry
    from app.fetch.robots import RobotsCache
    from app.jobs.actor_roster import run_actor_roster
    from app.jobs.context import RunContext

    module = importlib.import_module(SOURCES[args.source])
    archive = Archive(settings.archive_dir, settings.archive_keep_versions)

    def show(done, total, phase):
        print(f"\r  {phase}: {done}/{total}", end="", flush=True)

    with Fetcher(
        settings.user_agent, archive, RateLimiterRegistry(module.RATE),
        RobotsCache(settings.user_agent),
    ) as fetcher:
        ctx = RunContext(fetcher=fetcher, on_progress=show)
        with session_scope() as session:
            stats = run_actor_roster(
                module, session, ctx, fetch_profiles=not args.no_profiles
            )
    print("\r" + " " * 40)
    for k, v in stats.items():
        print(f"  {k:18} {v}")
    return 0


def cmd_migrate_engine(args: argparse.Namespace) -> int:
    from app.jobs.migrate_engine import copy_database, verify

    source = settings.db_url
    print(f"  from {source}")
    print(f"  to   {args.target.split('@')[-1]}")

    if not args.verify_only:
        def show(table, done, total):
            print(f"\r    {table:20} {done}/{total}   ", end="", flush=True)

        stats = copy_database(source, args.target, progress=show)
        print("\r" + " " * 50)
        for table, n in stats.items():
            if n:
                print(f"    {table:20} {n}")

    report = verify(source, args.target)
    if report["mismatches"]:
        print("\n  ROW COUNT MISMATCH:")
        for table, (a, b) in report["mismatches"].items():
            print(f"    {table:20} source={a} target={b}")
        return 1
    print(f"\n  verified: {len(report['tables'])} tables match row for row")
    return 0


def cmd_reindex(_args: argparse.Namespace) -> int:
    from app.db import session_scope
    from app.search import reindex

    with session_scope() as session:
        print(f"indexed {reindex(session)} titles")
    return 0


def cmd_avatars(args: argparse.Namespace) -> int:
    """Download actor headshots for people who do not have one stored."""
    from app.db import session_scope
    from app.fetch.archive import Archive
    from app.fetch.client import Fetcher
    from app.fetch.ratelimit import RateLimiterRegistry
    from app.fetch.robots import RobotsCache
    from app.fetch.types import RateSpec
    from app.images import fetch_avatars

    archive = Archive(settings.archive_dir, settings.archive_keep_versions)
    limiters = RateLimiterRegistry(RateSpec(concurrency=4, min_interval=0.2))

    def show(done, total, phase):
        print(f"\r  {phase}: {done}/{total}", end="", flush=True)

    with Fetcher(settings.user_agent, archive, limiters,
                 RobotsCache(settings.user_agent)) as fetcher:
        with session_scope() as session:
            stats = fetch_avatars(
                session, fetcher, settings.image_dir,
                source_key=args.source, limit=args.limit, progress=show,
            )
    print()
    for key, value in stats.items():
        print(f"  {key:12} {value}")
    return 0


def cmd_fix_episodes(args: argparse.Namespace) -> int:
    """One-off: drop ReelShort preview rows and renumber the real episodes."""
    from app.db import session_scope
    from app.episodes import repair_reelshort_numbering, resync_episode_counts

    if args.tag_editions:
        from app.episodes import tag_episode_editions
        from app.fetch.archive import Archive
        from app.sources import registry

        # Every platform, not just ReelShort: the contested pairs are mostly
        # DramaBox dubbed twins and GoodShort republishes.
        modules = {k: registry.load(k) for k in registry.PLATFORM_KEYS}
        archive = Archive(settings.archive_dir, settings.archive_keep_versions)
        with session_scope() as session:
            stats = tag_episode_editions(
                session, archive, modules, dry_run=not args.apply
            )
        for key, value in stats.items():
            print(f"  {key:22} {value}")
        if not args.apply:
            print("\ndry run -- nothing was written. Re-run with --apply.")
        return 0

    if args.resync_counts:
        import importlib

        from app.fetch.archive import Archive

        module = importlib.import_module(SOURCES["reelshort"])
        archive = Archive(settings.archive_dir, settings.archive_keep_versions)
        with session_scope() as session:
            stats = resync_episode_counts(session, archive, module)
        for key, value in stats.items():
            print(f"  {key:18} {value}")
        return 0

    with session_scope() as session:
        stats = repair_reelshort_numbering(session, dry_run=not args.apply)
    for key, value in stats.items():
        print(f"  {key:18} {value}")
    if not args.apply:
        print("\ndry run -- nothing was written. Re-run with --apply.")
    return 0


def cmd_facts(args: argparse.Namespace) -> int:
    """Mine performer pages for gender, height, weight and star sign.

    Reads only ids we already store, so it never guesses at a person: the page
    is addressed by the same identifier the catalogue sync recorded. The source
    prose is Chinese and is deliberately not kept -- only the facts inside it,
    plus the Han name as an alias.
    """
    import importlib

    from app.db import session_scope
    from app.fetch.archive import Archive
    from app.fetch.breaker import CircuitBreaker
    from app.fetch.client import Fetcher
    from app.fetch.ratelimit import RateLimiterRegistry
    from app.fetch.robots import RobotsCache
    from app.jobs.context import RunContext
    from app.jobs.person_facts import run_person_facts

    module = importlib.import_module(SOURCES[args.source])
    archive = Archive(settings.archive_dir, settings.archive_keep_versions)

    def show(done, total, phase):
        print(f"\r  {phase or 'facts'}: {done}/{total or '?'}", end="", flush=True)

    with Fetcher(
        settings.user_agent, archive, RateLimiterRegistry(module.RATE),
        RobotsCache(settings.user_agent), CircuitBreaker(module.KEY),
    ) as fetcher:
        ctx = RunContext(fetcher=fetcher, on_progress=show)
        with session_scope() as session:
            stats = run_person_facts(module, session, ctx, limit=args.limit)
    print()
    for key, value in stats.items():
        print(f"  {key:16} {value}")
    return 0


def cmd_people(args: argparse.Namespace) -> int:
    """Merge duplicate person rows, or restore ids a past merge deleted.

    Dry run by default in both modes: this writes to `person`, and the restore
    half inserts explicit primary keys, which is not something to trigger by
    accident.
    """
    from app.db import session_scope
    from app.people import merge_duplicate_people, restore_tombstones

    with session_scope() as session:
        if args.restore_tombstones:
            stats = restore_tombstones(
                session, args.restore_tombstones, dry_run=not args.apply
            )
        else:
            stats = merge_duplicate_people(session, dry_run=not args.apply)
    for key, value in stats.items():
        print(f"  {key:16} {value}")
    if not args.apply:
        print("\ndry run -- nothing was written. Re-run with --apply to write.")
    return 0


def cmd_dedup(args: argparse.Namespace) -> int:
    """Report duplicate titles, and merge them only when asked.

    Dry run is the default and `--apply` is a deliberate act: a merge rewrites
    eleven tables and retires ids the downstream app already stores.
    """
    from app.db import session_scope
    from app.jobs.dedup import run_dedup

    def show(done, total):
        print(f"\r  merging: {done}/{total}", end="", flush=True)

    with session_scope() as session:
        stats = run_dedup(
            session,
            dry_run=not args.apply,
            limit=args.limit or None,
            progress=show if args.apply else None,
        )
    plan = stats.pop("plan", [])
    if args.apply:
        print()

    auto = [p for p in plan if p["verdict"] == "auto"]
    queued = [p for p in plan if p["verdict"] == "queue"]

    if args.verbose:
        for entry in (auto if args.apply or not args.queued_only else []) + queued:
            mark = "MERGE" if entry["verdict"] == "auto" else "QUEUE"
            print(f"{mark} {entry['survivor']} {entry['survivor_title']!r}")
            for title_id, name in zip(entry["losers"], entry["loser_titles"]):
                print(f"      <- {title_id} {name!r}")
            print(f"      {entry['reason']}")

    print(
        f"\nscanned {stats['titles_scanned']} titles"
        f" -> {stats['auto']} to merge ({sum(len(p['losers']) for p in auto)} rows),"
        f" {stats['queued']} for review"
    )
    if args.apply:
        print(f"merged {stats['merged']}, failed {stats['failed']},"
              f" reindexed {stats.get('reindexed', 0)}")
    else:
        print("dry run -- nothing was written. Re-run with --apply to merge.")
    return 0


def cmd_posters(args: argparse.Namespace) -> int:
    from app.db import session_scope
    from app.fetch.archive import Archive
    from app.fetch.client import Fetcher
    from app.fetch.ratelimit import RateLimiterRegistry
    from app.fetch.robots import RobotsCache
    from app.fetch.types import RateSpec
    from app.images import fetch_posters

    archive = Archive(settings.archive_dir, settings.archive_keep_versions)
    # Image CDNs are separate hosts from the catalogue APIs, so they get their
    # own budget rather than inheriting a source's pacing.
    limiters = RateLimiterRegistry(RateSpec(concurrency=4, min_interval=0.2))

    def show(done, total, phase):
        print(f"\r  {phase}: {done}/{total}", end="", flush=True)

    with Fetcher(
        settings.user_agent, archive, limiters, RobotsCache(settings.user_agent)
    ) as fetcher:
        with session_scope() as session:
            stats = fetch_posters(
                session,
                fetcher,
                settings.image_dir,
                source_key=args.source,
                limit=args.limit,
                progress=show,
            )
    print("\r" + " " * 40)
    for key, value in stats.items():
        print(f"  {key:12} {value}")
    return 0


def cmd_imdb(args: argparse.Namespace) -> int:
    from app.db import session_scope
    from app.jobs.imdb_sync import run_imdb_refresh

    class _Ctx:
        max_items = 0
        watermark = None
        state: dict = {}

        def cancelled(self) -> bool:
            return False

        def progress(self, done, total=None, phase=None) -> None:
            print(f"\r  {phase or '':30} {done}{'/' + str(total) if total else ''}   ",
                  end="", flush=True)

    with session_scope() as session:
        stats = run_imdb_refresh(
            session,
            settings.data_dir,
            datasets=tuple(d.strip() for d in args.datasets.split(",") if d.strip()),
            download=not args.no_download,
            ctx=_Ctx(),
        )
    print("\r" + " " * 50)
    for key, value in stats.items():
        print(f"  {key:14} {value}")
    print(f"\n  {imdb_attribution()}")
    return 0


def imdb_attribution() -> str:
    from app.sources.imdb import ATTRIBUTION

    return ATTRIBUTION


def _refuse_if_container_owns_the_database() -> None:
    """Block host-side writes while the container is running.

    Two engines writing one SQLite file over a macOS bind mount corrupted the
    database once already: WAL needs shared memory and file locking that bind
    mounts do not fully provide. Run CLI work inside the container instead:

        docker compose exec dramadex dramadex <command>
    """
    import shutil
    import subprocess

    if not settings.db_url.startswith("sqlite"):
        return
    docker = shutil.which("docker")
    if docker is None:
        return
    try:
        running = subprocess.run(
            [docker, "compose", "ps", "-q", "dramadex"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - never block on a docker hiccup
        return
    if not running:
        return
    raise SystemExit(
        "The Dramadex container is running and owns this database.\n"
        "Writing to the same SQLite file from the host has corrupted it before.\n"
        "Run inside the container instead:\n"
        "    docker compose exec dramadex dramadex <command>\n"
        "or stop it first with `docker compose down`."
    )


def _require_sqlite():
    path = settings.sqlite_path
    if path is None:
        raise SystemExit("backup/restore is implemented for SQLite only")
    return path


def cmd_backup(args: argparse.Namespace) -> int:
    from app.jobs.backup import run_backup_any

    keep = args.keep if args.keep is not None else settings.backup_keep
    manifest = run_backup_any(settings.db_url, settings.backup_dir, keep=keep)
    print(f"  file       {manifest['file']}")
    print(f"  size       {manifest['bytes'] / 1e6:.2f} MB")
    print(f"  integrity  {manifest['integrity']}")
    print(f"  verified   {manifest['verified']}")
    if manifest["mismatches"]:
        print(f"  MISMATCH   {manifest['mismatches']}")
    if manifest["pruned"]:
        print(f"  pruned     {len(manifest['pruned'])} old backup(s)")
    for table, count in manifest["counts"].items():
        if count:
            print(f"    {table:18} {count}")
    return 0 if manifest["verified"] else 1


def cmd_backups(_args: argparse.Namespace) -> int:
    from app.jobs.backup import verify

    files = sorted(settings.backup_dir.glob("dramadex-*.db"))
    if not files:
        print(f"no backups in {settings.backup_dir}")
        return 0
    for path in files:
        checked = verify(path)
        print(
            f"  {path.name}  {path.stat().st_size / 1e6:6.2f} MB  "
            f"integrity={checked['integrity']}  titles={checked['counts'].get('title')}"
        )
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from app.jobs.backup import latest, restore

    db_path = _require_sqlite()
    source = Path(args.file) if args.file else latest(settings.backup_dir)
    if source is None:
        raise SystemExit("no backups found")
    if not source.exists():
        source = settings.backup_dir / source.name
    if not args.yes:
        answer = input(f"Replace {db_path} with {source.name}? [y/N] ").strip().lower()
        if answer != "y":
            print("aborted")
            return 1
    result = restore(source, db_path)
    print(f"  restored from  {result['restored_from']}")
    print(f"  previous db    {result['previous_database'] or '(none)'}")
    for table, count in result["counts"].items():
        if count:
            print(f"    {table:18} {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dramadex")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show configuration and schema state").set_defaults(
        func=cmd_info
    )

    archive = sub.add_parser("archive", help="inspect the raw response archive")
    archive.add_argument("--limit", type=int, default=20)
    archive.set_defaults(func=cmd_archive)

    sync = sub.add_parser("sync", help="run a catalogue sync")
    sync.add_argument("source", choices=sorted(SOURCES))
    sync.add_argument("--limit", type=int, default=None, help="cap items (0 = uncapped)")
    sync.add_argument("--incremental", action="store_true")
    sync.add_argument("--since", default=None, help="watermark for incremental runs")
    sync.add_argument("--no-details", action="store_true", help="listing only")
    sync.set_defaults(func=cmd_sync)

    reparse = sub.add_parser(
        "reparse", help="re-run parsers over the raw archive (no network)"
    )
    reparse.add_argument("source", choices=sorted(SOURCES))
    reparse.set_defaults(func=cmd_reparse)

    roster = sub.add_parser("roster", help="ingest the curated actor roster")
    roster.add_argument("source", choices=sorted(SOURCES))
    roster.add_argument("--no-profiles", action="store_true",
                        help="names only; skip the 229 per-actor pages")
    roster.set_defaults(func=cmd_roster)

    migrate = sub.add_parser(
        "migrate-engine", help="copy the whole database to another engine"
    )
    migrate.add_argument("target", help="target URL, e.g. postgresql+psycopg://...")
    migrate.add_argument("--verify-only", action="store_true")
    migrate.set_defaults(func=cmd_migrate_engine)

    sub.add_parser("reindex", help="rebuild the full-text search index").set_defaults(
        func=cmd_reindex
    )

    dedup = sub.add_parser(
        "dedup", help="find duplicate titles; reports unless --apply is given"
    )
    dedup.add_argument("--apply", action="store_true",
                       help="actually merge (default is a dry run that writes nothing)")
    dedup.add_argument("--limit", type=int, default=0, help="only the first N clusters")
    dedup.add_argument("--verbose", action="store_true", help="print every cluster")
    dedup.add_argument("--queued-only", action="store_true",
                       help="with --verbose, print only what needs review")
    dedup.set_defaults(func=cmd_dedup)

    people = sub.add_parser(
        "people", help="merge duplicate actors; reports unless --apply is given"
    )
    people.add_argument("--apply", action="store_true",
                        help="actually merge (default is a dry run)")
    people.add_argument(
        "--restore-tombstones", metavar="BACKUP.db", default=None,
        help="re-create ids an older merge deleted, reading the mapping out of a "
             "pre-merge backup (also honours --apply)",
    )
    people.set_defaults(func=cmd_people)

    fixeps = sub.add_parser(
        "fix-episodes",
        help="repair ReelShort episode numbering and drop preview rows",
    )
    fixeps.add_argument("--apply", action="store_true",
                        help="actually write (default is a dry run)")
    fixeps.add_argument("--resync-counts", action="store_true",
                        help="recompute each ReelShort link's episode_count from "
                             "its archived payload (safe to re-run)")
    fixeps.add_argument("--tag-editions", action="store_true",
                        help="point every episode at the title_source link that "
                             "published it, so ?lang= can separate a show's "
                             "language editions (safe to re-run)")
    fixeps.set_defaults(func=cmd_fix_episodes)

    facts = sub.add_parser(
        "facts",
        help="read a source's performer pages for structured facts about people",
    )
    facts.add_argument("--source", default="dramabox", choices=sorted(SOURCES))
    facts.add_argument("--limit", type=int, default=0)
    facts.set_defaults(func=cmd_facts)

    avatars = sub.add_parser("avatars", help="download actor photos for people missing one")
    avatars.add_argument("--source", default=None, choices=sorted(SOURCES))
    avatars.add_argument("--limit", type=int, default=0)
    avatars.set_defaults(func=cmd_avatars)

    posters = sub.add_parser("posters", help="download posters for titles missing one")
    posters.add_argument("--source", default=None, choices=sorted(SOURCES))
    posters.add_argument("--limit", type=int, default=0)
    posters.set_defaults(func=cmd_posters)

    imdb = sub.add_parser("imdb", help="download IMDb datasets, match and enrich")
    imdb.add_argument("--no-download", action="store_true", help="use cached datasets")
    imdb.add_argument(
        "--datasets", default="basics,ratings,episode,akas",
        help="comma-separated subset; akas is ~490MB and the slowest pass",
    )
    imdb.set_defaults(func=cmd_imdb)

    backup = sub.add_parser("backup", help="take a verified hot backup")
    backup.add_argument("--keep", type=int, default=None)
    backup.set_defaults(func=cmd_backup)

    backups = sub.add_parser("backups", help="list and verify existing backups")
    backups.set_defaults(func=cmd_backups)

    restore = sub.add_parser("restore", help="restore the database from a backup")
    restore.add_argument("file", nargs="?", help="defaults to the newest backup")
    restore.add_argument("--yes", action="store_true", help="skip the confirmation")
    restore.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    if getattr(args, "func", None) in (
        cmd_sync, cmd_reparse, cmd_posters, cmd_imdb, cmd_reindex, cmd_restore, cmd_roster
    ):
        _refuse_if_container_owns_the_database()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
