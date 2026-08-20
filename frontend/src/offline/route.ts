/** What the service worker should do with a request.
 *
 * Pure, and separate from `sw.ts`, because this is the half worth testing: vitest runs in node,
 * which has `Request` and `Response` but no `caches` and no `ServiceWorkerGlobalScope`. Keeping
 * the decision here and the plumbing there means the rules are covered and only the wiring is
 * not.
 */

export type Route = 'passthrough' | 'navigate' | 'media' | 'api'

/** JSON the app must be able to read with the server off. Everything a downloaded series needs
 *  to render, plus the session — without which `ProtectedRoute` sends you to the login page. */
const CACHEABLE_API = [
  '/api/auth/session',
  '/api/config',
  '/api/library/',
  '/api/me/',
  '/api/stream/images/',
]

/** Never cached, whatever else matches: these are reads of somebody else's state or writes. */
const NEVER_CACHE = ['/api/admin/', '/api/watch/', '/api/requests/']

export interface RequestFacts {
  url: string
  method: string
  /** `request.mode` — 'navigate' for a page load, which is how a cold boot is recognised. */
  mode: string
  /** Same-origin only. A cross-origin request is never ours to answer. */
  sameOrigin: boolean
}

export function classify(facts: RequestFacts): Route {
  // A write is never answered from a cache, and never recorded in one. Watch progress posted on
  // a plane must reach the server later, not be replayed as a response.
  if (facts.method !== 'GET') return 'passthrough'
  if (!facts.sameOrigin) return 'passthrough'

  if (facts.mode === 'navigate') return 'navigate'

  const path = pathOf(facts.url)
  if (path === null) return 'passthrough'

  if (NEVER_CACHE.some((prefix) => path.startsWith(prefix))) return 'passthrough'

  // Checked before the API list: `/api/stream/images/` is artwork and belongs with the JSON,
  // while `/api/stream/<id>` is an episode and is the only thing served cache-first.
  if (path.startsWith('/api/stream/') && !path.startsWith('/api/stream/images/')) return 'media'

  if (CACHEABLE_API.some((prefix) => path.startsWith(prefix))) return 'api'

  return 'passthrough'
}

function pathOf(url: string): string | null {
  try {
    return new URL(url).pathname
  } catch {
    return null
  }
}
