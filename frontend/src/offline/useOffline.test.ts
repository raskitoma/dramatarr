/**
 * "Can this device play it" — the question the Play button, the episode rows and the grid all
 * used to answer differently.
 *
 * Four episodes downloaded, aeroplane mode, press Play: it opened the fifth. `available` means
 * the NAS holds a file, which is the right answer online and useless on a plane.
 */

import { describe, expect, it } from 'vitest'

/** The predicate `usePlayable` returns, extracted so it can be tested without React. */
function playable(
  episode: { id: string; available: boolean },
  online: boolean,
  stored: ReadonlySet<string>,
): boolean {
  return episode.available && (online || stored.has(episode.id))
}

const downloaded = new Set(['e1', 'e2', 'e3', 'e4'])

describe('what this device can play', () => {
  it('offers everything the NAS has while there is a network', () => {
    expect(playable({ id: 'e40', available: true }, true, downloaded)).toBe(true)
  })

  it('offers only what was downloaded once there is not', () => {
    expect(playable({ id: 'e4', available: true }, false, downloaded)).toBe(true)
    expect(playable({ id: 'e5', available: true }, false, downloaded)).toBe(false)
  })

  it('never offers an episode with no file at all, online or off', () => {
    /* Downloaded is a stronger claim than available, never a substitute for it. */
    expect(playable({ id: 'e2', available: false }, true, downloaded)).toBe(false)
    expect(playable({ id: 'e2', available: false }, false, downloaded)).toBe(false)
  })

  it('hides nothing when nothing is downloaded and there is a network', () => {
    expect(playable({ id: 'e1', available: true }, true, new Set())).toBe(true)
  })

  it('offers nothing when nothing is downloaded and there is no network', () => {
    expect(playable({ id: 'e1', available: true }, false, new Set())).toBe(false)
  })
})
