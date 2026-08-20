/** What this device can actually play right now.
 *
 * `available` on an episode means *the NAS has a file*, which is the right answer online and
 * the wrong one on a plane. Downloaded is a different question, answered here, and the two
 * only coincide when a whole series came down.
 */

import { useEffect, useState, useSyncExternalStore } from 'react'

import { snapshot, subscribe } from './downloadManager'
import { offlineSupported } from './register'

/** Tracks the browser's own idea of connectivity.
 *
 * `navigator.onLine` is famously weak — it says "there is a network interface", not "the NAS is
 * reachable" — so it is used only to *widen* what the app offers, never to hide anything. The
 * cost of being wrong in this direction is an episode that fails to load, which is what already
 * happens; the cost in the other direction is a downloaded library the app refuses to show.
 */
export function useOnline(): boolean {
  const [online, setOnline] = useState(() =>
    typeof navigator === 'undefined' ? true : navigator.onLine,
  )

  useEffect(() => {
    const up = () => setOnline(true)
    const down = () => setOnline(false)
    window.addEventListener('online', up)
    window.addEventListener('offline', down)
    return () => {
      window.removeEventListener('online', up)
      window.removeEventListener('offline', down)
    }
  }, [])

  return online
}

const NOTHING: ReadonlySet<string> = new Set()

/** Every episode whose bytes are on this device, across every series.
 *
 * Read from the ledger rather than from Cache Storage so it is synchronous and cheap enough to
 * call from a render. `reconcile()` is what keeps the two honest, and it runs at boot and on
 * the Downloads screen.
 */
export function useStoredEpisodes(): ReadonlySet<string> {
  return useSyncExternalStore(
    subscribe,
    () => storedSnapshot(),
    () => NOTHING,
  )
}

let cachedRows: unknown = null
let cachedSet: ReadonlySet<string> = NOTHING

function storedSnapshot(): ReadonlySet<string> {
  if (!offlineSupported()) return NOTHING
  const rows = snapshot()
  // `useSyncExternalStore` compares by reference, so a fresh Set on every call would re-render
  // for ever. The ledger array is replaced on every change, which makes it a sound cache key.
  if (rows !== cachedRows) {
    cachedRows = rows
    cachedSet = new Set(rows.flatMap((row) => row.storedIds))
  }
  return cachedSet
}

export interface PlayableEpisode {
  id: string
  available: boolean
}

/**
 * "Can this device play it, right now."
 *
 * One predicate shared by the Play button, the episode rows and the grid, because the three
 * disagreeing is exactly the failure the owner hit: the list said an episode was there, the
 * button opened it, and the player found nothing.
 */
export function usePlayable(): (episode: PlayableEpisode) => boolean {
  const online = useOnline()
  const stored = useStoredEpisodes()
  return (episode) => episode.available && (online || stored.has(episode.id))
}
