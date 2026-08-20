/** One-at-a-time work queue, with the state kept out of the work.
 *
 * Split from `downloadManager` because the manager needs `caches`, `fetch` and IndexedDB, and
 * this is the part that actually broke: a global "is something running" flag meant pressing
 * Download on a second series did nothing at all, silently, which for "two or three series
 * before a trip" is the whole feature.
 *
 * Sequential on purpose. The bytes come through Cloudflare from a home NAS, so six concurrent
 * streams do not arrive sooner — they just make every progress number move at once.
 */

export interface Queue {
  /** Add a job, unless it is already the running one or already waiting. */
  push: (id: string) => void
  /** Take it out of the waiting list. Does not stop it if it is already running — that is the
   *  worker's business, and it is why `pause` also sets a cancellation flag. */
  drop: (id: string) => void
  active: () => string | null
  waiting: () => string[]
  /** Running plus waiting, which is the number worth showing on a badge. */
  size: () => number
}

export function createQueue(
  work: (id: string) => Promise<void>,
  onChange: () => void = () => {},
): Queue {
  let waiting: string[] = []
  let active: string | null = null
  let pumping = false

  async function pump(): Promise<void> {
    // Re-entrancy guard. `push` calls this every time, and without the flag a second push
    // during an await would start a parallel loop over the same list.
    if (pumping) return
    pumping = true
    try {
      while (waiting.length > 0) {
        const next = waiting[0]
        if (next === undefined) break
        waiting = waiting.slice(1)
        active = next
        onChange()
        try {
          await work(next)
        } catch {
          /* The worker records its own failure; the queue keeps moving. One bad series must
             not strand the ones behind it. */
        }
        active = null
        onChange()
      }
    } finally {
      pumping = false
    }
  }

  return {
    push(id) {
      if (active === id || waiting.includes(id)) return
      waiting = [...waiting, id]
      onChange()
      void pump()
    },
    drop(id) {
      waiting = waiting.filter((waitingId) => waitingId !== id)
      onChange()
    },
    active: () => active,
    waiting: () => waiting,
    size: () => (active === null ? 0 : 1) + waiting.length,
  }
}
