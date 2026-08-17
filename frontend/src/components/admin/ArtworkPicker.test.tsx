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

vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    selectArtwork: (...args: unknown[]) => selectArtwork(...args),
    uploadArtwork: (...args: unknown[]) => uploadArtwork(...args),
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
})

afterEach(cleanup)

describe('ArtworkPicker', () => {
  it('offers only the two paths that still exist, and says what choosing costs', async () => {
    renderIn(<ArtworkPicker open onClose={() => {}} seriesId="s1" seriesTitle="Mi Qing" />)

    await waitFor(() => expect(document.getElementById('artwork-url')).toBeTruthy())
    expect(document.getElementById('artwork-file')).toBeTruthy()

    // Choosing artwork locks the series' metadata, and that is said before any choice is made.
    expect(screen.getByText(/queda bloqueada/i)).toBeTruthy()

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
