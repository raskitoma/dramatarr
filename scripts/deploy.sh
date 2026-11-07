#!/usr/bin/env bash
# Existing installs must use the storage-aware preparation, restore check and staged build.
# This wrapper intentionally does not restart production or rsync --delete into a live tree.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
exec python3 "$here/scripts/stage-unification.py" "$@"
