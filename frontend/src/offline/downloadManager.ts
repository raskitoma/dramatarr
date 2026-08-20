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
import { offlineSupported } from './register'

const listeners = new Set<() => void>()
let rows: DownloadRow[] = []
let running: string | null = null
let cancelled = new Set<string>()

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
  rows = await all<DownloadRow>(DOWNLOADS)
  announce()
}

/**
 * Ask for every byte count before moving any bytes.
 *
 * Two passes on purpose. The first fetches each episode's record — which carries `sizeBytes` —
 * and caches it, so the series page can render offline *and* so the total shown in the
 * confirmation is the real one rather than an estimate. Only then does the second pass start
 * pulling video.
 */
export async function measure(detail: SeriesDetail): Promise<{ episodeIds: string[]; bytes: number }> {
  const available = (detail.seasons ?? [])
    .flatMap((season) => season.episodes)
    .filter((episode) => episode.available)

  const cache = await caches.open(API_CACHE)
  let bytes = 0
  const episodeIds: string[] = []

  for (const episode of available) {
    const record = await api.get<EpisodeDetail>(`/library/episodes/${encodeURIComponent(episode.id)}`)
    bytes += record.mediaFile?.sizeBytes ?? 0
    episodeIds.push(episode.id)
    await cache.put(
      `/api/library/episodes/${encodeURIComponent(episode.id)}`,
      new Response(JSON.stringify(record), { headers: { 'content-type': 'application/json' } }),
    )
  }
  return { episodeIds, bytes }
}

export async function start(detail: SeriesDetail): Promise<void> {
  if (!offlineSupported() || running) return
  running = detail.id
  cancelled.delete(detail.id)

  try {
    // Ask before filling the disk. On an installed app this is granted without a prompt on
    // Chromium and Android; on iOS it is the difference between surviving the trip and not.
    await navigator.storage?.persist?.()

    const existing = await get<DownloadRow>(DOWNLOADS, detail.id)
    const { episodeIds, bytes } = await measure(detail)

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
    row.state = 'running'
    row.error = null
    await save(row)

    // The poster, so the downloads shelf is not a row of grey rectangles on the plane.
    if (detail.posterUrl) {
      const cache = await caches.open(API_CACHE)
      try {
        await cache.add(detail.posterUrl)
      } catch {
        /* Artwork is not worth failing a download over; Poster already degrades to a glyph. */
      }
    }

    const media = await caches.open(MEDIA_CACHE)
    const done = new Set(row.storedIds)

    for (const id of episodeIds) {
      if (cancelled.has(detail.id)) break
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
      row.bytesDone = Math.round((bytes * done.size) / Math.max(1, episodeIds.length))
      await save(row)
    }

    row.state = cancelled.has(detail.id)
      ? 'paused'
      : done.size === episodeIds.length
        ? 'done'
        : 'paused'
    await save(row)
  } finally {
    running = null
    announce()
  }
}

export function pause(seriesId: string): void {
  cancelled.add(seriesId)
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

export function isRunning(): string | null {
  return running
}
