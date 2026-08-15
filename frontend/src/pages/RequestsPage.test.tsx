// @vitest-environment jsdom
/**
 * What the request screen promises, and the three answers it must not garble.
 *
 * The screen's contract with a member is that asking is *only* asking. So the first test is
 * about a sentence: the page has to say, in words, that an admin resolves this by hand. That
 * assertion is why the real translations are loaded here instead of a `t: key => key` stub —
 * stubbing would turn a claim about what the page says into a claim about a lookup table.
 *
 * The other three cover the branches whose failure modes all look like "the button is broken":
 *
 * * A card whose show is **already in the library** must become a link to it rather than an
 *   ask button. Getting this wrong sends a member to wait a week for something they could
 *   have watched immediately.
 * * A card this member has **already asked for** shows its status, not a second button. The
 *   server is idempotent so a double ask is harmless, but a button that appears to do nothing
 *   reads as breakage.
 * * When search finds nothing, the **ask-by-name** path has to be there. It is the only path
 *   that works on a household with no providers configured, and it is the one most easily
 *   lost in a refactor because nothing else depends on it.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RequestCandidate, RequestSearchResults, SeriesRequest } from '@/api/requests'
import { ToastProvider } from '@/components/Toast'
import { setupI18n } from '@/i18n'

const get = vi.fn()
const post = vi.fn()
const del = vi.fn()

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return {
    ...actual,
    api: {
      get: (path: string, query?: unknown) => get(path, query),
      post: (path: string, body?: unknown) => post(path, body),
      delete: (path: string) => del(path),
      put: vi.fn(),
      patch: vi.fn(),
      upload: vi.fn(),
    },
  }
})

const { RequestsPage } = await import('./RequestsPage')

function candidate(overrides: Partial<RequestCandidate> = {}): RequestCandidate {
  return {
    key: 'tmdb:tv:1396',
    title: 'Ruta de Cristal',
    year: 2024,
    posterUrl: null,
    sourceUrl: 'https://www.themoviedb.org/tv/1396',
    provider: 'themoviedb.org',
    snippet: 'Un drama corto',
    ids: { tmdbId: 1396, tmdbKind: 'tv' },
    inLibrary: false,
    inLibrarySlug: null,
    requested: false,
    requestStatus: null,
    ...overrides,
  }
}

function results(rows: RequestCandidate[]): RequestSearchResults {
  return { query: 'ruta', providers: ['tmdb'], configured: true, results: rows }
}

const MY_REQUEST: SeriesRequest = {
  id: 'r1',
  title: 'Otra Serie',
  year: null,
  posterUrl: null,
  sourceUrl: null,
  provider: '',
  ids: {},
  note: null,
  status: 'pending',
  adminNote: null,
  series: null,
  createdAt: '2026-08-01T10:00:00Z',
  updatedAt: '2026-08-01T10:00:00Z',
  resolvedAt: null,
  alreadyExisted: false,
}

/** Routes the GETs the page makes to whatever this test wants them to answer. */
function serve(
  search: RequestSearchResults,
  mine: SeriesRequest[] = [],
  poster: string | null = null,
): void {
  get.mockImplementation((path: string) => {
    if (path === '/requests/search') return Promise.resolve(search)
    if (path === '/requests') return Promise.resolve(mine)
    // Every card without a poster in the search response asks for one. Answered explicitly
    // rather than left to the reject branch, so a test failing here means the page asked for
    // something unexpected rather than that a thumbnail happened to error out.
    if (path === '/requests/poster') return Promise.resolve({ url: poster })
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
}

function renderPage(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>{ui}</ToastProvider>
    </QueryClientProvider>,
  )
}

/**
 * The card's own heading.
 *
 * A bare `getByText` matches twice: `Poster` repeats the title in a visually-hidden span so
 * the artwork still has an accessible name when there is no image, which is correct and is
 * exactly why these queries go through the heading role instead.
 */
async function cardHeading(title: string): Promise<HTMLElement> {
  return waitFor(() => screen.getByRole('heading', { name: title, level: 3 }))
}

/** Types into the search box and lets the page's debounce elapse. */
async function search(text: string): Promise<void> {
  const box = screen.getByRole('searchbox')
  fireEvent.change(box, { target: { value: text } })
  await vi.advanceTimersByTimeAsync(400)
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ shouldAdvanceTime: true })
  serve(results([]))
  post.mockResolvedValue({ ...MY_REQUEST, id: 'new' })
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

