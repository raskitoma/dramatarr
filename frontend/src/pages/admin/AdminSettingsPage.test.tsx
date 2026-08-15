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

import { ApiRequestError } from '@/api/client'

const getSettings = vi.fn()
const updateSettings = vi.fn()
const testSmtp = vi.fn()
const testTmdb = vi.fn()
const testSearch = vi.fn()
const listSearchEngines = vi.fn()

// Only the calls that would hit the network are replaced; the module's pure parts (query keys,
// the provider vocabulary, the engine-list parsing) are the real ones, so a test cannot pass
// against a stub of the logic the page depends on.
vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    getSettings: (...args: unknown[]) => getSettings(...args),
    updateSettings: (...args: unknown[]) => updateSettings(...args),
    testSmtp: (...args: unknown[]) => testSmtp(...args),
    testTmdb: (...args: unknown[]) => testTmdb(...args),
    testSearch: (...args: unknown[]) => testSearch(...args),
    listSearchEngines: (...args: unknown[]) => listSearchEngines(...args),
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
  'tmdb.api_key': '',
  'tmdb.language': 'es-ES',
  'images.search.provider': '',
  'images.search.api_key': '',
  'images.search.cse_id': '',
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

/** A household that already runs SearXNG and has saved the address. Nothing is dirty. */
const SEARXNG_SETTINGS = {
  ...DEFAULT_SETTINGS,
  'images.search.provider': 'searxng',
  'images.search.base_url': 'http://searx.casa.lan:8080',
  'images.search.safe_search': 'moderate',
  'images.search.language': '',
  'images.search.timeout': 8,
  'images.search.engines': '',
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

  it('saves a first-time TMDB key, which was silently dropped before', async () => {
    // The bug: the key only travelled when `editingTmdbKey` was true, and that flag is only
    // set by an "Edit" button that exists solely once a key is already stored. On a fresh
    // install the field was therefore a dead end — typeable, but never saved.
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('tmdb-key')).toBeTruthy())

    typeInto('tmdb-key', 'abcdef0123456789abcdef0123456789')

    const save = screen
      .getAllByRole('button', { name: /common\.save/i })
      .find((b) => b.closest('form')?.contains(document.getElementById('tmdb-key')))
    expect((save as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(save as HTMLButtonElement)

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).toMatchObject({
      'tmdb.api_key': 'abcdef0123456789abcdef0123456789',
    })
  })

  it('tests the typed TMDB key rather than the empty stored one', async () => {
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('tmdb-key')).toBeTruthy())

    typeInto('tmdb-key', 'abcdef0123456789abcdef0123456789')
    fireEvent.click(screen.getByRole('button', { name: /admin\.settings\.tmdbTest/i }))

    await waitFor(() => expect(testTmdb).toHaveBeenCalled())
    expect(testTmdb).toHaveBeenCalledWith('abcdef0123456789abcdef0123456789')
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

