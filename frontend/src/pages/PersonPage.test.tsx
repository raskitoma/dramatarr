// @vitest-environment jsdom
/**
 * The actor page, driven through the transition that broke it.
 *
 * Every actor page rendered "Algo salió mal". The cause was not the data and not the endpoint —
 * both were fine — but three hooks sitting *below* the component's early returns. The first
 * render is the loading one and returns before reaching them; the second reaches them and calls
 * three hooks more, React throws on the count changing, and the boundary catches it.
 *
 * Nothing else in this repo could have caught that. `tsc` has no opinion about hook order, and
 * the `lint` script names an `eslint` that is not in the dependencies at all — so
 * `react-hooks/rules-of-hooks`, the one rule written for exactly this, has never run here.
 *
 * So the test that matters is the one that renders **pending first and resolved after**. A test
 * that hands the component its data already loaded passes against the broken version.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '@/components/Toast'
import { setupI18n } from '@/i18n'

const dramadexPerson = vi.fn()
const post = vi.fn()

vi.mock('wouter', () => ({
  useParams: () => ({ id: '283' }),
  Link: ({ href, children }: { href: string; children: ReactNode }) => <a href={href}>{children}</a>,
}))

vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return { ...actual, dramadexPerson: (...args: unknown[]) => dramadexPerson(...args) }
})

vi.mock('@/api/client', () => ({
  ApiRequestError: class extends Error {
    code = ''
  },
  api: { post: (...args: unknown[]) => post(...args) },
}))

const { PersonPage } = await import('./PersonPage')

function credit(overrides: Record<string, unknown> = {}) {
  return {
    id: 668,
    title: 'Money, Guns, and a Merry Christmas',
    year: 2025,
    episodeCount: 60,
    posterUrl: '/api/library/dramadex/titles/668/poster',
    synopsis: null,
    studio: 'ReelShort',
    watchUrl: 'https://www.reelshort.com/movie/abc',
    genres: [],
    cast: [],
    matches: false,
    inLibrary: false,
    librarySlug: null,
    ...overrides,
  }
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
})

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(cleanup)

describe('PersonPage', () => {
  it('survives the render after loading, which is where the hooks used to change count', async () => {
    // Deliberately unresolved at first, so the component really does render its pending branch
    // before its loaded one. Resolving up front would pass against the broken version.
    let resolve!: (value: unknown) => void
    dramadexPerson.mockReturnValue(
      new Promise((r) => {
        resolve = r
      }),
    )

    renderIn(<PersonPage />)
    expect(screen.getByText(/cargando/i)).toBeTruthy()

    resolve({ id: 283, name: 'Drew Ater', bio: null, credits: [credit()] })

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Drew Ater' })).toBeTruthy())
    expect(screen.queryByText(/algo salió mal/i)).toBeNull()
  })

  it('links a credit the household already has, rather than only badging it', async () => {
    // `inLibrary` used to travel with no address behind it, so this card said "Ya la tenemos"
    // and went nowhere.
    dramadexPerson.mockResolvedValue({
      id: 283,
      name: 'Drew Ater',
      bio: null,
      credits: [credit({ inLibrary: true, librarySlug: 'money-guns-and-a-merry-christmas' })],
    })

    renderIn(<PersonPage />)

    const link = await screen.findByRole('link', { name: /Money, Guns/i })
    expect(link.getAttribute('href')).toBe('/series/money-guns-and-a-merry-christmas')
  })

  it('asks for a credit instead of navigating somewhere that ignores the query', async () => {
    // "Pedir" linked to `/requests?q=<title>`, and that page has never read `?q=` — so it landed
    // you on an empty wishlist having asked for nothing.
    dramadexPerson.mockResolvedValue({
      id: 283,
      name: 'Drew Ater',
      bio: null,
      credits: [credit()],
    })
    post.mockResolvedValue({ id: 'r1', alreadyExisted: false })

    renderIn(<PersonPage />)

    const ask = await screen.findByRole('button', { name: /pedir/i })
    ask.click()

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [path, body] = post.mock.calls[0] as [string, Record<string, unknown>]
    expect(path).toBe('/requests')
    expect(body.title).toBe('Money, Guns, and a Merry Christmas')
    expect(body.sourceUrl).toBe('https://www.reelshort.com/movie/abc')
  })
})
