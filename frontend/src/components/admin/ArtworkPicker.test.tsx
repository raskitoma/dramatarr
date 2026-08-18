// @vitest-environment jsdom
/**
 * What the poster picker must not get wrong, now that it is a manual override and only that.
 *
 * A pasted URL is checked here rather than by the browser: a `type="url"` input would let
 * native constraint validation block the submit and answer in its own words, so the field would
 * have two different error languages depending on which check happened to fire.
 *
 * Nothing is committed from a URL until the admin has *seen* it. The whole point of the preview
 * is that a link to a 404 page or to the wrong show should be visible here, not after it is the
 * series' face.
 *
 * And a file is screened before it is offered — wrong type or oversized is answered in the
 * field, without a round trip, because the server would answer the same thing more slowly.
 *
 * Real translations are loaded rather than a `t: key => key` stub — the assertions are about
 * sentences reaching the screen, not about keys being looked up.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '@/components/Toast'
import { setupI18n } from '@/i18n'

const selectArtwork = vi.fn()
const uploadArtwork = vi.fn()
const searchDramadex = vi.fn()
const chooseDramadexArtwork = vi.fn()
const patchSeries = vi.fn()

vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    selectArtwork: (...args: unknown[]) => selectArtwork(...args),
    uploadArtwork: (...args: unknown[]) => uploadArtwork(...args),
    searchDramadex: (...args: unknown[]) => searchDramadex(...args),
    chooseDramadexArtwork: (...args: unknown[]) => chooseDramadexArtwork(...args),
    patchSeries: (...args: unknown[]) => patchSeries(...args),
  }
})

const { ArtworkPicker } = await import('./ArtworkPicker')

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
  // jsdom has no blob URLs, and the file preview is built on one.
  if (typeof URL.createObjectURL !== 'function') {
    URL.createObjectURL = () => 'blob:preview'
    URL.revokeObjectURL = () => {}
  }
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
  // Every test mounts the dialog, and the dialog searches on mount now.
  searchDramadex.mockResolvedValue({ configured: true, query: '', items: [] })
  chooseDramadexArtwork.mockResolvedValue({ seriesId: 's1', kind: 'poster' })
  patchSeries.mockResolvedValue({})
})

afterEach(cleanup)

describe('ArtworkPicker', () => {
  it('offers only the two paths that still exist, and says what choosing costs', async () => {
    renderIn(<ArtworkPicker open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />)

    await waitFor(() => expect(document.getElementById('artwork-url')).toBeTruthy())
    expect(document.getElementById('artwork-file')).toBeTruthy()

    // What choosing costs is said before any choice is made — and what it does *not* cost.
    // Picking a poster used to lock the whole record; now it keeps the picture and nothing else.
    expect(screen.getByText(/esa imagen se conserva/i)).toBeTruthy()
    expect(screen.getByText(/resto de la metadata se sigue actualizando/i)).toBeTruthy()

    // No gallery is fetched, offered, or explained away: the sources that fed one are gone.
    expect(screen.queryByText(/TMDB/i)).toBeNull()
    expect(document.getElementById('artwork-language')).toBeNull()
  })

  it('previews a pasted URL before committing it, and refuses one that is not a URL', async () => {
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

  it('commits an uploaded file against the slot that is selected, not always the poster', async () => {
    renderIn(<ArtworkPicker open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />)
    await waitFor(() => expect(document.getElementById('artwork-file')).toBeTruthy())

    fireEvent.change(document.getElementById('artwork-kind') as HTMLSelectElement, {
      target: { value: 'backdrop' },
    })

    const file = new File([new Uint8Array([1, 2, 3])], 'art.png', { type: 'image/png' })
    fireEvent.change(document.getElementById('artwork-file') as HTMLInputElement, {
      target: { files: [file] },
    })

    const use = await screen.findByRole('button', { name: /usar/i })
    fireEvent.click(use)
    await waitFor(() => expect(uploadArtwork).toHaveBeenCalledWith('s1', 'backdrop', file))
  })

  it('refuses a file of the wrong type in the field, without a round trip', async () => {
    renderIn(<ArtworkPicker open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />)
    await waitFor(() => expect(document.getElementById('artwork-file')).toBeTruthy())

    const file = new File(['nope'], 'notes.txt', { type: 'text/plain' })
    fireEvent.change(document.getElementById('artwork-file') as HTMLInputElement, {
      target: { files: [file] },
    })

    expect(screen.getByText(/ese formato no se puede usar/i)).toBeTruthy()
    expect(uploadArtwork).not.toHaveBeenCalled()
  })
})

describe('ArtworkPicker — arriving pre-searched', () => {
  it('shows the bound record’s own art without anyone pressing search', async () => {
    // The complaint, verbatim: "no necesito estar copiando y pegando... debería ir prellenado y
    // prebuscando cuando ya aparece el modal". So the assertion is about *mount*, not about a
    // click — a test that pressed a button first would pass against the old dialog.
    searchDramadex.mockResolvedValue({ configured: true, query: 'Mi Qing', items: [] })
    renderIn(
      <ArtworkPicker
        open
        onClose={() => {}}
        seriesId="s1"
        seriesTitle="Mi Qing"
        dramadexId={4242}
      />,
    )

    await waitFor(() =>
      expect(document.querySelector('img[src="/api/library/dramadex/titles/4242/poster"]')).toBeTruthy(),
    )
    expect(searchDramadex).toHaveBeenCalled()
  })

  it('sends a title id, never a URL, when a poster is chosen', async () => {
    // The indexer's poster route wants an API key in a header, so the fetch happens on the
    // server. Passing our own proxy URL back through the SSRF-guarded fetcher would be a round
    // trip through our own gate to arrive where we started.
    searchDramadex.mockResolvedValue({
      configured: true,
      query: 'Mi Qing',
      items: [
        {
          id: 99,
          title: 'Vecina',
          year: 2025,
          episodeCount: 60,
          posterUrl: null,
          synopsis: null,
          studio: 'ReelShort',
          watchUrl: null,
          genres: [],
          cast: [],
          matches: false,
          inLibrary: false,
          librarySlug: null,
        },
      ],
    })
    renderIn(
      <ArtworkPicker open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" dramadexId={null} />,
    )

    const card = await screen.findByTitle('Vecina')
    fireEvent.click(card)

    await waitFor(() => expect(chooseDramadexArtwork).toHaveBeenCalledWith('s1', 99, 'poster'))
  })

  it('says when a series has no record to take art from', async () => {
    searchDramadex.mockResolvedValue({ configured: true, query: 'Mi Qing', items: [] })
    renderIn(
      <ArtworkPicker open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" dramadexId={null} />,
    )

    expect(await screen.findByText(/no está vinculada a DramaDex/i)).toBeTruthy()
  })

  it('offers a way back when the artwork is pinned', async () => {
    // Until this existed a chosen poster was permanent: the flag was not even on the payload,
    // so the only undo was uploading another picture.
    searchDramadex.mockResolvedValue({ configured: true, query: 'Mi Qing', items: [] })
    renderIn(
      <ArtworkPicker
        open
        onClose={() => {}}
        seriesId="s1"
        seriesTitle="Mi Qing"
        dramadexId={4242}
        artworkLocked
      />,
    )

    const release = await screen.findByRole('button', { name: /permitir que DramaDex cambie/i })
    fireEvent.click(release)

    await waitFor(() =>
      expect(patchSeries).toHaveBeenCalledWith('s1', { artworkLocked: false }),
    )
  })
})
