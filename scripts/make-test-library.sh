#!/usr/bin/env bash
# Generates a small but realistic test library so the scanner, player and search can be
# exercised without touching the real NAS. Every file is a genuine, tiny, playable video.
#
#   ./scripts/make-test-library.sh [target-dir]     (default: ./test-library)
#
# Requires ffmpeg on PATH.

set -euo pipefail

TARGET="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/test-library}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required but was not found on PATH." >&2
  exit 1
fi

echo "Building test library at: $TARGET"
mkdir -p "$TARGET"

# A short, real H.264/AAC mp4 — small enough to be disposable, valid enough that ffprobe
# reports genuine codec/duration/resolution data and the browser can actually play it.
make_video() {
  local out="$1" seconds="$2" label="$3"
  mkdir -p "$(dirname "$out")"
  [ -f "$out" ] && return 0
  # testsrc2 already burns in a frame counter and timestamp, so the clip is visually
  # distinguishable without drawtext — which many ffmpeg builds ship without.
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "testsrc2=size=640x360:rate=24:duration=${seconds}" \
    -f lavfi -i "sine=frequency=440:duration=${seconds}" \
    -metadata "title=${label}" \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -crf 40 \
    -c:a aac -b:a 48k -shortest "$out"
}

make_subtitle() {
  local out="$1" text="$2"
  [ -f "$out" ] && return 0
  cat > "$out" <<EOF
1
00:00:01,000 --> 00:00:05,000
${text}

2
00:00:06,000 --> 00:00:10,000
${text} — segunda línea
EOF
}

# --- 1. Standard SxxEyy layout, two seasons, with Spanish + English sidecar subtitles -------
for ep in 01 02 03; do
  f="$TARGET/Ruta de Cristal (2024)/Season 01/Ruta de Cristal - S01E${ep} - Episodio ${ep}.mp4"
  make_video "$f" 12 "Ruta de Cristal S01E${ep}"
  make_subtitle "${f%.mp4}.es.srt" "Subtítulo en español"
  make_subtitle "${f%.mp4}.en.srt" "English subtitle"
done
for ep in 01 02; do
  make_video "$TARGET/Ruta de Cristal (2024)/Season 02/Ruta de Cristal - S02E${ep} - Episodio ${ep}.mp4" 12 "Ruta de Cristal S02E${ep}"
done

# --- 2. Flat layout, bare episode numbers, no season folder --------------------------------
for ep in 1 2 3 4; do
  make_video "$TARGET/Longevity Record/Longevity Record - ${ep}.mp4" 10 "Longevity Record ${ep}"
done

# --- 3. CJK title with CJK episode markers — the multilingual search case ------------------
for ep in 1 2 3; do
  make_video "$TARGET/迷情 (2023)/迷情 第${ep}集.mp4" 10 "Mi Qing ${ep}"
done

# --- 4. Korean title, romanization case ----------------------------------------------------
for ep in 01 02; do
  make_video "$TARGET/왕의 귀향/왕의 귀향 - E${ep}.mp4" 10 "Wangui Gwihyang ${ep}"
done

# --- 5. Deliberately unparseable, to exercise the unmatched-files view ---------------------
make_video "$TARGET/Misc/random-clip-no-episode-info.mp4" 6 "unmatched"

echo
echo "Done. Layout:"
find "$TARGET" -type f | sort | sed "s|$TARGET|  .|"
echo
echo "Point dramatarr at it with:  DRAMATARR_LIBRARY_ROOT=$TARGET"
