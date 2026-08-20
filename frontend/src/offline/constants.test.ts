/** The sweep predicate, alone in its own file because getting it wrong is the worst thing this
 *  feature can do: a deploy that deletes a library somebody spent a night downloading, found out
 *  about on the plane. */

import { describe, expect, it } from 'vitest'

import { API_CACHE, MEDIA_CACHE, SHELL_PREFIX, isSweepableShell, mediaUrl } from './constants'

describe('cache sweeping', () => {
  it('deletes an older shell', () => {
    expect(isSweepableShell('dramatarr-shell-abc123', 'dramatarr-shell-def456')).toBe(true)
  })

  it('keeps the current shell', () => {
    expect(isSweepableShell('dramatarr-shell-def456', 'dramatarr-shell-def456')).toBe(false)
  })

  it('never touches the downloaded episodes', () => {
    expect(isSweepableShell(MEDIA_CACHE, 'dramatarr-shell-x')).toBe(false)
  })

  it('never touches the cached library JSON', () => {
    expect(isSweepableShell(API_CACHE, 'dramatarr-shell-x')).toBe(false)
  })

  it('leaves another app on the same origin alone', () => {
    expect(isSweepableShell('workbox-precache-v2', 'dramatarr-shell-x')).toBe(false)
  })

  it('keeps the build id out of the caches that must survive a deploy', () => {
    /* If either of these ever carried a hash, every deploy would wipe the library. */
    expect(API_CACHE.startsWith(SHELL_PREFIX)).toBe(false)
    expect(MEDIA_CACHE.startsWith(SHELL_PREFIX)).toBe(false)
  })
})

describe('mediaUrl', () => {
  it('is the exact path the player already asks for', () => {
    expect(mediaUrl('ep123')).toBe('/api/stream/ep123')
  })

  it('escapes an id rather than building a broken path', () => {
    expect(mediaUrl('a/b')).toBe('/api/stream/a%2Fb')
  })
})
