# DramaDown 🎬

An automated Drama Video URI Batch Extractor & Downloader that accepts a watch URL (e.g. GoodShort, DramaBox, Dramafren, etc.), bypasses Cloudflare Turnstile protection, locates the video player source, iterates through all associated episodes, outputs the complete list of video stream URIs line by line, and **automatically creates a dedicated subfolder `downloads/<Drama Title>/` to download all video files cleanly**.

---

## 🚀 Features

- **Automated Cloudflare Turnstile Bypass**: Handles modern bot challenges automatically.
- **Four Extraction Strategies**: `dramafren.org` serves several different player templates, so DramaDown picks a strategy from what the page actually exposes, and falls through to the next if one comes back empty. See [Supported players](#-supported-players).
- **Two Download Backends**: a built-in HLS/direct downloader, with **yt-dlp** (the engine MeTube wraps) as an automatic fallback — both inheriting the solved browser session.
- **Session-Aware Downloads**: The cookies, `User-Agent` and `Referer` from the solved browser session travel with every segment request, so CDNs that gate on referrer don't reject the download.
- **Full HLS Support**: Master playlists (picks the highest resolution), AES-128 encrypted segments, fMP4 init segments and byte-range playlists.
- **Title-Based Auto Subfolder**: Reads the drama's actual series title and creates `downloads/<Drama Title>/`.
- **Resumable**: Finished episodes are skipped and interrupted ones **continue where they stopped** — HLS from the last completed segment, direct files via an HTTP range request. Partial progress survives a failure, so retrying costs only the remainder. `--force` (or the UI checkbox) discards it and starts over.
- **Stall detection, not a stopwatch**: transfers are aborted only after a period of genuine silence, never for simply taking a long time.
- **Self-describing folders**: each series keeps a `dramadown.json` recording the watch URL it came from, how many episodes the site advertised, and how many are actually on disk — so a library stays auditable and re-extractable long after the fact.
- **Memory-Safe**: Segments stream to disk instead of being buffered in RAM, so a long episode won't blow up the process.
- **Clean Line-by-Line Output**: Prints URIs directly by line to the terminal and saves them to `uris.txt`.
- **CLI & Web UI**: Supports both command-line automation and an interactive local web dashboard with real-time SSE progress.
- **Containerized & Deployable**: Ready for Docker and Docker Compose deployment with persistent host volume mounting.

---

## 🤖 For AI Agents & Automated Handoff

If you are an **AI Coding Agent** (e.g. Antigravity, Claude, ChatGPT, Cursor, Windsurf) deploying or configuring this project, follow the structured onboarding protocol, scoping questions, pre-flight checks, and health tests defined in **[AGENTS.md](./AGENTS.md)**.

---

## 📦 Local Installation

Requires **Node.js v20+**, **Google Chrome**, and **FFmpeg** (optional but recommended — without it, episodes are saved as raw `.ts`-in-`.mp4` containers that still play in VLC/mpv).

```bash
cd Dramadown
npm install
```

Run the offline test suite to confirm everything is wired up (no network or browser needed):

```bash
npm test
```

---

## 🎭 Supported players

`dramafren.org` proxies several sources, and each one's player exposes its episode
list differently. DramaDown reads what the page offers and picks a strategy; if the
chosen one returns nothing (these origins intermittently answer with Cloudflare
5xx), it falls through to the next.

| Strategy | Used by | How it works |
| --- | --- | --- |
| `chapter-api` | goodshort | `window.chapterIds` + `action=get_video_url&chap_id=` per chapter |
| `video-endpoint` | dramabox | `action=get_video&ep=N` returns JSON with a quality list — usually **direct MP4s**, up to 1080p |
| `in-page-qualities` | reelshort | `availableQualities[].hash64` holds **base64-encoded** m3u8 URLs; other episodes are reached by navigating `ep=N` |
| `player-navigation` | fallback | Drives the player and captures whatever media it loads |

Two details make the newer templates work at all:

- **Metadata lives in the global lexical scope.** Newer players declare `dramaId`,
  `totalEp` (or `maxEp`) and `videoEndpointUrl` with `let`/`const`, which are
  *not* properties of `window`. They are read with a direct `eval` instead.
- **hls.js hides the real playlist.** Those players feed the `<video>` element a
  `blob:` URL through Media Source Extensions, so the playlist never appears on
  the element. DramaDown hooks `Hls.prototype.loadSource` before any page script
  runs, which captures the URL no matter how the site encoded it.

Whichever strategy runs, the highest advertised resolution is chosen.

---

## 🖥️ Headless / Container Behaviour

In Docker the container runs Chrome **headful on a virtual X display (Xvfb)**. There
is no physical screen and no visible window, but Chrome believes it has a real
display — which is what lets it pass Cloudflare Turnstile. True `--headless` is
detected and rejected far more often, so it is opt-in only (`DRAMADOWN_NO_XVFB=1`
or `--headless`), and exists for debugging.

The Chrome profile is persisted, so the `cf_clearance` cookie survives restarts and
most extractions skip the challenge entirely (~4s instead of ~90s).

Only one extraction runs at a time — each launches a full browser and they would
otherwise fight over the shared profile. A concurrent request gets a `409`.

**For deploying to a server**, see **[DEPLOY.md](./DEPLOY.md)** — it covers the bind
mount, file ownership, and putting it behind Nginx Proxy Manager with Authelia.

---

## 🐳 Docker Deployment

You can deploy and run DramaDown in a container without configuring Chrome, FFmpeg, or Node locally:

### Option 1: Using Docker Compose (Recommended)

```bash
docker compose up -d --build
```

Access the dashboard at **`http://localhost:3000`**. Downloaded videos are saved to your host `./downloads/` folder.

To use a different host port or download location, set these before running compose:

```bash
DRAMADOWN_PORT=8080 DRAMADOWN_DOWNLOADS=/Volumes/Media/dramas docker compose up -d --build
```

To view logs / stop:

```bash
docker compose logs -f
```

```bash
docker compose down
```

### Option 2: Using Docker CLI

```bash
docker build -t dramadown .
```

```bash
docker run -d -p 3000:3000 -v "$(pwd)/downloads:/app/downloads:rw" --shm-size=2g --init --name dramadown dramadown
```

---

## 💻 CLI Usage

### Extraction & Automatic Video Download

```bash
node cli.js "https://goodshort.dramafren.org/index.php?page=watch&id=31001349156&ep=0&lang=en&slug=found-a-homeless-genius-to-save-my-company&sv=1"
```

### Options

| Flag | Description |
| --- | --- |
| `-r`, `--raw` | Print only the URIs. Implies `--no-download` and writes no files unless `-o` is given. |
| `--no-download`, `--extract-only` | Extract URIs but skip downloading video files. |
| `-o`, `--output <file>` | Write the URI list to `<file>` (default `./uris.txt`). |
| `--out-dir <dir>` | Base download directory (default `./downloads`). |
| `-c`, `--concurrency <n>` | Episodes downloaded in parallel, 1–8 (default 3). |
| `--segment-concurrency <n>` | Segments fetched in parallel per episode (default 6). |
| `--force` | Start over: discard the existing file *and* any partial progress. Without it, interrupted episodes resume. |
| `-t`, `--title <name>` | Override the series name used for the download folder. |
| `--headless` | Run Chrome with `--headless=new`. Less reliable against Turnstile — prefer the default. |
| `-h`, `--help` | Show usage. |

```bash
node cli.js "<URL>" --no-download
```

```bash
node cli.js "<URL>" --raw > uris.txt
```

```bash
node cli.js "<URL>" -c 5 --out-dir /Volumes/Media/dramas
```

The CLI exits `0` on success, `1` if any episode failed, and `130` if you interrupt it with Ctrl-C. Re-running the same command retries only the episodes that are missing.

---

## 🌐 Web UI Usage

```bash
npm run web
```

Open **`http://localhost:3000`**, paste a drama watch URL and click **"Extract All URIs"**.

The **Episodes** table is the primary view. From it you can:

- **Tick individual episodes** and use **Download Selected**, or **Download All**.
- **Re-download a single episode** with the `↓` button on its row — always forced,
  so it overwrites what is already there. For several at once, tick them and
  enable *Re-download episodes that already exist*.
- **Stop** a running download. Finished episodes are kept, and re-running resumes
  from where it left off rather than starting over.
- **Retry Failed** appears when a run ends with errors and re-queues exactly those.
- **Edit the folder name** before downloading if the detected series title is wrong.

Episodes already present on disk are marked **• On disk** as soon as the results
render, so it is obvious what a plain download would skip.

Starting a new extraction resets the table, statuses, progress bars and button
labels, so nothing from the previous series carries over.

The **Downloads** panel at the bottom lists what is already in the download
folder, each series showing **downloaded / total** episodes and a **complete** or
**N missing** badge. Expand one to see its files and the watch URL it came from —
**↺ Use** puts that URL back in the box, ready to re-extract and fill the gaps.
It refreshes automatically after every download.

Those facts live in `downloads/<Series>/dramadown.json`, written on every run and
merged rather than replaced, so downloading a single episode never erases the
series total or the original source URL.

---

## ⚙️ Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `3000` | Web dashboard port. |
| `HOST` | `127.0.0.1` | Bind address. Containers set `0.0.0.0`. |
| `DOWNLOAD_DIR` | `./downloads` | Where series folders are created. |
| `PUPPETEER_EXECUTABLE_PATH` / `CHROME_PATH` | auto-detected | Chrome/Chromium binary. |
| `HEADLESS` | `0` | Set to `1` for true `--headless=new`. Cloudflare fails it far more often — prefer Xvfb. |
| `CHROME_USER_DATA_DIR` | unset | Persist the Chrome profile (keeps `cf_clearance` between runs). |
| `DRAMADOWN_NO_XVFB` | `0` | Container only: skip the virtual display and run truly headless. |
| `CHALLENGE_TIMEOUT_MS` | `60000` | How long to wait for Cloudflare to clear. |
| `NAVIGATION_TIMEOUT_MS` | `60000` | Page navigation timeout. |
| `DRAMADOWN_ALLOW_PRIVATE_HOSTS` | `0` | Set to `1` to permit fetching loopback/RFC1918 URLs (testing only). |
| `DRAMADOWN_BACKEND` | `auto` | `auto` (native, falling back to yt-dlp), `native`, or `ytdlp`. |
| `MAX_EPISODES` | `0` | Cap how many episodes are resolved. `0` means all — useful for testing a new site. |
| `CHALLENGE_ATTEMPTS` | `3` | Navigation retries when the origin errors or the challenge sticks. |

---

## 🔒 Security Notes

The web server is a local tool and is hardened accordingly:

- It binds to **loopback only** unless `HOST` says otherwise, so `npm run web` is not exposed to your network.
- It sends **no wildcard CORS header** and **rejects cross-origin API requests**, so another site you have open cannot drive it.
- The download endpoint works from a **server-side session id**, not from URLs in the request body — a page cannot ask the server to fetch a URL of its choosing and write it to disk.
- Outbound fetches are restricted to `http(s)` and refuse loopback/private hosts by default.
- Stream URIs are rendered with DOM APIs, never `innerHTML`, so remote content cannot inject markup.

**Known advisory**: `npm audit` reports a high-severity issue in `extract-zip`, reached via `puppeteer-real-browser` → `rebrowser-puppeteer-core` → `@puppeteer/browsers@2.6.1` (an exact pin). That code path only runs when Puppeteer *downloads* a browser archive; DramaDown always launches a system Chrome and sets `PUPPETEER_SKIP_DOWNLOAD`, so it is never exercised. A patched version exists only in `@puppeteer/browsers@3.x`, which is a major bump of an exactly-pinned transitive dependency — overriding it risks breaking browser launch, so it is deliberately left alone.

---

## 📁 Project Structure

```
Dramadown/
├── Dockerfile              # Production container build with Chrome, FFmpeg, Xvfb & Node.js
├── docker-compose.yml      # Container orchestration with volume mounts
├── cli.js                  # Command-line interface runner
├── server.js               # Local web server with SSE live progress endpoints
├── src/
│   ├── extractor.js        # Browser automation, Turnstile handling & episode traversal
│   ├── downloader.js       # Title folder manager, segment downloader & ffmpeg remuxer
│   ├── hls.js              # HLS/m3u8 playlist parser (master, AES-128, byte ranges)
│   ├── ytdlp.js            # yt-dlp download backend (session-aware, MeTube-style)
│   └── net.js              # Session-aware fetch, cookie scoping, retries & URL safety
├── public/                 # Web interface (HTML, CSS, JS)
├── test/                   # Offline test suite (node --test)
├── downloads/              # Downloaded dramas organized by title
│   └── <Drama Title>/
│       ├── Episode_01.mp4
│       ├── dramadown.json   # Source URL, series total, how many are on disk
│       └── uris.txt
├── uris.txt                # Generated list of video URIs (line by line)
└── package.json
```

---

## 🧯 Troubleshooting

| Symptom | Cause / Fix |
| --- | --- |
| `Could not launch Chrome` | Install Google Chrome, or set `PUPPETEER_EXECUTABLE_PATH` to the binary. |
| `Failed to resolve the Cloudflare challenge` | Raise `CHALLENGE_TIMEOUT_MS`, and don't use `--headless` — Turnstile is far more likely to pass headful. |
| `The site returned a block page` | You've been rate limited. Wait, then retry. |
| Episodes fail with `HTTP 403` | The extraction session expired. Re-run extraction so fresh cookies are captured. |
| `No playable video URIs were found` | The page is not a supported watch URL, or the site changed its player. |
| `The site returned HTTP 524` | The origin (not Cloudflare) timed out. DramaDown retries automatically; if it persists the site is down. |
| Only episode 1 is found | The player did not publish an episode count. Check the log for which strategy ran. |
| Videos play but aren't real MP4s | FFmpeg is missing. Install it and re-run with `--force`. |
