# Dramadex — Plan

A self-hosted indexer for vertical short-drama (micro-drama) metadata.
Python + FastAPI · SQLAlchemy on SQLite (Postgres-ready) · local poster store · REST API + small admin UI.

Status: **plan, pre-implementation.** All source findings below were empirically probed on 2026-08-16 and
independently re-verified by a second adversarial pass. Corrections from that pass are marked ⚠.

### Decisions locked (2026-08-16)

| Decision | Choice |
|---|---|
| Stack | Python + FastAPI |
| Database | SQLAlchemy on SQLite (WAL), Postgres-ready — no engine-specific extensions |
| Posters | Downloaded to a local store, hashed, with provenance |
| Interface | REST API + minimal admin UI (sync progress/ETA, manual triggers) |
| Deployment posture | **Personal, single-user, self-hosted** → inside IMDb's non-commercial carve-out |
| IMDb | Primary enricher, used fully, attribution carried, rows source-tagged so they stay strippable |
| TMDB | **Skipped entirely** — 31% coverage doesn't justify a 6-month TTL job plus attribution terms |
| MicroDramaRadar | Adapter built but **disabled**; daily 1-request availability probe only |
| ReelShort full crawl | **Concurrency 4, multi-pass until the unique-ID set saturates** |
| Headless browser | **Not used** — neither primary source needs one |
| "Studio" | Means the **source platform** (ReelShort, DramaBox, …). Modelled as `studio`, one row per source |
| Origin URL | Every title carries a **durable, self-healing watch URL per studio** — see §2.2 |
| Episodes | **Full per-episode rows.** Metadata only — signed/expiring video URLs are never stored |
| New sources | **Python adapter module** implementing the `Source` protocol + a DB row. No config DSL |
| API auth | **Static API key** header on every endpoint except `/health` |
| Consumption | **REST with `?updated_since=`** keyset polling |
| Test cap | `SOURCE_MAX_ITEMS=50` per source on `full_catalog` until parsers are proven |

---

## 1. What recon actually found

### 1.1 DramaBox — the easy one

No Cloudflare, no WAF, no rate limiting. Origin is openresty behind AWS CloudFront.
A full 1,055-request catalogue crawl at concurrency 8 returned **1,055/1,055 HTTP 200, zero failures** — run
twice, by two independent agents. Burst tests at concurrency 16 also clean.

Next.js **Pages Router**, so every page's props are available as pure JSON. No HTML parsing anywhere:

```
https://www.dramabox.com/_next/data/{buildId}/browse/{categoryId}/{page}.json
https://www.dramabox.com/_next/data/{buildId}/drama/{bookId}/{bookNameEn}.json
https://www.dramabox.com/_next/data/{buildId}/name/{performerId}.json
```

- `buildId` is date-stamped (`dramabox_prod_20260720`) and **rotates on deploy** — re-read it from `/` every run.
  A stale buildId 404s, so this is fail-fast, not silent.
- **No sitemap** (8 variants checked, all 404). Catalogue discovery is the browse tree.
- Enumeration: 53 categories. ⚠ Category `0` ("all") is **hard-capped at 250 pages / 3,000 titles** and only
  reaches back to 2026-04-22. The per-category lists are uncapped and reach 2023-04-14. You must sweep all 53
  and union on `bookId`. Measured: 1,055 requests → **~6,555 unique English titles** (2023: 84, 2024: 794,
  2025: 1,516, 2026: 4,161).
- Incremental is trivial: the "all" list is **strictly `shelfTime`-descending**. Keep a watermark, page from the
  front, stop at the first record ≤ watermark. Verified live: 66 new titles in 6 requests over ~2.5 days.
  Publication rate ≈ 25 titles/day, so a daily run costs **~3 requests**.
- `firstShelfTime` vs `shelfTime`: identical on 6,437/6,516. The 79 that differ are *re-shelved* titles —
  use `firstShelfTime` for "is this new", `shelfTime` for ordering.

**Traps (all verified):**
- Page 1 of a category has no page segment. `/browse/161/1.json` returns HTTP 200 with
  `{"__N_REDIRECT":"/browse/161","__N_REDIRECT_STATUS":301}`. Handle the redirect stub.
- ⚠ Out-of-range and invalid requests return **HTTP 200 with an empty list, never 404**.
  `/browse/0/251` → `pages=0, bookList=[]`. `/browse/999` (nonexistent category) → 200 with `bookList=[]`.
  Only the `/name/{id}` space 404s cleanly (`{"notFound":true}`), so only that one is safe to probe by status.
- **Honeypot: `/server_api/honey`** is robots-disallowed and echoes your IP and User-Agent back. It exists to
  fingerprint robots-violating crawlers. Never request it. A sibling `sapi.dramaboxdb.com/drama-box/ad/cache/ua`
  does the same thing and *does* respond — avoid it too.
- ⚠ `web.dramaboxdb.com` (the search API host) is UA-gated and 403s a default curl UA. We don't need it —
  and `/search?*` is robots-disallowed anyway.
- Locales are fully separate catalogues (`/es/browse` → 131 pages, **zero** bookId overlap with English).
  Pin the bare English paths.

**Field semantics — ⚠ the first recon pass got these wrong, corrected by measurement over 6,555 records:**
- `ratings`: **not** 0–10 with 0 = unrated. Actual range **7.1–10.0, zero records below 7.1**
  (7.x: 830, 8.x: 2476, 9.x: 1914, 10.0: 1335). It is a floor-clamped promotional score, useless for ranking.
- `lastUpdateTimeDisplay` == `"Completed"` on **100%** of records. It is *not* an ongoing/completed flag.
- `free` == 1 and `status` == `"PUBLISHED"` on 100%. Constants, not signal.
- **No year field** — derive from `firstShelfTime`. **No audience/content rating** of any kind.
  **No studio** — `author` is a distributor label ("Webfic") and is empty for essentially all records.
- **No genuine alternate titles** (measured at M2). `bookNameEn`, `replacedBookName` and `bookNameLower` look
  like alt-title fields but are all just slug/case variants of `bookName` — across 50 ingested titles, every one
  normalised to the primary title and zero real aliases survived. DramaBox contributes nothing to the
  alt-titles requirement; that has to come from IMDb `akas` (M8) or the multi-locale idea in §7.2.
- **Cast is absent, not empty, on many titles** (measured at M2). `performerList` was missing entirely from all
  50 of the newest titles sampled — the key does not exist, rather than holding `[]`. Titles that do have cast
  carry it in full. Treat absence as ordinary, never as a parse failure.

### 1.2 ReelShort — workable, but needs care

AWS WAF behind CloudFront. Next.js, data in `__NEXT_DATA__` on server-rendered HTML. `robots.txt` is
`User-agent: * / Allow: /` with nothing disallowed and no sitemap.

**The single most dangerous finding: the WAF challenge is silent.**
When tripped you get `HTTP 202`, `content-length: 0`, and header `x-amzn-waf-action: challenge` — *not* a
403 or 429. `curl` reports success. A naive crawler records hundreds of empty pages as valid and indexes
nothing. **Every response must be asserted to contain `__NEXT_DATA__`.**

