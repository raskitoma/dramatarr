// @vitest-environment jsdom
/**
 * The two ways this panel writes to a series, and why they are not the same button.
 *
 * The ordinary "use this metadata" fills **blanks only**. That default is load-bearing rather
 * than cautious: this household writes its synopses in Spanish, OMDb's plot is English, and an
 * admin who wrote a paragraph and then came back for the year must not pay for the year with
 * the paragraph. So overwriting is a separate, deliberate action — and because it destroys text
 * a person wrote, it costs a confirmation, and the confirmation names what is about to go.
 *
 * The other half is routing. Replace goes **straight to OMDb** and never tries TMDB first: a
 * TMDB match has no overwrite flag, so routing there would silently do something other than
 * what the button says. That is the assertion that would catch a "simplification" merging the
 * two mutations back together.
 *
 * Real translations, like the sibling panel's tests: the assertions are about sentences reaching
 * the screen, and a confirmation nobody can read is not a confirmation.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '@/components/Toast'
import { setupI18n } from '@/i18n'

const discoverSeries = vi.fn()
const lookupTmdb = vi.fn()
const matchSeries = vi.fn()
const applyImdbMetadata = vi.fn()

vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    discoverSeries: (...args: unknown[]) => discoverSeries(...args),
    lookupTmdb: (...args: unknown[]) => lookupTmdb(...args),
    matchSeries: (...args: unknown[]) => matchSeries(...args),
    applyImdbMetadata: (...args: unknown[]) => applyImdbMetadata(...args),
  }
})

const { SynopsisFinder } = await import('./SynopsisFinder')

function result(overrides: Record<string, unknown> = {}) {
  return {
    title: 'Definitely Not the Dragon God — IMDb',
    url: 'https://www.imdb.com/title/tt43685491/',
    domain: 'imdb.com',
    snippet: 'A short drama about a reluctant god.',
    engine: 'naver',
    tmdbId: null,
    imdbId: 'tt43685491',
    network: null,
    ...overrides,
  }
}

function discovery(overrides: Record<string, unknown> = {}) {
  return {
    query: 'Definitely Not the Dragon God',
    provider: 'searxng',
    configured: true,
    narrowed: true,
    results: [result()],
    unresponsiveEngines: [],
    ...overrides,
  }
}

function renderFinder(
  onApplied = () => {},
  onNetwork: ((network: string) => void) | undefined = () => {},
): ReactElement {
  return (
    <SynopsisFinder
      seriesId="s1"
      seriesTitle="Definitely Not the Dragon God"
      onApplied={onApplied}
      onNetwork={onNetwork}
    />
  )
}

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

/** The panel is closed until asked: opening it reaches the household's SearXNG. */
async function open(
  onApplied = () => {},
  onNetwork: ((network: string) => void) | undefined = () => {},
) {
  renderIn(renderFinder(onApplied, onNetwork))
  fireEvent.click(screen.getByRole('button', { name: /buscar la ficha/i }))
  await waitFor(() => expect(discoverSeries).toHaveBeenCalled())
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  vi.clearAllMocks()
  discoverSeries.mockResolvedValue(discovery())
  lookupTmdb.mockResolvedValue([])
  matchSeries.mockResolvedValue({ id: 'j1', type: 'metadata_refresh', status: 'queued' })
  applyImdbMetadata.mockResolvedValue({ id: 's1', title: 'Definitely Not the Dragon God' })
})

afterEach(cleanup)

