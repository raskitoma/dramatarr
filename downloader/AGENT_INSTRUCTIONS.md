# 🤖 AI Agent Deployment & Handoff Instructions

The full deployment guide and interactive protocol lives in **[AGENTS.md](./AGENTS.md)**. This file is only a summary — follow `AGENTS.md` for the authoritative steps.

### Quick Summary for Receiving AI Agents

1. **Ask the user the 4 scoping questions** in Phase 1 of `AGENTS.md` (deployment mode, port, download destination, and whether to run a verification test).
2. **Execute pre-flight checks** (Docker engine, port availability, Chrome/FFmpeg if running locally).
3. **Deploy** via `docker compose up -d --build` or `npm run web`.
4. **Perform health checks** — `GET /api/health` returns `200` with `{"status":"ok"}`, and the volume mount must be writable.
5. **Confirm and provide the dashboard link** `http://localhost:3000` to the user.
