// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'

import { setupI18n } from '@/i18n'
import { captureReportError, Reporter } from './Reporter'

const mocks = vi.hoisted(() => ({ post: vi.fn(), auth: { user: { id: 'viewer' } as { id: string } | null } }))
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => mocks.auth }))
vi.mock('@/api/client', async original => ({
  ...await original<typeof import('@/api/client')>(),
  api: { post: mocks.post },
}))

beforeAll(async () => {
  await setupI18n('es')
  HTMLDialogElement.prototype.showModal = function () { this.open = true }
  HTMLDialogElement.prototype.close = function () { this.open = false }
})
beforeEach(() => {
  mocks.auth.user = { id: 'viewer' }
  mocks.post.mockReset().mockResolvedValue(undefined)
})
afterEach(cleanup)

function mount() {
  return render(<QueryClientProvider client={new QueryClient()}><Reporter /></QueryClientProvider>)
}

it('requires a signed-in session to show the launcher', () => {
  mocks.auth.user = null
  mount()
  expect(screen.queryByRole('button', { name: 'Enviar un reporte' })).toBeNull()
})

it('sends selected errors with shared context and leaves the rest in the stack', async () => {
  mount()
  act(() => {
    captureReportError(new Error('First error'))
    captureReportError(new Error('Second error'))
  })
  const launcher = screen.getByRole('button', { name: 'Enviar un reporte' })
  expect(launcher.dataset.alert).toBe('true')
  fireEvent.click(launcher)
  fireEvent.click(screen.getAllByRole('checkbox')[1]!)
  fireEvent.change(screen.getByLabelText('Comentario para los errores seleccionados (opcional)'), {
    target: { value: 'Shared context' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Enviar' }))
  expect(mocks.post).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirmar' }))
  await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/reports', expect.objectContaining({
    category: 'error',
    message: 'Shared context',
    errors: [expect.objectContaining({ message: 'First error' })],
  })))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  fireEvent.click(launcher)
  expect(screen.getByText('Second error')).toBeTruthy()
  expect(screen.queryByText('First error')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Ignorar' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirmar' }))
  expect(launcher.dataset.alert).toBe('false')
})

it('requires a category and a nonblank message for a manual report', () => {
  mount()
  fireEvent.click(screen.getByRole('button', { name: 'Enviar un reporte' }))
  const send = screen.getByRole('button', { name: 'Enviar' }) as HTMLButtonElement
  expect(send.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Tipo de reporte'), { target: { value: 'suggestion' } })
  expect(send.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Mensaje'), { target: { value: 'A suggestion' } })
  expect(send.disabled).toBe(false)
})
