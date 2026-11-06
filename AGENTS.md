# Notes for an incoming agent

Read this before `README.md` and `docs/ARCHITECTURE.md`. Both are stale in ways that matter.

## The prose docs are stale

- **`README.md:7-11` and `docs/ARCHITECTURE.md:16` both say "two roles."** There are **three**:
  `admin`, `viewer`, `viewer_r`. ARCHITECTURE.md calls itself "the contract the implementation
  follows", which makes it especially convincing. Trust `backend/app/models/base.py:73-91` and
  `backend/app/models/identity.py:54-68`.
- **`docs/ARCHITECTURE.md` still describes TMDB and SearXNG** as metadata sources. Both were
  deleted. Dramadex is the only source now.
- **`backend/app/services/importer.py:10-13` claims `LIBRARY_ROOT` is mounted read-only** and that
  nothing ever writes media. The renamer writes, gated behind an admin action — see
  `docker-compose.yml` and `docs/ARCHITECTURE.md:20-26`.

## Roles and visibility

Three role values, **two** visibility tiers, and the restriction filter is spread across roughly
fifteen call sites rather than one chokepoint.

- **Always ask the User object: `user.sees_restricted`** (`backend/app/models/identity.py:54-68`).
  Never `user.role.sees_restricted` — `users.role` is a `String(16)`, so a row read back from the
  database carries the plain string `"viewer_r"`, not the enum. Test fixtures construct the enum,
  so the mistake passes tests and fails in production.
- **It fails open in two known, deliberate ways. Leave them.** (1) The `0012` seed flags only
  clearly-adult ratings, so an unclassified series is visible. (2) Offline revocation only
  reconciles at the next start with a server in reach. Both look like bugs; the owner knows.
- **Register episode and image routes through `playable()`** (`backend/app/api/stream.py:74-110`)
  and nothing else. The guards attach *inside* the decorator by path shape rather than being
  called from handlers, so a route registered any other way silently inherits no check.
- A role change revokes every `AuthSession` row for that user (`backend/app/api/admin.py:576-580`).
  Forcing a new sign-in is the point, not an oversight.
- Leave `restricted` out of `SERIES_METADATA_FIELDS`. Classifying a row is a household decision,
  not curation of its metadata.

## Every episode is a vertical short

Median duration is **72 seconds**, maximum 295. **Any absolute number of seconds is suspect.** A
120-second rule written for television already shipped and marked a 121-second episode "watched"
two seconds in. Before touching a threshold, ask whether it is a **duration or a fraction** —
fractions are almost always right.

## Offline

- **Only test offline against the HTTPS origin.** A LAN address is not a secure context: no
  service worker, no Cache Storage, and storage is per-origin, so LAN testing proves nothing
  either way. `offlineSupported()` in `frontend/src/offline/register.ts:26-31` encodes this.
- **Do not check `'serviceWorker' in navigator` to decide whether a device can download.** On iOS
  every browser is WebKit, but Apple exposes the API only to Safari, SFSafariViewController and
  Home Screen web apps. Probe the capability; record a refusal as a state distinct from "no API".
- **Only `dramatarr-shell-*` caches are sweepable** (`frontend/src/offline/constants.ts:31-33`).
  Broadening the `activate` sweep to `API_CACHE` or `MEDIA_CACHE`, or giving them a build id for
  consistency, deletes a library someone spent a night downloading — and reads as cache hygiene.
- **Do not call `skipWaiting()`** in the install handler or remove `updateViaCache: 'none'`. A new
  build waits, the app says so, and the person chooses.
- `available` on an episode means **"the NAS holds a file"**, never "downloaded on this device".
  Use the single `usePlayable()` predicate (`frontend/src/offline/useOffline.ts:82`) everywhere, so
  surfaces cannot disagree. A full download hides the difference; it only surfaces on a partial.
- **Leave `MEASURE_CONCURRENCY = 4`** and keep downloads sequential. Eight produced a real 504.
- **Do not re-wrap cached bytes as `new Response(blob)`** or reach for blob URLs or a range shim.
  It drops `Content-Length` and Safari fails hard. Return the network response as-is.

## Storage and scanning

- **This project is SQLite (WAL), not Postgres.** Dramadex is the Postgres one; they sit side by
  side and share an owner, which makes this easy to get wrong. See `backend/app/config.py` and
  `docker-compose.yml`.
- **Do not add a second API or worker container.** The job queue's serialisation guarantee comes
  from there being exactly one — `docs/ARCHITECTURE.md:51-53` states it outright.