describe('AdminSettingsPage — Image search subsection', () => {
  /** The save button belonging to whichever section owns `id`. */
  function saveButtonFor(id: string): HTMLButtonElement {
    const button = screen
      .getAllByRole('button', { name: /common\.save/i })
      .find((b) => b.closest('form')?.contains(document.getElementById(id)))
    expect(button, `the section holding #${id} should have a save button`).toBeTruthy()
    return button as HTMLButtonElement
  }

  it('saves a first-time image-search API key', async () => {
    // The third instance of the same trap as the TMDB key and the Google client secret: the
    // input is on screen from the first render on a fresh install, and the "Edit" button that
    // would flip an `editing` flag only exists once a value is already stored. Gating the patch
    // on that flag makes the field typeable but unsavable, and the artwork picker's web search
    // would never come on — with nothing on screen to explain why.
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('image-search-provider')).toBeTruthy())

    const provider = document.getElementById('image-search-provider') as HTMLSelectElement
    fireEvent.change(provider, { target: { value: 'google_cse' } })
    typeInto('image-search-cse', 'a1b2c3d4e5f6g7h8i')
    typeInto('image-search-key', 'AIzaSyNotARealKey0123456789')

    const save = saveButtonFor('image-search-key')
    expect(save.disabled, 'save must be enabled after typing a key').toBe(false)
    fireEvent.click(save)

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).toMatchObject({
      'images.search.provider': 'google_cse',
      'images.search.cse_id': 'a1b2c3d4e5f6g7h8i',
      'images.search.api_key': 'AIzaSyNotARealKey0123456789',
    })
  })

  it('never echoes a stored image-search key back as if it were a new one', async () => {
    getSettings.mockResolvedValue({
      ...DEFAULT_SETTINGS,
      // A provider has to be selected for its fields to exist at all — an engine id under no
      // provider is a question with no answer.
      'images.search.provider': 'google_cse',
      'images.search.api_key': '__SET__',
    })
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('image-search-cse')).toBeTruthy())

    // Stored secret: the input is replaced by a badge and an Edit button, so there is nothing
    // to type into and nothing about the key may travel with an unrelated change.
    expect(document.getElementById('image-search-key')?.tagName).toBe('BUTTON')

    typeInto('image-search-cse', 'new-engine-id')
    fireEvent.click(saveButtonFor('image-search-cse'))

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).toEqual({ 'images.search.cse_id': 'new-engine-id' })
  })

  it('leaves the image-search keys out of the patch when they were not touched', async () => {
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('tmdb-key')).toBeTruthy())

    // The subsection shares the metadata section's save button, so an untouched image search
    // must not ride along with a TMDB edit.
    typeInto('tmdb-key', 'abcdef0123456789abcdef0123456789')
    fireEvent.click(saveButtonFor('tmdb-key'))

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    const patch = updateSettings.mock.calls[0]?.[0] as Record<string, unknown>
    expect(patch).not.toHaveProperty('images.search.api_key')
    expect(patch).not.toHaveProperty('images.search.provider')
    expect(patch).not.toHaveProperty('images.search.cse_id')
  })

  it('swaps which fields render when the provider changes', async () => {
    // The two providers are configured by entirely different things, and a field belonging to
    // the other one is not harmless clutter: it is a question ("do I need a cx here?") the
    // admin has to work out the answer to every time they open this screen.
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('image-search-provider')).toBeTruthy())
    const provider = document.getElementById('image-search-provider') as HTMLSelectElement

    // Off is the shipped state: no provider's fields, and nothing to test.
    expect(document.getElementById('searxng-base-url')).toBeNull()
    expect(document.getElementById('image-search-cse')).toBeNull()
    expect(document.getElementById('search-test')).toBeNull()

    fireEvent.change(provider, { target: { value: 'searxng' } })
    expect(document.getElementById('searxng-base-url')).toBeTruthy()
    expect(document.getElementById('searxng-safe-search')).toBeTruthy()
    expect(document.getElementById('searxng-language')).toBeTruthy()
    expect(document.getElementById('searxng-timeout')).toBeTruthy()
    expect(document.getElementById('search-test')).toBeTruthy()
    expect(document.getElementById('image-search-cse'), 'no Google cx under SearXNG').toBeNull()

    fireEvent.change(provider, { target: { value: 'google_cse' } })
    expect(document.getElementById('image-search-cse')).toBeTruthy()
    expect(document.getElementById('searxng-base-url'), 'no SearXNG URL under Google').toBeNull()
    expect(document.getElementById('searxng-safe-search')).toBeNull()

    // The one field both providers share, so it survives the switch either way.
    expect(document.getElementById('image-search-key')).toBeTruthy()
  })
})

