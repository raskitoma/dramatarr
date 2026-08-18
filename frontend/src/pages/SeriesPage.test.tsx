// @vitest-environment jsdom
/**
 * The series hero, after the synopsis moved into it and the words came off the buttons.
 *
 * Two things here are worth a test rather than a screenshot.
 *
 * **The "info" button appears only when the clamp is actually hiding something.** The clamp is
 * CSS, so whether a synopsis is "too long" depends on the rendered width — the same paragraph
 * fits in four lines on a desktop and spills on a phone. Deciding by character count would put
 * a button on a paragraph that is fully visible, opening a dialog to show text already on
 * screen. jsdom does no layout, so the two heights are stubbed here; that is the only way to
 * exercise both sides of the decision at all.
 *
 * **Four glyph-only controls still have to be four named controls.** Removing a label and
 * hiding a label look identical in a screenshot and are opposite outcomes for anyone using a
 * screen reader. The admin pair is asserted the same way, and asserted absent for a viewer —
 * that gating is enforced in the route and in the API too, but this is the layer that decides
 * whether a member is shown a button they cannot use.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SeriesDetail } from '@/api/types'
import { ToastProvider } from '@/components/Toast'
import { setupI18n } from '@/i18n'

// Trimmed because testing-library normalizes whitespace before matching, so a trailing space
// here would never match the text it finds in the DOM.
const LONG = 'Después de la muerte de su padre, Lana queda muda. '.repeat(12).trim()

let isAdmin = true
const seriesDetail = vi.fn()

vi.mock('wouter', () => ({
  Link: ({ children, href, ...rest }: { children: ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
  useLocation: () => ['/series/heiress', vi.fn()],
  useParams: () => ({ slug: 'heiress' }),
}))

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'u1' }, isAdmin, isLoading: false, patchUser: vi.fn() }),
}))

vi.mock('@/api/library', () => ({
  useSeriesDetail: () => seriesDetail(),
  useRefreshSeriesMetadata: () => ({ mutate: vi.fn(), isPending: false }),
  useSetSeriesWatched: () => ({ mutate: vi.fn(), isPending: false }),
}))

const { SeriesPage } = await import('./SeriesPage')

function detail(overrides: Partial<SeriesDetail> = {}): SeriesDetail {
  return {
    id: 's1',
    slug: 'heiress',
    title: 'The Return of the Lost Heiress',
    year: 2024,
    status: 'unknown',
    posterUrl: null,
    genres: [],
    availableEpisodeCount: 90,
    declaredEpisodeCount: null,
    watchedEpisodeCount: 0,
    progressPercent: 0,
    overview: 'Corta.',
    originalTitle: null,
    originalLanguage: null,
    backdropUrl: null,
    // Both ends of the catalogue by default, so a test that says nothing about the arrows is
    // exercising the disabled pair rather than an accidental one.
    previousSeriesSlug: null,
    previousSeriesTitle: null,
    nextSeriesSlug: null,
    nextSeriesTitle: null,
    network: 'ReelShort',
    contentRating: null,
    tmdbId: null,
    imdbId: null,
    metadataLocked: false,
    libraryPath: 'The Return of the Lost Heiress (2024)',
    addedAt: '2026-01-01T00:00:00Z',
    titles: [],
    seasons: [],
    nextEpisode: null,
    ...overrides,
  }
}

/**
 * Stand in for layout, which jsdom has none of: both heights read 0, so nothing ever looks
 * clipped. `clipped` makes the paragraph report more content than box.
 */
function stubHeights({ clipped }: { clipped: boolean }) {
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get: () => 80,
  })
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    configurable: true,
    get: () => (clipped ? 240 : 80),
  })
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

beforeAll(async () => {
  await setupI18n('es')
  // jsdom implements `<dialog>` as an element but not its modal behaviour, so the real
  // component's `showModal()` throws. Stubbed to the part the assertions care about: the
  // children are in the document and the element reports itself open.
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true
  }
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false
  }
})

beforeEach(() => {
  vi.clearAllMocks()
  isAdmin = true
  stubHeights({ clipped: false })
  seriesDetail.mockReturnValue({ data: detail(), isLoading: false, isError: false })
})

afterEach(cleanup)

