/** The queue, because the bug it replaces was silent.
 *
 * A global "something is running" flag made a second Download press do nothing — no error, no
 * toast, no row. For a feature whose entire purpose is "put two or three series on a device
 * before a trip", that is the feature missing.
 */

import { describe, expect, it, vi } from 'vitest'

import { createQueue } from './queue'

/** A worker that can be released on demand, so the test controls the interleaving. */
function controllable() {
  const releases: Array<() => void> = []
  const started: string[] = []
  const work = vi.fn(async (id: string) => {
    started.push(id)
    await new Promise<void>((resolve) => releases.push(resolve))
  })
  return { work, started, release: () => releases.shift()?.() }
}

describe('createQueue', () => {
  it('takes a second job while the first is running', async () => {
    const { work, started, release } = controllable()
    const queue = createQueue(work)

    queue.push('a')
    await Promise.resolve()
    queue.push('b')

    expect(queue.active()).toBe('a')
    expect(queue.waiting()).toEqual(['b'])

    release()
    await vi.waitFor(() => expect(started).toEqual(['a', 'b']))
  })

  it('runs one at a time, in the order they were asked for', async () => {
    const { work, started, release } = controllable()
    const queue = createQueue(work)

    queue.push('a')
    queue.push('b')
    queue.push('c')
    await Promise.resolve()

    expect(started).toEqual(['a'])
    release()
    await vi.waitFor(() => expect(started).toEqual(['a', 'b']))
    release()
    await vi.waitFor(() => expect(started).toEqual(['a', 'b', 'c']))
  })

  it('ignores a job already running or already waiting', async () => {
    const { work, release } = controllable()
    const queue = createQueue(work)

    queue.push('a')
    await Promise.resolve()
    queue.push('a')
    queue.push('b')
    queue.push('b')

    expect(queue.waiting()).toEqual(['b'])
    release()
    await vi.waitFor(() => expect(queue.size()).toBe(1))
  })

  it('does not strand the rest when one job throws', async () => {
    const started: string[] = []
    const queue = createQueue(async (id) => {
      started.push(id)
      if (id === 'bad') throw new Error('disk full')
    })

    queue.push('bad')
    queue.push('good')

    await vi.waitFor(() => expect(started).toEqual(['bad', 'good']))
    await vi.waitFor(() => expect(queue.size()).toBe(0))
  })

  it('starts only one loop however many times it is pushed', async () => {
    /* Every push calls the pump. Without the re-entrancy guard a push during an await starts a
       second loop over the same list, and two series download at once over one home uplink. */
    const { work, started, release } = controllable()
    const queue = createQueue(work)

    queue.push('a')
    await Promise.resolve()
    queue.push('b')
    queue.push('c')
    await Promise.resolve()

    expect(started).toEqual(['a'])
    release()
    await vi.waitFor(() => expect(started).toEqual(['a', 'b']))
  })

  it('counts what a badge should show', async () => {
    const { work, release } = controllable()
    const queue = createQueue(work)

    expect(queue.size()).toBe(0)
    queue.push('a')
    queue.push('b')
    await Promise.resolve()
    expect(queue.size()).toBe(2)

    release()
    await vi.waitFor(() => expect(queue.size()).toBe(1))
  })

  it('drops something still waiting without touching what is running', async () => {
    const { work, release } = controllable()
    const queue = createQueue(work)

    queue.push('a')
    await Promise.resolve()
    queue.push('b')
    queue.drop('b')

    expect(queue.active()).toBe('a')
    expect(queue.waiting()).toEqual([])
    release()
    await vi.waitFor(() => expect(queue.size()).toBe(0))
  })

  it('reports a change so the badge can follow it', async () => {
    const onChange = vi.fn()
    const { work, release } = controllable()
    const queue = createQueue(work, onChange)

    queue.push('a')
    await Promise.resolve()
    release()

    await vi.waitFor(() => expect(onChange.mock.calls.length).toBeGreaterThanOrEqual(3))
  })
})
