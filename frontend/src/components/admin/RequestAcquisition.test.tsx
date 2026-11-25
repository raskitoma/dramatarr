// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { setupI18n } from '@/i18n'
const get = vi.fn()
const post = vi.fn()
vi.mock('@/api/client', () => ({ api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) } }))
const { RequestAcquisition } = await import('./RequestAcquisition')
const { AcquisitionProgress } = await import('./AcquisitionProgress')
const url = 'https://reelshort.dramafren.org/index.php?page=watch&id=abc&lang=pt'
const job = { id: 'a'.repeat(32), type: 'find_source', status: 'succeeded', payload: { acquisition: { stage: 'awaiting_review' }, candidates: [{ title: 'O Rei Dragão', url, provider: 'reelshort', language: 'pt', episodes: 32, idMatch: true }] } }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><RequestAcquisition id="r1" approved review languages="pt,en" onReview={vi.fn()} onLanguages={vi.fn()} /></QueryClientProvider>)
}
beforeAll(async () => { await setupI18n('es') })
beforeEach(() => { vi.clearAllMocks(); get.mockResolvedValue(job); post.mockResolvedValue({ ...job, type: 'download_series', status: 'queued' }) })
afterEach(cleanup)
it('shows a localized edition for review and starts nothing before explicit acceptance', async () => {
  mount()
  await screen.findByText('O Rei Dragão')
  expect(screen.getByText(/^portugués$/i)).toBeTruthy()
  expect(post).not.toHaveBeenCalled()
  const link = screen.getByRole('link', { name: 'Abrir y revisar' })
  expect(link.getAttribute('href')).toBe(url)
  fireEvent.click(screen.getByRole('button', { name: 'Descargar' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/admin/integration/requests/r1/download', { searchJobId: job.id, url }))
})
it('submits the chosen language order and review preference when searching again', async () => {
  mount()
  await screen.findByText('O Rei Dragão')
  fireEvent.click(screen.getByRole('button', { name: 'Actualizar resultados' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/admin/integration/requests/r1/search', { languages: ['pt','en'], reviewBeforeDownload: true, refresh: true }))
})
it('shows episode counters and an unknown size without inventing a total', () => {
  const download = { ...job, type: 'download_series', status: 'running', progressPercent: 35, payload: { acquisition: { stage: 'downloading', episodesTotal: 32, episodesCompleted: 12, currentEpisode: 13, currentEpisodePercent: 25, downloadedBytes: 1048576 } } }
  // Only progress fields are relevant to this standalone presentation component.
  render(<AcquisitionProgress job={download as unknown as import('@/api/types').Job} />)
  expect(screen.getByText(/12 de 32 episodios/)).toBeTruthy()
  expect(screen.getByRole('progressbar', { name: 'Avance total de la descarga' }).getAttribute('aria-valuenow')).toBe('35')
  expect(screen.getByRole('progressbar', { name: 'Avance del episodio' }).getAttribute('aria-valuenow')).toBe('25')
  expect(screen.getByText(/Descargando episodio 13/)).toBeTruthy()
  expect(screen.getByText('La fuente todavía no informa el tamaño completo.')).toBeTruthy()
})

it('offers a retry that preserves the failed search configuration', async () => {
  get.mockResolvedValue({
    ...job,
    status: 'failed',
    error: 'REMOTE_IMPORT_FAILED: Source search failed',
    finishedAt: '2026-11-10T12:00:00Z',
    payload: { searchErrors: [{ provider: 'reelshort', language: 'es', error: 'Navigation timeout' }] },
  })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Reintentar búsqueda' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith(`/admin/jobs/${job.id}/retry`))
})


it('keeps the saved results visible after a failed download and retries the existing job', async () => {
  const download = { id: 'd'.repeat(32), type: 'download_series', status: 'failed', payload: { url } }
  get.mockImplementation((path: string) => Promise.resolve(path.endsWith('/results') ? job : download))
  mount()
  await screen.findByText('O Rei Dragão')
  expect((screen.getByRole('button', { name: 'Descargar' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Reintentar descarga' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith(`/admin/jobs/${download.id}/retry`))
  expect(post).toHaveBeenCalledTimes(1)
})
