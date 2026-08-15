// @vitest-environment jsdom
/**
 * The founding account must be *visibly* untouchable.
 *
 * The server refuses to demote, deactivate or delete it, and that refusal is the real guard.
 * What this covers is the half the server cannot do: an admin who can click a control that is
 * guaranteed to fail has been told nothing, and an admin who finds the control missing has been
 * told even less. So every one of those controls renders disabled, with the reason beside it.
 *
 * It is a regression test because the flag is easy to lose: it arrives from the API on one
 * field, and dropping it anywhere between the wire and the row silently re-enables three
 * destructive controls on the one account that must never lose them.
 *
 * Real translations are loaded rather than a `t: key => key` stub, so the assertion that the
 * reason is *stated* is an assertion about a sentence, not about a lookup.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { User } from '@/api/types'
import { ToastProvider } from '@/components/Toast'
import { setupI18n } from '@/i18n'

const listUsers = vi.fn()
const listInvites = vi.fn()
const patchUser = vi.fn()
const deleteUser = vi.fn()

vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    listUsers: () => listUsers(),
    listInvites: () => listInvites(),
    patchUser: (...args: unknown[]) => patchUser(...args),
    deleteUser: (...args: unknown[]) => deleteUser(...args),
    createInvite: vi.fn(),
    resendInvite: vi.fn(),
    revokeInvite: vi.fn(),
  }
})

const { AdminUsersPage } = await import('./AdminUsersPage')

function makeUser(overrides: Partial<User>): User {
  return {
    id: 'u1',
    email: 'ana@casa.example',
    displayName: 'Ana',
    role: 'admin',
    isActive: true,
    locale: 'es',
    theme: 'auto',
    preferredAudioLang: null,
    preferredSubtitleLang: null,
    autoplayNext: true,
    avatarColor: '#6c8cff',
    avatarUrl: null,
    createdAt: '2026-01-04T10:00:00Z',
    lastLoginAt: null,
    ...overrides,
  }
}

const FOUNDER = makeUser({ id: 'u1', displayName: 'Ana', isProtected: true })
const REGULAR = makeUser({
  id: 'u2',
  displayName: 'Beto',
  email: 'beto@casa.example',
  role: 'viewer',
  isProtected: false,
})

function renderPage(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>{ui}</ToastProvider>
    </QueryClientProvider>,
  )
}

/** The `<tr>` a member's name sits in. */
function rowFor(name: string): HTMLElement {
  const cell = screen.getByText(name)
  const row = cell.closest('tr')
  expect(row, `${name} should have a row`).toBeTruthy()
  return row as HTMLElement
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  vi.clearAllMocks()
  listUsers.mockResolvedValue([FOUNDER, REGULAR])
  listInvites.mockResolvedValue([])
})

afterEach(cleanup)

describe('AdminUsersPage — the protected founding account', () => {
  it('disables its role, activation and delete controls, and says why', async () => {
    renderPage(<AdminUsersPage />)
    await waitFor(() => expect(screen.getByText('Ana')).toBeTruthy())

    const row = rowFor('Ana')

    const role = within(row).getByRole('combobox') as HTMLSelectElement
    expect(role.disabled, 'the role selector must be disabled').toBe(true)

    const buttons = within(row).getAllByRole('button') as HTMLButtonElement[]
    expect(buttons.length, 'activation and delete should both be rendered').toBeGreaterThanOrEqual(2)
    for (const button of buttons) {
      expect(button.disabled, `${button.textContent} must be disabled`).toBe(true)
    }

    // Disabled is not an explanation on its own; the reason has to be on screen.
    expect(within(row).getByText(/consola del servidor/i)).toBeTruthy()
  })

  it('shows a badge next to the name rather than hiding the controls', async () => {
    renderPage(<AdminUsersPage />)
    await waitFor(() => expect(screen.getByText('Ana')).toBeTruthy())

    const row = rowFor('Ana')
    // Exact text: the badge, not the longer sentence that also mentions the founding account.
    expect(within(row).getByText('Cuenta fundadora')).toBeTruthy()
    // Hiding a control raises a question; disabling it answers one. The controls stay.
    expect(within(row).queryByRole('combobox')).toBeTruthy()
  })

  it('leaves every other member fully editable', async () => {
    renderPage(<AdminUsersPage />)
    await waitFor(() => expect(screen.getByText('Beto')).toBeTruthy())

    const row = rowFor('Beto')
    expect((within(row).getByRole('combobox') as HTMLSelectElement).disabled).toBe(false)
    for (const button of within(row).getAllByRole('button') as HTMLButtonElement[]) {
      expect(button.disabled).toBe(false)
    }
    expect(within(row).queryByText(/cuenta fundadora/i)).toBeNull()
  })

  it('treats a member without the flag as unprotected', async () => {
    // A server that predates the founding-account flag omits it entirely. That must read as
    // "not protected" and behave exactly as this screen always did — never as protected.
    const legacy = makeUser({ id: 'u3', displayName: 'Caro', email: 'caro@casa.example' })
    delete (legacy as Partial<User>).isProtected
    listUsers.mockResolvedValue([legacy])

    renderPage(<AdminUsersPage />)
    await waitFor(() => expect(screen.getByText('Caro')).toBeTruthy())

    const row = rowFor('Caro')
    expect((within(row).getByRole('combobox') as HTMLSelectElement).disabled).toBe(false)
    expect(within(row).queryByText(/cuenta fundadora/i)).toBeNull()
  })
})
