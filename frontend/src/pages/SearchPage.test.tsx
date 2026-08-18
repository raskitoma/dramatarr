// @vitest-environment jsdom
/**
 * Search: the filters, and the sentence a cast hit puts under a poster.
 *
 * The case worth trapping is the one that is invisible until somebody uses both halves at
 * once — this page writes the query into the URL from a debounced effect while a filter is
 * written from a click, and the first version of that rebuilt the whole query string from
 * `q` alone. Picking a genre erased what had been typed, and typing erased the genre.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { setupI18n } from '@/i18n'

const navigate = vi.fn()
let locationSearch = ''

vi.mock('wouter', () => ({
  useLocation: () => ['/search', navigate],
  useSearch: () => locationSearch,
  Link: ({ children }: { children?: unknown }) => children as ReactElement,
}))

const get = vi.fn()
vi.mock('@/api/client', () => ({ api: { get: (path: string, query?: unknown) => get(path, query) } }))

const { SearchPage } = await import('./SearchPage')

function seriesHit(overrides: Record<string, unknown> = {}) {
  return {
    series: {
      id: 's1',
      slug: 'feast-of-power',
      title: 'Feast of Power',
      year: 2025,
      status: 'unknown',
      posterUrl: null,
      genres: [],
      availableEpisodeCount: 10,
      declaredEpisodeCount: null,
      watchedEpisodeCount: 0,
      progressPercent: 0,
    },
    score: 90,
    matchedTitle: 'Feast of Power',
    matchedLanguage: 'en',
    matchedField: 'title',
    ...overrides,
  }
}

function draw(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <SearchPage />
    </QueryClientProvider>,
  )
}

/** The page keeps its query in `window.location.search`, so the mock has to move it too. */
function goTo(search: string): void {
  locationSearch = search
  window.history.replaceState({}, '', `/search${search}`)
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  navigate.mockReset()
  get.mockReset()
  get.mockImplementation((path: string) => {
    if (path === '/library/genres') return Promise.resolve([{ genre: 'Romance', count: 4 }])
    if (path === '/library/years') return Promise.resolve([{ year: 2025, count: 3 }])
    return Promise.resolve({ query: '', series: [], episodes: [], tookMs: 1 })
  })
  goTo('')
})

afterEach(cleanup)

describe('SearchPage — how a hit explains itself', () => {
  it('says whose name matched when the hit came from the cast', async () => {
    goTo('?q=Adam%20Daniel')
    get.mockImplementation((path: string) => {
      if (path === '/library/genres') return Promise.resolve([])
      if (path === '/library/years') return Promise.resolve([])
      return Promise.resolve({
        query: 'Adam Daniel',
        series: [seriesHit({ matchedField: 'cast', matchedTitle: 'Adam Daniel' })],
        episodes: [],
        tookMs: 3,
      })
    })
    draw()

    expect(await screen.findByText('Aparece Adam Daniel')).toBeTruthy()
    // Not the title phrasing — that would claim the show is called Adam Daniel.
    expect(screen.queryByText('Coincide con «Adam Daniel»')).toBeNull()
  })

  it('says nothing extra when the title itself matched', async () => {
    goTo('?q=Feast')
    get.mockImplementation((path: string) => {
      if (path.startsWith('/library/search')) {
        return Promise.resolve({ query: 'Feast', series: [seriesHit()], episodes: [], tookMs: 3 })
      }
      return Promise.resolve([])
    })
    draw()

    // The card names itself twice: once for screen readers, once aria-hidden for the eye.
    await screen.findAllByText('Feast of Power')
    expect(screen.queryByText(/Coincide con/)).toBeNull()
    expect(screen.queryByText(/Aparece/)).toBeNull()
  })
})

describe('SearchPage — the filters', () => {
  it('sends the facets with the query', async () => {
    goTo('?q=Adam&genre=Romance&year=2025&availableOnly=1')
    draw()

    await waitFor(() => {
      const call = get.mock.calls.find(([path]) => path === '/library/search')
      expect(call).toBeTruthy()
      expect(call?.[1]).toMatchObject({
        q: 'Adam',
        genre: 'Romance',
        year: 2025,
        availableOnly: true,
      })
    })
  })

  it('keeps the query when a filter changes', async () => {
    goTo('?q=Adam')
    draw()

    // The options arrive from `/library/genres`; firing before they land sets the select to
    // "" and the assertion would pass on a page that had quietly dropped the filter.
    await screen.findByRole('option', { name: /Romance/ })
    fireEvent.change(screen.getByLabelText('Género'), { target: { value: 'Romance' } })

    const [href] = navigate.mock.calls.at(-1) ?? []
    expect(String(href)).toContain('q=Adam')
    expect(String(href)).toContain('genre=Romance')
  })

  it('keeps the filters when the query changes', async () => {
    goTo('?q=Adam&genre=Romance')
    draw()

    fireEvent.change(await screen.findByLabelText('Buscar'), { target: { value: 'Cameron' } })

    await waitFor(() => {
      const [href] = navigate.mock.calls.at(-1) ?? []
      expect(String(href)).toContain('q=Cameron')
      expect(String(href)).toContain('genre=Romance')
    })
  })

  it('drops a filter set back to "todos" instead of sending an empty one', async () => {
    goTo('?q=Adam&genre=Romance')
    draw()

    await screen.findByRole('option', { name: /Romance/ })
    fireEvent.change(screen.getByLabelText('Género'), { target: { value: '' } })

    const [href] = navigate.mock.calls.at(-1) ?? []
    expect(String(href)).not.toContain('genre')
    expect(String(href)).toContain('q=Adam')
  })
})
