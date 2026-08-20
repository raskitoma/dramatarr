/** The ledger: what is downloaded on *this* device, and how far along.
 *
 * Hand-rolled rather than a dependency. Three object stores and no queries worth the name — an
 * IndexedDB wrapper would be more code to read than the thing it wraps.
 *
 * The bytes themselves are **not** here; they live in Cache Storage, where the service worker
 * can answer a `<video>` element from them directly. This is only the bookkeeping, which is why
 * it has to be reconciled against the cache rather than trusted: a browser that evicts under
 * disk pressure takes the bytes and leaves these rows untouched.
 */

const DB_NAME = 'dramatarr'
const DB_VERSION = 1

export const DOWNLOADS = 'downloads'
export const PROGRESS = 'progress'

export type DownloadState = 'queued' | 'running' | 'paused' | 'done' | 'error'

/** One episode, as the download screen needs to show it. */
export interface DownloadEpisode {
  id: string
  /** "T01E04 · Título", built once at measure time so the list needs no second lookup. */
  label: string
  /** The real size of this file, read from its record. Not an average. */
  sizeBytes: number
}

export interface DownloadRow {
  seriesId: string
  slug: string
  title: string
  posterUrl: string | null
  /** Episodes in play order, so a resumed download picks up where it stopped and the expanded
   *  list can show each one with its own size. */
  episodes: DownloadEpisode[]
  /** Ids whose bytes are in the media cache. A subset of `episodes`. */
  storedIds: string[]
  bytesTotal: number
  /** The **sum of the episodes actually stored**, not a share of the total. Episodes here run
   *  from 57 to 112 seconds and their files differ accordingly, so the proportional estimate
   *  this replaced was wrong on every partially-downloaded series. */
  bytesDone: number
  state: DownloadState
  /** Set when the browser refused, so the UI can say which episode it stopped at. */
  error: string | null
  updatedAt: number
}

export interface ProgressRow {
  episodeId: string
  seriesId: string
  positionSeconds: number
  durationSeconds: number
  completed: boolean
  /** Wall clock at the moment of watching. Used to order the sync, not to backdate the write —
   *  the server has no bulk or backdated progress route and this plan does not add one. */
  watchedAt: number
  synced: boolean
}

let open: Promise<IDBDatabase> | null = null

export function db(): Promise<IDBDatabase> {
  if (open) return open
  open = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onupgradeneeded = () => {
      const database = request.result
      if (!database.objectStoreNames.contains(DOWNLOADS)) {
        database.createObjectStore(DOWNLOADS, { keyPath: 'seriesId' })
      }
      if (!database.objectStoreNames.contains(PROGRESS)) {
        database.createObjectStore(PROGRESS, { keyPath: 'episodeId' })
      }
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error ?? new Error('indexedDB refused to open'))
  })
  return open
}

function run<T>(store: IDBObjectStore, request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error ?? new Error(`${store.name} failed`))
  })
}

export async function put<T>(name: string, value: T): Promise<void> {
  const database = await db()
  const store = database.transaction(name, 'readwrite').objectStore(name)
  await run(store, store.put(value))
}

export async function get<T>(name: string, key: string): Promise<T | undefined> {
  const database = await db()
  const store = database.transaction(name, 'readonly').objectStore(name)
  return run(store, store.get(key) as IDBRequest<T | undefined>)
}

export async function all<T>(name: string): Promise<T[]> {
  const database = await db()
  const store = database.transaction(name, 'readonly').objectStore(name)
  return run(store, store.getAll() as IDBRequest<T[]>)
}

export async function remove(name: string, key: string): Promise<void> {
  const database = await db()
  const store = database.transaction(name, 'readwrite').objectStore(name)
  await run(store, store.delete(key))
}
