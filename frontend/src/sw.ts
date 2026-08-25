/// <reference lib="webworker" />
/** dramatarr's service worker.
 *
 * It exists for one journey: the owner shuts the NAS down, gets on a plane, and opens the app.
 * Everything follows from that. The shell has to boot with no network, the library JSON has to
 * answer, the session has to say "signed in", and the episodes have to play.
 *
 * The decision the whole design rests on: **the worker answers `/api/stream/<id>` from Cache
 * Storage**. Because it does, the player never learns that anything changed — `VideoPlayer`
 * still sets `element.src = '/api/stream/<id>'` and the bytes arrive. The alternative, blob
 * URLs, would have meant a resolver, a revoke lifecycle, and a second code path to keep correct.
 *
 * Measured before committing to it, in Safari 27 and Chrome 151 on a real 6 MB file: a cached
 * network 200 replayed straight from `cache.match()` both **plays and seeks**. No range shim is
 * needed, because `streaming.py` already sends `accept-ranges` and `content-length` on the
 * unranged response, and that is the shape stored. The one thing that must not be done is
 * re-wrapping the bytes (`new Response(blob)`) — that drops `Content-Length`, and Safari fails
 * hard on it.
 */

import { API_CACHE, MEDIA_CACHE, SHELL_CACHE, isSweepableShell } from './offline/constants'
import { classify } from './offline/route'

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<{ url: string; revision: string | null }>
}

const SHELL_ENTRY = '/index.html'

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE)
      const urls = self.__WB_MANIFEST.map((entry) => entry.url)
      // `reload` so an install never picks the shell out of the HTTP cache — that is how a
      // worker ends up serving the previous deploy forever.
      await cache.addAll(urls.map((url) => new Request(url, { cache: 'reload' })))
    })(),
  )
  // Deliberately no skipWaiting(). An open tab holds references to hashed chunks that the new
  // precache does not have; swapping under it turns a working page into a screen of 404s. The
  // app offers a reload instead, and the user presses it.
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys()
      // Only shells. `API_CACHE` and `MEDIA_CACHE` carry no build id precisely so that they
      // survive every deploy — deleting them here would wipe a downloaded library.
      await Promise.all(
        names.filter((name) => isSweepableShell(name, SHELL_CACHE)).map((name) => caches.delete(name)),
      )
      await self.clients.claim()
    })(),
  )
})

self.addEventListener('message', (event) => {
  if ((event.data as { type?: string } | null)?.type === 'SKIP_WAITING') void self.skipWaiting()
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  const route = classify({
    url: request.url,
    method: request.method,
    mode: request.mode,
    sameOrigin: new URL(request.url).origin === self.location.origin,
  })

  if (route === 'passthrough') return
  if (route === 'navigate') return event.respondWith(navigateOr(request))
  if (route === 'media') return event.respondWith(mediaFirst(request))
  return event.respondWith(apiThenCache(request))
})

/** A cold boot on a plane. Any path is a client route, so any path may be answered with the
 *  shell — which is exactly what nginx does online. */
async function navigateOr(request: Request): Promise<Response> {
  try {
    return await fetch(request)
  } catch {
    const shell = await caches.match(SHELL_ENTRY, { cacheName: SHELL_CACHE })
    return shell ?? Response.error()
  }
}

/**
 * Cache first, and never opportunistically cached on the way past.
 *
 * A miss goes to the network and stays uncached: an episode enters the cache only because
 * somebody pressed Download. Filling this cache as a side effect of watching would let a
 * casual evening put a gigabyte on a phone nobody asked to fill.
 *
 * `ignoreVary` because the stored copy came from a plain `fetch` in the download manager while
 * this request comes from a `<video>` element, and their `Accept` headers differ.
 */
async function mediaFirst(request: Request): Promise<Response> {
  const hit = await caches.match(request, { cacheName: MEDIA_CACHE, ignoreVary: true })
  if (hit) return hit
  try {
    return await fetch(request)
  } catch {
    return new Response('', { status: 504, statusText: 'Offline and not downloaded' })
  }
}

/** Statuses that mean "not yours any more", as opposed to "not right now". A 5xx or a dropped
 *  connection must never evict anything: that is the trip the whole cache exists for. */
const REVOKED = new Set([401, 403, 404])

/** Network first so the app is never stale while there is a server, cache second so it works
 *  when there is not. */
async function apiThenCache(request: Request): Promise<Response> {
  try {
    const fresh = await fetch(request)
    if (fresh.ok) {
      const cache = await caches.open(API_CACHE)
      await cache.put(request, fresh.clone())
    } else if (REVOKED.has(fresh.status)) {
      /* The server has stopped answering this — the session ended, or a series was marked
         adults-only and this member may no longer have it. A cache-second policy would keep
         handing back the last good copy every time the network was gone, which turns a
         withdrawn permission into one that lapses only while online. Drop it here, at the one
         moment we have a definite answer. */
      await (await caches.open(API_CACHE)).delete(request, { ignoreVary: true })
    }
    return fresh
  } catch {
    const hit = await caches.match(request, { cacheName: API_CACHE, ignoreVary: true })
    if (hit) return hit
    throw new Error('offline and not cached')
  }
}
