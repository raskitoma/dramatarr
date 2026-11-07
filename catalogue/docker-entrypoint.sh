#!/bin/sh
# Migrations run on every start. They are idempotent, and it means a fresh host
# only ever needs `docker compose up`.
set -e
alembic upgrade head
exec "$@"
