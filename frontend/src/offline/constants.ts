/** Names and shapes shared by the app and the service worker.
 *
 * Imported from both sides, so it must stay free of anything DOM-specific — the worker is
 * typechecked against `WebWorker` and has no `window`, no `document`.
 */

/** Substituted at build time by Vite's `define`, so a new deploy gets a new shell cache name.
 *  Not `import.meta.env`: the worker is compiled without `vite/client`'s types on purpose —
 *  those pull in the DOM lib, where `self` means something else entirely. */
declare const __BUILD_ID__: string
const BUILD = typeof __BUILD_ID__ === 'string' ? __BUILD_ID__ : 'dev'

/** The app itself: JS, CSS, index.html, icons. Versioned, and swept on activate. */
export const SHELL_CACHE = `dramatarr-shell-${BUILD}`

/** JSON the app needs to render a downloaded series with nothing to ask. */
export const API_CACHE = 'dramatarr-api-v1'

/** The episodes themselves. Gigabytes of them. */
export const MEDIA_CACHE = 'dramatarr-media-v1'

/**
 * Only the shell carries the build id, and `activate` may delete **only** caches matching this.
 *
 * This is the single most damaging thing this design could get wrong: sweeping by a looser
 * prefix would make every deploy silently delete a library somebody spent a night downloading,
 * and they would find out on the plane.
 */
export const SHELL_PREFIX = 'dramatarr-shell-'

export function isSweepableShell(name: string, keep: string = SHELL_CACHE): boolean {
  return name.startsWith(SHELL_PREFIX) && name !== keep
}

/** Where an episode's bytes live. The worker answers this exact path from `MEDIA_CACHE`, which
 *  is why the player needs no change at all — it keeps asking for the same URL it always did. */
export function mediaUrl(episodeId: string): string {
  return `/api/stream/${encodeURIComponent(episodeId)}`
}
