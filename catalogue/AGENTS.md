# Notes for an incoming agent

Read this before `README.md`. A fuller cross-project handoff (this project plus Dramatarr and
Dramadown, including deployment and operational detail) lives as `HANDOFF.md` in the directory
that contains this repo — outside version control, because it names infrastructure.

## The prose docs are stale

- **`README.md` and `PLAN.md` describe a two-source project.** There are **four** platforms
  (DramaBox, ReelShort, GoodShort, NetShort) plus IMDb as an enricher. README's milestone table
  shows everything "done" and its test counts are months old.
- **`DEPLOY.md` configures SQLite.** Production runs **Postgres**. The doc predates the cutover
  and is internally consistent, which is why it reads as current.
- Trust `app/sources/registry.py` (`ADAPTERS`) for which sources exist, the module docstrings in
  `app/sources/*.py`, and `app/config.py` for configuration.

## Two disciplines — do not "improve" either

**Identity is decided only by exact keys the source publishes.** Fuzzy matching, trigram
similarity, perceptual hashing, synopsis comparison and episode-count heuristics are forbidden,
with the measurements that killed each one recorded in `app/dedup.py:1-45`. Read that before
touching identity resolution. The catalogue looks exactly like a case for fuzzy matching and it is
a trap: six *different* shows are called "Fated to My Forbidden {Alpha,Guardian,…}", and
normalised-title equality put 26 of 35 title-equal pairs on unrelated shows. Join editions and
duplicates only on a source-published key — ReelShort's `t_book_id`, NetShort's
`shortPlayLibraryId`.

**Every raw response is archived before parsing**, so a wrong parser is fixed by `reparse` at zero
network cost. Don't add a parser that consumes a live response directly.

## The things that fail silently

- **The test suite runs on SQLite; production is Postgres.** SQLite ignores declared column
  lengths, accepts bare columns in `GROUP BY`, and orders NULLs first. Three defects of this class
  have shipped — including `GET /studios`, a hard 500 on Postgres that had never once worked in
  production while the suite stayed green. Treat any change touching column lengths, `GROUP BY`,
  or ordering on a nullable column as unverified until it has run on Postgres.
- **Never use Alembic's `batch_alter_table` on `title`.** Batch mode emulates an unsupported
  SQLite `ALTER` by rebuilding — create, copy, **drop** — and under `PRAGMA foreign_keys=ON`
  (which `app/db.py` sets) the DROP fired `ON DELETE CASCADE` and destroyed 638,600 episode rows
  on a real catalogue copy. `title` itself came out perfect, so every parent row-count check
  passed. Add columns and indexes natively; gate anything that forces a rebuild behind a
  dialect check.
- **Losing `DRAMADEX_DB_URL` does not fail.** It silently opens `sqlite:///./data/dramadex.db` in
  the data volume — and a stale pre-cutover database once sat at exactly that path. Read the
  resolved engine from the startup log line (`app.main: database: …`), never from reasoning about
  the environment.
- **A `Verdict.REJECT` in `app/dedup.py` is permanent invisibility**, not deprioritisation. Never
  express a heuristic as a rejection — rank on the review queue instead (`app/api/admin.py`,
  `_merge_rank`). A `LIMIT` of 200 over 439 merge proposals did not present them arbitrarily; it
  hid 239 while looking like pagination. Rank, then cut, and say how many of the total you are
  showing.
- **A filter that can multiply rows breaks keyset pagination.** `JOIN` + `LIMIT` counts *rows* and
  dedupes afterwards, so a short page looks like the end of the catalogue: `?studio=netshort`
  returned 5,925 of 6,223 titles and reported a clean finish. Use `EXISTS`. The invariant to test
  is not "a full page has a cursor" but "a short page cannot claim to be last".
- **`classify()` stays per-source.** A 2xx from ReelShort and an empty list from DramaBox mean
  opposite things: ReelShort's WAF challenge arrives as **HTTP 202 with content-length 0**, which
  curl reports as success. Never hoist it into the fetch layer.
- **"Succeeded" is not "worked."** An empty crawl, a capped queue and a capability nothing
  triggers are all indistinguishable from normal operation. After shipping anything that fills a
  column, go and measure that column.

## Shape worth knowing before you edit

- `title` is the canonical series; `title_source` is one row per (platform, external_id). A series
  on three platforms is **one** `title` with three `title_source` rows. Every fact carries
  `source_id`, which is what makes a bad source revocable in a single DELETE.
- `delisted_at` (the platform dropped it) and `merged_into_id` (folded into another row) are not
  interchangeable. Merged rows are kept as **tombstones**, never deleted — a consumer already
  holds the id, and a 404 tells it nothing.
- **A merge touches eleven tables**: nine ORM foreign keys plus `title_fts` (SQLite) and
  `title_search` (Postgres), which `app/search.py` creates in raw SQL and Alembic cannot see.
  `app/merge.py:1-26` says so. A merge **repoints** `title_source` and never deletes it.
- Episode **counts** and episode **rows** are deliberately separate facts on three of four
  sources. Never derive `episode_count` from `len(episodes)`.
- NetShort's `/drama/` URLs are **genre pages**, not titles, and return 200 with valid structured
  data. Titles are at `/full-episodes/<slug>-<id>`.
- Changing `DRAMADEX_USER_AGENT` is a **robots.txt decision**: Protego matches user-agents by
  substring and NetShort blanket-disallows seven tokens. The deployed string is not the code
  default.
- **No video or playback URL is ever stored.** The sources hand out signed, expiring links in the
  same payload that gets parsed; they are dropped deliberately (`app/models.py:294-301`). This is
  load-bearing for the IMDb non-commercial licence, not a style preference.

## Version

`app/__init__.py` has said `__version__ = "0.1.0"` since the first commit, and that is what
`/health` reports. **It is not a contract version — never branch on it.** Any `0.6.x` numbers you
find in notes elsewhere are an agent-side numbering that appears nowhere in the code. No image tag
carries provenance either, so identify a build by git HEAD.

## Known open bugs

- `GET /people` always returns `photo_url: null` — the handler constructs `PersonBrief(id, name)`
  and nothing else, though the schema declares the field and the detail endpoint populates it.
  (The search parameter is `?q=`, not `?name=`.)
- Duplicate person rows are served within a single title, reported from the consumer side. The ids
  are genuinely distinct, so id-dedup cannot catch it; the tell is that the duplicate is always
  the copy arriving without `billing_order`. Not yet confirmed against the production database.

## Operational cautions

- **Run CLI work inside the container.** Running the CLI from a host against a bind-mounted SQLite
  file has corrupted the database — WAL needs shared memory the host does not share.
- **Deploying is not a cheap iteration step.** A deploy restarts the container and reaps any
  in-flight crawl, and the reaped source then stays locked for up to six hours, because pids reset
  on restart and the liveness check sees a dead worker's pid reused by a live process. Check for a
  `running` row in `sync_run` first.
- **The build context is `git ls-files`** — tracked files only. An Alembic migration that was
  never `git add`ed produced an image whose code queried a column its own migration would have
  created, and routes 500'd in production while everything local passed. Run
  `git status --porcelain app alembic` before building.
- `dramadex sync <source>` from the CLI records no run state, so a source crawled by hand still
  shows its last scheduled failure as its most recent outcome. Trigger through the API when the
  record matters.
