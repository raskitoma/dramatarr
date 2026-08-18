/** Where a search hit points, and what is worth saying about how it matched.
 *
 * Extracted so the header's dropdown and `/search` cannot drift. They render the same records
 * with the same vocabulary, and two copies of "where does this row go" is precisely the seam
 * this repo keeps producing defects in.
 */

import type { EpisodeHit, SeriesSummary } from '@/api/types'

/** The matched variant is only worth showing when it is not simply the title already on screen. */
export function matchedVia(matchedTitle: string, displayTitle: string): string | null {
  const matched = matchedTitle.trim()
  if (!matched) return null
  if (matched.toLocaleLowerCase() === displayTitle.trim().toLocaleLowerCase()) return null
  return matched
}

export function seriesHref(series: SeriesSummary): string {
  return `/series/${encodeURIComponent(series.slug)}`
}

/**
 * Where an episode hit goes: the player when the file is there, its series when it is not.
 *
 * The series fallback uses `seriesSlug`, not `seriesId`. The route is `/series/:slug` and it
 * resolves either — the detail endpoint accepts both — so an id would work by accident and read
 * as a bug to the next person, and the address bar would show a hex string instead of a name.
 */
export function episodeHref(hit: EpisodeHit): string {
  if (hit.episode.available) return `/watch/${encodeURIComponent(hit.episode.id)}`
  return `/series/${encodeURIComponent(hit.seriesSlug)}`
}
