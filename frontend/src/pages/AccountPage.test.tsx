// @vitest-environment jsdom
/**
 * The profile picture block, which has one obligation that is not about pixels.
 *
 * A photo taken with a phone usually carries the coordinates of the place it was taken. The
 * server strips them when it re-encodes the image, and this screen says so *before* the file
 * input — so the sentence being on screen is a test, not a copy detail. Losing it would leave
 * members uploading location data to their household without ever being told.
 *
 * The rest is the ordinary shape of a good upload: nothing is sent until the member has seen
 * the picture they chose, and a format the api container cannot decode is refused here rather
 * than after eight megabytes have crossed the network.
 *
 * Real translations are loaded rather than a `t: key => key` stub, because the point of the
 * privacy assertion is that the sentence exists, not that a key was looked up.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { User } from '@/api/types'
import { ToastProvider } from '@/components/Toast'
import { setupI18n } from '@/i18n'

const uploadAvatar = vi.fn()
const deleteAvatar = vi.fn()
const patchUser = vi.fn()

const USER: User = {
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
}

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    user: USER,
    config: { siteName: 'dramatarr', googleEnabled: false },
    isAdmin: true,
    isLoading: false,
    refresh: vi.fn(),
    signOut: vi.fn(),
    patchUser: (...args: unknown[]) => patchUser(...args),
  }),
}))

vi.mock('@/hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'auto', setTheme: vi.fn(), resolved: 'light' }),
}))

vi.mock('@/api/account', async () => {
  const actual = await vi.importActual<typeof import('@/api/account')>('@/api/account')
  return {
    ...actual,
    listPasskeys: () => Promise.resolve([]),
    listSessions: () => Promise.resolve([]),
    updateProfile: vi.fn(),
    uploadAvatar: (...args: unknown[]) => uploadAvatar(...args),
    deleteAvatar: (...args: unknown[]) => deleteAvatar(...args),
  }
})

vi.mock('@/api/auth', async () => {
  const actual = await vi.importActual<typeof import('@/api/auth')>('@/api/auth')
  return {
    ...actual,
    getSignInMethods: () =>
      Promise.resolve({ passkeys: 1, googleLinked: false, googleEmail: null, magicLinkEnabled: true }),
    disconnectGoogle: vi.fn(),
    startGoogleLink: vi.fn(),
  }
})

vi.mock('@/lib/webauthn', () => ({
  isPasskeySupported: () => true,
  isPasskeyCancellation: () => false,
  startPasskeyRegistration: vi.fn(),
}))

const { AccountPage } = await import('./AccountPage')

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
  await setupI18n('es')
  // jsdom has no object-URL support; the preview path needs one.
  if (typeof URL.createObjectURL !== 'function') {
    Object.defineProperty(URL, 'createObjectURL', { value: () => 'blob:preview', writable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: () => {}, writable: true })
  }
})

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(cleanup)

describe('AccountPage avatar block', () => {
  it('states the privacy consequence and previews before uploading', async () => {
    renderIn(<AccountPage />)
    await waitFor(() => expect(document.getElementById('account-avatar-file')).toBeTruthy())

    expect(screen.getByText(/ubicación donde fue tomada/i)).toBeTruthy()

    const upload = screen.getByRole('button', { name: /subir foto/i }) as HTMLButtonElement
    expect(upload.disabled, 'nothing chosen yet').toBe(true)

    const input = document.getElementById('account-avatar-file') as HTMLInputElement
    const file = new File(['x'], 'me.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() => expect(screen.getByText(/todavía no se subió/i)).toBeTruthy())
    expect((screen.getByRole('button', { name: /subir foto/i }) as HTMLButtonElement).disabled).toBe(
      false,
    )
    expect(uploadAvatar).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /subir foto/i }))
    await waitFor(() => expect(uploadAvatar).toHaveBeenCalledWith(file))
  })

  it('refuses a file type ffmpeg cannot decode', async () => {
    renderIn(<AccountPage />)
    await waitFor(() => expect(document.getElementById('account-avatar-file')).toBeTruthy())

    const input = document.getElementById('account-avatar-file') as HTMLInputElement
    const heic = new File(['x'], 'IMG_0001.HEIC', { type: 'image/heic' })
    fireEvent.change(input, { target: { files: [heic] } })

    expect(screen.getByText(/ese formato no se puede usar/i)).toBeTruthy()
    expect(uploadAvatar).not.toHaveBeenCalled()
  })
})
