#!/usr/bin/env bash
# Rename a folder of sequentially-numbered files into S01Exx episode names.
#
# For libraries whose files carry an opaque numeric id instead of an episode number. The
# numeric order of the original names is taken as the episode order — which is an assumption,
# so the mapping is written out first and the rename is reversible from it.
#
#   ./rename-sequential.sh "/path/to/Series Folder" [season] [--apply]
#
# Without --apply it only prints what it would do.

set -euo pipefail

DIR="${1:?usage: rename-sequential.sh <folder> [season] [--apply]}"
SEASON="${2:-1}"
APPLY="${3:-}"

[ -d "$DIR" ] || { echo "Not a directory: $DIR" >&2; exit 1; }

TITLE=$(basename "$DIR")
MAP="$DIR/.rename-map.tsv"

cd "$DIR"

mapfile -t FILES < <(
  find . -maxdepth 1 -type f \( -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.m4v' -o -iname '*.avi' \) \
    -printf '%f\n' | sed 's|^\./||' | sort -t. -k1,1n
)

[ "${#FILES[@]}" -gt 0 ] || { echo "No video files in $DIR" >&2; exit 1; }

printf 'Folder : %s\nSeason : %s\nFiles  : %s\n\n' "$TITLE" "$SEASON" "${#FILES[@]}"

# Build the plan first and refuse on any collision, rather than discovering one halfway
# through and leaving the folder in a half-renamed state.
declare -a PLAN_OLD PLAN_NEW
i=1
for f in "${FILES[@]}"; do
  ext="${f##*.}"
  new=$(printf '%s - S%02dE%02d.%s' "$TITLE" "$SEASON" "$i" "$ext")
  if [ "$f" != "$new" ] && [ -e "$new" ]; then
    echo "REFUSING: target already exists: $new" >&2
    exit 1
  fi
  PLAN_OLD+=("$f"); PLAN_NEW+=("$new")
  i=$((i+1))
done

for idx in "${!PLAN_OLD[@]}"; do
  [ "$idx" -lt 3 ] || [ "$idx" -ge $((${#PLAN_OLD[@]} - 2)) ] && \
    printf '  %s\n    -> %s\n' "${PLAN_OLD[$idx]}" "${PLAN_NEW[$idx]}"
  [ "$idx" -eq 3 ] && printf '  … %s more …\n' $((${#PLAN_OLD[@]} - 5))
done

if [ "$APPLY" != "--apply" ]; then
  printf '\nDry run. Re-run with --apply to perform it.\n'
  exit 0
fi

: > "$MAP"
for idx in "${!PLAN_OLD[@]}"; do
  printf '%s\t%s\n' "${PLAN_OLD[$idx]}" "${PLAN_NEW[$idx]}" >> "$MAP"
done

for idx in "${!PLAN_OLD[@]}"; do
  [ "${PLAN_OLD[$idx]}" = "${PLAN_NEW[$idx]}" ] && continue
  mv -n -- "${PLAN_OLD[$idx]}" "${PLAN_NEW[$idx]}"
done

printf '\nRenamed %s file(s).\nMapping written to %s — to undo:\n' "${#PLAN_OLD[@]}" "$MAP"
printf "  cd %q && while IFS=\$'\\\\t' read -r a b; do mv -n -- \"\$b\" \"\$a\"; done < .rename-map.tsv\n" "$DIR"