⚠ Corrections from the verify pass:
- Concurrency threshold was overstated 4×. 180 requests at **concurrency 4 ran completely clean**; it took
  concurrency 16 to trip the WAF. The "1.2s sequential" advice was far more conservative than needed.
- Recovery is **~34 seconds**, not "minutes" (polled at 10s intervals, back to 200 on attempt 4).
- The WAF is **host-wide, not path-scoped** — once challenged, `/fandom/wp-json/*` returns 202 too.
  Do not treat the WordPress side as a separate budget. Only the image CDN stays up.
- ⚠ **End-of-pagination sentinel in the first report was wrong and would break the crawler.**
  `/tags/mc-types/375` returns HTTP 404 *and still contains `__NEXT_DATA__`* (with `page:"/404"`).
  Correct sentinel: `status != 200` **or** `tagBooks is None`.

**Enumeration is non-deterministic** — the other big trap. Repeated fetches of the same browse page return
different items (page 120 fetched 4× gave two entirely different 10-item sets, with `x-cache: Miss` on every
fetch — origin-side, not caching). One full 374-page pass yields ~10% dupes (⚠ first report said 29%; verifier
measured 10.3%) against a reported total of 3,738. **Loop passes until the unique-ID set saturates**, and union
across several `/tags/{category}/` paths.

```
/tags/mc-types/{1..374}          → props.pageProps.tagBooks.books  (10/page, total 3738 — widest view)
/movie/{book_id}                 → props.pageProps.data            (needs -L; canonicalises without the slug)
/tags/movie-actors/{slug}-movies-{tag_id}  → per-actor filmography
/fandom/wp-json/wp/v2/posts      → real WordPress REST API, x-wp-total 3190, clean incremental via &after=
```

- Incremental: `/shelf/new-release-short-movies-dramas-51001290` → 200 records ≈ 3 weeks of releases.
  **Diff by id, don't stop at the first known title** — it's an editorial shelf, not strictly date-sorted.
  ⚠ Its own pagination is fake: pages 1/2/17 are byte-identical. Shelves cap at 200.
- `/search` requires the param `keywords` (not `q`) and silently returns 0 for wrong param names.
  Relevance-capped ~530, so useless for enumeration.

**Fields:** `book_title`, `book_sub_title[]` (alt titles, sparse), `special_desc` (synopsis), `book_pic`
(poster), `total`/`online_base[]` (episodes), `online_at` (release, unix epoch), `tag_list[]` (the real
taxonomy — 15 category namespaces incl. **1015 = audience rating**, e.g. "All Ages", present ~73%),
`read_count`/`collect_count` (real popularity).

**`actor_info` is the crown jewel — it carries IMDb ids for free**: `{imdb_id: "tt…", actors: [{actor_name,
outside_url: "nm…", inside_url: fandom page}]}`. That removes fuzzy matching entirely where present.
⚠ But coverage is ~20% — the key exists on 100% of detail pages while `actors[]` is empty on ~80%
(much of the catalogue is animation with no credited cast). Key-presence ≠ data-presence.

**Measured at M3, correcting recon's estimates:**
- ⚠ **`online_at` is literally `'0'` on the legacy catalogue** — 21 of the first 50 titles ingested, all low
  `t_book_id`s from 2023. `publish_at` is populated on exactly those, so it is the fallback. Without it, `year`
  was missing on 42% of the sample. Recon said "prefer `online_at`" and never hit the zeroed case.
- **All numeric fields arrive as strings** (`total: '119'`, `online_at: '1779951600'`, `paid_start: '16'`).
  Coerce everything.
- **Cast coverage is ~60%, not ~20%** (30/50 titles had credits). Better than recon's estimate.
- **Audience rating coverage is 100%, not ~73%** (50/50 carried a `1015` tag).
- **Multi-pass discovery is confirmed necessary:** a second pass over the same page window surfaced **18 new
  titles (+36%)** that the first pass missed entirely. A single-pass crawler would silently under-collect.
- **Zero WAF challenges** across two full runs at concurrency 4 (`{'ok': 57}` each time).

**Junk fields to ignore:** ld+json `contentRating` is the literal string `"RATING NOT KNOWN"` on 30/30 titles.
`popularityScore` is 4.5–4.9 across the board — marketing filler. `book_genre` and `book_source` are
unlabelled integers with no published mapping. ⚠ The FAQ prose block disagrees with `data.total` by one episode
— don't parse counts out of prose. ⚠ The `book_id` hex prefix is a creation timestamp, **not** a release date
(one title: id says 2026-04-10, `online_at` says 2026-05-28).

**Actor roster** — two disjoint namespaces, joined only by `actor_info.actors[].inside_url`:
1. `/fandom/reelshort-actors-actresses/` — 229 curated name→URL pairs, single page, no pagination.
   ⚠ Slugs are irregular and unsynthesisable (`adam-huss-01-6-3faa2218`, `reelshort-actress-agathe-levi-01-6`,
   one with a typo). Always harvest hrefs.
2. Main-site tag ids (24-hex) with clean paginated filmographies.
   ⚠ The `/tags/movie-actors` index shows only 47 tags and repeats **the same 47 on every page** — trusting its
   pagination caps you at 94 actors. Derive the real roster from detail pages instead.

The native `/api/ms/*` endpoints are signature-gated (`channelId`, `ts`, `sign`). **We don't need them** —
every SSR page embeds the same JSON unsigned. No reason to go near the signing scheme.

### 1.3 MicroDramaRadar — currently unusable, ship it disabled

The whole site sits behind **Vercel Attack Challenge Mode**. Every path returns `HTTP 429` with
`x-vercel-mitigated: challenge` — including `/robots.txt`, `/favicon.ico`, and *nonexistent* paths. The
verifier re-tested 12 endpoints: all 429. Because even garbage paths 429, there is no observable 200-vs-404
distinction over HTTP at all.

Passing it requires executing a JS proof-of-work to obtain a `_vcrcs` cookie. **That is an access control, and
we are not going to defeat it.** Two further blockers: their robots.txt disallows `ClaudeBot`/`anthropic-ai`,
and WebFetch gets a hard 403 independent of the challenge.

⚠ Everything valuable ever reported about this site (26,568 English series, an 8.85 MB sitemap, a slug→native-id
cross-walk) came from a **single unreproducible browser session** and could not be independently verified.
Treat those figures as plausible-but-unaudited.

One genuinely useful, fully-open exception: the **Supabase poster CDN** needs no auth and doubles as a free
*platform-attribution oracle* —
`…/storage/v1/object/public/posters/{platform}/{series-slug}.webp` returns 200 on a hit and
`400 {"code":"NoSuchKey"}` on a miss, so you can determine which app a series belongs to without ever touching
the protected origin. Verified on slugs the original agent never saw.

