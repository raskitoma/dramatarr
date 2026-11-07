# Dramadex

A self-hosted indexer for vertical short-drama (micro-drama) metadata.
Python + FastAPI · SQLAlchemy · local image store · REST API + admin console.
**Production runs Postgres**; the test suite runs SQLite.

Sources: four platforms — **DramaBox**, **ReelShort**, **GoodShort**, **NetShort** — plus
**IMDb** as an enricher (no watch URL, never the subject of a catalogue sync). The one
authoritative list is `ADAPTERS` in [app/sources/registry.py](app/sources/registry.py).

> **Two documents in this repo are stale and read as current.** [PLAN.md](PLAN.md) is the
> original two-source design and recon, kept as a historical record, and
> [DEPLOY.md](DEPLOY.md) still configures SQLite, including a SQLite-specific backup story —
> it predates the Postgres cutover. Where prose and code disagree the code is right: trust
> `ADAPTERS` in [app/sources/registry.py](app/sources/registry.py) for which sources exist,
> the module docstrings in `app/sources/` for how each behaves, and
> [app/config.py](app/config.py) for configuration.

## Status

| Milestone | State |
|---|---|
| M0 skeleton, schema, migrations, Docker | **done** |
| M1 fetch layer, per-source classifiers, archive, breaker | **done** |
| M2 DramaBox adapter | **done** |
| M3 ReelShort adapter + `reparse` | **done** |
| M4 precedence, order-independent resolution | **done** |
| M5 job engine: leases, states, progress/ETA, reaping | **done** |
| M6 REST API, search, admin console | **done** |
| M7 posters and avatars | **done** |
| M9 scheduler daemon | **done** |
| M8 IMDb enrichment + match review queue | **done** |
| M10 backup/restore drill | **done** |

Work continued past M10: GoodShort and NetShort adapters, localised catalogues, episode
editions, and ranking on the merge review queue. `git log` is the record; the table above is
not.

Measured on the deployment, 2026-11-06: **27,350 titles · 1,713,721 episodes · 2,367 people ·
1,841 tags · 35,428 posters · 2,350 avatars**, with 2,163 titles merged and **1,042 merge
proposals still queued for review**. Source rows per platform: NetShort 28,245, ReelShort
10,035, DramaBox 8,573, GoodShort 6,077, IMDb 24 — the total exceeds the title count because
one title carries one row per platform it appears on. Coverage: synopsis 100%, episode counts
100%, tags 100%, episodes 99.8%, year 60%, first paid episode 46.3%, aliases 44.7%, audience
rating 13.8%, cast 8.8%.

**558 tests collected, 557 run** (one is marked `live` and deselected by default).

## Quick start

```bash
cp .env.example .env
openssl rand -hex 32          # paste into DRAMADEX_API_KEY in .env
docker compose up -d
open http://127.0.0.1:8080/admin
```

Set `DRAMADEX_PORT` in `.env` if 8080 is taken. The port binds to loopback only —
put your own reverse proxy in front to expose it.

Sign in to the console with the same API key. It is stored in an HttpOnly cookie,
because browsers send no custom headers when you navigate to a page or load an image.

## Using it

**Admin console** — `/admin` dashboard (counts, per-source lease and breaker state,
one-click sync triggers), `/admin/runs` (live progress with ETA, cancel), `/admin/titles`
(search, posters, watch links), `/admin/quarantine`.

**REST API** — interactive docs at `/docs`. Every endpoint except `/health` needs
`X-API-Key`.

```bash
KEY=$(grep '^DRAMADEX_API_KEY=' .env | cut -d= -f2)
curl -H "X-API-Key: $KEY" 'localhost:8080/titles?q=billionaire&limit=5'
curl -H "X-API-Key: $KEY" 'localhost:8080/titles/61'
curl -H "X-API-Key: $KEY" 'localhost:8080/stats'
curl -X POST -H "X-API-Key: $KEY" 'localhost:8080/sync/runs?source=dramabox&job_type=incremental'
```

### The "Watch on ReelShort" button

Every title carries one link per studio:

```json
"studios": [{
  "key": "reelshort", "name": "ReelShort",
  "external_id": "69d91b3ee15105112e0dc022",
  "watch_url": "https://www.reelshort.com/movie/69d91b3ee15105112e0dc022"
}]
```

`watch_url` is built from the **id, not the slug**. The platforms 301 the id-only form
to the current canonical URL, so the link survives a retitle — which these platforms do
constantly. A live test asserts every generated URL still resolves 200.

NetShort is the exception worth knowing: its `/drama/` URLs are **genre pages**, not titles,
and they return 200 with valid structured data. Titles live at `/full-episodes/<slug>-<id>`.