describe('RequestsPage', () => {
  it('says in words that an admin resolves this by hand and nothing is downloaded', async () => {
    renderPage(<RequestsPage />)
    // The promise the whole feature rests on. If this sentence goes, the screen starts
    // looking like a download button to the person using it.
    expect(screen.getByText(/no se descarga nada/i)).toBeTruthy()
    expect(screen.getByText(/a mano/i)).toBeTruthy()
  })

  it('offers a link to the series instead of an ask button when the library has it', async () => {
    serve(results([candidate({ inLibrary: true, inLibrarySlug: 'ruta-de-cristal' })]))
    renderPage(<RequestsPage />)
    await search('ruta')

    await cardHeading('Ruta de Cristal')

    const link = screen.getByRole('link', { name: /ya la tenemos/i })
    expect(link.getAttribute('href')).toBe('/series/ruta-de-cristal')
    expect(screen.queryByRole('button', { name: /^pedir$/i })).toBeNull()
    expect(post).not.toHaveBeenCalled()
  })

  it('shows the status of something this member already asked for', async () => {
    serve(results([candidate({ requested: true, requestStatus: 'approved' })]))
    renderPage(<RequestsPage />)
    await search('ruta')

    await cardHeading('Ruta de Cristal')
    expect(screen.getByText('En curso')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^pedir$/i })).toBeNull()
  })

  it('illustrates a card the search could not, and asks with that poster', async () => {
    // The bug this fixes: TMDB has no entry for most of this library, so the search response
    // carries no image and every card rendered blank.
    serve(results([candidate()]), [], '/api/stream/images/requests/abc.webp')
    renderPage(<RequestsPage />)
    await search('ruta')

    await cardHeading('Ruta de Cristal')
    await waitFor(() => {
      const image = screen.getByRole('img', { name: 'Ruta de Cristal' }) as HTMLImageElement
      expect(image.src).toContain('/api/stream/images/requests/abc.webp')
    })

    fireEvent.click(screen.getByRole('button', { name: /^pedir$/i }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [, body] = post.mock.calls[0] as [string, Record<string, unknown>]
    // The poster the card actually resolved travels with the request, so the admin's queue
    // shows the same picture the member was looking at when they chose.
    expect(body.posterUrl).toBe('/api/stream/images/requests/abc.webp')
  })

  it('sends the whole card — ids and all — when one is chosen', async () => {
    serve(results([candidate()]))
    renderPage(<RequestsPage />)
    await search('ruta')

    await cardHeading('Ruta de Cristal')
    fireEvent.click(screen.getByRole('button', { name: /^pedir$/i }))

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [path, body] = post.mock.calls[0] as [string, Record<string, unknown>]
    expect(path).toBe('/requests')
    expect(body.title).toBe('Ruta de Cristal')
    expect(body.year).toBe(2024)
    // The ids are what make the admin's next step one click rather than another search.
    expect(body.ids).toEqual({ tmdbId: 1396, tmdbKind: 'tv' })
    expect(body.sourceUrl).toBe('https://www.themoviedb.org/tv/1396')
  })

  it('still lets a member ask by name when the search finds nothing', async () => {
    serve({ query: 'nada', providers: [], configured: false, results: [] })
    renderPage(<RequestsPage />)
    await search('una serie que nadie tiene')

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /pedir por título/i })).toBeTruthy(),
    )
    fireEvent.click(screen.getByRole('button', { name: /pedir por título/i }))

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [, body] = post.mock.calls[0] as [string, Record<string, unknown>]
    expect(body.title).toBe('una serie que nadie tiene')
  })

  it('shows the admin reply on a declined request rather than just a colour', async () => {
    serve(results([]), [
      { ...MY_REQUEST, status: 'declined', adminNote: 'No la encontré en ningún lado' },
    ])
    renderPage(<RequestsPage />)

    // Anchored on the status chip rather than the title: the title is deliberately rendered
    // twice (see `cardHeading`), the chip is not.
    await waitFor(() => expect(screen.getByText('Rechazado')).toBeTruthy())
    expect(screen.getAllByText('Otra Serie').length).toBeGreaterThan(0)
    // A verdict with no reason is the outcome this screen most needs to avoid.
    expect(screen.getByText(/no la encontré en ningún lado/i)).toBeTruthy()
  })
})
