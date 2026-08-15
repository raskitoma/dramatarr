// @vitest-environment jsdom
/**
 * What the web-search panel has to get right, and what it must never do.
 *
 * The row's whole value is the distinction between a hit that carries an id and one that does
 * not: the first can bind the series in a single click, the second is a lead the admin follows
 * by hand. Rendering a "Link" control on a row with no id would offer an action that cannot
 * work; hiding the id-less rows would throw away the MyDramaList page that tells the admin the
 * show's real title.
 *
 * And an unconfigured search provider is the shipped state of a fresh install, not a failure —
 * so it has to read as a sentence with the settings path attached, never as an empty list.
 *
 * Real translations are loaded rather than a `t: key => key` stub: the assertions are about
 * sentences reaching the screen.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { setupI18n } from '@/i18n'

const discoverSeries = vi.fn()
const lookupTmdb = vi.fn()
const matchSeries = vi.fn()

vi.mock('wouter', () => ({
  Link: ({ children, href }: { children: ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}))

vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    discoverSeries: (...args: unknown[]) => discoverSeries(...args),
    lookupTmdb: (...args: unknown[]) => lookupTmdb(...args),
    matchSeries: (...args: unknown[]) => matchSeries(...args),
  }
})

const { DiscoveryPanel } = await import('./DiscoveryPanel')

function result(overrides: Record<string, unknown> = {}) {
  return {
    title: 'Mi Qing (2021) — MyDramaList',
    url: 'https://mydramalist.com/12345-mi-qing',
    domain: 'mydramalist.com',
    snippet: 'Mi Qing is a 2021 Chinese web series about… Sign in · Feeds · Watchlist',
    engine: 'duckduckgo',
    tmdbId: null,
    imdbId: null,
    ...overrides,
  }
}

function discovery(overrides: Record<string, unknown> = {}) {
  return {
    query: 'Mi Qing',
    provider: 'searxng',
    configured: true,
    results: [],
    ...overrides,
  }
}

function renderIn(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

function renderPanel(onLinked = () => {}) {
  return renderIn(<DiscoveryPanel seriesId="s1" initialQuery="Mi Qing" onLinked={onLinked} />)
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  vi.clearAllMocks()
  discoverSeries.mockResolvedValue(discovery())
  lookupTmdb.mockResolvedValue([])
  matchSeries.mockResolvedValue({ id: 'j1', type: 'metadata_refresh', status: 'queued' })
})

afterEach(cleanup)

describe('DiscoveryPanel', () => {
  it('offers the one-step link only on a result that carries an id', async () => {
    discoverSeries.mockResolvedValue(
      discovery({
        results: [
          result({
            title: 'Mi Qing (TV Series 2021) — TMDB',
            url: 'https://www.themoviedb.org/tv/4242-mi-qing',
            domain: 'themoviedb.org',
            tmdbId: 4242,
          }),
          result(),
        ],
      }),
    )

    renderPanel()

    // The id is printed on the row, because it is the reason that row has a button at all.
    await waitFor(() => expect(screen.getByText('TMDB #4242')).toBeTruthy())
    expect(screen.getByRole('button', { name: /vincular usando tmdb #4242/i })).toBeTruthy()

    // Exactly one actionable row, even though both are listed.
    expect(screen.getAllByRole('button', { name: /vincular usando/i }).length).toBe(1)

    // The id-less hit is still a lead: listed, and opening in a new tab.
    const lead = screen.getByRole('link', { name: /abrir en otra pestaña/i })
    expect(lead.getAttribute('href')).toBe('https://mydramalist.com/12345-mi-qing')
    expect(lead.getAttribute('target')).toBe('_blank')
    expect(lead.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('points at the settings screen when no search provider is configured', async () => {
    discoverSeries.mockResolvedValue(discovery({ provider: '', configured: false, results: [] }))

    renderPanel()

    const note = /no hay un buscador web configurado/i
    await waitFor(() => expect(screen.getByText(note)).toBeTruthy())
    // …and names where it is switched on, rather than leaving an empty list to interpret.
    const link = screen.getByRole('link', { name: /preferencias → metadata/i })
    expect(link.getAttribute('href')).toBe('/admin/settings#settings-metadata')
  })

  it('resolves the id through the lookup and binds the series in one click', async () => {
    discoverSeries.mockResolvedValue(
      discovery({
        results: [
          result({
            url: 'https://www.imdb.com/title/tt1234567/',
            domain: 'imdb.com',
            imdbId: 'tt1234567',
          }),
        ],
      }),
    )
    lookupTmdb.mockResolvedValue([{ tmdbId: 4242, name: 'Mi Qing' }])
    const onLinked = vi.fn()

    renderPanel(onLinked)

    const link = await screen.findByRole('button', { name: /vincular usando imdb tt1234567/i })
    fireEvent.click(link)

    // The lookup is the existing one, and the match follows it without a second act.
    await waitFor(() => expect(matchSeries).toHaveBeenCalledWith('s1', 4242))
    expect(lookupTmdb).toHaveBeenCalledWith({ tmdbId: null, imdbId: 'tt1234567' })
    await waitFor(() => expect(onLinked).toHaveBeenCalled())
  })

  it('shows the snippet as a credited search excerpt, never as a synopsis', async () => {
    discoverSeries.mockResolvedValue(discovery({ results: [result()] }))

    renderPanel()

    await waitFor(() => expect(screen.getByText(/Extracto de mydramalist\.com/)).toBeTruthy())
    // Quoted, so it reads as someone else's text rather than as the series' own description.
    expect(screen.getByText(/^«.*»$/s)).toBeTruthy()
    // And the list says once, above every row, that none of it is stored.
    expect(screen.getByText(/nunca se guardan como sinopsis/i)).toBeTruthy()
  })
})
