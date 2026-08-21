// @vitest-environment jsdom
/**
 * The downloaded mark, which answers a question none of the other marks do.
 *
 * An episode row already says whether it was watched and whether the NAS has a file. Neither
 * tells you what is on *this* phone, which is the only thing that matters when deciding what
 * to take on a plane.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { EpisodeSummary } from '@/api/types'
import { setupI18n } from '@/i18n'

let stored = new Set<string>()
vi.mock('@/offline/useOffline', () => ({
  usePlayable: () => (episode: { available: boolean }) => episode.available,
  useStoredEpisodes: () => stored,
}))
vi.mock('wouter', () => ({ Link: ({ children }: { children?: unknown }) => children as never }))
vi.mock('@/api/library', () => ({
  useMarkEpisodeWatched: () => ({ mutate: vi.fn(), isPending: false }),
  useClearEpisodeProgress: () => ({ mutate: vi.fn(), isPending: false }),
}))

const { EpisodeRow } = await import('./EpisodeRow')

function episode(overrides: Partial<EpisodeSummary> = {}): EpisodeSummary {
  return {
    id: 'e1',
    seasonNumber: 1,
    episodeNumber: 1,
    absoluteNumber: null,
    title: 'Uno',
    stillUrl: null,
    airDate: null,
    runtimeSeconds: 80,
    available: true,
    watched: false,
    progressPercent: 0,
    resumePositionSeconds: null,
    ...overrides,
  } as EpisodeSummary
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  stored = new Set()
})

afterEach(cleanup)

describe('EpisodeRow — the downloaded mark', () => {
  it('marks an episode whose bytes are on this device', () => {
    stored = new Set(['e1'])
    render(<EpisodeRow episode={episode()} />)

    expect(screen.getByText('Descargado en este dispositivo')).toBeTruthy()
  })

  it('says nothing about one that is not', () => {
    render(<EpisodeRow episode={episode()} />)

    expect(screen.queryByText('Descargado en este dispositivo')).toBeNull()
  })

  it('is a separate fact from watched', () => {
    /* Both can be true, and they mean different things: one is about you, the other about this
       phone. A downloaded-and-watched episode must show both. */
    stored = new Set(['e1'])
    render(<EpisodeRow episode={episode({ watched: true })} />)

    expect(screen.getByText('Descargado en este dispositivo')).toBeTruthy()
    expect(screen.getByText('Visto')).toBeTruthy()
  })
})
