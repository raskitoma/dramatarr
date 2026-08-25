/** Giving back a series this device is no longer allowed to have — and, far more important,
 *  not giving back anything else.
 *
 * Bytes already in Cache Storage are past every gate the server has: the player answers from
 * the cache and never asks the network again. So a series marked adults-only after somebody
 * downloaded it can only be taken back at the next start with a server in reach.
 *
 * The dangerous half is the other one. The whole reason these downloads exist is the trip where
 * the NAS is switched off, and a check that erased the library whenever the server went quiet
 * would destroy exactly the thing it was added to protect.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { MEDIA_CACHE, mediaUrl } from './constants'
import type { DownloadRow } from './db'

const store = new Map<string, DownloadRow>()
const media = new Set<string>()
const apiCache = new Set<string>()

vi.mock('./db', async (importOriginal) => {
  const real = await importOriginal<typeof import('./db')>()
  return {
    ...real,
    all: async () => [...store.values()],
    get: async (_name: string, key: string) => store.get(key),
    put: async (_name: string, value: DownloadRow) => void store.set(value.seriesId, value),
    remove: async (_name: string, key: string) => void store.delete(key),
  }
})

vi.mock('./register', () => ({ offlineSupported: () => true }))

function fakeCache(entries: Set<string>) {
  return {
    keys: async () => [...entries].map((url) => ({ url: `https://test${url}` })),
    delete: async (url: string) => entries.delete(url),
    open: undefined,
  }
}

vi.stubGlobal('caches', {
  open: async (name: string) => fakeCache(name === MEDIA_CACHE ? media : apiCache),
})

const { reconcile, snapshot } = await import('./downloadManager')

function aDownloadedSeries(seriesId: string, slug: string): DownloadRow {
  return {
    seriesId,
    slug,
    title: slug,
    posterUrl: null,
    episodes: [{ id: `${seriesId}-e1`, label: 'T01E01', sizeBytes: 100 }],
    storedIds: [`${seriesId}-e1`],
    bytesTotal: 100,
    bytesDone: 100,
    state: 'done',
    error: null,
    updatedAt: 1,
  }
}

beforeEach(() => {
  store.clear()
  media.clear()
  apiCache.clear()
  const seeded: Array<[string, string]> = [
    ['s1', 'adults-only'],
    ['s2', 'something-else'],
  ]
  for (const [id, slug] of seeded) {
    store.set(id, aDownloadedSeries(id, slug))
    media.add(mediaUrl(`${id}-e1`))
    apiCache.add(`/api/library/episodes/${id}-e1`)
    apiCache.add(`/api/library/series/${slug}`)
  }
  vi.stubGlobal('navigator', { onLine: true })
})

describe('reconcile', () => {
  it('erases a series the server now refuses, bytes and records alike', async () => {
    vi.stubGlobal('fetch', async (url: string) =>
      url.includes('adults-only') ? { status: 404 } : { status: 200 },
    )

    await reconcile()

    expect(snapshot().map((row) => row.seriesId)).toEqual(['s2'])
    expect(media.has(mediaUrl('s1-e1'))).toBe(false)
    // The synopsis and the episode names went with the video. Leaving them behind is a series
    // that still renders offline, complete except that nothing plays.
    expect([...apiCache].filter((url) => url.includes('adults-only'))).toEqual([])
    expect(apiCache.has('/api/library/episodes/s1-e1')).toBe(false)
    // Untouched, and not by luck: nothing about s2 was asked to change.
    expect(media.has(mediaUrl('s2-e1'))).toBe(true)
  })

  it('keeps everything when the server is simply not there', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('Failed to fetch')
    })

    await reconcile()

    expect(snapshot().map((row) => row.seriesId).sort()).toEqual(['s1', 's2'])
    expect(media.size).toBe(2)
  })

  it('keeps everything when the server answers badly rather than refusing', async () => {
    vi.stubGlobal('fetch', async () => ({ status: 503 }))

    await reconcile()

    expect(snapshot()).toHaveLength(2)
    expect(media.size).toBe(2)
  })

  it('does not even ask while the device is offline', async () => {
    vi.stubGlobal('navigator', { onLine: false })
    const asked = vi.fn()
    vi.stubGlobal('fetch', asked)

    await reconcile()

    expect(asked).not.toHaveBeenCalled()
    expect(snapshot()).toHaveLength(2)
  })
})
