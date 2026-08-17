// @vitest-environment jsdom
/**
 * The match dialog's three tabs, and the two contracts that are invisible until they break.
 *
 * The search language has to reach the proxy: without it a Chinese drama is unfindable by its
 * Chinese title, which is the exact case this library exists for — and the failure looks like
 * "TMDB has nothing", not like a dropped parameter.
 *
 * The unlink tab has to know what the series is bound to. Told nothing, it would claim an
 * unlinked series on a row the library table marks as linked, and the admin would be left
 * without the one control that undoes a bad match.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '@/components/Toast'
import { setupI18n } from '@/i18n'

const searchTmdb = vi.fn()
const lookupTmdb = vi.fn()
const unlinkSeries = vi.fn()
const discoverSeries = vi.fn()

vi.mock('wouter', () => ({
  Link: ({ children }: { children: ReactNode }) => <a href="/admin/settings">{children}</a>,
}))

vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    searchTmdb: (...args: unknown[]) => searchTmdb(...args),
    lookupTmdb: (...args: unknown[]) => lookupTmdb(...args),
    unlinkSeries: (...args: unknown[]) => unlinkSeries(...args),
    discoverSeries: (...args: unknown[]) => discoverSeries(...args),
    matchSeries: vi.fn(),
  }
})

const { TmdbMatchDialog } = await import('./TmdbMatchDialog')

function renderIn(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>{ui}</ToastProvider>
    </QueryClientProvider>,
  )
}

beforeAll(async () => {
  // jsdom ships <dialog> without the modal methods the Dialog component relies on.
  if (typeof HTMLDialogElement.prototype.showModal !== 'function') {
    HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
      this.setAttribute('open', '')
    }
    HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
      this.removeAttribute('open')
      this.dispatchEvent(new Event('close'))
    }
  }
  await setupI18n('es')
})

beforeEach(() => {
  vi.clearAllMocks()
  searchTmdb.mockResolvedValue([])
  lookupTmdb.mockResolvedValue([])
  discoverSeries.mockResolvedValue({
    query: '',
    provider: 'searxng',
    configured: true,
    results: [],
  })
})

afterEach(cleanup)

describe('TmdbMatchDialog', () => {
  it('sends the chosen search language to the proxy', async () => {
    renderIn(<TmdbMatchDialog open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />)

    fireEvent.change(document.getElementById('tmdb-query') as HTMLInputElement, {
      target: { value: '迷情' },
    })
    fireEvent.change(document.getElementById('tmdb-language') as HTMLSelectElement, {
      target: { value: 'zh' },
    })
    fireEvent.click(screen.getAllByRole('button', { name: /^buscar$/i })[0] as HTMLButtonElement)

    await waitFor(() => expect(searchTmdb).toHaveBeenCalled())
    expect(searchTmdb.mock.calls[0]).toEqual(['迷情', null, 'zh'])
  })

  it('shows the vote count and the original title on a result', async () => {
    searchTmdb.mockResolvedValue([
      {
        tmdbId: 4242,
        name: 'Mi Qing',
        originalName: '迷情',
        overview: 'Una historia.',
        firstAirDate: '2021-03-01',
        year: 2021,
        posterUrl: null,
        voteCount: 1234,
        originalLanguage: 'zh',
      },
    ])

    renderIn(
      <TmdbMatchDialog
        open
        onClose={() => {}}
        seriesId="s1"
        seriesTitle="Mi Qing"
        initialQuery="Mi Qing"
      />,
    )

    // The vote count is what separates the real show from the fan entry with the same title.
    await waitFor(() => expect(screen.getByText('1234 votos en TMDB')).toBeTruthy())
    expect(screen.getByText(/迷情/)).toBeTruthy()
  })

  it('looks a series up by IMDb id and refuses an id in the wrong shape', async () => {
    renderIn(<TmdbMatchDialog open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />)

    fireEvent.click(screen.getByRole('tab', { name: /por id/i }))
    fireEvent.click(screen.getByRole('button', { name: /buscar por id/i }))
    expect(screen.getByText(/uno de los dos ID/i)).toBeTruthy()

    fireEvent.change(document.getElementById('imdb-id') as HTMLInputElement, {
      target: { value: 'nope' },
    })
    fireEvent.click(screen.getByRole('button', { name: /buscar por id/i }))
    expect(screen.getByText(/no tiene el formato esperado/i)).toBeTruthy()
    expect(lookupTmdb).not.toHaveBeenCalled()

    fireEvent.change(document.getElementById('imdb-id') as HTMLInputElement, {
      target: { value: 'tt1234567' },
    })
    fireEvent.click(screen.getByRole('button', { name: /buscar por id/i }))

    await waitFor(() => expect(lookupTmdb).toHaveBeenCalled())
    expect(lookupTmdb.mock.calls[0]?.[0]).toEqual({ tmdbId: null, imdbId: 'tt1234567' })
    // A real-looking id TMDB does not know is a sentence, not a red box.
    await waitFor(() => expect(screen.getByText(/no tiene nada con ese id/i)).toBeTruthy())
  })

  it('searches the web from the series title without being asked twice', async () => {
    renderIn(
      <TmdbMatchDialog
        open
        onClose={() => {}}
        seriesId="s1"
        seriesTitle="Mi Qing"
        initialQuery="Mi Qing"
      />,
    )

    fireEvent.click(screen.getByRole('tab', { name: /en la web/i }))

    // The admin reached this tab because the title search already failed — running it on arrival
    // is what makes finding a hard title one action rather than three. The third argument is
    // the platform-scoped mode, which defaults on for exactly the same reason.
    await waitFor(() => expect(discoverSeries).toHaveBeenCalledWith('s1', 'Mi Qing', true))
    expect((document.getElementById('discover-query') as HTMLInputElement).value).toBe('Mi Qing')
  })

  it('offers the unlink only when the series is actually linked', async () => {
    const { unmount } = renderIn(
      <TmdbMatchDialog open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />,
    )
    fireEvent.click(screen.getByRole('tab', { name: /desvincular/i }))
    expect(screen.getByText(/no está vinculada/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /desvincular de tmdb/i })).toBeNull()
    unmount()

    renderIn(
      <TmdbMatchDialog
        open
        onClose={() => {}}
        seriesId="s1"
        seriesTitle="Mi Qing"
        currentTmdbId={4242}
      />,
    )
    fireEvent.click(screen.getByRole('tab', { name: /desvincular/i }))
    expect(screen.getByText('4242')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /desvincular de tmdb/i }))
    await waitFor(() => expect(unlinkSeries).toHaveBeenCalledWith('s1'))
  })
})
