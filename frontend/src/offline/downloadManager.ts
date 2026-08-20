/** Pulling a series into this device's browser storage, one episode at a time.
 *
 * A module singleton rather than a hook, because a download outlives the screen that started
 * it: the owner presses Download on the series page and then goes to make coffee. React state
 * would be torn down by the first navigation.
 *
 * **One episode per request, sequentially.** These are 57-to-112-second files of about 20 MB,
 * so a series is sixty-odd small fetches rather than one enormous one. That is what makes the
 * whole thing resumable for free — an interruption costs at most the file in flight, and the
 * ledger already knows which ones landed. Sequential rather than parallel because the bytes
 * come through Cloudflare from a home NAS, and six concurrent streams do not make that faster.
 *
 * The bytes go into Cache Storage, keyed by the very URL the player asks for. The service
 * worker answers from there, which is why nothing in the player had to change.
 */

import { api } from '@/api/client'
import type { EpisodeDetail, SeriesDetail } from '@/api/types'

import { API_CACHE, MEDIA_CACHE, mediaUrl } from './constants'
import { DOWNLOADS, all, get, put, remove, type DownloadRow } from './db'
import { createQueue } from './queue'
import { offlineSupported } from './register'

const listeners = new Set<() => void>()
let rows: DownloadRow[] = []
const cancelled = new Set<string>()

/** One series at a time, in the order they were asked for. Tested on its own in `queue.ts`. */
const queue = createQueue((seriesId) => drain(seriesId), () => announce())

function announce(): void {
  for (const listener of listeners) listener()
}

async function save(row: DownloadRow): Promise<void> {
  row.updatedAt = Date.now()
  await put(DOWNLOADS, row)
  rows = rows.map((existing) => (existing.seriesId === row.seriesId ? row : existing))
  if (!rows.some((existing) => existing.seriesId === row.seriesId)) rows = [...rows, row]
  announce()
}

/** Read the ledger, then check it against reality.
 *
 * The check is not paranoia: a browser evicting under disk pressure takes the bytes out of
 * Cache Storage and leaves these rows exactly as they were. Without this, a series would go on
 * saying "Descargada" until somebody pressed play on a plane.
 */
export async function reconcile(): Promise<void> {
  if (!offlineSupported()) return
  const stored = await all<DownloadRow>(DOWNLOADS)
  const cache = await caches.open(MEDIA_CACHE)
  const present = new Set((await cache.keys()).map((request) => new URL(request.url).pathname))

  for (const row of stored) {
    const kept = row.episodeIds.filter((id) => present.has(mediaUrl(id)))
    if (kept.length !== row.storedIds.length) {
      row.storedIds = kept
      row.bytesDone = Math.round((row.bytesTotal * kept.length) / Math.max(1, row.episodeIds.length))
      row.state = kept.length === row.episodeIds.length ? 'done' : 'paused'
      if (kept.length === 0) row.state = 'queued'
      await put(DOWNLOADS, row)
    }
  }
  /* A row still marked `running` is a lie: the loop that was writing it lived in a page that
     has since been closed or reloaded, and nothing has been downloading since. Left alone it
     would sit there claiming progress forever. Put it back to `paused` — and then, if there is
     a network, pick it straight back up, because a reload is not a decision to stop. */
  const interrupted: string[] = []
  for (const row of await all<DownloadRow>(DOWNLOADS)) {
    if (row.state !== 'running') continue
    row.state = 'paused'
    await put(DOWNLOADS, row)
    interrupted.push(row.seriesId)
  }

  rows = await all<DownloadRow>(DOWNLOADS)
  announce()

  if (navigator.onLine) for (const seriesId of interrupted) resume(seriesId)
}

/**
 * Ask for every byte count before moving any bytes.
 *
 * Two passes on purpose. The first fetches each episode's record — which carries `sizeBytes` —
 * and caches it, so the series page can render offline *and* so the total shown in the
 * confirmation is the real one rather than an estimate. Only then does the second pass start
 * pulling video.
 */
/** How many episode records to read at once.
 *
 * Sequential was the mistake that made this feel broken: sixty-seven round trips one after the
 * other, through Cloudflare, is most of a minute of a spinner that looks exactly like a
 * download.
 *
 * Four, not eight. Eight produced a real `504 Gateway Timeout` from the NAS on this library —
 * each of those records is a handful of queries against SQLite on a home box behind Cloudflare,
 * and the ceiling is lower than a browser's six-connection limit suggests. Four is still an
 * order of magnitude better than one, and it stops short of the thing that broke.
 */
const MEASURE_CONCURRENCY = 4

/** One retry, because a 504 is a timeout rather than an answer: the record is there, the box
 *  was just busy. Without this a single slow episode fails the whole measurement and the dialog
 *  says it could not work out the size. */
async function withRetry<T>(work: () => Promise<T>): Promise<T> {
  try {
    return await work()
  } catch {
    await new Promise((resolve) => setTimeout(resolve, 400))
    return work()
  }
}

async function inBatches<T, R>(
  items: T[],
  size: number,
  work: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = []
  for (let index = 0; index < items.length; index += size) {
    results.push(...(await Promise.all(items.slice(index, index + size).map(work))))
  }
  return results
}

export interface Measured {
  episodeIds: string[]
  bytes: number
}