- **An unscoped incremental scan marks the entire library MISSING, silently**, and the result is
  indistinguishable from the NAS mount having gone away. `_sweep_missing` marks every known file it
  did not see, and an incremental pass deliberately never looks inside skipped directories. Read
  `backend/app/services/scanner/scan.py:840-872` before touching the scanner.
- **Most real configuration is database rows, not environment variables**
  (`backend/app/services/settings.py:41-65`) — SMTP, the Dramadex URL, the scan schedule. An audit
  that stops at `.env.example` concludes the app is unconfigured.
- The nightly scan cron is evaluated against **UTC**, not local time. Do not "fix" that by
  changing `TZ`; change the expression.

## The Dramadex seam

Two live defects, both silent, both on this side:

- **`CastMember` drops `photo_url`.** Dramadex's `PersonBrief` carries it; the dataclass in
  `backend/app/services/dramadex.py` declares only `person_id, name, role, billing_order`, so
  every cast face falls back to initials regardless of what the indexer has. The person-*detail*
  path handles photos correctly, which is why this misdiagnoses as a missing upstream endpoint —
  `GET /people/{id}/photo` has existed since August. The fix is four lines here.
- **The title normaliser facing Dramadex is the wrong one.** `dramadex.py:60` uses `[^a-z0-9]+`,
  which deletes every non-ASCII character, so CJK and Cyrillic titles collapse to keys that can
  cross-match unrelated shows. There are **two** functions named `normalize_title` in this repo;
  the other (`app/services/search/normalize.py:79`) preserves those scripts but disagrees with
  Dramadex on apostrophes, so it is not a drop-in fix. Port Dramadex's actual rule.

Also worth knowing: the client parser hard-requires only `id` and `primary_title`, so a field the
indexer renames or stops sending **raises nothing** — it quietly disappears from the household's
screens. Eleven-plus field groups it sends today are already dropped, `updated_at` among them.
There is no version handshake, and the indexer's `/health` version is pinned and useless as one.
`DramadexClient.best_match` is on **no live code path** despite being the best-documented function
in the client — do not reason about current safety from it.

## Known-broken, low stakes

- **`npm run lint` does not work.** The script is declared but eslint is in no dependency block
  and the flat config is untracked. The real frontend gates are `npx tsc -b` and
  `npx tsc -p tsconfig.sw.json` (the service worker is typechecked separately).
- **`.github/workflows/pr-review.yml:42,49,51` contain backticks inside double quotes**, which
  bash executes as command substitution. Under `set -e` the job **dies** instead of warning,
  taking the missing-migration and i18n-parity checks with it. The sensitive-files check is the
  only one of the three that currently works. That workflow also posts nothing to the PR despite
  its name and `pull-requests: write` — findings land in the Actions run summary.

## Process

- **Never run two agent workflows that touch `frontend/src/i18n/{es,en}.json`.** The bundles are
  read-modify-written whole, so the second writer drops the first's keys — and the parity test
  only compares the two files against each other, so it stays green when both lost the same keys.
  Sequence them, and run the seam checkers before deploying.
- **This repo was built by many parallel agents, and almost every real defect lived in the seam
  between two of their scopes** — not inside either. When you split work, check the joins.
- **Deploy only from the repository's main worktree.** `scripts/deploy.sh` computes its source as
  the tree the script lives in, so running it from a secondary worktree would ship that tree.
  Check `git worktree list` — a clean `git status` reports the current worktree only.
- The server's `.env` is the one piece of state not in git, and the deploy's `rsync --delete` will
  erase it unless excluded; excluding it is the entire reason that script exists. Anything Compose
  needs is pinned **inside** version control for the same reason — leave the project `name` in
  `docker-compose.yml` alone, or Compose infers it from the directory and builds a second parallel
  stack against the same database and library mount.
- **`scripts/remote-verify.sh:51-64` applies a destructive library cleanup with no prompt**, in a
  script named "verify". Its first cleanup call is a dry run that prints what would go, which
  reads like the whole step; three lines later it does it for real.
- The app's Spanish is **neutral Latin American**, not the owner's own Rioplatense voseo, even
  though they write voseo themselves. `scripts/check_neutral_spanish.py` is the reference and CI
  runs it. English technical terms they mix in are deliberate.
- **Nothing new leaves the house without asking the owner** — no digests, no push notifications,
  no second kind of outbound mail. Notify only on terminal outcomes, and on the *transition* into
  one rather than on the status itself, or an admin fixing a typo later re-sends it.

## Related projects

This is one of three. The indexer it reads metadata from is a separate project, and a third
project downloads video. **The third is deliberately not wired to this one** — the admin UI links
to it as a bookmark, and the comments beside that constant say so explicitly. The owner has
declined connecting them twice. A fuller cross-project handoff exists outside version control,
alongside the repositories.