describe('SeriesPage hero', () => {
  it('shows the synopsis in the hero, with no way to open a dialog on a short one', () => {
    renderIn(<SeriesPage />)

    expect(screen.getByText('Corta.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /sinopsis completa/i })).toBeNull()
  })

  it('offers the dialog when the clamp is hiding text, and shows all of it', () => {
    stubHeights({ clipped: true })
    seriesDetail.mockReturnValue({
      data: detail({ overview: LONG }),
      isLoading: false,
      isError: false,
    })

    renderIn(<SeriesPage />)

    const more = screen.getByRole('button', { name: /sinopsis completa/i })
    fireEvent.click(more)

    // The dialog holds the whole paragraph — the clamped copy in the hero is the same string,
    // so finding two of them is what proves nothing was truncated on the way in.
    expect(screen.getAllByText(LONG).length).toBe(2)
  })

  it('keeps every glyph-only action named, with no words on screen', () => {
    renderIn(<SeriesPage />)

    const controls = [
      screen.getByRole('button', { name: /marcar serie como vista/i }),
      screen.getByRole('link', { name: /editar/i }),
      screen.getByRole('button', { name: /actualizar metadata/i }),
    ]
    for (const control of controls) {
      // The visually-hidden recipe keeps the text in the DOM, which is where the accessible
      // name comes from. `display: none` would have passed a screenshot and failed this.
      expect(control.textContent?.trim()).not.toBe('')
    }
  })

  it('shows a member no admin controls at all', () => {
    isAdmin = false

    renderIn(<SeriesPage />)

    expect(screen.queryByRole('button', { name: /actualizar metadata/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /editar/i })).toBeNull()
    // Their own actions are untouched.
    expect(screen.getByRole('button', { name: /marcar serie como vista/i })).toBeTruthy()
  })
})

describe('SeriesPage — moving between titles', () => {
  it('links each arrow at the neighbour the payload named, and names it out loud', async () => {
    seriesDetail.mockReturnValue({
      data: detail({
        previousSeriesSlug: 'american-sniper',
        previousSeriesTitle: 'American Sniper The Kid of Guns',
        nextSeriesSlug: 'djinn-under-contract',
        nextSeriesTitle: 'Djinn Under Contract',
      }),
      isLoading: false,
      isError: false,
    })

    renderIn(<SeriesPage />)

    const previous = screen.getByRole('link', { name: /título anterior: American Sniper/i })
    const next = screen.getByRole('link', { name: /título siguiente: Djinn Under Contract/i })
    expect(previous.getAttribute('href')).toBe('/series/american-sniper')
    expect(next.getAttribute('href')).toBe('/series/djinn-under-contract')
  })

  it('keeps both arrows in place at the ends, saying which end rather than vanishing', async () => {
    // The pair must not change width between the first title and the second: a control that
    // appears and disappears moves under the cursor of somebody clicking through the library.
    seriesDetail.mockReturnValue({
      data: detail({
        previousSeriesSlug: null,
        previousSeriesTitle: null,
        nextSeriesSlug: 'djinn-under-contract',
        nextSeriesTitle: 'Djinn Under Contract',
      }),
      isLoading: false,
      isError: false,
    })

    const { container } = renderIn(<SeriesPage />)

    const nav = container.querySelector('nav[aria-label="Navegar entre títulos"]')
    expect(nav, 'the pair should be present').toBeTruthy()
    expect(nav?.children.length, 'both arrows render at an end').toBe(2)

    // The dead end is inert and still explains itself.
    expect(screen.queryByRole('link', { name: /título anterior/i })).toBeNull()
    expect(screen.getByText(/es el primer título de la biblioteca/i)).toBeTruthy()
    expect(screen.getByRole('link', { name: /título siguiente/i })).toBeTruthy()
  })

  it('shows a lone title both ends disabled rather than a pair that goes nowhere', async () => {
    seriesDetail.mockReturnValue({ data: detail(), isLoading: false, isError: false })
    renderIn(<SeriesPage />)

    expect(screen.queryByRole('link', { name: /título anterior/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /título siguiente/i })).toBeNull()
    expect(screen.getByText(/es el primer título/i)).toBeTruthy()
    expect(screen.getByText(/es el último título/i)).toBeTruthy()
  })
})

describe('SeriesPage — watching it somewhere else', () => {
  const links = [
    { studioKey: 'reelshort', studioName: 'ReelShort', url: 'https://www.reelshort.com/movie/a' },
    { studioKey: 'dramabox', studioName: 'DramaBox', url: 'https://www.dramabox.com/drama/b' },
  ]

  it('offers one control rather than a run of wrapping text links', () => {
    seriesDetail.mockReturnValue({ data: detail({ links }), isLoading: false, isError: false })
    renderIn(<SeriesPage />)

    // Closed, the platforms are not on screen at all — that is what stopped the action row
    // wrapping mid-phrase.
    expect(screen.queryByRole('link', { name: /ReelShort/i })).toBeNull()
    const trigger = screen.getByRole('button', { name: /plataforma original/i })
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })

  it('opens every platform it has, each one leaving the app', () => {
    seriesDetail.mockReturnValue({ data: detail({ links }), isLoading: false, isError: false })
    renderIn(<SeriesPage />)

    fireEvent.click(screen.getByRole('button', { name: /plataforma original/i }))

    const reelshort = screen.getByRole('link', { name: /ReelShort/i })
    expect(reelshort.getAttribute('href')).toBe('https://www.reelshort.com/movie/a')
    // `noopener` is not decoration: `target="_blank"` hands the opened page a handle back to
    // this one without it.
    expect(reelshort.getAttribute('rel')).toContain('noopener')
    expect(screen.getByRole('link', { name: /DramaBox/i })).toBeTruthy()
  })

  it('closes on Escape and gives focus back to the control that opened it', () => {
    seriesDetail.mockReturnValue({ data: detail({ links }), isLoading: false, isError: false })
    renderIn(<SeriesPage />)

    const trigger = screen.getByRole('button', { name: /plataforma original/i })
    fireEvent.click(trigger)
    expect(screen.getByRole('link', { name: /ReelShort/i })).toBeTruthy()

    fireEvent.keyDown(trigger, { key: 'Escape' })

    expect(screen.queryByRole('link', { name: /ReelShort/i })).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('shows nothing at all when the indexer knows of no platform', () => {
    // Not an empty panel behind a button that opens onto nothing.
    seriesDetail.mockReturnValue({ data: detail({ links: [] }), isLoading: false, isError: false })
    renderIn(<SeriesPage />)

    expect(screen.queryByRole('button', { name: /plataforma original/i })).toBeNull()
  })
})
