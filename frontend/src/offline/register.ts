/** Registering the worker, and noticing when a new one is waiting.
 *
 * Deliberately not automatic about updates. A worker that calls `skipWaiting()` on its own
 * swaps the precache under a tab that still holds references to the previous build's hashed
 * chunks, and the next lazy import 404s — a working page turns into a blank one for no reason
 * the user can see. So a new build waits, the app says so, and the person presses reload.
 */

import { useSyncExternalStore } from 'react'

let waiting: ServiceWorker | null = null
const listeners = new Set<() => void>()

function announce(): void {
  for (const listener of listeners) listener()
}

/** True where a worker can exist at all. False on `http://<nas-ip>:8090`, which is not a secure
 *  context — everything offline is unavailable there, and the UI has to say so rather than
 *  offering a button that cannot work. */
export function offlineSupported(): boolean {
  return typeof navigator !== 'undefined' && 'serviceWorker' in navigator && 'caches' in globalThis
}

/** Whether a worker is actually in control of this page right now. The honest signal for "this
 *  device can open the app with the server off" — a registration that has never taken control
 *  answers nothing. */
export function offlineReady(): boolean {
  return offlineSupported() && navigator.serviceWorker.controller !== null
}

/** Push everything watched offline back to the server, one terminal write per episode.
 *
 * Fired on app start and whenever the browser says it is online again. Failure is fine and
 * silent — the rows stay pending and keep their order, and the Downloads screen shows how many
 * are still waiting so it is not a thing you have to take on faith.
 */
async function flushWatchState(): Promise<void> {
  const { syncPending } = await import('./progress')
  const { api } = await import('@/api/client')
  await syncPending(async (write) => {
    await api.post<void>(`/watch/${encodeURIComponent(write.episodeId)}/progress`, {
      positionSeconds: write.positionSeconds,
      durationSeconds: write.durationSeconds,
    })
    if (write.completed) {
      await api.post<void>(`/watch/${encodeURIComponent(write.episodeId)}/complete`, {})
    }
  })
}

export function registerServiceWorker(): void {
  if (!offlineSupported()) return

  void flushWatchState()
  window.addEventListener('online', () => void flushWatchState())

  // Check the ledger against the cache at boot, so a series the browser evicted stops claiming
  // to be downloaded before somebody relies on it rather than after.
  void import('./downloadManager').then((module) => void module.reconcile())

  void navigator.serviceWorker.register('/sw.js', { scope: '/' }).then((registration) => {
    if (registration.waiting) {
      waiting = registration.waiting
      announce()
    }
    registration.addEventListener('updatefound', () => {
      const next = registration.installing
      if (!next) return
      next.addEventListener('statechange', () => {
        // `controller` is null on the very first install; that one is not an update and must
        // not raise a reload prompt on a page that has only just started working.
        if (next.state === 'installed' && navigator.serviceWorker.controller) {
          waiting = next
          announce()
        }
      })
    })
  })

  // The page is being controlled by a different worker than the one that loaded it, which only
  // happens after the user asked for it below.
  let reloading = false
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (reloading) return
    reloading = true
    window.location.reload()
  })
}

/** Take the update the user just asked for. */
export function applyUpdate(): void {
  waiting?.postMessage({ type: 'SKIP_WAITING' })
}

export function useWaitingUpdate(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => waiting !== null,
    () => false,
  )
}
