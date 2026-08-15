// @vitest-environment jsdom
/**
 * The two things the poster picker must not get wrong.
 *
 * An unconfigured web image search is not a failure — it is the shipped state — so it has to
 * read as a sentence with a way to fix it, never as an empty grid the admin is left to
 * interpret and never as a red error box for something nobody broke.
 *
 * And a pasted URL is checked here rather than by the browser: a `type="url"` input would let
 * native constraint validation block the submit and answer in its own words, so the field would
 * have two different error languages depending on which check happened to fire.
 *
 * Real translations are loaded rather than a `t: key => key` stub — the assertions are about
 * sentences reaching the screen, not about keys being looked up.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '@/components/Toast'
import { setupI18n } from '@/i18n'

const listArtwork = vi.fn()
const selectArtwork = vi.fn()

vi.mock('wouter', () => ({
  Link: ({ children }: { children: ReactNode }) => <a href="/admin/settings">{children}</a>,
}))

vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    listArtwork: (...args: unknown[]) => listArtwork(...args),
    selectArtwork: (...args: unknown[]) => selectArtwork(...args),
    uploadArtwork: vi.fn(),
  }
})

const { ArtworkPicker } = await import('./ArtworkPicker')

type Reason = 'NOT_LINKED' | 'NOT_CONFIGURED' | 'PROVIDER_ERROR'

function candidate(overrides: Record<string, unknown> = {}) {
  return {
    source: 'tmdb',
    kind: 'poster',
    url: 'https://img.example/a.jpg',
    thumbnailUrl: 'https://img.example/a-thumb.jpg',
    width: 1000,
    height: 1500,
    language: 'ko',
    voteAverage: 5.42,
    voteCount: 12,
    title: '',
    contextUrl: null,
    ...overrides,
  }
}

/**
 * An `ArtworkCandidates` payload. `tmdb` / `web` take either the candidates that source
 * returned or the reason it returned none — which is exactly the shape of the decision the
 * picker has to make about each section.
 */
function options(input: {
  posters?: ReturnType<typeof candidate>[]
  backdrops?: ReturnType<typeof candidate>[]
  webResults?: ReturnType<typeof candidate>[]
  tmdb?: Reason
  web?: Reason
}) {
  const posters = input.posters ?? []
  const backdrops = input.backdrops ?? []
  const web = input.webResults ?? []
  return {
    seriesId: 's1',
    tmdbId: 4242,
    posters,
    backdrops,
    web,
    sources: [
      input.tmdb
        ? { name: 'tmdb', provider: 'tmdb', available: false, reason: input.tmdb, count: 0 }
        : {
            name: 'tmdb',
            provider: 'tmdb',
            available: true,
            reason: '',
            count: posters.length + backdrops.length,
          },
      input.web
        ? { name: 'web', provider: '', available: false, reason: input.web, count: 0 }
        : { name: 'web', provider: 'google_cse', available: true, reason: '', count: web.length },
    ],
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
})

afterEach(cleanup)

describe('ArtworkPicker', () => {
  it('shows dimensions, rating and language on each candidate', async () => {
    listArtwork.mockResolvedValue(
      options({
        posters: [
          candidate({ url: 'https://img.example/a.jpg', width: 2000, height: 3000 }),
        ],
      }),
    )

    renderIn(<ArtworkPicker open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />)

    // The whole basis for choosing between two thumbnails that look identical at this size.
    await waitFor(() => expect(screen.getByText('2000×3000')).toBeTruthy())
    expect(screen.getByText('★ 5.4')).toBeTruthy()
    // Once in the language filter, once on the card itself.
    expect(screen.getAllByText('coreano').length).toBe(2)
    // Choosing artwork locks the series' metadata, and that is said before any choice is made.
    expect(screen.getByText(/queda bloqueada/i)).toBeTruthy()
  })

  it('answers an unconfigured web search with a note, not an empty grid', async () => {
    listArtwork.mockResolvedValue(options({ web: 'NOT_CONFIGURED' }))
    renderIn(<ArtworkPicker open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />)

    await waitFor(() => expect(screen.getByText(/no está configurada/i)).toBeTruthy())
    // …and it points at the screen that fixes it.
    expect(screen.getByRole('link', { name: /preferencias/i })).toBeTruthy()
  })

  it('previews a pasted URL before committing it, and refuses one that is not a URL', async () => {
    listArtwork.mockResolvedValue(options({}))
    renderIn(<ArtworkPicker open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />)
    await waitFor(() => expect(document.getElementById('artwork-url')).toBeTruthy())

    const input = document.getElementById('artwork-url') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'not a url' } })
    fireEvent.click(screen.getByRole('button', { name: /ver antes de aplicar/i }))
    expect(screen.getByText(/no es válida/i)).toBeTruthy()
    expect(selectArtwork).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: 'https://img.example/poster.jpg' } })
    fireEvent.click(screen.getByRole('button', { name: /ver antes de aplicar/i }))

    // Preview first: nothing is committed until the admin has seen what they are choosing.
    const use = await screen.findByRole('button', { name: /usar este póster/i })
    expect(selectArtwork).not.toHaveBeenCalled()

    fireEvent.click(use)
    await waitFor(() =>
      expect(selectArtwork).toHaveBeenCalledWith('s1', 'poster', 'https://img.example/poster.jpg'),
    )
  })
})
