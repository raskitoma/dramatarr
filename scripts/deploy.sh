#!/usr/bin/env bash
#
# Push this working tree to the NAS and restart the stack.
#
# The reason this exists rather than living in someone's shell history: the rsync needs
# `--delete` (so removed files actually disappear) and `--delete` will happily take the
# server's `.env` with it, because that file exists only there. It holds the secret key that
# signs every session. Losing it does not merely break the deploy — it invites regenerating
# the key, which signs out the whole household.
#
# So the excludes below are the point of the script. `.env` is server-owned state, not source.
#
# Recovery, if it is ever lost anyway: the running containers still hold the resolved values.
#   docker inspect dramatarr-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}'
# gives the nine pass-through variables including the secret; the host-side paths, uid/gid and
# published port come from that container's Mounts, Config.User and the web container's Ports.
# Do this *before* recreating anything — once the containers are gone, so are the values.
set -euo pipefail

HOST=${DRAMATARR_HOST:-root@10.0.50.240}
ROOT=${DRAMATARR_REMOTE_ROOT:-/mnt/apps/docker_data/dramatarr}
here=$(cd "$(dirname "$0")/.." && pwd)

echo "→ syncing $here to $HOST:$ROOT/src"
rsync -az --delete \
  --exclude '.env' \
  --exclude '.git' \
  --exclude 'node_modules' \
  --exclude '__pycache__' \
  --exclude '.venv' \
  --exclude 'dist' \
  --exclude '.pytest_cache' \
  --exclude '.mypy_cache' \
  "$here/" "$HOST:$ROOT/src/"

# Migrations run from the app's lifespan on boot, so the snapshot has to be taken before the
# containers come back up, every time — not only on the deploys known to carry one.
#
# The stamp is computed *here* and interpolated. Writing `\$(date …)` for the remote shell to
# expand looks equivalent and is not: the path is inside single quotes on the far side, so it
# never expanded, and every deploy quietly overwrote one file literally named
# `dramatarr-$(date +%Y%m%d-%H%M%S).db`. The safety net looked present in `ls` and held exactly
# one snapshot. Caught only by reading the directory.
stamp=$(date +%Y%m%d-%H%M%S)
echo "→ snapshotting the database"
ssh "$HOST" "cp '$ROOT/data/dramatarr.db' '$ROOT/backups/dramatarr-$stamp.db'"

echo "→ building and recreating"
ssh "$HOST" "cd '$ROOT/src' && docker compose build && docker compose up -d --force-recreate"

echo "→ health"
ssh "$HOST" "docker ps --filter label=com.docker.compose.project=dramatarr \
  --format '{{.Names}}\t{{.Status}}'"
