// @vitest-environment jsdom
/**
 * The header search, from collapsed button to somewhere-to-go.
 *
 * Real translations are loaded rather than a `t: key => key` stub, because most of what can go
 * wrong here is a sentence not reaching the screen, and the accessible names are the sentences.
 *
 * The cases below are chosen for what fails *silently*: a field that opens without focus, a
 * dropdown that offers nothing when the search finds nothing, an `aria-controls` pointing at an
 * id that is not rendered, and a stale result set hanging under an emptied box.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { setupI18n } from '@/i18n'

const navigate = vi.fn()
const useSearch = vi.fn()

vi.mock('wouter', () => ({
  useLocation: () => ['/', navigate],
}))

vi.mock('@/api/library', async () => {
  const actual = await vi.importActual<typeof import('@/api/library')>('@/api/library')
  return { ...actual, useSearch: (...args: unknown[]) => useSearch(...args) }
})

const { HeaderSearch } = await import('./HeaderSearch')

function seriesHit(index: number) {
  return {
    series: {
      id: `s${index}`,
      slug: `serie-${index}`,
      title: `Serie ${index}`,
      year: 2024,
      status: 'unknown',
      posterUrl: null,
      genres: [],
      availableEpisodeCount: 10,
      declaredEpisodeCount: null,
      watchedEpisodeCount: 0,
      progressPercent: 0,
    },
    score: 1,
    matchedTitle: `Serie ${index}`,
    matchedLanguage: 'es',
    matchedField: 'title' as const,
  }
}

function episodeHit(index: number, available = true) {
  return {
    episode: {
      id: `e${index}`,
      seasonNumber: 1,
      episodeNumber: index,
      absoluteNumber: null,
      title: `Episodio ${index}`,
      available,
      watched: false,
      runtimeSeconds: 90,
      progressPercent: 0,
    },
    seriesId: `sid${index}`,
    seriesSlug: `serie-${index}`,
    seriesTitle: `Serie ${index}`,
    score: 1,
    matchedTitle: `Episodio ${index}`,
  }
}

/** What `useSearch` answers. Defaults to "asked, and here is nothing". */
function answer(overrides: Record<string, unknown> = {}) {
  return {
    data: { query: '', series: [], episodes: [], tookMs: 1 },
    isError: false,
    isLoading: false,
    ...overrides,
  }
}

function renderOpen(): ReturnType<typeof render> {
  return renderIn(<Harness />)
}

function Harness() {
  return <HeaderSearch open onOpenChange={onOpenChange} />
}

const onOpenChange = vi.fn()

function renderIn(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  vi.clearAllMocks()
  useSearch.mockReturnValue(answer())
})

afterEach(cleanup)