**Decision:** build the adapter, ship it **disabled**, and attach a cheap daily `curl` probe of `/robots.txt`.
If the site owner ever turns the toggle off, the probe flips to 200 and we enable it. No bypass, no solver.

### 1.4 Enrichment — the brief's premise was inverted

Measured against 29 real ReelShort + DramaBox titles:

| Source | Coverage | Verdict |
|---|---|---|
| **IMDb** bulk datasets | **27/29 (93%)** — ReelShort 19/19, DramaBox 8/10 | **Primary enricher** |
| TMDB API | 9/29 (31%) | **Skipped** — see below |
| Wikidata | 0/3 | Skip — best licence, no data |
| MyDramaList | not present; `/v1/*` robots-disallowed, `api.` 403s | Skip |

**TMDB is skipped by decision.** Its unique contributions — overview text, poster paths, and
`external_ids.imdb_id` as an IMDb bridge — are already covered: synopsis and correct-aspect key art come from
the platforms themselves, and ReelShort hands us IMDb ids directly via `actor_info`. What remained was a
mandatory 6-month cache-expiry job and a logo-attribution requirement, for a third of the catalogue. Not worth
the ongoing complexity or the extra set of terms to honour. No adapter will be written.

IMDb is bulk TSV, refreshed **daily**, no API key, no rate limits, ~1.85 GiB compressed. Ratings existed for
10/10 sampled titles. AKAs are rich (18 regional rows on one title). ⚠ Files land in two waves (~00:40 and
~12:43 GMT), so snapshot per-file by ETag and only promote a batch when all seven are same-day.

**IMDb is also the only genuine rating source in the entire project** — both platforms' native ratings are
promotional filler (see above). That makes IMDb load-bearing for ratings, which collides with its licence
(§4 below).

What IMDb structurally lacks: **posters, synopsis, production company, streaming platform.** Those all come
from the platforms themselves, which is why skipping TMDB costs us nothing material.

**Title matching is the hard problem, and fuzzy matching will actively hurt.** These titles are generated from
a template grammar. A single query returned seven *different shows*:

```
Fated to My Forbidden Alpha · Fated to My Forbidden Guardian · Fated to My Forbidden Vampire
Fated to My Bad Boy Alpha   · Fated to My Swapped Alpha      · Fated to My Homeless Billionaire Alpha
```

Pairwise trigram similarity > 0.8. Any standard fuzzy matcher cross-links them confidently and wrongly.
Worse, generic titles collide with mainstream media — "Married at First Sight" resolves to three tconsts
including a 2014 reality franchise.

**The matching rule, therefore:**
1. Normalize (lowercase, strip non-alphanumerics, collapse whitespace) and match **exactly** against
   `primaryTitle` ∪ `originalTitle` ∪ `title.akas.title`. This alone hit 27/29 — fuzzy adds nothing but false
   positives.
2. Confirm with the **episode-shape fingerprint**: 50–80 episodes at ≤5 min mean runtime. No reality show
   shares that shape. ⚠ It's a strong positive confirmer but its *absence* proves nothing — some IMDb entries
   are series-level stubs with 1 episode row.
3. Year is a **±1 tiebreaker only**, never a filter — apps re-release and `startYear` reflects first air.
4. ⚠ Never filter on `titleType` — siblings are inconsistently `tvSeries` vs `tvMiniSeries`.
5. Everything that doesn't clear 1+2 goes to a **manual review queue**. No fuzzy auto-accept, ever.
6. Free shortcut: where ReelShort's `actor_info.imdb_id` exists, use it and skip matching entirely.

### 1.5 Headless browser and bot protection — the honest answer

**Neither primary source needs a browser.** Both serve complete JSON over plain HTTP. That's a large
simplification: no Playwright, no Chromium, a **304 MB image (measured at M0) instead of ~2 GB**, no
`shm_size`/`ipc: host` tuning, no browser memory ceiling.

**No CAPTCHA solving is needed either**, because neither primary source challenges a correctly-paced client:
- DramaBox: no protection at all, at any concurrency we tested.
- ReelShort: WAF challenge only appears at concurrency ≥16, and self-heals in ~34s. Pace at concurrency ≤4 and
  you never see it. The fix is *politeness*, not evasion.
- MicroDramaRadar: a real challenge — so we don't scrape it (§1.3).

The engineering work is therefore **classification and backoff**, not bypass: recognise 202/empty-body and
429/`Retry-After`, back off with jitter, circuit-break the source for the day, and surface an alert. If a source
ever starts challenging in a way pacing can't fix, the escape hatch is a **human-in-the-loop cookie hand-off**
(operator completes the challenge once in their own browser, pastes the cookie), not an automated solver.

---

## 2. Architecture

```
app/
  fetch/
    client.py        httpx + per-domain token bucket + robots (Protego) + conditional GET
    cache.py         raw response archive on disk, keyed by URL hash
    classify.py      OK | CHALLENGE | RATE_LIMITED | NOT_FOUND | ERROR   <- per-source validator
    breaker.py       per-source circuit breaker + state persistence
  sources/
    base.py          Source protocol: discover() / fetch_detail() / parse() / validate()
    dramabox.py
    reelshort.py
    microdramaradar.py   (disabled; probe only)
    imdb.py              (bulk TSV ingest)
  normalize.py       raw source record -> canonical dict; field precedence (§2.4)
  match.py           cross-source identity resolution (exact-normalized + episode fingerprint)
  search.py          search_titles(); FTS5 impl now, tsvector impl at the Postgres flip
  models.py          SQLAlchemy
  jobs/
    runner.py        subprocess worker, writes progress heartbeats
    scheduler.py     due-check, once-daily constraint, per-source lock
    types/           one module per job type (full_catalog, incremental, reparse, …)
  images.py          download, hash, downscale, pHash, provenance — posters and avatars
  api/
    routes.py        FastAPI REST
    admin.py         Jinja operator console
    auth.py          X-API-Key dependency
  cli.py             dramadex sync / status / search / reparse
tests/
  fixtures/          recorded raw responses — the parser corpus
    reelshort/challenge_202.bin      the silent-WAF response
    reelshort/tags_404_page375.html  404 that still carries __NEXT_DATA__
    dramabox/browse_empty.json       HTTP 200 with bookList: [] (valid, not an error)
    dramabox/redirect_stub.json      __N_REDIRECT page-1 trap
  test_matcher.py    incl. the "Fated to My …" negative control (§2.5)
```

**Fetch and parse are strictly separated.** Every raw response is archived to disk before parsing. This is the
highest-leverage decision in the whole design: parsers can be iterated offline against a fixture corpus with
zero network traffic, and those fixtures double as regression tests. When a site changes its shape, you replay,
you don't re-crawl.

### 2.1 Data model (sketch)

