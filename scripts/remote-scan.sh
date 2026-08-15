#!/usr/bin/env bash
# Kick off a library scan through the API and follow it to completion.
#
# Runs ON the docker host, next to the compose project. Going through HTTP rather than calling
# the scanner directly is deliberate: it exercises the same path the admin UI uses, including
# the queue, so a green run here means the button in the UI works too.
#
#   ./remote-scan.sh [compose-dir] [base-url]

set -euo pipefail

DIR="${1:-/mnt/apps/docker_data/dramatarr/src}"
BASE="${2:-http://127.0.0.1:8090}"
cd "$DIR"

TOKEN=$(docker compose exec -T api python - <<'PY' | tr -d '\r'
import asyncio, hashlib, secrets
from sqlalchemy import select
from app.db import get_sessionmaker
from app.models import AuthSession, User
from app.utils.time import in_days

async def main():
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User))).scalars().first()
        raw = secrets.token_urlsafe(32)
        session.add(AuthSession(
            user_id=user.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=in_days(1),
        ))
        await session.commit()
        print(raw)

asyncio.run(main())
PY
)

cleanup() {
  docker compose exec -T api python - "$TOKEN" <<'PY' >/dev/null 2>&1 || true
import asyncio, hashlib, sys
from sqlalchemy import delete
from app.db import get_sessionmaker
from app.models import AuthSession

async def main():
    async with get_sessionmaker()() as session:
        await session.execute(delete(AuthSession).where(
            AuthSession.token_hash == hashlib.sha256(sys.argv[1].encode()).hexdigest()))
        await session.commit()

asyncio.run(main())
PY
}
trap cleanup EXIT

code=$(curl -sS -b "dramatarr_session=$TOKEN" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/admin/jobs" -d '{"type":"library_scan","payload":{}}' \
  -o /dev/null -w '%{http_code}')
echo "  scan enqueued: HTTP $code"

for _ in $(seq 1 120); do
  curl -sS -b "dramatarr_session=$TOKEN" "$BASE/api/admin/queue" > /tmp/_q.json
  python3 - <<'PY'
import json
d = json.load(open('/tmp/_q.json'))
cur = d.get('current')
line = f"  running={d['running']} queued={d['queuedCount']}"
if cur:
    line += f" | {cur['type']} {cur['progressPercent']:.0f}% {cur['progressMessage'][:52]}"
print(line, flush=True)
PY
  if python3 -c "
import json,sys
d=json.load(open('/tmp/_q.json'))
sys.exit(0 if (not d['running'] and d['queuedCount']==0) else 1)"; then
    break
  fi
  sleep 5
done

echo
echo "  --- result ---"
curl -sS -b "dramatarr_session=$TOKEN" "$BASE/api/admin/stats" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for k in ('seriesCount','episodeCount','availableEpisodeCount','unmatchedCount'):
    print(f'  {k:24} {d[k]}')"