### Incremental sync for a downstream app

```bash
curl -H "X-API-Key: $KEY" 'localhost:8080/titles?updated_since=2026-08-16T00:00:00Z&limit=100'
```

Pagination is **keyset**, not offset: syncs write while you poll, and offset would
silently skip or repeat rows mid-scan. Follow `next_cursor` until it is null.

## Ratings — read this before sorting by them

The platforms publish scores that are marketing filler. Three sources side by side, as
measured during recon in August 2026, make the point better than any argument:

```
dramabox  n=236  range 7.1–10.0   EDITORIAL   (floor-clamped; nothing below 7.1)
reelshort n=114  range 4.5–4.9    EDITORIAL   (whole catalogue in 0.4 points)
imdb      n=30   range 4.9–8.1    GENUINE     (with real vote counts: 2824, 399, …)
```

Platform scores are stored flagged `is_editorial: true` and kept **off** the `Title`
table entirely, so no query can sort by them. Filter on `is_editorial == false` to
get real ones.

## IMDb matching

IMDb supplies the only genuine ratings and the only real alternate titles
(`La Double Vie d'une Héritière Milliardaire`, and so on). Getting the *links* right
is the hard part, and the matcher is built to refuse rather than guess.

It auto-accepts exactly two things:

1. An IMDb id a platform handed us outright — ReelShort publishes `actor_info.imdb_id`
   on ~21 of our titles, so there is nothing to match.
2. A **unique** exact title match on normalised text, **confirmed by episode shape**
   (50–80 episodes of 1–3 minutes, a fingerprint no reality show shares).

Everything else goes to `/admin/matches` with the evidence. **Every candidate has its
own "Use this" button** — the queue exists because the matcher was unsure, so being able
to accept only its guess would be useless. Its guess is marked `suggested`, and choosing
a different row records both, so the decision stays auditable. "None of these" rejects
the lot, and a rejected target is never proposed again.

The canonical example is live in the queue right now: *Married at First Sight* matches
five tconsts exactly, including the 2014 reality franchise at **404 episodes**. Ours has
90, so only the 2023 entry (89) is plausible — the episode column marks each candidate
✓ or ✗ against ours. A title-only matcher attaches a reality show to a vertical drama and
never tells you.

There is no fuzzy branch, deliberately. These titles come from a template grammar —
six *different* shows are called `Fated to My {Forbidden Alpha | Forbidden Guardian |
Forbidden Vampire | Bad Boy Alpha | Swapped Alpha | Homeless Billionaire Alpha}`, with
pairwise trigram similarity above 0.8. `tests/test_matcher.py` pins that family as a
negative control: zero auto-accepts, every pairing cross-checked.

```bash
docker compose exec dramadex dramadex imdb              # download, match, enrich
docker compose exec dramadex dramadex imdb --no-download --datasets basics,ratings,episode
```

The datasets are ~800 MB and refresh daily; downloads revalidate with ETag. The full
run including `title.akas` (59M rows) takes ~8 minutes.

## CLI

**Run CLI work inside the container.** Writing to the same SQLite file from the host
while the container is running corrupted the database once: WAL needs shared memory and
file locking that macOS bind mounts do not fully provide. The CLI now refuses to start
if the container is up.

```bash
docker compose exec dramadex dramadex info
docker compose exec dramadex dramadex imdb --no-download
```

With the container stopped, the same commands work on the host:

```bash
uv run dramadex info                      # config and schema state
uv run dramadex sync dramabox --limit 50  # capped catalogue sync
uv run dramadex sync reelshort --incremental
uv run dramadex reparse reelshort         # re-run parsers over the archive, NO network
uv run dramadex posters                   # download missing key art
uv run dramadex reindex                   # rebuild the search index
```

`reparse` is why every response is archived before it is parsed. When a parser turns
out to be wrong — and one did, on ReelShort's zeroed `online_at` — the fix costs zero
requests. The entire database has been rebuilt from the archive with sockets and httpx
blocked, and it reproduced exactly.

## Backup and restore

> **This section describes the SQLite era and has not been rewritten for Postgres.** The
> commands below still work against a SQLite database; the production deployment runs
> Postgres, so treat its backup story as unverified here and check the deployment before
> relying on any of it.

Backups use SQLite's **online backup API**, not a file copy: `cp` of a live database can
capture a torn file, and with WAL the committed data may sit in the `-wal` file a copy
misses. Each backup is verified when taken — integrity check plus a row-count comparison
against the live database — and written with a manifest beside it.

