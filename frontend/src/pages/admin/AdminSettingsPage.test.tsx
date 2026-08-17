// @vitest-environment jsdom
/**
 * Regression cover for the settings form.
 *
 * The SMTP section silently failed to persist in production, and the API was proven fine by
 * hand, so the suspicion fell on the form. These tests drive the real component against a
 * mocked API and assert the exact payload that leaves it.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { ToastProvider } from '@/components/Toast'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getSettings = vi.fn()
const updateSettings = vi.fn()
const testSmtp = vi.fn()

// Only the calls that would hit the network are replaced; the module's pure parts (the query
// keys) are the real ones, so a test cannot pass against a stub of the logic the page depends
// on.
vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    getSettings: (...args: unknown[]) => getSettings(...args),
    updateSettings: (...args: unknown[]) => updateSettings(...args),
    testSmtp: (...args: unknown[]) => testSmtp(...args),
  }
})

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'u1', role: 'admin', locale: 'es' }, isAdmin: true }),
}))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: 'es' },
  }),
}))

const { AdminSettingsPage } = await import('./AdminSettingsPage')

/** What the API returns on a database that has only ever been seeded with defaults. */
const DEFAULT_SETTINGS = {
  'site.name': 'dramatarr',
  'site.default_locale': 'es',
  'library.scan_cron': '0 4 * * *',
  'library.source_base_url': '',
  'smtp.host': '',
  'smtp.port': 587,
  'smtp.username': '',
  'smtp.password': '',
  'smtp.from_email': '',
  'smtp.from_name': 'dramatarr',
  'smtp.use_tls': true,
  'smtp.use_ssl': false,
  'auth.magic_link_enabled': true,
  'auth.invite_ttl_hours': 168,
  'oauth.google.enabled': false,
  'oauth.google.client_id': '',
  'oauth.google.client_secret': '',
  'oauth.google.allow_invites': true,
  // Server-derived and read-only. They come from DRAMATARR_PUBLIC_URL, which is deliberately
  // NOT this jsdom document's origin — that difference is the whole point of the assertion
  // below, and it is the situation of every install reached through a proxy or a LAN address.
  'derived.google.redirect_uri': 'https://dramatarr.casa.example/api/auth/google/callback',
  'derived.google.js_origin': 'https://dramatarr.casa.example',
}

function renderPage(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  // The page now contains a section that raises a toast on save. In the app every screen
  // renders inside `ToastProvider` (App.tsx wraps the whole tree), so providing it here makes
  // the harness match production rather than working around the component.
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>{ui}</ToastProvider>
    </QueryClientProvider>,
  )
}

function typeInto(id: string, value: string) {
  const el = document.getElementById(id) as HTMLInputElement
  expect(el, `#${id} should exist`).toBeTruthy()
  fireEvent.change(el, { target: { value } })
}

beforeEach(() => {
  vi.clearAllMocks()
  getSettings.mockResolvedValue({ ...DEFAULT_SETTINGS })
  updateSettings.mockImplementation(async (patch: Record<string, unknown>) => ({
    ...DEFAULT_SETTINGS,
    ...patch,
  }))
})

afterEach(cleanup)

describe('AdminSettingsPage — SMTP section', () => {
  it('sends exactly the changed keys when the form is saved', async () => {
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('smtp-host')).toBeTruthy())

    typeInto('smtp-host', '10.0.50.140')
    typeInto('smtp-from', 'servers@raskitoma.com')

    // The relay advertises no STARTTLS, so the admin turns the default off.
    const tlsSwitch = screen.getByRole('switch', { name: /smtpTls/i })
    fireEvent.click(tlsSwitch)

    const saveButtons = screen.getAllByRole('button', { name: /common\.save/i })
    const emailSave = saveButtons.find((b) => b.closest('form')?.contains(document.getElementById('smtp-host')))
    expect(emailSave, 'the email section should have a save button').toBeTruthy()
    expect((emailSave as HTMLButtonElement).disabled, 'save must be enabled after edits').toBe(false)

    fireEvent.click(emailSave as HTMLButtonElement)

    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1))
    expect(updateSettings).toHaveBeenCalledWith({
      'smtp.host': '10.0.50.140',
      'smtp.from_email': 'servers@raskitoma.com',
      'smtp.use_tls': false,
    })
  })

  it('keeps the save button disabled while nothing has changed', async () => {
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('smtp-host')).toBeTruthy())

    const saveButtons = screen.getAllByRole('button', { name: /common\.save/i })
    for (const button of saveButtons) {
      expect((button as HTMLButtonElement).disabled).toBe(true)
    }
    expect(updateSettings).not.toHaveBeenCalled()
  })

  it('saves a first-time SMTP password', async () => {
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('smtp-password')).toBeTruthy())

    typeInto('smtp-password', 'hunter2')
    const save = screen
      .getAllByRole('button', { name: /common\.save/i })
      .find((b) => b.closest('form')?.contains(document.getElementById('smtp-password')))
    fireEvent.click(save as HTMLButtonElement)

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).toMatchObject({ 'smtp.password': 'hunter2' })
  })

  it('does not send the port unless it actually changed', async () => {
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('smtp-host')).toBeTruthy())

    typeInto('smtp-host', 'mail.example.com')
    const emailSave = screen
      .getAllByRole('button', { name: /common\.save/i })
      .find((b) => b.closest('form')?.contains(document.getElementById('smtp-host')))
    fireEvent.click(emailSave as HTMLButtonElement)

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).not.toHaveProperty('smtp.port')
  })
})

