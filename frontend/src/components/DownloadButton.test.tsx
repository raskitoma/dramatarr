// @vitest-environment jsdom
/**
 * The download control, and the dialog behind it.
 *
 * Written because the button shipped wrong twice over: the label rendered as visible text
 * spilling out of a square that its three neighbours fill with a glyph, and the confirmation
 * dialog could not be checked without opening it. Both are things a screenshot shows and a
 * type checker does not.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SeriesDetail } from '@/api/types'
import { setupI18n } from '@/i18n'

const measure = vi.fn()
const start = vi.fn()
const show = vi.fn()
vi.mock('@/offline/downloadManager', () => ({
  measure: (...args: unknown[]) => measure(...args),
  start: (...args: unknown[]) => start(...args),
  pause: vi.fn(),
  erase: vi.fn(),
}))
vi.mock('@/offline/register', () => ({ offlineSupported: () => true }))
vi.mock('@/offline/useDownloads', () => ({ useSeriesDownload: () => null }))
vi.mock('@/components/Toast', () => ({ useToast: () => ({ show: (...a: unknown[]) => show(...a) }) }))

const { DownloadButton } = await import('./DownloadButton')

const detail = {
  id: 's1',
  slug: 'x-ray',
  title: 'My X-Ray Vision',
  posterUrl: null,
  seasons: [{ seasonNumber: 1, episodes: [{ id: 'a', available: true }, { id: 'b', available: true }] }],
} as unknown as SeriesDetail

beforeAll(async () => {
  await setupI18n('es')
  // jsdom implements neither, and <Dialog> calls both.
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true
  }
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false
  }
})

beforeEach(() => {
  show.mockReset()
  measure.mockReset()
  start.mockReset()
  start.mockResolvedValue(undefined)
  measure.mockResolvedValue({ episodeIds: ['a', 'b'], bytes: 1_370_000_000 })
})

afterEach(cleanup)

describe('DownloadButton', () => {
  it('is a glyph, not a word — the label is announced and never seen', () => {
    render(<DownloadButton detail={detail} />)

    const label = screen.getByText('Descargar')
    // In the DOM, which is where the accessible name comes from…
    expect(label).toBeTruthy()
    // …and clipped out of sight, the same recipe its three neighbours use.
    expect(label.className).toContain('label')
    expect(document.querySelector('svg')).toBeTruthy()
  })

  it('opens at once and fills the size in, rather than making you wait for it', async () => {
    /* Measuring reads every episode record. When that ran *before* the dialog opened, the
       spinner looked like a download in progress and the confirmation arrived a minute later
       as if nothing had happened. */
    let resolve: (value: { episodeIds: string[]; bytes: number }) => void = () => {}
    measure.mockReturnValue(new Promise((r) => (resolve = r)))

    render(<DownloadButton detail={detail} />)
    fireEvent.click(screen.getByRole('button'))

    // Open immediately, saying what it already knows.
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeTruthy()
    expect(screen.getByText(/calculando el tamaño/)).toBeTruthy()

    resolve({ episodeIds: ['a', 'b'], bytes: 1_370_000_000 })
    await waitFor(() => expect(screen.getByText(/1,4GB/)).toBeTruthy())
  })

  it('will not start a download before the size is known', async () => {
    measure.mockReturnValue(new Promise(() => {}))

    render(<DownloadButton detail={detail} />)
    fireEvent.click(screen.getByRole('button'))
    await screen.findByRole('dialog')

    const confirm = screen
      .getAllByRole('button')
      .find((node) => node.textContent?.includes('Descargar') && node.closest('dialog'))
    expect(confirm).toHaveProperty('disabled', true)
    expect(start).not.toHaveBeenCalled()
  })

  it('hands the measurement to the download instead of taking it twice', async () => {
    const plan = { episodeIds: ['a', 'b'], bytes: 1_370_000_000 }
    measure.mockResolvedValue(plan)

    render(<DownloadButton detail={detail} />)
    fireEvent.click(screen.getByRole('button'))
    await screen.findByText(/1,4GB/)

    const confirm = screen
      .getAllByRole('button')
      .find((node) => node.textContent?.includes('Descargar') && node.closest('dialog'))
    fireEvent.click(confirm as HTMLElement)

    expect(start).toHaveBeenCalledWith(detail, plan)
  })

  it('shows the count and the real size before moving a byte', async () => {
    render(<DownloadButton detail={detail} />)
    fireEvent.click(screen.getByRole('button'))

    await screen.findByRole('dialog')
    expect(screen.getByText(/2 episodios/)).toBeTruthy()
    // Spanish locale, one decimal: "1,4GB". The point is that a real figure is shown at all.
    expect(screen.getByText(/1,4GB/)).toBeTruthy()
    // And it says where the bytes go, which is the thing the owner asked for explicitly.
    expect(screen.getByText(/no en tu carrete/)).toBeTruthy()
    expect(start).not.toHaveBeenCalled()
  })

  it('only downloads once the dialog is confirmed', async () => {
    render(<DownloadButton detail={detail} />)
    fireEvent.click(screen.getByRole('button'))
    await screen.findByRole('dialog')

    const confirm = screen
      .getAllByRole('button')
      .find((node) => node.textContent?.includes('Descargar') && node.closest('dialog'))
    fireEvent.click(confirm as HTMLElement)

    expect(start).toHaveBeenCalled()
  })

  it('cancels without downloading', async () => {
    render(<DownloadButton detail={detail} />)
    fireEvent.click(screen.getByRole('button'))
    await screen.findByRole('dialog')

    fireEvent.click(screen.getByRole('button', { name: 'Cancelar' }))

    expect(start).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('says why it cannot work on an origin with no service worker', async () => {
    vi.resetModules()
    vi.doMock('@/offline/register', () => ({ offlineSupported: () => false }))
    const { DownloadButton: Unsupported } = await import('./DownloadButton')

    render(<Unsupported detail={detail} />)

    expect(screen.getByRole('button')).toHaveProperty('disabled', true)
    expect(screen.getByText('No disponible aquí')).toBeTruthy()
  })

  it('says so when the download cannot even be started', async () => {
    /* This used to be `void start(...)`, which swallowed everything: a download that died on
       IndexedDB or a refused quota looked exactly like one that had never been pressed. */
    start.mockRejectedValue(new Error('QuotaExceededError'))
    render(<DownloadButton detail={detail} />)
    fireEvent.click(screen.getByRole('button'))
    await screen.findByText(/1,4GB/)

    const confirm = screen
      .getAllByRole('button')
      .find((node) => node.textContent?.includes('Descargar') && node.closest('dialog'))
    fireEvent.click(confirm as HTMLElement)

    await waitFor(() =>
      expect(show).toHaveBeenCalledWith(expect.stringContaining('QuotaExceededError'), 'error'),
    )
  })
})