```bash
docker compose exec dramadex dramadex backup     # take + verify, prunes to the last 7
docker compose exec dramadex dramadex backups    # list and re-verify
docker compose down && uv run dramadex restore   # newest backup; --yes to skip the prompt
```

A daily backup is on the schedule. Restore refuses a corrupt backup (so a bad restore
cannot turn a recoverable situation into an unrecoverable one) and moves the replaced
database aside as `dramadex.superseded-<timestamp>` rather than deleting it.

**Drilled for real** (on the 359-title SQLite database of August 2026): a hot backup, then
16 KB scribbled over the middle of the file, then restored — 359 titles, 18,465 episodes, 45
queued matches and the poster store all came back intact. The drill has not been repeated
against Postgres.

## Remote access

The container publishes on loopback by default. To reach it from another machine:

```bash
# in .env
DRAMADEX_BIND=0.0.0.0
```

There is **no TLS**, so the API key travels in clear. Keep this to a network you trust,
and put a proxy terminating TLS in front before exposing it anywhere else.

## Scheduling

The container runs an in-process scheduler. It reaps dead runs and starts what is due,
but it does **not** decide whether something already ran today — a partial unique index
does, so two ticks can never both start the same job.

- One scheduled run per source per job type per day. Manual triggers bypass the cap.
- `DRAMADEX_SYNC_HOUR=3` sets the earliest local hour a scheduled sync may start.
- Never two jobs against one site at once; different sites run in parallel.
- A source lease is renewed on a timer, so a multi-hour crawl is never mistaken for an
  abandoned one, and a worker that loses its lease stops rather than crawling alongside
  whoever took it.

## Development

```bash
uv sync --extra dev
uv run pytest                    # 557 tests, no network
uv run pytest -m live            # hits the real sites; run manually
uv run python tests/capture_fixtures.py   # refresh the fixture corpus
```

Tests never touch the network. They run against recorded responses in `tests/fixtures/`,
one per trap found during recon:

| Fixture | What it proves |
|---|---|
| `reelshort/challenge_202` | The WAF challenge arrives as **HTTP 202 with an empty body** — a 2xx. Treating it as success would silently index nothing. |
| `reelshort/tags_404_page375` | End-of-pagination returns 404 **while still containing valid `__NEXT_DATA__`**. |
| `reelshort/detail_legacy_dates` | `online_at` is `'0'` across the legacy catalogue; the year has to come from `publish_at`. |
| `reelshort/detail_no_episodes` | A real, cast-credited title reporting `total=0`. Must not be dropped. |
| `dramabox/browse_empty` | HTTP 200 with `bookList: []` is a **valid empty result**, not a block. |
| `dramabox/redirect_stub` | Page 1 of a category returns a 200-wrapped `__N_REDIRECT` stub. |
| `dramabox/detail_with_cast` | Cast is *absent* on most newer titles, so the cast path is pinned to one that has it. |

The last pair is why `classify` belongs to the source rather than the framework: an empty
body means "no results" on DramaBox and "you were blocked" on ReelShort.

## Operating notes

- **Politeness is the design.** Per-domain rate limits, `robots.txt` compliance,
  conditional requests, backoff with jitter, and a circuit breaker that halts a source
  for the day rather than hammering it. Concurrency is capped at 4 on ReelShort, where
  the WAF trips at 16.
- **`DRAMADEX_MAX_ITEMS=50`** caps runs while parsers are being proven. Raise it when
  you want the full catalogue — expect ~1,055 requests / ~6 min for DramaBox, and
  **hours** for ReelShort, whose listing is non-deterministic and needs repeated passes.
- **Never fetch `/server_api/honey`** on DramaBox: robots-disallowed, and it echoes your
  IP and User-Agent back to fingerprint crawlers. Listed in `FORBIDDEN_PATHS`.
- **Back up `./data`** — response archive and images. On SQLite the database needs the
  `.backup` API or a WAL checkpoint, not a naive `cp` of a live file; on Postgres the
  database lives outside this directory entirely.
- **No video URLs are ever stored.** The platforms hand us signed, expiring playback
  links in the same payload we parse; a database-wide audit (775 table/column/marker
  combinations) confirms none reach storage. This is load-bearing for the IMDb
  non-commercial licence, not a style preference.

## Licensing

Built for **personal, single-user, self-hosted** use, which is what the IMDb
non-commercial licence permits. Publishing the instance, multi-tenanting it, or exposing
a public API over it would fall outside it. IMDb-derived rows carry `source_id` so they
can be excluded or dropped wholesale. Cached key art stays private for the same reason.
See [PLAN.md §6](PLAN.md).

Information courtesy of IMDb (https://www.imdb.com). Used with permission.
