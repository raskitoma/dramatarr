/** Collapsing a trip's worth of watching into writes the server will not misread.
 *
 * These are the cases that would silently destroy state on landing. `watch.py` assigns
 * `position_seconds` unconditionally and resets a completed episode to zero when a mid-episode
 * ping arrives after the completion — so a raw replay of pings would rewind episodes and
 * un-watch finished ones, and nothing would report it.
 */

import { describe, expect, it } from 'vitest'

import { collapse } from './progress'
import type { ProgressRow } from './db'

function row(overrides: Partial<ProgressRow> = {}): ProgressRow {
  return {
    episodeId: 'e1',
    seriesId: 's1',
    positionSeconds: 10,
    durationSeconds: 80,
    completed: false,
    watchedAt: 1000,
    synced: false,
    ...overrides,
  }
}

describe('collapse', () => {
  it('sends one write per episode, not one per ping', () => {
    const writes = collapse([
      row({ positionSeconds: 10, watchedAt: 1000 }),
      row({ positionSeconds: 40, watchedAt: 2000 }),
      row({ positionSeconds: 70, watchedAt: 3000 }),
    ])

    expect(writes).toHaveLength(1)
    expect(writes[0]?.positionSeconds).toBe(70)
  })

  it('keeps the furthest position, whatever order the pings arrive in', () => {
    const writes = collapse([
      row({ positionSeconds: 70, watchedAt: 3000 }),
      row({ positionSeconds: 40, watchedAt: 2000 }),
    ])

    expect(writes[0]?.positionSeconds).toBe(70)
  })

  it('never un-completes an episode that was finished', () => {
    /* Watched to the end, then scrubbed back to look at something. Sending the later, lower
       position would make the server reset it to unwatched. */
    const writes = collapse([
      row({ completed: true, positionSeconds: 80, watchedAt: 2000 }),
      row({ completed: false, positionSeconds: 12, watchedAt: 3000 }),
    ])

    expect(writes[0]?.completed).toBe(true)
  })

  it('sends oldest first, so continue-watching still sorts like the trip happened', () => {
    const writes = collapse([
      row({ episodeId: 'later', watchedAt: 5000 }),
      row({ episodeId: 'earlier', watchedAt: 1000 }),
      row({ episodeId: 'middle', watchedAt: 3000 }),
    ])

    expect(writes.map((write) => write.episodeId)).toEqual(['earlier', 'middle', 'later'])
  })

  it('ignores anything already sent', () => {
    expect(collapse([row({ synced: true })])).toHaveLength(0)
  })

  it('has nothing to say about an empty store', () => {
    expect(collapse([])).toEqual([])
  })
})