/**
 * Read every episode record, so the size shown is the real one and the series page can render
 * offline afterwards.
 *
 * Two things at once, deliberately: the byte total for the confirmation, and the JSON cached
 * for the trip. Doing them separately would mean asking the server for the same sixty-seven
 * records twice.
 */
export async function measure(detail: SeriesDetail): Promise<Measured> {
  const available = (detail.seasons ?? [])
    .flatMap((season) => season.episodes)
    .filter((episode) => episode.available)

  const cache = await caches.open(API_CACHE)

  const sizes = await inBatches(available, MEASURE_CONCURRENCY, async (episode) => {
    const record = await withRetry(() =>
      api.get<EpisodeDetail>(`/library/episodes/${encodeURIComponent(episode.id)}`),
    )
    await cache.put(
      `/api/library/episodes/${encodeURIComponent(episode.id)}`,
      new Response(JSON.stringify(record), { headers: { 'content-type': 'application/json' } }),
    )
    return record.mediaFile?.sizeBytes ?? 0
  })

  return {
    episodeIds: available.map((episode) => episode.id),
    bytes: sizes.reduce((total, size) => total + size, 0),
  }
}

/**
 * Put a series in the queue. Returns as soon as it is recorded, not when it is downloaded.
 *
 * The work runs in a module-level pump rather than under the component that pressed the button,
 * so navigating away — or closing the series page entirely — does not stop it. That is the
 * whole point: press Download, go and look at something else, and watch the count climb on the
 * Downloads screen.
 */
export async function start(detail: SeriesDetail, measured?: Measured): Promise<void> {
  if (!offlineSupported()) return
  cancelled.delete(detail.id)

  // Ask before filling the disk. On an installed app this is granted without a prompt on
  // Chromium and Android; on iOS it is the difference between surviving the trip and not.
  await navigator.storage?.persist?.()

  const existing = await get<DownloadRow>(DOWNLOADS, detail.id)
  // The dialog has usually just measured it; asking the server for the same sixty-seven
  // records a second time is a minute of nothing.
  const { episodeIds, bytes } = measured ?? (await measure(detail))

  const row: DownloadRow = existing ?? {
    seriesId: detail.id,
    slug: detail.slug,
    title: detail.title,
    posterUrl: detail.posterUrl,
    episodeIds,
    storedIds: [],
    bytesTotal: bytes,
    bytesDone: 0,
    state: 'queued',
    error: null,
    updatedAt: Date.now(),
  }
  row.episodeIds = episodeIds
  row.bytesTotal = bytes
  row.state = 'queued'
  row.error = null
  await save(row)

  // The poster, so the downloads shelf is not a row of grey rectangles on the plane.
  if (detail.posterUrl) {
    try {
      await (await caches.open(API_CACHE)).add(detail.posterUrl)
    } catch {
      /* Artwork is not worth failing a download over; Poster already degrades to a glyph. */
    }
  }

  queue.push(detail.id)
}

/** Resume something already measured — a paused row, or one a reload interrupted. No
 *  `SeriesDetail` needed: the row already knows its episodes. */
export function resume(seriesId: string): void {
  if (!offlineSupported()) return
  cancelled.delete(seriesId)
  queue.push(seriesId)
}

async function drain(seriesId: string): Promise<void> {
  const row = await get<DownloadRow>(DOWNLOADS, seriesId)
  if (!row) return

  row.state = 'running'
  row.error = null
  await save(row)

  const media = await caches.open(MEDIA_CACHE)
  const done = new Set(row.storedIds)

  for (const id of row.episodeIds) {
    if (cancelled.has(seriesId)) break
    if (done.has(id)) continue

    const url = mediaUrl(id)
    try {
      // No Range header, so the API answers a full 200 with content-length — the one shape
      // that both Chrome and Safari replay from a cache with seeking intact.
      const response = await fetch(url, { credentials: 'same-origin' })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      await media.put(url, response)
    } catch (error) {
      row.state = 'error'
      row.error = error instanceof Error ? error.message : 'failed'
      await save(row)
      return
    }

    done.add(id)
    row.storedIds = [...done]
    row.bytesDone = Math.round((row.bytesTotal * done.size) / Math.max(1, row.episodeIds.length))
    await save(row)
  }

  row.state = cancelled.has(seriesId)
    ? 'paused'
    : done.size === row.episodeIds.length
      ? 'done'
      : 'paused'
  await save(row)
}

export function pause(seriesId: string): void {
  cancelled.add(seriesId)
  queue.drop(seriesId)
}

export async function erase(seriesId: string): Promise<void> {
  const row = await get<DownloadRow>(DOWNLOADS, seriesId)
  if (row) {
    const media = await caches.open(MEDIA_CACHE)
    await Promise.all(row.episodeIds.map((id) => media.delete(mediaUrl(id))))
  }
  await remove(DOWNLOADS, seriesId)
  rows = rows.filter((existing) => existing.seriesId !== seriesId)
  announce()
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function snapshot(): DownloadRow[] {
  return rows
}

/** The series being pulled right now, if any. */
export function activeSeries(): string | null {
  return queue.active()
}

/** How many are downloading or waiting — the number the header badge shows, so a download in
 *  progress is visible from anywhere rather than only on its own screen. */
export function pendingCount(): number {
  return queue.size()
}
