# dramatarr

A self-hosted, invite-only series library for one household. It indexes a media library you
already have on a NAS, fetches metadata for it, and streams it to the people you invite —
each with their own watch progress.

Two roles:

- **Viewer** — browse, search across languages, watch, resume, and keep their own history.
- **Admin** — everything a viewer can do, plus the job queue, metadata editing, users,
  invitations and system settings.

---

## What it does

**Indexes a read-only NAS mount.** dramatarr walks the library, probes each file with `ffprobe`,
parses series and episode numbers from the paths, and builds a catalogue. It never writes to
your library.

**Fetches metadata, but yields to you.** Series are matched against DramaDex — a self-hosted
indexer of DramaBox, ReelShort, GoodShort and NetShort — for overviews, posters, cast,
alternate titles and links back to the platform each show lives on. It is the only index that
catalogues this genre at all; TMDB, OMDb and a web-search fallback all preceded it, all existed
because it did not, and all are gone. The moment an admin edits a field by hand, that series is
marked *metadata-locked* and automatic refreshes leave it alone — with a visible banner saying
so, and a control to unlock it again.

**Searches across languages.** Titles are indexed in their native script *and* in every
romanization dramatarr can generate. A Chinese drama stored as 迷情 is found by `迷情`,
`mi qing`, `miqing`, `mq`, or its registered English title — and the result tells you which
title matched, so a cross-language hit never looks like a mistake.

**Runs one job at a time.** Scans, metadata refreshes, image downloads and thumbnail generation
all go through a single serialized queue with a live progress page. Jobs can be paused,
reordered, cancelled and retried.

**Tracks progress per person.** Two people watching the same series never disturb each other.
An episode is watched at 90%, resume is offered above 15 seconds in, and finished episodes stay
replayable — starting one again simply begins a rewatch.

**Signs in with passkeys.** Face, fingerprint or device PIN. No passwords anywhere. New members
join only by emailed invitation.

---

## Requirements

- Docker and Docker Compose
- A media library reachable as a filesystem path on the Docker host
- A DramaDex instance and its API key — optional, configured in the UI rather than in a file.
  Without one, dramatarr still indexes and streams the library; what it loses is metadata,
  cast, and the ability to search for something it does not already hold
- An SMTP account if you want invitations delivered by email (you can also copy invite links by
  hand)

---

## Getting started

```bash
cp .env.example .env
```

Edit `.env` — at minimum `DRAMATARR_SECRET_KEY`, `DRAMATARR_LIBRARY_PATH` and
`DRAMATARR_BOOTSTRAP_ADMIN_EMAIL`. Generate the secret with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then bring it up:

```bash
docker compose up -d --build
```

On an empty database, the bootstrap email becomes the first admin and an enrollment link is
printed to the log. Find it with:

```bash
docker compose logs api | grep -i enrollment
```

Open that link, create your passkey, and you are in. From there: **Admin → Settings** to add
your DramaDex URL and key and your SMTP details, then **Admin → Queue → Scan library** to
index the NAS.

### Passkeys and your domain

WebAuthn binds credentials to a domain, and this trips people up more than anything else in the
setup:

| Where you run it | `RP_ID` | `ORIGIN` |
| --- | --- | --- |
| Local machine | `localhost` | `http://localhost:8080` |
| Behind TLS | `dramatarr.example.com` | `https://dramatarr.example.com` |
| LAN by bare IP | not supported — browsers require `localhost` or a real domain over HTTPS | |

`RP_ID` is the registrable domain with no scheme and no port. `ORIGIN` is the full origin,
exactly as the browser address bar shows it. If they disagree, registration fails with an
unhelpful browser error. If you serve dramatarr over the LAN, give it a hostname and a
certificate — a reverse proxy with Caddy or Traefik is the usual answer.

---

## How your library should be laid out

Both of these work:

```
library/
├── Ruta de Cristal (2024)/
│   ├── Season 01/
│   │   ├── Ruta de Cristal - S01E01 - Episodio 01.mp4
│   │   ├── Ruta de Cristal - S01E01 - Episodio 01.es.srt
│   │   └── Ruta de Cristal - S01E01 - Episodio 01.en.srt
│   └── Season 02/
│       └── Ruta de Cristal - S02E01 - Episodio 01.mp4
├── Longevity Record/
│   ├── Longevity Record - 1.mp4
│   └── Longevity Record - 2.mp4
└── 迷情 (2023)/
    ├── 迷情 第1集.mp4
    └── 迷情 第2集.mp4
```

The parser understands `SxxEyy`, `Season x Episode y`, the CJK forms `第N集` and `第N話`,
`- NN -`, `ENN`, `EPNN`, `[NN]`, and a bare trailing number. Sidecar subtitles are matched by
filename stem, with the language read from the suffix (`.es.srt`, `.spa.srt`, `.pt-BR.srt`).

A file the parser cannot place is never dropped — it appears under **Admin → Library →
Unmatched**, where you assign it a series and episode by hand.

---

## Development

```bash
# Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
DRAMATARR_ENVIRONMENT=dev \
DRAMATARR_LIBRARY_ROOT=../test-library \
DRAMATARR_DATA_DIR=../data \
.venv/bin/uvicorn app.main:app --reload

# Frontend
cd frontend
npm install
npm run dev        # proxies /api to localhost:8000
```

A realistic test library — including the CJK and Korean cases, sidecar subtitles and a
deliberately unparseable file — can be generated with:

```bash
./scripts/make-test-library.sh
```

Tests:

```bash
cd backend && .venv/bin/pytest
cd frontend && npm test
```

---

## Architecture

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the contract the implementation follows: the
full data model, every API route, the search design, the scanner rules and the queue semantics.
Read it before changing anything structural.

Briefly: FastAPI + SQLAlchemy 2.0 + SQLite (WAL) behind nginx, with a React 19 + TypeScript SPA.
The queue worker runs in-process precisely so that "one job at a time" is enforced by the
runtime rather than by coordination.

---

## Scope

dramatarr organizes and streams media you already have and are entitled to serve. It reads a
local mount; it does not crawl, scrape or download from third-party streaming sites, and the
ingestion layer ships only a local-folder provider. Point it at content you have the rights to.