describe('AdminSettingsPage — Sign-in section', () => {
  /** The save button belonging to whichever section owns `id`. */
  function saveButtonFor(id: string): HTMLButtonElement {
    const button = screen
      .getAllByRole('button', { name: /common\.save/i })
      .find((b) => b.closest('form')?.contains(document.getElementById(id)))
    expect(button, `the section holding #${id} should have a save button`).toBeTruthy()
    return button as HTMLButtonElement
  }

  it('saves a first-time Google client secret', async () => {
    // Exactly the TMDB bug, one section over: the secret input is on screen from the first
    // render on a fresh install, and the "Edit" button that would flip an `editing` flag only
    // exists once a value is already stored. Gating the patch on that flag would make the
    // field typeable but unsavable, and Google sign-in would never work.
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('google-client-secret')).toBeTruthy())

    typeInto('google-client-id', '1234567890-abcdef.apps.googleusercontent.com')
    typeInto('google-client-secret', 'GOCSPX-notarealsecret')

    const save = saveButtonFor('google-client-secret')
    expect(save.disabled, 'save must be enabled after typing a secret').toBe(false)
    fireEvent.click(save)

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).toMatchObject({
      'oauth.google.client_id': '1234567890-abcdef.apps.googleusercontent.com',
      'oauth.google.client_secret': 'GOCSPX-notarealsecret',
    })
  })

  it('sends only the switches that actually moved', async () => {
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('google-client-secret')).toBeTruthy())

    fireEvent.click(screen.getByRole('switch', { name: /googleEnabled/i }))
    fireEvent.click(saveButtonFor('google-client-secret'))

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).toEqual({ 'oauth.google.enabled': true })
  })

  it('never echoes a stored secret back as if it were a new one', async () => {
    getSettings.mockResolvedValue({ ...DEFAULT_SETTINGS, 'oauth.google.client_secret': '__SET__' })
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('google-client-id')).toBeTruthy())

    // Stored secret: the input is replaced by a badge and an Edit button, so there is nothing
    // to type into and nothing about the secret may travel with an unrelated change.
    expect(document.getElementById('google-client-secret')?.tagName).toBe('BUTTON')

    typeInto('google-client-id', 'new-client-id.apps.googleusercontent.com')
    fireEvent.click(saveButtonFor('google-client-id'))

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).not.toHaveProperty('oauth.google.client_secret')
  })

  it('shows the redirect URI and JavaScript origin the server reports, not the browser origin', async () => {
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('google-redirect-uri')).toBeTruthy())

    const redirect = document.getElementById('google-redirect-uri') as HTMLInputElement
    const origin = document.getElementById('google-js-origin') as HTMLInputElement

    // What the admin pastes into Google Cloud Console has to be what the *server* will send,
    // and only the server knows that. Deriving it here from window.location.origin would be
    // right on a plain localhost install and wrong on every deployment behind a proxy, where
    // Google then answers redirect_uri_mismatch with nothing in the UI to explain it.
    await waitFor(() =>
      expect(redirect.value).toBe('https://dramatarr.casa.example/api/auth/google/callback'),
    )
    expect(origin.value).toBe('https://dramatarr.casa.example')
    expect(redirect.value).not.toContain(window.location.origin)

    expect(redirect.readOnly).toBe(true)
    expect(origin.readOnly).toBe(true)
  })
})
