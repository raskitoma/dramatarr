#!/usr/bin/env bash
# Move the live Dramadex from SQLite to Postgres, unattended but gated.
#
# The one rule: DRAMADEX_DB_URL is only switched AFTER a row-count verification
# passes. If the copy is short by a single row the script stops with the app
# still on SQLite and nothing lost.
#
# Order matters. The container has to join the Postgres network BEFORE the
# migration runs, because the migration needs to reach both databases at once.
set -uo pipefail

PORTAINER="https://10.0.50.240:9443/api"
ENDPOINT=2
STACK=53
APP="https://dramadex.raskitoma.io"
LOG=/tmp/cutover.log

PKEY=$(python3 -c "import json;print(json.load(open('/Users/raskitoma/networking/credentials.json'))['portainer']['tenguemerengue']['api_key'])")
APIKEY=$(cat /tmp/dramadex_prod_key)
PGPW=$(cat /tmp/dramadex_pgpw)
PGURL="postgresql+psycopg://dramadex:${PGPW}@arr-postgres:5432/dramadex"

say() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }
die() { say "ABORTED: $*"; exit 1; }

api()  { curl -sk -H "X-API-Key: $PKEY" "$@"; }
app()  { curl -sS --max-time 30 -H "X-API-Key: $APIKEY" "$@"; }

cid() {
  api "$PORTAINER/endpoints/$ENDPOINT/docker/containers/json" | python3 -c "
import json,sys
for c in json.load(sys.stdin):
    if c['Names'][0].lstrip('/')=='dramadex': print(c['Id']); break"
}

# Run a command inside the app container and echo its output.
inside() {
  python3 -c "
import json,sys
print(json.dumps({'AttachStdout':True,'AttachStderr':True,'Cmd':json.loads(sys.argv[1])}))" "$1" > /tmp/cut_exec.json
  local eid
  eid=$(api -X POST -H 'Content-Type: application/json' --data @/tmp/cut_exec.json \
        "$PORTAINER/endpoints/$ENDPOINT/docker/containers/$(cid)/exec" \
        | python3 -c "import json,sys;print(json.load(sys.stdin)['Id'])")
  api -X POST -H 'Content-Type: application/json' -d '{"Detach":false,"Tty":false}' \
      "$PORTAINER/exec/$eid/start" 2>/dev/null | tr -d '\000-\010\013\014\016-\037'
}

deploy() {  # $1 = DB url
  python3 - "$1" "$APIKEY" <<'PY' > /tmp/cut_stack.json
import json, sys
db_url, api_key = sys.argv[1], sys.argv[2]
compose = f"""services:
  dramadex:
    image: dramadex:0.1.0
    container_name: dramadex
    environment:
      DRAMADEX_DB_URL: {db_url}
      DRAMADEX_DATA_DIR: /data
      DRAMADEX_API_KEY: ${{DRAMADEX_API_KEY}}
      DRAMADEX_TZ: America/Guayaquil
      DRAMADEX_MAX_ITEMS: "0"
      DRAMADEX_SYNC_HOUR: "3"
      DRAMADEX_SCHEDULER_ENABLED: "true"
      DRAMADEX_BACKUP_KEEP: "7"
      DRAMADEX_USER_AGENT: "DramadexBot/0.1 (self-hosted metadata indexer; +https://dramadex.raskitoma.io)"
    volumes:
      - /mnt/apps/docker_data/dramadex:/data
    ports:
      - "8099:8080"
    restart: unless-stopped
    networks: [default, arrpg]

networks:
  arrpg:
    external: true
    name: arr-postgres_default
"""
print(json.dumps({"stackFileContent": compose,
                  "env": [{"name": "DRAMADEX_API_KEY", "value": api_key}],
                  "prune": False, "pullImage": False}))
PY
  api -X PUT -H 'Content-Type: application/json' --data @/tmp/cut_stack.json \
      "$PORTAINER/stacks/$STACK?endpointId=$ENDPOINT" -o /dev/null -w '%{http_code}'
}

wait_healthy() {
  for _ in $(seq 1 60); do
    [ "$(curl -so /dev/null -w '%{http_code}' --max-time 10 "$APP/health")" = "200" ] && return 0
    sleep 5
  done
  return 1
}

say "=== waiting for all running jobs to finish ==="
while true; do
  n=$(app "$APP/sync/runs?limit=4" | python3 -c "
import json,sys
try: print(sum(1 for r in json.load(sys.stdin) if r['state'] in ('pending','running')))
except Exception: print(1)")
  [ "$n" = "0" ] && break
  sleep 120
done
say "all jobs idle"
app "$APP/sync/runs?limit=2" | python3 -c "
import json,sys
for r in json.load(sys.stdin): print('   ', r['job_type'], r['state'], json.dumps(r['stats'])[:160])" | tee -a "$LOG"

say "=== 1. backup while still on SQLite ==="
inside '["dramadex","backup"]' | grep -E "file|verified|integrity" | tee -a "$LOG"

say "=== 2. attach the container to the postgres network (still on SQLite) ==="
code=$(deploy "sqlite:////data/dramadex.db"); say "   stack update -> $code"
wait_healthy || die "app did not come back after attaching the network"
say "   healthy"

say "=== 3. copy SQLite -> Postgres ==="
inside "$(python3 -c "
import json,os
print(json.dumps(['dramadex','migrate-engine',os.environ['PGURL']]))" PGURL="$PGURL")" | tail -30 | tee -a "$LOG"

say "=== 4. verify row counts (the gate) ==="
VERIFY=$(inside "$(python3 -c "
import json,os
print(json.dumps(['dramadex','migrate-engine',os.environ['PGURL'],'--verify-only']))" PGURL="$PGURL")")
echo "$VERIFY" | tail -20 | tee -a "$LOG"
echo "$VERIFY" | grep -q "match row for row" || die "row counts differ; STAYING ON SQLITE"
say "   verification passed"

say "=== 5. switch to Postgres ==="
code=$(deploy "$PGURL"); say "   stack update -> $code"
wait_healthy || die "app did not come back on Postgres — revert with: deploy sqlite:////data/dramadex.db"

say "=== 6. build the tsvector index ==="
inside '["dramadex","reindex"]' | tee -a "$LOG"

say "=== 7. verify on Postgres ==="
curl -sS --max-time 20 "$APP/health" | tee -a "$LOG"; echo
app "$APP/stats" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f\"   {d['titles']} titles · {d['episodes']} episodes · {d['people']} people\")
print('   by studio:', d['by_studio'])" | tee -a "$LOG"
for q in "Djinn Under Contract" "The Return of the Lost Heiress"; do
  curl -sSG --max-time 25 -H "X-API-Key: $APIKEY" --data-urlencode "q=$q" --data-urlencode "limit=2" "$APP/titles" \
  | Q="$q" python3 -c "
import json,sys,os
d=json.load(sys.stdin); q=os.environ['Q']
top=d['items'][0]['primary_title'] if d['items'] else '—'
print(f\"   q={q!r} n={d['count']} top={top!r}{'  EXACT' if top.lower()==q.lower() else ''}\")" | tee -a "$LOG"
done

say "=== 8. resume the poster backfill (should no longer contend) ==="
app -X POST "$APP/sync/runs?source=dramabox&job_type=poster_fetch" \
  | python3 -c "import json,sys;print('   run', json.load(sys.stdin).get('id'))" | tee -a "$LOG"

say "=== CUTOVER COMPLETE ==="