```
-- sources & studios ---------------------------------------------------------
source(id, key, base_url, enabled, watch_url_template, rate_json, config_json)
studio(id, source_id, name, slug, homepage_url)

-- titles --------------------------------------------------------------------
title(id, primary_title, normalized_title, year, synopsis, episode_count,
      first_paid_episode, audience_rating, completed, delisted_at,
      created_at, updated_at)
title_source(title_id, source_id, studio_id, external_id,
             url,          -- resolved canonical watch page, refreshed each sync (pretty, may rot)
             share_url,    -- first-party share link where published (ReelShort book_share_url)
             raw_sha256, first_seen, last_seen, delisted_at)
        UNIQUE(source_id, external_id)
title_alias(title_id, alias, normalized_alias, kind, region, source_id)
title_change(id, title_id, source_id, field, old_value, new_value, observed_at)

-- episodes ------------------------------------------------------------------
episode(id, title_id, source_id, external_id, number, name, duration_ms,
        is_free, still_image_id, published_at, first_seen)
        UNIQUE(source_id, external_id)

-- people --------------------------------------------------------------------
person(id, name, normalized_name, bio)
person_source(person_id, source_id, external_id, url, avatar_image_id)
        UNIQUE(source_id, external_id)   -- dramabox performerId | reelshort tag_id
                                         -- | fandom url | imdb nconst
title_person(title_id, person_id, role, billing_order, source_id)

-- taxonomy ------------------------------------------------------------------
tag(id, namespace, value)        -- namespace: dramabox.category | reelshort.1015 | imdb.genre …
title_tag(title_id, tag_id, source_id)

-- media ---------------------------------------------------------------------
image(id, kind, local_path, sha256, phash, width, height, bytes,
      source_id, source_url, fetched_at, license_tag)
title_image(title_id, image_id, kind, is_primary)

-- ratings, kept separable ----------------------------------------------------
rating(title_id, source_id, value, votes, scale, is_editorial)

-- identity resolution --------------------------------------------------------
match_candidate(id, title_id, target_source_id, target_external_id,
                method, score, evidence_json, state, decided_at, decided_by)

-- jobs ----------------------------------------------------------------------
sync_run(id, source_id, job_type, run_date, state, manual, started_at,
         finished_at, pid, cancel_requested, error, stats_json)
        UNIQUE(source_id, job_type, run_date) WHERE manual = 0
sync_progress(run_id, phase, items_done, items_total, rate_ewma, eta_seconds, heartbeat_at)
source_state(source_id, watermark, breaker_state, breaker_until,
             last_success_at, robots_sha256, schema_canary_json)
parse_quarantine(id, source_id, external_id, raw_sha256, error, created_at)
```

Notes on specific choices:

- **`UNIQUE(source_id, job_type, run_date) WHERE manual = 0`** — a *partial* index (supported identically by
  SQLite and Postgres). Scheduled runs are capped at one per source per job type per day at the database level;
  manually triggered runs bypass it, exactly as specified.
- **`episode` carries no video URL.** DramaBox's `chapterList[]` includes signed CloudFront mp4 links with an
  `Expires` param, and ReelShort's `online_base[]` is similar. They expire, they're not metadata, and storing
  them invites a use this project isn't for. We keep number, name, duration, free/paid flag and the still image.
- **`raw_sha256` instead of an inline `raw_json` blob.** The full payload lives in the on-disk archive
  (§2.7); the row stores its hash. That keeps the DB small and makes "did anything change?" a hash comparison.
- **`title_change`** gives a field-level history. These platforms retitle and re-shelve constantly — recon found
  79 DramaBox titles whose `shelfTime` had moved off `firstShelfTime`. Since we already hash every payload,
  diffing on hash mismatch is nearly free and answers "what changed and when".
- **`image.phash`** — a perceptual hash, computed once at download. Doubles as a cross-source match signal and
  catches key-art replacement. Free, since we're decoding the image to downscale it anyway.
