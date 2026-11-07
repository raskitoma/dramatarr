# 🤖 AI Agent Deployment & Handoff Instructions

This document provides instructions for an AI Agent (e.g. Antigravity, Claude, ChatGPT, Cursor, Windsurf, Copilot) when onboarding, deploying, configuring, or troubleshooting **DramaDown**.

---

## 🎯 Primary Objective for the Receiving Agent
Your goal is to guide the user through a reliable, error-free deployment of **DramaDown**, verify container/system health, and ensure video streams can be extracted and saved to the user's host `downloads/` directory with full read-write permissions.

---

## 📋 Phase 1: Clarifying & Scoping Questions (Ask the User First)

Before running deployment commands, the receiving agent **MUST** present the following questions to the user:

1. **Deployment Method**:
   - `[Option A] (Recommended)` **Docker Compose** (automated container with Chrome, FFmpeg, Xvfb, and persistent volume mount).
   - `[Option B]` **Standalone Docker Container** (`docker run`).
   - `[Option C]` **Local Node.js Environment** (requires local Google Chrome, Node.js v18+, and FFmpeg installed on the host OS).

2. **Port Configuration**:
   - Do you want to expose the Web Dashboard on the default port **`3000`** (`http://localhost:3000`), or do you prefer a custom port (e.g. `8080`, `3001`)?
   - *Compose:* set `DRAMADOWN_PORT`. *Local:* set `PORT`.

3. **Storage & Download Directory**:
   - The default download directory is `./downloads` on your host machine. Do you want to use the default `./downloads` or a custom path (e.g. an external hard drive, NAS mount, or specific folder)?
   - *Compose:* set `DRAMADOWN_DOWNLOADS`. *Local:* set `DOWNLOAD_DIR`, or pass `--out-dir` to the CLI.

4. **Initial Verification Test**:
   - Would you like to run an automated smoke test with the default sample drama URL upon deployment to confirm video extraction and downloading?

---

## 🔍 Phase 2: Pre-Flight System Checks

Once the user answers, run the following verification checks based on the chosen deployment mode:

### If Docker / Docker Compose:
- [ ] Check Docker Engine is running: `docker info`
- [ ] Check Docker Compose availability: `docker compose version`
- [ ] Verify target port is not already occupied: `lsof -i :<PORT>` or `nc -z localhost <PORT>`
- [ ] Ensure the host `./downloads/` directory exists and has write permissions: `mkdir -p ./downloads && ls -ld ./downloads`

### If Local Node.js:
- [ ] Check Node.js version (>= 20): `node -v`
- [ ] Check Google Chrome is installed:
  - macOS: `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --version`
  - Linux: `google-chrome --version` or `chromium --version`
- [ ] Check FFmpeg is installed: `ffmpeg -version` (optional — without it episodes stay in a raw container)
- [ ] Ensure dependencies are installed: `npm install`
- [ ] Run the offline test suite: `npm test` (35+ tests, no network or browser required)

---

## 🚀 Phase 3: Deployment Execution Steps

### 1. Docker Compose Deployment (Preferred)
Execute the build and launch command:
```bash
docker compose up -d --build
```

### 2. Standalone Docker Deployment
```bash
docker build -t dramadown .
docker run -d \
  -p <PORT>:3000 \
  -v "$(pwd)/downloads:/app/downloads:rw" \
  --shm-size=2gb \
  --name dramadown \
  dramadown
```

### 3. Local Node.js Deployment
```bash
npm install
npm run web
```

---

## 🩺 Phase 4: Automated Verification & Health Checks

The receiving agent **MUST** perform the following post-deployment checks:

1. **HTTP Service Health**:
   ```bash
   curl -fsS http://localhost:<PORT>/api/health
   ```
   *Expected result: `{"status":"ok","downloadDir":"...","sessions":0}`*

   Note: if running locally (not in Docker) the server binds `127.0.0.1` by default.
   Set `HOST=0.0.0.0` only if you deliberately want it reachable from other machines.

2. **Container Volume Read-Write Verification (if Docker)**:
   ```bash
   docker exec dramadown sh -c 'touch /app/downloads/.mount_test && rm /app/downloads/.mount_test'
   ```
   *Expected result: Exits with code 0 (confirming read-write permissions inside the container).*

3. **Check Container Logs for Clean Startup**:
   ```bash
   docker compose logs --tail=20
   ```
   *Expected output includes: `🚀 DramaDown Server running at: http://localhost:3000`*

4. **Confirm the API refuses cross-origin callers** (this should return `403`):
   ```bash
   curl -s -o /dev/null -w "%{http_code}" -H "Origin: https://example.com" http://localhost:<PORT>/api/health
   ```
   *Expected result: `403`. A `200` here means the security check is not active — stop and investigate.*

---

## 📊 Phase 5: Handoff Confirmation to User

After verification passes, summarize the deployment status to the user with:
- ✅ **Dashboard URL**: `http://localhost:<PORT>`
- 📁 **Host Download Location**: Absolute path to `./downloads/<Drama Title>/`
- 🎬 **Quick Start Command (CLI)**:
  ```bash
  node cli.js "<DRAMA_WATCH_URL>"
  ```
- 💡 **Instruction**: "Paste any drama watch link in the dashboard and click 'Extract All URIs' -> 'Download All to Folder'."
- ♻️ **Note on retries**: downloads are resumable. Re-running the same CLI command (or clicking download again) skips completed episodes and retries only what failed. Use `--force` to re-download everything.

---

## ⚠️ Notes for the Receiving Agent

- **Do not add `--headless`** unless the user asks. Cloudflare Turnstile is markedly more likely to pass with a headful browser (Xvfb handles this inside the container).
- **`npm audit` reports a high-severity `extract-zip` advisory** via `@puppeteer/browsers@2.6.1`. It is unreachable here (the app never lets Puppeteer download a browser) and the only patched version is a major bump of an exactly-pinned transitive dep. **Do not run `npm audit fix --force`** — it downgrades `puppeteer-real-browser` and breaks extraction. See the Security Notes in `README.md`.
- **The server binds `127.0.0.1` by default.** If a health check from another host fails, that is expected behaviour, not a bug.
