// @vitest-environment jsdom
/**
 * The sign-in screen's two contracts that are easy to break silently.
 *
 * 1. Google appears only when the household has it configured, and clicking it hands the
 *    browser to the API rather than fetching — a fetch would sail straight into Google's
 *    CORS wall and look like a server fault.
 * 2. The Google callback has no UI of its own; it reports back as `/login?error=CODE`. If
 *    that code is not turned into a sentence here, the most likely failure of the whole
 *    feature — a Google account that was never invited — renders as a blank screen.
 *
 * Real translations are loaded rather than a `t: key => key` stub, because the point of the
 * error assertions is that the *sentence* exists, not that a key was looked up.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PublicConfig } from '@/api/types'
import { setupI18n } from '@/i18n'

const navigate = vi.fn()
const startGoogleSignIn = vi.fn()
const startPasskeyLogin = vi.fn()

let search = ''
let config: PublicConfig | null = null

vi.mock('wouter', () => ({
  useLocation: () => ['/login', navigate],
  useSearch: () => search,
}))

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: null, config, refresh: vi.fn(), isLoading: false, isAdmin: false }),
}))

vi.mock('@/lib/webauthn', () => ({
  isPasskeySupported: () => true,
  isPasskeyCancellation: () => false,
  startPasskeyLogin: () => startPasskeyLogin(),
}))

vi.mock('@/api/auth', () => ({
  startGoogleSignIn: (...args: unknown[]) => startGoogleSignIn(...args),
  authKeys: { all: ['auth'], methods: () => ['auth', 'methods'] },
}))

const { LoginPage } = await import('./LoginPage')

const CONFIG: PublicConfig = {
  siteName: 'dramatarr',
  defaultLocale: 'es',
  magicLinkEnabled: true,
  googleEnabled: true,
  hasUsers: true,
}

function renderPage(ui: ReactElement) {
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
  search = ''
  config = { ...CONFIG }
  startGoogleSignIn.mockResolvedValue(undefined)
})

afterEach(cleanup)

describe('LoginPage — Google', () => {
  it('offers Google only when the household has it configured', () => {
    const { unmount } = renderPage(<LoginPage />)
    expect(screen.getByRole('button', { name: /Continuar con Google/i })).toBeTruthy()
    unmount()

    config = { ...CONFIG, googleEnabled: false }
    renderPage(<LoginPage />)
    expect(screen.queryByRole('button', { name: /Continuar con Google/i })).toBeNull()
  })

  it('keeps the passkey first and the emailed link last', () => {
    renderPage(<LoginPage />)
    const labels = screen
      .getAllByRole('button')
      .map((button) => button.textContent ?? '')
      .filter((text) => text.trim() !== '')

    const passkey = labels.findIndex((text) => /passkey/i.test(text))
    const google = labels.findIndex((text) => /Google/i.test(text))
    const magic = labels.findIndex((text) => /enlace de acceso/i.test(text))

    expect(passkey).toBeGreaterThanOrEqual(0)
    expect(google).toBeGreaterThan(passkey)
    expect(magic).toBeGreaterThan(google)
  })

  it('hands the browser over instead of fetching', async () => {
    renderPage(<LoginPage />)
    fireEvent.click(screen.getByRole('button', { name: /Continuar con Google/i }))
    await waitFor(() => expect(startGoogleSignIn).toHaveBeenCalledTimes(1))
    // `next` travels so the callback lands where the person was headed.
    expect(startGoogleSignIn).toHaveBeenCalledWith({ next: '/' })
  })

  it('carries a safe ?next= through to the API and ignores an off-site one', async () => {
    search = '?next=/series/kingdom'
    const { unmount } = renderPage(<LoginPage />)
    fireEvent.click(screen.getByRole('button', { name: /Continuar con Google/i }))
    await waitFor(() => expect(startGoogleSignIn).toHaveBeenCalledWith({ next: '/series/kingdom' }))
    unmount()

    startGoogleSignIn.mockClear()
    search = '?next=//evil.example.com'
    renderPage(<LoginPage />)
    fireEvent.click(screen.getByRole('button', { name: /Continuar con Google/i }))
    await waitFor(() => expect(startGoogleSignIn).toHaveBeenCalledWith({ next: '/' }))
  })
})

describe('LoginPage — ?error= from the Google callback', () => {
  it('explains that an uninvited Google account is not a member', () => {
    search = '?error=GOOGLE_NO_ACCOUNT'
    renderPage(<LoginPage />)

    const alert = screen.getByRole('alert')
    // The whole point of this one: it must say why, not just that something failed.
    expect(alert.textContent).toMatch(/solo por invitaci[óo]n/i)
    expect(alert.textContent).not.toMatch(/Algo salió mal/i)
  })

  it.each([
    'GOOGLE_EMAIL_UNVERIFIED',
    'GOOGLE_CANCELLED',
    'GOOGLE_DISABLED',
    'GOOGLE_TOKEN_INVALID',
    'ACCOUNT_DISABLED',
  ])('has a sentence of its own for %s', (code) => {
    search = `?error=${code}`
    renderPage(<LoginPage />)

    const text = screen.getByRole('alert').textContent ?? ''
    expect(text.trim()).not.toBe('')
    expect(text).not.toMatch(/Algo salió mal/i)
    expect(text).not.toContain(code)
  })

  it('falls back to the generic sentence for a code it has never heard of', () => {
    search = '?error=SOMETHING_NEW'
    renderPage(<LoginPage />)
    expect(screen.getByRole('alert').textContent).toMatch(/Algo salió mal/i)
  })

  it('says nothing when there is no error in the query', () => {
    renderPage(<LoginPage />)
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