- **`rating.is_editorial`** — flags DramaBox's floor-clamped 7.1–10 and ReelShort's 4.5–4.9 filler so they are
  *stored* (they're real fields on the source) but can never leak into sorting or into `title` as a real score.
- **`delisted_at`** — titles vanish from these catalogues. A stale `last_seen` sets this, which is what stops
  the "Watch on ReelShort" button from pointing at a dead page.

Three deliberate choices:
- **Provenance on every fact.** `source_id` is carried on aliases, tags, people links, ratings and images. That
  is what makes conflicts resolvable, IMDb strippable (§4), and a bad source revocable in one DELETE.
- **`UNIQUE(source_id, job_type, run_date)`** makes "once per day" a database invariant rather than an
  advisory check that races.
- **`studio` is its own table, not a string column.** One row per source platform, so a title can be linked to
  more than one studio when the same drama appears on both apps — which the cross-source matcher (§1.4) will
  find. That's what makes "available on DramaBox *and* ReelShort" representable.

### 2.2 Origin watch URLs

Every title must expose a link back to its origin so a downstream app can render a **"Watch on ReelShort"**
button. The durable form is the **id-only URL**, because both platforms 301-redirect it to the current
canonical slug — meaning it self-heals when a title gets renamed. Verified live on 2026-08-16:

```
https://www.dramabox.com/drama/42000013483
        → 301 → https://www.dramabox.com/drama/42000013483/Revenge-Puck            (200)
https://www.reelshort.com/movie/6858f90310d21493f5048723
        → 301 → https://www.reelshort.com/movie/poolboy-6858f90310d21493f5048723   (200)
https://www.dramaboxapp.com/film/41000117517                                        (200, no redirect)
```

So the design is:

- **`source.watch_url_template`** — e.g. `https://www.reelshort.com/movie/{external_id}` and
  `https://www.dramabox.com/drama/{external_id}`. The watch URL is *rendered* from the template plus the
  `external_id` we already store. Nothing to keep in sync, nothing that can rot, no storage cost.
- **`title_source.url`** — the resolved pretty canonical (with slug), refreshed on each sync. Nice for display;
  allowed to be stale, never the thing the button depends on.
- **`title_source.share_url`** — ReelShort publishes an official `book_share_url` in its search payload. Where
  a source gives us a first-party share link, we keep it and prefer it.

The API returns `watch_url` per studio on the title response, so the consuming app just reads a field.

Two caveats worth encoding:
- **DramaBox locales are separate catalogues** (`/es/browse` has zero bookId overlap with English), so the
  template pins the bare English path. ReelShort is the opposite — its locale prefixes 301 back to the English
  URL anyway, so there's nothing to pin.
- `dramaboxapp.com/film/{bookId}` is a working mirror form. We store the `www.dramabox.com` form as canonical
  and treat the mirror as derivable, not as a second row.

### 2.3 The source adapter contract

A new source is a Python module implementing one protocol, plus a `source` row. No config DSL — recon showed
every site needs real code (silent 202 challenges, redirect stubs, non-deterministic pagination, an
end-of-pagination 404 that still carries valid-looking JSON). A selector DSL could not express any of those, and
would leak the moment a site misbehaved.

```python
class Source(Protocol):
    key: str                     # "dramabox"
    studio_name: str             # "DramaBox"
    watch_url_template: str      # "https://www.dramabox.com/drama/{external_id}"
    rate: RateSpec               # concurrency, min_interval, daily_request_budget

    def preflight(self, ctx: Ctx) -> None:
        """Per-run setup. DramaBox re-reads the rotating buildId here."""

    def discover(self, ctx: Ctx, mode: Mode) -> Iterator[Ref]:
        """Yield title refs. mode is FULL or INCREMENTAL; the adapter owns
        watermark logic, multi-pass saturation, and stop conditions."""

    def fetch_detail(self, ctx: Ctx, ref: Ref) -> RawDoc: ...
    def parse(self, raw: RawDoc) -> SourceRecord:
        """Pure function: bytes in, normalized record out. No network. This is
        what the fixture corpus tests, and what `reparse` re-runs offline."""

    def classify(self, resp: Response) -> Verdict:
        """OK | CHALLENGE | RATE_LIMITED | NOT_FOUND | EMPTY | ERROR"""

    def roster(self, ctx: Ctx) -> Iterator[PersonRef] | None:
        """Optional. ReelShort implements it; DramaBox returns None (its actors
        are derived from detail records instead)."""
```

`parse` being a pure function is the load-bearing part: it makes the entire parser layer testable offline
against recorded fixtures and re-runnable over the archive without touching the network.

**`classify` is per-source because the failure signatures are genuinely different**, and getting this wrong is
the single most expensive bug available:

| Source | Verdict | Signal |
|---|---|---|
| ReelShort | `CHALLENGE` | HTTP 202 · `content-length: 0` · `x-amzn-waf-action: challenge` — **not** 4xx/5xx |
| ReelShort | `OK` | HTTP 200 **and** body contains `__NEXT_DATA__` **and** `tagBooks`/`data` is non-null |
| ReelShort | `NOT_FOUND` | HTTP 404 — note the body still contains `__NEXT_DATA__` with `page:"/404"` |
| DramaBox | `OK` | HTTP 200 — **including `bookList: []`**, which is a valid empty result, not an error |
| DramaBox | `NOT_FOUND` | HTTP 404 `{"notFound": true}` (only the `/name/{id}` space returns this) |
| DramaBox | `ERROR` | `__N_REDIRECT` stub where a record was expected, or a stale-buildId 404 |

Note the asymmetry: an empty list means "no results" on DramaBox and "you got blocked" on ReelShort. A shared
classifier would be wrong for one of them.

### 2.4 Normalization and field precedence

Every source writes into `title_source`; a merge step computes the canonical `title` row. Precedence is
explicit per field rather than "last writer wins".

**Implemented at M4** in [`app/precedence.py`](app/precedence.py). Two design points that turned out to matter:

- Each source stores its own view in `title_source.parsed_json`, and the canonical row is **recomputed from
  every contribution** on each write. Applying only the incoming record would make the result depend on which
  source synced last — the kind of bug you notice months later when a field flips for no reason. A test asserts
  `resolve([a, b]) == resolve([b, a])`.
- Precedence is expressed over source **kinds** (`PLATFORM` / `ENRICHER`), not source names, so adding a source
  does not mean editing the table. Ties between two platforms break on the oldest link, which is deterministic
  rather than an arbitrary preference for one app.

Measured at M4, the two platforms' rating scales side by side — the argument for excluding them, in one line:

```
dramabox   n=50   editorial=50   range 7.1–10.0
reelshort  n=64   editorial=64   range 4.5–4.9
```

Non-overlapping, neither a real distribution. `rating` is deliberately absent from `FIELD_PRECEDENCE` **and**
from the `Title` table, so no code path can sort by them. A whole-database audit at M4 scanned 775
table/column/marker combinations and found **zero** signed video URLs stored anywhere.

| Field | Precedence | Reasoning |
|---|---|---|
| `primary_title` | Platform > IMDb | The platform's catalogue is authoritative for its own title |
| aliases | **Union of all** | ReelShort `book_sub_title`, DramaBox `bookNameEn`/`replacedBookName`, IMDb `akas` |
| `year` | Platform release > IMDb `startYear` | Platform date = when it actually appeared; IMDb = first air. Both kept per-source |
| `synopsis` | Platform only | IMDb ships none; the platform copy is what users saw |
| genres/tags | **Union, namespaced** | No forced single vocabulary — see below |
| `rating` | **IMDb only** | Both platforms' ratings are filler (§1.1, §1.2); stored with `is_editorial=1` |
| `audience_rating` | ReelShort tag ns `1015` | The only source that publishes one at all |
| `episode_count` | Platform | Authoritative; IMDb episode rows used only as a match confirmer |
| poster | Platform | Correct 2:3/9:16 aspect; IMDb has no images |
| cast | Union, deduped by person identity | Platform + IMDb `principals` |

**Tags stay namespaced (`dramabox.category`, `reelshort.1015`, `imdb.genre`) rather than being merged into one
vocabulary.** ReelShort alone has 15 tag namespaces and 228 distinct tags appeared in just 30 sampled detail
pages. Flattening them would destroy information and force arbitrary judgement calls. A curated
`tag_alias` mapping for the common cases (Romance, Revenge, CEO, …) can come later, driven by real query needs
rather than guessed up front.

Derived fields worth naming: `year` comes from `firstShelfTime` on DramaBox (no year field exists) and
`online_at` on ReelShort. `completed` cannot come from DramaBox's `lastUpdateTimeDisplay` — that string is
`"Completed"` on 100% of records and carries zero information.

### 2.5 Cross-source identity resolution

Two separate problems, often conflated:

**(a) Platform → IMDb (enrichment).** Rules in §1.4. Auto-accept only on one of:
- ReelShort handed us `actor_info.imdb_id` directly — no matching required, and it's free.
- Exact normalized title match against `primaryTitle` ∪ `originalTitle` ∪ `akas`, **and** the episode-shape
  fingerprint confirms (count within tolerance, mean runtime ≤5 min).

Everything else becomes a `match_candidate` in state `proposed` with `evidence_json` recording *why* it was
proposed — the candidate titles, the similarity, the episode counts compared. The admin review queue shows that
evidence so a decision takes seconds rather than a re-investigation.

**(b) Platform → platform (same drama on both apps).** Same skeleton, different confirmers: episode count,
release date proximity, and **poster pHash**, which is a strong signal because both apps often carry the same
key art. Always routed through review — never auto-merged. A wrong merge here silently fuses two different
dramas, and is far harder to detect afterwards than a missing link.

**The negative-control test case, which ships as a real test:** the `Fated to My {Forbidden Alpha | Forbidden
Guardian | Forbidden Vampire | Bad Boy Alpha | Swapped Alpha | Homeless Billionaire Alpha}` family. Six
different shows, pairwise trigram similarity > 0.8. The matcher must produce **zero auto-accepts** across that
set. If a future change makes it link any two of them, the test fails.

Person identity has the same hazard in a milder form — actor names collide, and DramaBox exposes
`allLanguagesActorname` (per-locale spellings) which is useful for reconciliation but also a false-positive
source. People merge on exact normalized name **within** a source, and across sources only via an explicit id
(IMDb `nconst` from ReelShort's `actor_info.actors[].outside_url`, or a fandom `inside_url` join).

### 2.6 Search and indexing

Search lives behind one function, `search_titles(q, filters, cursor)`, with a SQLite FTS5 implementation now
and a Postgres `tsvector` implementation added at flip time. Only the implementation is dialect-specific; no
engine-specific SQL leaks into callers, and no Postgres-only extension (`pg_trgm`) is depended on anywhere.

- **FTS5 external-content table** over `primary_title`, aliases, `synopsis`, and cast names.
- **`normalized_title` / `normalized_alias` B-tree indexes** — the matcher's hot path is exact-on-normalized
  lookup, and it must not table-scan.
- **`title(updated_at, id)` composite index** — this is what makes `?updated_since=` keyset pagination correct
  and cheap for your downstream app.
- Filter indexes: `title.year`, `title_tag(tag_id)`, `title_source(studio_id)`, `title_person(person_id)`.

### 2.7 Job engine

- **Job types:** `full_catalog` · `incremental` · `detail_backfill` · `actor_roster` · `imdb_refresh` ·
  `poster_fetch` · `reparse` · `link_health` · `robots_check` · `probe`.
- **Once daily, enforced by the unique index.** A manual trigger sets `manual=true` and bypasses it.
- **Run states:** `pending → running → succeeded | failed | cancelled`. `run_date` is computed in a configured
  timezone (`DRAMADEX_TZ`, default UTC) and stored as a DATE, so "once a day" has an unambiguous boundary.
- **Cancellation** is cooperative: the admin sets `cancel_requested`, the worker checks it between items and
  exits cleanly, committing what it has.
- **No same-day auto-retry.** A failed scheduled run waits for tomorrow — retrying inside the day would violate
  the once-daily rule. Manual re-trigger is always available.
- **Per-source lock:** one row in `source_state` holding the owning pid. Same site never runs twice
  concurrently; different sites run in parallel. Matches the stated requirement exactly.
- **Separate processes:** each job runs as a `multiprocessing.Process`. Workers write a progress heartbeat
  (`items_done`, `items_total`, EWMA rate → ETA) that the API reads. A worker that dies without finishing is
  reaped by heartbeat staleness and marked `failed`, not left hanging.
- **SQLite in WAL mode**, batched writes. At ~10k titles this is comfortably within range; the SQLAlchemy layer
  keeps the Postgres flip cheap when concurrency grows.

**Cadence:**

| Job | Source | Cadence | Est. cost |
|---|---|---|---|
| `incremental` | DramaBox | daily | ~3–10 requests |
| `incremental` | ReelShort | daily | 1 shelf request + N new details |
| `detail_backfill` | both | daily, bounded | fills cast/exact counts for new ids |
| `actor_roster` | ReelShort | weekly | 229 fandom pages, paced |
| `full_catalog` | DramaBox | once, then quarterly audit | 1,055 requests, ~6 min |
| `full_catalog` | ReelShort | once, multi-pass to saturation | ~374 × N passes at **concurrency 4**, looped until the unique-ID set stops growing |
| `imdb_refresh` | IMDb | weekly | 7 files, ETag-conditional |
| `probe` | MDR | daily | 1 request |

Plus the low-cost hygiene jobs: `robots_check` (1 request — re-hash each source's robots.txt and **halt the
source if its rules tightened**), `link_health` (sample watch URLs, flag dead ones), `probe` (MicroDramaRadar
availability), and `reparse` (no network at all — re-run parsers over the archive after a parser fix).

**Progress and ETA.** `items_total` is known after `discover()` for full crawls and estimated for incremental.
Workers heartbeat every 5s or N items with `items_done` and an EWMA rate over the last 30 seconds; ETA is
derived from that rather than from a naive average, so a run that slows down reports honestly. A run whose
heartbeat is >2 minutes stale with no live pid is reaped to `failed`.

**Circuit breaker.** Consecutive `CHALLENGE`/`ERROR` verdicts trip the source to OPEN for the remainder of the
day and surface an alert. This is what keeps a broken parser or a newly-hostile site from turning into a
thousand pointless requests.

**Test mode:** `SOURCE_MAX_ITEMS=50` per source, applied to `full_catalog`, as requested.

### 2.8 Poster and avatar pipeline

One pipeline serves both title key art and actor avatars.

1. Fetch from the platform CDN (conditional GET where ETag/Last-Modified are offered).
2. Compute **sha256** → content-addressed path `posters/{sha[:2]}/{sha}.webp`. Identical art reused across
   titles stores once.
3. Downscale to max 720px on the long edge, WebP q80, aspect preserved. We do not warehouse originals.
4. Compute **pHash** during the decode we're already doing (§2.5 uses it as a match signal).
5. Record `width`, `height`, `bytes`, `source_url`, `fetched_at`, `license_tag`.
6. Skip entirely if the sha already exists — re-runs cost nothing.

DramaBox cover URLs carry a size suffix (`…42000013483.jpg@w=240&h=400`) which we rewrite upward before
fetching rather than storing the thumbnail. Served from the API at `/titles/{id}/poster`, so your downstream app
never hotlinks a third-party CDN.

### 2.9 API surface

Every endpoint except `/health` requires `X-API-Key`. FastAPI generates OpenAPI, so your downstream app can
generate a typed client rather than hand-writing one.

```
GET  /health                                        no auth; liveness only
GET  /titles?q=&studio=&genre=&year=&audience=&person=&updated_since=&limit=&cursor=
GET  /titles/{id}                                   full record incl. studios[] with watch_url
GET  /titles/{id}/episodes
GET  /titles/{id}/poster                            served from local store
GET  /people?q=                     GET /people/{id}          (+ filmography)
GET  /studios                       GET /genres
GET  /sync/runs?source=&state=      GET /sync/runs/{id}       (live progress + ETA)
POST /sync/runs                     {source, job_type, force}  manual trigger
POST /sync/runs/{id}/cancel
GET  /matches?state=proposed        POST /matches/{id}/confirm | /reject
GET  /stats                         coverage + footprint (§2.11)
```

The title response carries what the button needs, per studio:

```json
{ "id": 1412, "primary_title": "Bound by Love", "year": 2026, "episode_count": 119,
  "studios": [ { "key": "reelshort", "name": "ReelShort",
                 "external_id": "69d91b3ee15105112e0dc022",
                 "watch_url": "https://www.reelshort.com/movie/69d91b3ee15105112e0dc022",
                 "share_url": null, "delisted_at": null } ] }
```

**Pagination is keyset on `(updated_at, id)`, not offset.** Offset pagination silently skips or repeats rows
when the underlying data changes mid-scan — which it will, since syncs run while your app polls. Keyset
`?updated_since=` is what makes incremental consumption actually correct.

### 2.10 Admin UI

Server-rendered Jinja templates. No JS build step, no bundler, no framework — it is an operator console, not a
product surface.

- `/admin` — sources, last run, next due, breaker state, today's request footprint.
- `/admin/runs` — live progress bars with ETA, cancel button, run history and errors.
- `/admin/matches` — the review queue, showing `evidence_json` so a decision is one glance.
- `/admin/titles/{id}` — canonical record beside each source's raw payload, plus the change log.
- `/admin/quarantine` — records that failed to parse, with the raw doc, for fixing parsers.

### 2.11 Configuration and observability

Config via environment variables through `pydantic-settings`: `DRAMADEX_DB_URL`, `DRAMADEX_DATA_DIR`,
`DRAMADEX_API_KEY`, `DRAMADEX_TZ`, `DRAMADEX_USER_AGENT`, `DRAMADEX_MAX_ITEMS`, and per-source
`DRAMADEX_<SOURCE>_ENABLED` / `_CONCURRENCY`. Per-source rate limits live in the `source` row so they're
tunable without a redeploy.

**Identify the crawler honestly.** A descriptive User-Agent with contact info is both good citizenship and
practically useful — it means a site operator who objects can reach you instead of silently blocking you.

Structured JSON logs, plus counters persisted per run in `stats_json`: origin fetches, cache hits,
revalidations, verdicts by kind, parse failures, items written. `/stats` exposes two things worth watching:

- **Field coverage per source** — what fraction of titles actually have cast, audience rating, alt titles. Given
  that ReelShort cast is only ~20% populated, knowing real coverage prevents mistaking sparse data for a bug.
- **Daily request footprint per source** — the number that tells you whether you're being a good guest.

**Schema-drift canary.** Each source declares a set of keys it expects on every record. If the presence rate of
any of them drops below a threshold during a run, the run fails loudly with a diff rather than quietly writing
thousands of half-empty rows. Sites change shape; `buildId` rotation already proves these are live systems.

**Parse failures are quarantined, not fatal.** A record that fails to parse lands in `parse_quarantine` with its
raw doc and the exception; the run continues. Fix the parser, then `reparse` — no re-crawl.

---

## 3. Build order

Each step has a verifiable success criterion; nothing proceeds until its check passes.

| # | Step | Verify |
|---|---|---|
| M0 | Skeleton: uv, models, alembic, `/health` | `docker compose up` → 200; `alembic upgrade head` creates schema; partial unique index present |
| M1 | Fetch layer: client, token bucket, robots, archive, classifier, breaker | Unit tests over fixtures; **the ReelShort 202-empty fixture classifies as `CHALLENGE`, and the DramaBox empty-`bookList` fixture classifies as `OK`** |
| M2 | DramaBox adapter, `full_catalog` capped at 50 | 50 titles with title/synopsis/genres/episodes, linked to the DramaBox studio; re-run inserts 0 rows; **every `watch_url` resolves 200**; page-1 redirect stub handled |
| M3 | ReelShort adapter, capped at 50 | 50 titles, cast linked where present, **0 WAF challenges at concurrency 4**; every `watch_url` resolves 200; page-375 sentinel correct |
| M4 | Episodes + normalization + precedence | Episode rows for all 100 titles; no video URLs stored anywhere; `rating.is_editorial=1` on all platform ratings |
| M5 | Job engine: states, locks, progress, breaker | Two sources run concurrently; same source twice in a day → rejected; manual override works; `kill -9` a worker → reaped to `failed`; cancel commits partial work |
| M6 | REST API + auth + admin UI | Keyset `?updated_since=` returns no dupes across a concurrent write; missing API key → 401; progress page shows live ETA |
| M7 | Images: posters + avatars | Files content-addressed by sha256; pHash populated; re-run refetches nothing |
| M8 | IMDb ingest, matcher, review queue | **The "Fated to My …" negative control produces zero auto-accepts**; ReelShort `actor_info.imdb_id` shortcut used where present; unmatched land in the queue with evidence |
| M9 | Incremental + scheduler + hygiene jobs | Watermark advances; DramaBox daily run < 10 requests; `robots_check` halts a source on tightened rules; `reparse` runs with the network disabled |
| M10 | Docker packaging + deploy | Fresh host: `docker compose up` → working instance; volumes persist across recreate; restore-from-backup verified |

Uncapping the 50-item limit happens between M8 and M9, once the parsers are proven against real data. The first
uncapped DramaBox run is ~1,055 requests / ~6 minutes; the first ReelShort run is multi-pass at concurrency 4
and should be expected to take hours.

---

## 4. Deployment

Single image, `python:3.14-slim`, dependencies via `uv` — no Chromium, no browser, no Redis, no separate queue
broker. **Measured at M0: 304 MB** via a multi-stage build that keeps `uv` out of the runtime layer. (My
pre-build estimate of ~180 MB was wrong: the `python:3.14-slim` base and the compiled wheels — pydantic-core,
uvloop, watchfiles, greenlet — account for the difference. Dropping `uvicorn[standard]` to plain `uvicorn`
would save perhaps 30 MB and is not worth the loss of uvloop.)

```yaml
services:
  dramadex:
    build: .
    environment:
      DRAMADEX_DB_URL:   sqlite:////data/dramadex.db
      DRAMADEX_DATA_DIR: /data
      DRAMADEX_API_KEY:  ${DRAMADEX_API_KEY:?required}
      DRAMADEX_TZ:       UTC
    volumes: [ "./data:/data" ]          # db + raw archive + images
    ports:   [ "127.0.0.1:8080:8080" ]   # bind loopback; expose via your own proxy
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8080/health')"]
```

One container runs the API, the scheduler, and forks job workers. Runs as a non-root user. Everything mutable
lives under `/data`, so backup is "copy one directory" — but note SQLite must be backed up with the
`.backup` API or with WAL checkpointing, not a naive `cp` of a live database.

**Archive growth.** The raw response archive is the one thing that grows without bound. Responses are stored
gzipped and content-addressed; only the latest payload per URL is retained by default, with a
`DRAMADEX_ARCHIVE_KEEP_VERSIONS` knob for when you want history. At ~10k titles this is on the order of a few
hundred MB.

**Postgres flip**, when it's wanted: change `DRAMADEX_DB_URL`, add the `tsvector` branch in `search.py`, run
migrations. Nothing else should need to change — which is precisely why no `pg_trgm`, no SQLite-specific SQL,
and no raw dialect strings appear outside `search.py`.

---

## 5. Testing

The archive-first design makes most of this cheap, because parsers are pure functions over recorded bytes.

- **Golden parse tests** — a fixture corpus of real recorded responses per source; `parse()` output is asserted
  field-by-field. These are the regression net when a site changes shape.
- **Adversarial fixtures**, one per trap recon actually found: the ReelShort silent-202, the 404-with-valid-
  `__NEXT_DATA__`, the DramaBox `__N_REDIRECT` page-1 stub, the DramaBox HTTP-200-with-empty-list, a stale
  `buildId` 404.
- **Matcher precision test** — the "Fated to My …" family as a negative control; zero auto-accepts required.
- **Idempotency test** — running any sync twice writes zero new rows the second time.
- **Job engine tests** — once-daily rejection, manual bypass, concurrent-different-source, serial-same-source,
  worker reaping, cooperative cancel.
- **No network in CI.** Every test runs against fixtures. A separate, explicitly-marked `live` suite hits the
  real sites and is run manually — it's the canary for "did the site change", not a gate on every commit.

---

## 6. Things you need to decide or accept

**IMDb licensing — resolved, but stay inside the line.** The non-commercial licence permits personal use but
states the data "must not be altered/republished/resold/repurposed to create any kind of online/offline
database of movie information" — carved out only for *individual personal use*. **Dramadex is a single-user
self-hosted instance, so it sits inside that carve-out** and we use the bulk datasets fully.

What would move it outside: publishing the instance, multi-tenanting it, putting it behind a paywall, or
exposing a public API over it. Because IMDb is also our **only real rating source**, that boundary is
load-bearing rather than academic. Two mitigations are baked in from day one: IMDb-derived rows carry
`source_id` so they can be excluded from any export or dropped wholesale without touching platform data, and
the required attribution string ships with the app — *"Information courtesy of IMDb (https://www.imdb.com).
Used with permission."* If the project ever becomes shareable, that flag is the switch.

**"Studio" = the source platform — resolved.** Clarified 2026-08-16: studio means ReelShort, DramaBox, etc.,
which is exactly what the sources do publish, so there is no gap. Modelled as a first-class `studio` table with
one row per source (§2.1), linked from `title_source`, and many-to-many with titles so a drama appearing on
both apps shows both studios.

Worth recording what *isn't* available, so it doesn't get confused with the above later: no source publishes the
actual **production company**. DramaBox's `author` is a distributor label and effectively always empty,
ReelShort's `book_source` is an unlabelled integer with no published mapping, and MicroDramaRadar's `publisher`
is the app rather than the producer. If production-house data is ever wanted, it would have to be manual entry.

**Audience rating (PG/R/M) will be sparse.** DramaBox publishes none at all. ReelShort has it under tag
namespace 1015 (~73% of titles, values like "All Ages"). IMDb's bulk files carry no certificates. Expect this
field to be populated for roughly ReelShort-only.

**Year is derived, not given.** DramaBox has no year field (derive from `firstShelfTime`); ReelShort has
`online_at`; IMDb has `startYear`. These will occasionally disagree by a year — we store per-source and pick a
precedence.

**Posters stay private.** Locally-cached third-party key art for a single-user instance is defensible; re-serving
that cache publicly is redistribution of copyrighted promotional art, which no source here licenses.

---

## 7. Recommended additions

### 7.1 Already folded into the plan above — each is close to free

These earn their place because the expensive part already exists for another reason.

| Addition | Why it's nearly free |
|---|---|
| **`title_change` field-level history** | We already hash every payload; diff on hash mismatch. Answers "what changed, when" on platforms that retitle constantly |
| **Delisting detection + `link_health`** | `last_seen` is already stored. This is what stops your "Watch on ReelShort" button pointing at a dead page |
| **Poster pHash** | We decode every image to downscale it anyway. Doubles as a cross-source match signal and catches key-art swaps |
| **`reparse` job** | The raw archive already exists. Fix a parser, re-run over 10k records with the network off, zero requests |
| **`robots_check`** | One request a day. If a source tightens its rules we stop, instead of finding out by getting banned |
| **Schema-drift canary** | A handful of key-presence asserts. Fails loudly instead of writing thousands of half-empty rows |
| **`parse_quarantine`** | One table. A bad record doesn't kill a 6,000-item run |
| **Coverage stats** | A few SQL aggregates. Given ReelShort cast is only ~20% populated, this is how you tell sparse data from a bug |
| **Actor avatars** | Same pipeline as posters, different `kind` |
| **New-episode detection** | Falls out of per-episode rows: compare stored count to fetched. Catches ongoing series adding episodes, which the release-date watermark never sees |

### 7.2 Worth doing later — genuinely valuable, not needed for v1

- **Re-release / duplicate detection within a platform.** These apps republish the same drama under new titles,
  and DramaBox hands us the evidence: `originalBookId` and `replacedBookName` are already on every record. Add
  poster pHash and you can cluster re-releases rather than listing them as unrelated shows. This is the most
  interesting domain-specific feature available, and I'd put it first on the v2 list.
- **Multi-locale catalogues as an alias source.** DramaBox runs 17 locale catalogues with **zero ID overlap**.
  Crawling one non-English locale and matching by poster pHash and episode count would yield genuine alternate
  titles — the field you asked for that is currently sparse. DramaBox's `allLanguagesActorname` gives the same
  for actor-name transliteration.
- **Curated tag vocabulary.** A `tag_alias` mapping to collapse `dramabox.category:Romance`,
  `reelshort.1010:Romance` and `imdb.genre:Romance` into one browsable facet — driven by real query needs, once
  you've used the data and know which facets you actually reach for.
- **RSS/Atom feed of new titles.** Trivial on top of `?updated_since=`, and a pleasant way to watch a personal
  indexer without writing a client.
- **`backup` job type.** Scheduled SQLite `.backup` (not `cp`) plus an image manifest, written to the volume.
- **Webhooks.** You chose polling, which is right to start; a `POST` on new-titles-found is ~30 lines whenever
  polling starts to chafe.

### 7.3 Deliberately not doing

Recorded so these don't get quietly reconsidered later without the reasoning:

- **CAPTCHA/challenge bypass.** Not built, for MicroDramaRadar or anything else. If a source challenges,
  the answers are pace correctly, or a one-off human cookie hand-off, or don't scrape it.
- **Storing video URLs or media.** Both platforms hand us signed, expiring stream URLs in the same payload we
  parse. We take metadata and leave those. This is a metadata indexer.
- **Public redistribution.** Posters and IMDb-derived rows stay on your instance (§6).
- **A config DSL for sources.** Recon showed every site needs real code; a selector language would be a leaky
  abstraction from day one.
- **A headless browser.** Nothing in scope needs one, and it would cost ~1.8 GB of image for zero benefit.

---

## 8. On the "Karpathy method"

Worth being accurate about what that repo is, since it shapes how I'll work rather than what I'll build.
It's **one skill, four principles, ~65 lines**, derived from a single Karpathy tweet — and it is about *agent
behaviour*, not code style. It says **nothing** about dependencies, naming, file size, or project structure.

The four principles, applied here:
1. **Think before coding** — surface assumptions, present interpretations rather than picking silently.
   That's what §4 and the questions above are for.
2. **Simplicity first** — "no abstractions for single-use code", "no flexibility that wasn't requested".
   Concretely: no Playwright (nothing needs it), no Celery/Redis (a job table and subprocesses suffice), no
   plugin system until there's a third source that actually needs one.
3. **Surgical changes** — match existing style, don't refactor what isn't broken, don't drive-by-clean.
4. **Goal-driven execution** — every step gets a verifiable success criterion. That's §3.

Its own stated acceptance test: *fewer unnecessary changes in diffs, fewer rewrites due to overcomplication,
clarifying questions before implementation rather than after mistakes.*