describe('HeaderSearch', () => {
  it('is one named button and nothing else while it is closed', () => {
    renderIn(<HeaderSearch open={false} onOpenChange={onOpenChange} />)

    expect(screen.getByRole('button', { name: 'Buscar' })).toBeTruthy()
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('asks to be opened rather than opening itself', () => {
    renderIn(<HeaderSearch open={false} onOpenChange={onOpenChange} />)

    fireEvent.click(screen.getByRole('button', { name: 'Buscar' }))
    expect(onOpenChange).toHaveBeenCalledWith(true)
  })

  it('puts the cursor in the field, and claims no list before there is one', async () => {
    // A field that opens without focus is a dead control — you click it and then have to click
    // it again. And `aria-controls` pointing at an id nothing renders is a broken promise to a
    // screen reader, which is why it is absent rather than empty.
    renderOpen()

    const field = screen.getByRole('combobox')
    await waitFor(() => expect(document.activeElement).toBe(field))
    expect(field.getAttribute('aria-expanded')).toBe('false')
    expect(field.getAttribute('aria-controls')).toBeNull()
  })

  it('opens the list on the keystroke, not on the debounce', () => {
    // The request waits 250 ms; the box must not. A visibly open field with nothing under it
    // for a quarter of a second reads as broken, and `aria-expanded` flapping is worse.
    renderOpen()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'mi' } })

    const list = screen.getByRole('listbox', { name: 'Sugerencias' })
    expect(list).toBeTruthy()
    expect(screen.getByRole('combobox').getAttribute('aria-expanded')).toBe('true')
  })

  it('always offers a way to the full page, even with nothing to show', () => {
    // The whole reason the fall-through row exists: a dropdown that can only offer what it
    // happens to have is a dead end.
    renderOpen()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzz' } })

    const options = screen.getAllByRole('option')
    expect(options.length).toBe(1)
    expect(options[0]?.textContent).toContain('Buscar «zzz»')
  })

  it('caps what it shows, and leaves the rest to the full page', async () => {
    useSearch.mockReturnValue(
      answer({
        data: {
          query: 'mi',
          series: Array.from({ length: 30 }, (_, index) => seriesHit(index)),
          episodes: Array.from({ length: 30 }, (_, index) => episodeHit(index)),
          tookMs: 3,
        },
      }),
    )
    renderOpen()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'mi' } })

    // 5 series + 3 episodes + the fall-through.
    await waitFor(() => expect(screen.getAllByRole('option').length).toBe(9))
  })

  it('walks the list with the arrows without taking focus off the field', async () => {
    // The reason this is a combobox and not a menu: a menu moves DOM focus onto each item,
    // which would stop the user typing mid-search.
    useSearch.mockReturnValue(
      answer({ data: { query: 'mi', series: [seriesHit(1)], episodes: [], tookMs: 1 } }),
    )
    renderOpen()
    const field = screen.getByRole('combobox')
    fireEvent.change(field, { target: { value: 'mi' } })
    await waitFor(() => expect(screen.getAllByRole('option').length).toBe(2))

    fireEvent.keyDown(field, { key: 'ArrowDown' })
    const first = screen.getAllByRole('option')[0]
    expect(field.getAttribute('aria-activedescendant')).toBe(first?.getAttribute('id'))
    expect(document.activeElement).toBe(field)
  })

  it('wraps from the top of the list round to the fall-through', async () => {
    // Off-by-one here means the last row is unreachable by keyboard, and the last row is the
    // only one guaranteed to exist.
    useSearch.mockReturnValue(
      answer({ data: { query: 'mi', series: [seriesHit(1)], episodes: [], tookMs: 1 } }),
    )
    renderOpen()
    const field = screen.getByRole('combobox')
    fireEvent.change(field, { target: { value: 'mi' } })
    await waitFor(() => expect(screen.getAllByRole('option').length).toBe(2))

    fireEvent.keyDown(field, { key: 'ArrowUp' })
    const options = screen.getAllByRole('option')
    const last = options[options.length - 1]
    expect(field.getAttribute('aria-activedescendant')).toBe(last?.getAttribute('id'))
    expect(last?.textContent).toContain('Buscar «mi»')
  })

  it('sends Enter to the full page when nothing is highlighted', () => {
    renderOpen()
    const field = screen.getByRole('combobox')
    fireEvent.change(field, { target: { value: 'mi' } })
    fireEvent.submit(field.closest('form') as HTMLFormElement)

    expect(navigate).toHaveBeenCalledWith('/search?q=mi')
  })

  it('sends Enter to the highlighted series instead', async () => {
    useSearch.mockReturnValue(
      answer({ data: { query: 'mi', series: [seriesHit(7)], episodes: [], tookMs: 1 } }),
    )
    renderOpen()
    const field = screen.getByRole('combobox')
    fireEvent.change(field, { target: { value: 'mi' } })
    await waitFor(() => expect(screen.getAllByRole('option').length).toBe(2))
    fireEvent.keyDown(field, { key: 'ArrowDown' })
    fireEvent.submit(field.closest('form') as HTMLFormElement)

    expect(navigate).toHaveBeenCalledWith('/series/serie-7')
  })

  it('sends an unavailable episode to its series by slug, not by id', async () => {
    // The asymmetry `searchHits.ts` exists to fix: the route is `/series/:slug` and resolves an
    // id too, so an id would work by accident and put a hex string in the address bar.
    useSearch.mockReturnValue(
      answer({ data: { query: 'mi', series: [], episodes: [episodeHit(4, false)], tookMs: 1 } }),
    )
    renderOpen()
    const field = screen.getByRole('combobox')
    fireEvent.change(field, { target: { value: 'mi' } })
    await waitFor(() => expect(screen.getAllByRole('option').length).toBe(2))
    fireEvent.keyDown(field, { key: 'ArrowDown' })
    fireEvent.submit(field.closest('form') as HTMLFormElement)

    expect(navigate).toHaveBeenCalledWith('/series/serie-4')
  })

  it('drops the previous hits the moment the query stops matching them', async () => {
    // `useSearch` keeps the last page's data while the next loads, which is right for a results
    // page and wrong here: deleting back to one character would leave yesterday's hits hanging
    // under an emptied box.
    useSearch.mockReturnValue(
      answer({ data: { query: 'mi', series: [seriesHit(1)], episodes: [], tookMs: 1 } }),
    )
    renderOpen()
    const field = screen.getByRole('combobox')
    fireEvent.change(field, { target: { value: 'mi' } })
    await waitFor(() => expect(screen.getAllByRole('option').length).toBe(2))

    fireEvent.change(field, { target: { value: 'm' } })
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(field.getAttribute('aria-expanded')).toBe('false')
  })

  it('keeps the escape hatch when the request itself fails', () => {
    useSearch.mockReturnValue(answer({ isError: true, data: undefined }))
    renderOpen()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'mi' } })

    const list = screen.getByRole('listbox', { name: 'Sugerencias' })
    expect(list.textContent).toContain('No pudimos cargar las sugerencias.')
    expect(screen.getAllByRole('option').length).toBe(1)
  })

  it('closes on Escape without letting the keypress reach the panel behind it', () => {
    // Without `stopPropagation` the same Escape closes the mobile disclosure panel too, and the
    // user sees two things vanish for one keypress — reproducible only below 720px.
    const onParentKeyDown = vi.fn()
    renderIn(
      <div onKeyDown={onParentKeyDown}>
        <HeaderSearch open onOpenChange={onOpenChange} />
      </div>,
    )

    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })

    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(onParentKeyDown).not.toHaveBeenCalled()
  })
})