describe('AdminSettingsPage — SearXNG', () => {
  function saveButtonFor(id: string): HTMLButtonElement {
    const button = screen
      .getAllByRole('button', { name: /common\.save/i })
      .find((b) => b.closest('form')?.contains(document.getElementById(id)))
    expect(button, `the section holding #${id} should have a save button`).toBeTruthy()
    return button as HTMLButtonElement
  }

  it('saves a first-time SearXNG API token', async () => {
    // The same trap as the TMDB key, the SMTP password and the Google client secret: the input
    // is on screen from the first render on a fresh install, and the "Edit" button that flips
    // the `editing` flag only exists once something is stored. Gating the patch on that flag
    // makes a first-time token typeable but unsavable — and an instance that requires a token
    // would then answer every artwork search with a 403 nobody can explain.
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('image-search-provider')).toBeTruthy())

    fireEvent.change(document.getElementById('image-search-provider') as HTMLSelectElement, {
      target: { value: 'searxng' },
    })
    typeInto('searxng-base-url', 'http://searx.casa.lan:8080')
    typeInto('image-search-key', 'searxng-token-abc123')

    const save = saveButtonFor('image-search-key')
    expect(save.disabled, 'save must be enabled after typing a token').toBe(false)
    fireEvent.click(save)

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).toMatchObject({
      'images.search.provider': 'searxng',
      'images.search.base_url': 'http://searx.casa.lan:8080',
      'images.search.api_key': 'searxng-token-abc123',
    })
  })

  it('refuses a base URL that is not an http(s) address', async () => {
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('image-search-provider')).toBeTruthy())

    fireEvent.change(document.getElementById('image-search-provider') as HTMLSelectElement, {
      target: { value: 'searxng' },
    })
    typeInto('searxng-base-url', 'searx.casa.lan')

    expect(saveButtonFor('searxng-base-url').disabled).toBe(true)
    expect(screen.getByText('admin.settings.searxngUrlInvalid')).toBeTruthy()
  })

  it('makes the admin save before testing, as the test uses stored settings', async () => {
    getSettings.mockResolvedValue({ ...SEARXNG_SETTINGS })
    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('searxng-base-url')).toBeTruthy())

    const test = document.getElementById('search-test') as HTMLButtonElement
    expect(test.disabled, 'nothing is dirty, so the stored config is what is on screen').toBe(false)

    typeInto('searxng-base-url', 'http://searx.other.lan')
    expect(test.disabled, 'an unsaved URL would be tested against the old one').toBe(true)
    expect(testSearch).not.toHaveBeenCalled()
  })

  it('lists the instance engines after a successful test and saves the ticked ones', async () => {
    getSettings.mockResolvedValue({ ...SEARXNG_SETTINGS })
    testSearch.mockResolvedValue({
      ok: true,
      detail: null,
      provider: 'searxng',
      version: '2024.9.1',
      imageEngineCount: 2,
    })
    listSearchEngines.mockResolvedValue([
      { name: 'bing images', categories: ['images'], enabled: true, shortcut: 'bii' },
      { name: 'duckduckgo images', categories: ['images'], enabled: true, shortcut: 'ddi' },
      // Not an image engine: it must not be offered as one.
      { name: 'wikipedia', categories: ['general'], enabled: true, shortcut: 'wp' },
    ])

    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('search-test')).toBeTruthy())

    // Before the test there is nothing to discover from, so the free-text field carries the
    // admin who already knows the names rather than blocking them behind a probe.
    expect(document.getElementById('search-engines')).toBeTruthy()

    fireEvent.click(document.getElementById('search-test') as HTMLButtonElement)
    await waitFor(() => expect(listSearchEngines).toHaveBeenCalled())

    const bing = await screen.findByRole('checkbox', { name: 'bing images' })
    expect(screen.getByRole('checkbox', { name: 'duckduckgo images' })).toBeTruthy()
    expect(screen.queryByRole('checkbox', { name: 'wikipedia' })).toBeNull()

    fireEvent.click(bing)
    fireEvent.click(saveButtonFor('search-test'))

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).toEqual({ 'images.search.engines': 'bing images' })
  })

  it('falls back to a free-text engine field when the instance cannot be listed', async () => {
    getSettings.mockResolvedValue({ ...SEARXNG_SETTINGS })
    testSearch.mockResolvedValue({
      ok: true,
      detail: null,
      provider: 'searxng',
      version: null,
      imageEngineCount: null,
    })
    listSearchEngines.mockRejectedValue(new Error('no such route'))

    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('search-test')).toBeTruthy())
    fireEvent.click(document.getElementById('search-test') as HTMLButtonElement)

    await waitFor(() => expect(listSearchEngines).toHaveBeenCalled())
    // Discovery is a convenience, never a gate.
    await waitFor(() =>
      expect(screen.getByText('admin.settings.searchEnginesFallback')).toBeTruthy(),
    )

    typeInto('search-engines', 'bing images, google images')
    fireEvent.click(saveButtonFor('search-engines'))

    await waitFor(() => expect(updateSettings).toHaveBeenCalled())
    expect(updateSettings.mock.calls[0]?.[0]).toEqual({
      'images.search.engines': 'bing images, google images',
    })
  })

  it('answers SEARXNG_JSON_DISABLED with the settings.yml fix, not a generic failure', async () => {
    // The single most likely thing to be wrong on a fresh instance: SearXNG answers in HTML
    // only until `json` is listed under search.formats. "The search failed" would send an
    // admin looking at their URL, their network and their token — none of which are the cause.
    getSettings.mockResolvedValue({ ...SEARXNG_SETTINGS })
    testSearch.mockRejectedValue(
      new ApiRequestError(502, {
        code: 'SEARXNG_JSON_DISABLED',
        message: 'The instance does not serve JSON',
        details: {},
      }),
    )

    renderPage(<AdminSettingsPage />)
    await waitFor(() => expect(document.getElementById('search-test')).toBeTruthy())
    fireEvent.click(document.getElementById('search-test') as HTMLButtonElement)

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.getByText('admin.settings.searchJsonDisabledFix')).toBeTruthy()
    // The YAML itself is shown verbatim: describing an indented block in prose is how an admin
    // ends up pasting something that does not parse.
    expect(screen.getByText(/search:\s+formats:/)).toBeTruthy()
    expect(screen.getByText('admin.settings.searchJsonDisabledRestart')).toBeTruthy()
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
