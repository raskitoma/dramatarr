// @vitest-environment jsdom
/**
 * The grid card, and the one claim it must not make.
 *
 * A green tick titled "Visto" on a series whose own page says an episode is missing is the
 * counter contradiction one screen earlier, with the disclosure removed — and it was live on
 * twelve of nineteen series the moment their held files were watched.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import type { SeriesSummary } from '@/api/types'
import { setupI18n } from '@/i18n'

vi.mock('wouter', () => ({
  Link: ({ children }: { children?: unknown }) => <a href="#">{children as never}</a>,
}))

const { SeriesCard } = await import('./SeriesCard')

function series(overrides: Partial<SeriesSummary> = {}): SeriesSummary {
  return {
    id: 's1',
    slug: 'x-ray',
    title: 'My X-Ray Vision Sees Right Through You',
    year: 2025,
    status: 'unknown',
    posterUrl: null,
    genres: [],
    availableEpisodeCount: 62,
    declaredEpisodeCount: null,
    watchedEpisodeCount: 62,
    progressPercent: 100,
    ...overrides,
  } as SeriesSummary
}

beforeAll(async () => {
  await setupI18n('es')
})

afterEach(cleanup)

describe('SeriesCard — finished means finished', () => {
  it('does not call a series watched while the indexer knows of more', () => {
    render(<SeriesCard series={series({ declaredEpisodeCount: 63 })} />)

    expect(screen.queryByTitle('Visto')).toBeNull()
    // And it says both numbers, the same sentence the series page shows.
    expect(screen.getByText('62 de 63 disponibles')).toBeTruthy()
  })

  it('calls it watched when the library holds the whole run', () => {
    render(<SeriesCard series={series({ declaredEpisodeCount: 62 })} />)

    expect(screen.getByTitle('Visto')).toBeTruthy()
    expect(screen.getByText('62 disponibles')).toBeTruthy()
  })

  it('calls it watched when the indexer never said how many there are', () => {
    /* Most of this catalogue. An absent declared count is not evidence of a shortfall. */
    render(<SeriesCard series={series({ declaredEpisodeCount: null })} />)

    expect(screen.getByTitle('Visto')).toBeTruthy()
  })

  it('ignores a declared total below what is already on the NAS', () => {
    /* A wrong match claiming one episode for a folder of sixty-two. `declaredIsPlausible`
       refuses it, and the card must not then read that as a shortfall either. */
    render(<SeriesCard series={series({ declaredEpisodeCount: 1 })} />)

    expect(screen.getByTitle('Visto')).toBeTruthy()
    expect(screen.getByText('62 disponibles')).toBeTruthy()
  })

  it('says nothing about being watched when it is not', () => {
    render(<SeriesCard series={series({ watchedEpisodeCount: 30, declaredEpisodeCount: 62 })} />)

    expect(screen.queryByTitle('Visto')).toBeNull()
  })
})
