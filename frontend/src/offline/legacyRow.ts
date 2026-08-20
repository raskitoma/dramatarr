/** Reading a download row written by an older shape of this app.
 *
 * Split out so the migration is testable without `caches` and IndexedDB. Rows written before
 * episodes carried their own size and label have `episodeIds: string[]` and no `episodes`, and
 * every reader of the new shape throws on the first `.filter` — on a device that has already
 * downloaded something, that kills the Downloads screen instead of showing what is there.
 */

import type { DownloadEpisode, DownloadRow } from './db'

export function normaliseRow(row: DownloadRow): { row: DownloadRow; changed: boolean } {
  if (Array.isArray(row.episodes)) return { row, changed: false }

  const legacy = (row as unknown as { episodeIds?: string[] }).episodeIds ?? []
  const episodes: DownloadEpisode[] = legacy.map((id) => ({ id, label: id, sizeBytes: 0 }))
  return {
    // Not `done`-clearing: the ids survive in `storedIds`, so nothing is re-downloaded. A
    // re-press re-measures and skips straight past whatever is already in the cache.
    row: { ...row, episodes, state: row.state === 'done' ? 'done' : 'paused' },
    changed: true,
  }
}
