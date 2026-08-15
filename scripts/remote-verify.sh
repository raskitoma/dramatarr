#!/usr/bin/env bash
# Post-scan verification: clear orphaned rows, then prove streaming works end to end.
#
#   ./remote-verify.sh [compose-dir] [local-base] [public-base]

set -euo pipefail

DIR="${1:-/mnt/apps/docker_data/dramatarr/src}"
BASE="${2:-http://127.0.0.1:8090}"
PUBLIC="${3:-https://dramatarr.raskitoma.com}"
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
        session.add(AuthSession(user_id=user.id,
                                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                                expires_at=in_days(1)))
        await session.commit()
        print(raw)

asyncio.run(main())
PY
)
C="dramatarr_session=$TOKEN"

cleanup_session() {
  docker compose exec -T api python - "$TOKEN" <<'PY' >/dev/null 2>&1 || true
import asyncio, hashlib, sys
from sqlalchemy import delete
from app.db import get_sessionmaker
from app.models import AuthSession
async def main():
    async with get_sessionmaker()() as s:
        await s.execute(delete(AuthSession).where(
            AuthSession.token_hash == hashlib.sha256(sys.argv[1].encode()).hexdigest()))
        await s.commit()
asyncio.run(main())
PY
}
trap cleanup_session EXIT

echo "=== CLEANUP (dry run first) ==="
curl -sS -b "$C" -H 'Content-Type: application/json' -X POST "$BASE/api/library/cleanup" \
  -d '{"dryRun":true}' | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"  would remove: {d['removedEpisodes']} episode(s), {d['removedMediaFiles']} file row(s)\")
print(f\"  series that would disappear: {d['removedSeries'] or 'none'}\")"

echo "  applying…"
curl -sS -b "$C" -H 'Content-Type: application/json' -X POST "$BASE/api/library/cleanup" \
  -d '{"dryRun":false}' | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"  removed: {d['removedEpisodes']} episode(s), {d['removedMediaFiles']} file row(s)\")"

echo
echo "=== LIBRARY STATE ==="
curl -sS -b "$C" "$BASE/api/admin/stats" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for k in ('seriesCount','episodeCount','availableEpisodeCount','unmatchedCount'):
    print(f'  {k:24} {d[k]}')"

echo
echo "=== SERIES AS THE UI SEES IT ==="
SLUG=$(curl -sS -b "$C" "$BASE/api/library/series" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for s in d['items']:
    print(f\"  {s['title']!r}  {s['availableEpisodeCount']} available  progress={s['progressPercent']}%\",
          file=sys.stderr)
print(d['items'][0]['slug'] if d['items'] else '')")

EP=$(curl -sS -b "$C" "$BASE/api/library/series/$SLUG" | python3 -c "
import sys, json
d = json.load(sys.stdin)
e = d['seasons'][0]['episodes'][0]
print(f\"  first episode: E{e['episodeNumber']} available={e['available']} directPlay={e['directPlayOk']} runtime={e['runtimeSeconds']}s\", file=sys.stderr)
print(e['id'])")

echo
echo "=== EPISODE DETAIL ==="
curl -sS -b "$C" "$BASE/api/library/episodes/$EP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for k in ('episodeNumber','durationSeconds','width','height','videoCodec','audioCodec','container','directPlayOk','streamUrl'):
    print(f'  {k:18} {d.get(k)}')"

echo
echo "=== RANGE REQUESTS ON A REAL EPISODE, THROUGH THE PUBLIC DOMAIN ==="
for r in "bytes=0-1023" "bytes=-500" "bytes=999999999-"; do
  printf '  %-20s ' "$r"
  curl -sS --max-time 25 -b "$C" -o /dev/null -D- -H "Range: $r" "$PUBLIC/api/stream/$EP" 2>/dev/null \
    | grep -iE '^(HTTP|content-range)' | tr -d '\r' | tr '\n' ' '
  echo
done
printf '  %-20s ' "no range"
curl -sS --max-time 25 -b "$C" -o /dev/null -w 'HTTP %{http_code}  %{content_type}  %{size_download} bytes\n' "$PUBLIC/api/stream/$EP" 2>/dev/null || echo "failed"
