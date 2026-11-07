#!/bin/sh
set -e

# Run the whole process tree under a virtual X display.
#
# Chrome then launches *headful* against Xvfb, which is what lets it pass
# Cloudflare Turnstile, while the container itself needs no physical screen.
# Setting DISPLAY here also tells the extractor not to spawn its own Xvfb per
# launch (see manageXvfb in src/extractor.js), so a single long-lived X server
# is shared by every extraction.
#
# Set DRAMADOWN_NO_XVFB=1 to skip this and run the browser truly headless.

if [ "${DRAMADOWN_NO_XVFB:-0}" = "1" ]; then
  echo "[entrypoint] Xvfb disabled (DRAMADOWN_NO_XVFB=1) — Chrome will run --headless=new."
  export HEADLESS=1
  exec "$@"
fi

if ! command -v xvfb-run >/dev/null 2>&1; then
  echo "[entrypoint] xvfb-run not found; starting without a virtual display." >&2
  exec "$@"
fi

echo "[entrypoint] Starting virtual display for headful Chrome..."
exec xvfb-run \
  --auto-servernum \
  --server-args="-screen 0 1920x1080x24 -ac -nolisten tcp" \
  "$@"
