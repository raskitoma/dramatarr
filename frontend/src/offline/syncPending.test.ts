/** What happens to a write the server will never accept.
 *
 * The sync stops at its first failure on purpose: sending the tail out of order is what
 * rewinds an episode. That rule has a sharp edge once a series can be withdrawn — an episode
 * marked adults-only while the device was away answers 404 for good, and under the old rule
 * every write queued behind it would wait on it forever. A trip's worth of watch state on
 * completely unrelated series, held up by one row that can never succeed.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ProgressRow } from './db'

const store = new Map<string, ProgressRow>()

vi.mock('./db', async (importOriginal) => {
  const real = await importOriginal<typeof import('./db')>()
  return {
    ...real,
    all: async () => [...store.values()],
    get: async (_name: string, key: string) => store.get(key),
    put: async (_name: string, value: ProgressRow) => void store.set(value.episodeId, value),
    remove: async (_name: string, key: string) => void store.delete(key),
  }
})

const { syncPending } = await import('./progress')

function row(episodeId: string, watchedAt: number): ProgressRow {
  return {
    episodeId,
    seriesId: 's1',
    positionSeconds: 30,
    durationSeconds: 80,
    completed: false,
    watchedAt,
    synced: false,
  }
}

class Refusal extends Error {
  constructor(readonly status: number) {
    super(`refused with ${status}`)
  }
}

beforeEach(() => {
  store.clear()
  for (const [index, id] of ['gone', 'a', 'b'].entries()) store.set(id, row(id, 1000 + index))
})

describe('syncPending', () => {
  it('drops a write the server will refuse forever and keeps going', async () => {
    const sent: string[] = []
    const result = await syncPending(async (write) => {
      if (write.episodeId === 'gone') throw new Refusal(404)
      sent.push(write.episodeId)
    })

    expect(sent).toEqual(['a', 'b'])
    expect(result).toEqual({ sent: 2, failed: 0, discarded: 1 })
    // And it is gone from the store, so it cannot hold anything up again next time.
    expect(store.has('gone')).toBe(false)
  })

  it('treats a withdrawn permission the same way as a deleted episode', async () => {
    const result = await syncPending(async (write) => {
      if (write.episodeId === 'gone') throw new Refusal(403)
    })
    expect(result.discarded).toBe(1)
    expect(result.sent).toBe(2)
  })

  it('still stops at a transient failure, and keeps the rest in order', async () => {
    const result = await syncPending(async (write) => {
      if (write.episodeId === 'gone') throw new Refusal(503)
    })

    expect(result).toEqual({ sent: 0, failed: 3, discarded: 0 })
    // Nothing was sent past it and nothing was thrown away.
    expect([...store.keys()].sort()).toEqual(['a', 'b', 'gone'])
  })

  it('never discards over an expired session, which is a login and not a refusal', async () => {
    const result = await syncPending(async (write) => {
      if (write.episodeId === 'gone') throw new Refusal(401)
    })

    expect(result.discarded).toBe(0)
    expect(store.has('gone')).toBe(true)
  })
})
