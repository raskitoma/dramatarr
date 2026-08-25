/** Watch state written while there is no server, and how it gets back.
 *
 * Local is authoritative during the trip: every position and every completion is written here
 * first, and the network call fires afterwards as it always did. With the server off that call
 * simply fails, which today is silent — `useProgressReporter` sends with `retry: false` and no
 * error handler, and `sendBeacon` reports nothing at all. Three weeks of viewing would have
 * been lost on landing.
 *
 * **The sync is not a replay.** The backend has no bulk or backdated progress route, and
 * replaying raw pings against it would be actively harmful: `watch.py` assigns
 * `position_seconds` unconditionally, so an out-of-order ping rewinds an episode, and a
 * mid-episode ping arriving after a completion resets that episode to zero — un-watching
 * something the person actually finished. So each episode is collapsed to **one terminal
 * write**, and the writes are sent oldest-first so continue-watching still sorts the way the
 * trip happened.
 */

import { PROGRESS, all, get, put, remove, type ProgressRow } from './db'

export interface PendingWrite {
  episodeId: string
  completed: boolean
  positionSeconds: number
  durationSeconds: number
  watchedAt: number
}

/** One write per episode, in the order they were watched. Exported for its own test — this is
 *  the part that can quietly destroy a trip's worth of state. */
export function collapse(rows: ProgressRow[]): PendingWrite[] {
  const byEpisode = new Map<string, PendingWrite>()

  for (const row of rows) {
    if (row.synced) continue
    const seen = byEpisode.get(row.episodeId)
    if (!seen) {
      byEpisode.set(row.episodeId, {
        episodeId: row.episodeId,
        completed: row.completed,
        positionSeconds: row.positionSeconds,
        durationSeconds: row.durationSeconds,
        watchedAt: row.watchedAt,
      })
      continue
    }
    // Finishing beats any later position: somebody who watched the end and then scrubbed back
    // has still watched it, and the server would otherwise un-complete it.
    seen.completed = seen.completed || row.completed
    if (row.positionSeconds > seen.positionSeconds) seen.positionSeconds = row.positionSeconds
    if (row.durationSeconds > seen.durationSeconds) seen.durationSeconds = row.durationSeconds
    seen.watchedAt = Math.max(seen.watchedAt, row.watchedAt)
  }

  return [...byEpisode.values()].sort((a, b) => a.watchedAt - b.watchedAt)
}

export async function recordLocal(entry: {
  episodeId: string
  seriesId: string
  positionSeconds: number
  durationSeconds: number
  completed: boolean
}): Promise<void> {
  const previous = await get<ProgressRow>(PROGRESS, entry.episodeId)
  await put(PROGRESS, {
    ...entry,
    completed: entry.completed || (previous?.completed ?? false),
    watchedAt: Date.now(),
    synced: false,
  } satisfies ProgressRow)
}

/** Where the player should resume, when the local store knows better than the cached JSON —
 *  which it does the moment anything has been watched on the plane. */
export async function localResume(episodeId: string): Promise<number | null> {
  const row = await get<ProgressRow>(PROGRESS, episodeId)
  if (!row || row.completed) return null
  return row.positionSeconds > 0 ? row.positionSeconds : null
}

export async function pendingCount(): Promise<number> {
  const rows = await all<ProgressRow>(PROGRESS)
  return rows.filter((row) => !row.synced).length
}

/** A refusal that will still be a refusal tomorrow.
 *
 * The episode was deleted, or its series was marked adults-only while this device was away.
 * Retrying costs the same 404 forever — and because the sync stops at its first failure to keep
 * the writes in order, one such row would hold back every write behind it for good: a trip's
 * worth of watch state on other series, stuck behind one episode nobody can reach any more.
 *
 * 401 is deliberately *not* here. That is a session to renew, not a permission withdrawn, and
 * discarding the queue over it would throw away the trip on the way in the door.
 */
function isPermanent(error: unknown): boolean {
  const status = (error as { status?: unknown })?.status
  return status === 403 || status === 404
}

export async function syncPending(
  send: (write: PendingWrite) => Promise<void>,
): Promise<{ sent: number; failed: number; discarded: number }> {
  const rows = await all<ProgressRow>(PROGRESS)
  const writes = collapse(rows)
  let sent = 0
  let discarded = 0
  let failed = 0

  for (const [index, write] of writes.entries()) {
    try {
      await send(write)
      // Dropped rather than flagged: a synced row has served its purpose, and keeping it means
      // deciding later how long to keep it.
      await remove(PROGRESS, write.episodeId)
      sent += 1
    } catch (error) {
      if (isPermanent(error)) {
        await remove(PROGRESS, write.episodeId)
        discarded += 1
        continue
      }
      // Stop at the first *transient* failure. The rest stay pending and keep their order for
      // next time — sending the tail out of sequence is exactly what rewinds an episode.
      failed = writes.length - index
      break
    }
  }
  return { sent, failed, discarded }
}