describe('SynopsisFinder', () => {
  it('fills blanks only, without being asked to overwrite', async () => {
    await open()

    fireEvent.click(await screen.findByRole('button', { name: /usar esta metadata/i }))

    // TMDB first — it carries episodes and artwork — then IMDb when TMDB has never heard of it,
    // which for this library is the ordinary case rather than the exception.
    await waitFor(() => expect(applyImdbMetadata).toHaveBeenCalled())
    expect(lookupTmdb).toHaveBeenCalledWith({ tmdbId: null, imdbId: 'tt43685491' })
    // The third argument is the overwrite flag, and this button must never send it.
    expect(applyImdbMetadata).toHaveBeenCalledWith('s1', 'tt43685491')
  })

  it('will not replace on a single click, and says what would be lost', async () => {
    await open()

    fireEvent.click(await screen.findByRole('button', { name: /reemplazar lo que ya hay/i }))

    // Arming writes nothing on its own.
    expect(applyImdbMetadata).not.toHaveBeenCalled()
    // And the warning names the fields, including that the synopsis arrives in English —
    // "replace" alone does not tell a Spanish-writing admin what they are trading.
    expect(screen.getByText(/pisa el título, el año, la sinopsis/i)).toBeTruthy()
    expect(screen.getByText(/en inglés/i)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /^cancelar$/i }))
    expect(applyImdbMetadata).not.toHaveBeenCalled()
    expect(screen.queryByText(/pisa el título/i)).toBeNull()
  })

  it('replaces through OMDb only, never through a TMDB match', async () => {
    // A TMDB row exists for it — the replace path must still ignore that, because binding to
    // TMDB has no overwrite and would quietly do something other than what the button says.
    lookupTmdb.mockResolvedValue([{ tmdbId: 4242, name: 'Definitely Not the Dragon God' }])
    const onApplied = vi.fn()
    await open(onApplied)

    fireEvent.click(await screen.findByRole('button', { name: /reemplazar lo que ya hay/i }))
    fireEvent.click(screen.getByRole('button', { name: /sí, reemplazar/i }))

    await waitFor(() =>
      expect(applyImdbMetadata).toHaveBeenCalledWith('s1', 'tt43685491', true),
    )
    expect(lookupTmdb).not.toHaveBeenCalled()
    expect(matchSeries).not.toHaveBeenCalled()
    await waitFor(() => expect(onApplied).toHaveBeenCalled())
    // The confirmation clears itself, so the row cannot be fired twice by a stray second click.
    await waitFor(() => expect(screen.queryByText(/pisa el título/i)).toBeNull())
  })

  it('offers replacing only where an IMDb record backs it', async () => {
    discoverSeries.mockResolvedValue(
      discovery({
        results: [
          result({
            title: 'Definitely Not the Dragon God — TMDB',
            url: 'https://www.themoviedb.org/tv/4242',
            domain: 'themoviedb.org',
            tmdbId: 4242,
            imdbId: null,
          }),
          result({
            title: 'Definitely Not the Dragon God — DramaBox',
            url: 'https://www.dramabox.com/drama/999',
            domain: 'dramabox.com',
            imdbId: null,
          }),
        ],
      }),
    )

    await open()

    // A TMDB row can be applied, but not replaced: there is no overwrite on that path.
    await waitFor(() =>
      expect(screen.getAllByRole('button', { name: /usar esta metadata/i }).length).toBe(1),
    )
    expect(screen.queryByRole('button', { name: /reemplazar lo que ya hay/i })).toBeNull()
    // The platform row stays a plain link — behind it there is a snippet, not a record.
    expect(screen.getByRole('link', { name: /DramaBox/i })).toBeTruthy()
  })

  it('hands the platform back to the form instead of writing it', async () => {
    discoverSeries.mockResolvedValue(
      discovery({
        results: [
          result({
            title: 'Dragon God — DramaBox',
            url: 'https://www.dramabox.com/drama/999',
            domain: 'dramabox.com',
            imdbId: null,
            network: 'DramaBox',
          }),
        ],
      }),
    )
    const onNetwork = vi.fn()
    await open(() => {}, onNetwork)

    fireEvent.click(await screen.findByRole('button', { name: /poner cadena: DramaBox/i }))

    // Into the form. Nothing was saved, so an edit in progress on the same page survives.
    expect(onNetwork).toHaveBeenCalledWith('DramaBox')
    expect(applyImdbMetadata).not.toHaveBeenCalled()
    expect(matchSeries).not.toHaveBeenCalled()
  })

  it('gives a row with neither an id nor a platform nothing to press', async () => {
    discoverSeries.mockResolvedValue(
      discovery({
        results: [
          result({
            title: 'Some blog post',
            url: 'https://example.com/post',
            domain: 'example.com',
            imdbId: null,
            network: null,
          }),
        ],
      }),
    )

    await open()

    await waitFor(() => expect(screen.getByRole('link', { name: /Some blog post/i })).toBeTruthy())
    // A wrong network is worse than an empty one, so an unrecognized domain offers nothing.
    expect(screen.queryAllByRole('button', { name: /poner cadena/i })).toEqual([])
    expect(screen.queryAllByRole('button', { name: /usar esta metadata/i })).toEqual([])
  })
})
