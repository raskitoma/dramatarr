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
import {
  DOWNLOADS,
  all,
  get,
  put,
  remove,
  type DownloadEpisode,
  type DownloadRow,
} from './db'
import { normaliseRow } from './legacyRow'
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
    // Rows written by the previous shape carry `episodeIds` and no `episodes`; see
    // `legacyRow.ts` for why reading one unmigrated kills this screen.
    const { row: migrated, changed } = normaliseRow(row)
    if (changed) {
      Object.assign(row, migrated)
      await put(DOWNLOADS, row)
    }

    const kept = row.episodes.filter((episode) => present.has(mediaUrl(episode.id)))
    if (kept.length !== row.storedIds.length) {
      row.storedIds = kept.map((episode) => episode.id)
      row.bytesDone = kept.reduce((total, episode) => total + episode.sizeBytes, 0)
      row.state = kept.length === row.episodes.length ? 'done' : 'paused'
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
  episodes: DownloadEpisode[]
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

  const episodes = await inBatches(available, MEASURE_CONCURRENCY, async (episode) => {
    const record = await withRetry(() =>
      api.get<EpisodeDetail>(`/library/episodes/${encodeURIComponent(episode.id)}`),
    )
    await cache.put(
      `/api/library/episodes/${encodeURIComponent(episode.id)}`,
      new Response(JSON.stringify(record), { headers: { 'content-type': 'application/json' } }),
    )
    return {
      id: episode.id,
      label: episodeLabel(episode),
      sizeBytes: record.mediaFile?.sizeBytes ?? 0,
    } satisfies DownloadEpisode
  })

  return {
    episodes,
    bytes: episodes.reduce((total, episode) => total + episode.sizeBytes, 0),
  }
}

/** `T01E04 · Título`. Built once, at measure time, so the expanded list is a render and not a
 *  second round of lookups on a device with no server. */
function episodeLabel(episode: { seasonNumber: number; episodeNumber: number; title: string | null }): string {
  const code = `T${String(episode.seasonNumber).padStart(2, '0')}E${String(episode.episodeNumber).padStart(2, '0')}`
  return episode.title ? `${code} · ${episode.title}` : code
}

/**
 * Put a series in the queue. Returns as soon as it is recorded, not when it is downloaded.
 *
 * The work runs in a module-level pump rather than under the component that pressed the button,
 * so navigating away — or closing the series page entirely — does not stop it. That is the
 * whole point: press Download, go and look at something else, and watch the count climb on the
 * Downloads screen.
 */
/**
 * Cache the screens a downloaded series is reached *through*, not just the series itself.
 *
 * The worker caches API responses network-first, which means a page only works offline if it
 * happened to be opened while there was a server. Downloading a series and flying is the case
 * that has to work without that coincidence: the owner's words were that the host will be off
 * and they do not want to lose access to what they already have.
 *
 * So the download also takes the poster, the series' own record, the session (or the app sends
 * you to a login form it cannot submit), the config, and Home's two shelves — which is what
 * makes the app open onto something rather than an error.
 *
 * Failure is per-URL and silent. None of this is the download; a shelf that does not render is
 * worth less than the episodes, and losing the episodes over it would be absurd.
 */
async function prefetchShell(detail: SeriesDetail): Promise<void> {
  // Requested through `api`, not by hand-built URLs, so the address is identical to the one the
  // page will ask for — down to the query-string order. A prefetch that differs by a parameter
  // misses in silence and looks exactly like a prefetch that never ran. The service worker
  // caches each of these on the way past; nothing here writes to a cache itself.
  await Promise.all(
    [
      () => api.get(`/library/series/${encodeURIComponent(detail.slug)}`),
      () => api.get('/auth/session'),
      () => api.get('/config'),
      () => api.get('/me/continue-watching'),
      // Exactly what `useRecentlyAdded(20)` asks for on Home.
      () => api.get('/library/series', { sort: 'added', page: 1, pageSize: 20 }),
    ].map(async (ask) => {
      try {
        await ask()
      } catch {
        /* One missing shelf is worth far less than the episodes; never fail a download for it. */
      }
    }),
  )

  // The poster is not an API call, so it is the one thing cached here directly.
  if (detail.posterUrl) {
    try {
      await (await caches.open(API_CACHE)).add(detail.posterUrl)
    } catch {
      /* `Poster` already degrades to a glyph. */
    }
  }
}

export async function start(detail: SeriesDetail, measured?: Measured): Promise<void> {
  if (!offlineSupported()) throw new Error('offline storage is unavailable on this origin')
  cancelled.delete(detail.id)

  // The ledger row is written **first**, before anything that can fail. Everything below it —
  // asking for persistent storage, reading sixty-seven records, fetching a poster — can throw,
  // and when it did the whole thing vanished without trace: no row, no error, nothing on the
  // Downloads screen. A row that says "error" is worth more than silence.
  const existing = await get<DownloadRow>(DOWNLOADS, detail.id)
  const row: DownloadRow = existing ?? {
    seriesId: detail.id,
    slug: detail.slug,
    title: detail.title,
    posterUrl: detail.posterUrl,
    episodes: measured?.episodes ?? [],
    storedIds: [],
    bytesTotal: measured?.bytes ?? 0,
    bytesDone: 0,
    state: 'queued',
    error: null,
    updatedAt: Date.now(),
  }
  row.state = 'queued'
  row.error = null
  await save(row)

  try {
    // Ask before filling the disk. On an installed app this is granted without a prompt on
    // Chromium and Android; on iOS it is the difference between surviving the trip and not.
    await navigator.storage?.persist?.()

    // The dialog has usually just measured it; asking the server for the same sixty-seven
    // records a second time is a minute of nothing.
    const plan = measured ?? (await measure(detail))
    row.episodes = plan.episodes
    row.bytesTotal = plan.bytes
    await save(row)

    await prefetchShell(detail)
  } catch (error) {
    row.state = 'error'
    row.error = error instanceof Error ? error.message : 'failed'
    await save(row)
    throw error
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

  for (const episode of row.episodes) {
    const id = episode.id
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
    // The real sum, not a share of the total: these files differ enough that a proportional
    // estimate was wrong on every partially-downloaded series.
    row.bytesDone = row.episodes
      .filter((stored) => done.has(stored.id))
      .reduce((total, stored) => total + stored.sizeBytes, 0)
    await save(row)
  }

  row.state = cancelled.has(seriesId)
    ? 'paused'
    : done.size === row.episodes.length
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
    await Promise.all(row.episodes.map((episode) => media.delete(mediaUrl(episode.id))))
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
