/** WebAuthn ceremonies, wrapped so pages never touch the raw browser API.
 *
 * Every ceremony is the same three steps: ask the server for options, let the browser prompt
 * the user, hand the result back to the server. The two API calls already throw
 * `ApiRequestError`; the browser step throws `DOMException`s (wrapped by SimpleWebAuthn),
 * which are translated here into that same shape with a stable `code`. A caller therefore has
 * exactly one error type to reason about, and one place to look up a localized message.
 *
 * `PASSKEY_CANCELLED` is not a failure. Dismissing the system prompt is a normal thing to do,
 * and the UI must say so calmly rather than showing an error state.
 */

import { browserSupportsWebAuthn, startAuthentication, startRegistration } from '@simplewebauthn/browser'

import { ApiRequestError, api } from '@/api/client'
import type { Passkey, User } from '@/api/types'

/** Derived from the library's own signatures so this file never guesses an exported type name. */
type RegistrationOptionsJSON = Parameters<typeof startRegistration>[0]['optionsJSON']
type AuthenticationOptionsJSON = Parameters<typeof startAuthentication>[0]['optionsJSON']

/** The server wraps options exactly as `navigator.credentials.*` wants to receive them. */
interface RegistrationOptionsResponse {
  publicKey: RegistrationOptionsJSON
}

interface AuthenticationOptionsResponse {
  publicKey: AuthenticationOptionsJSON
}

/** `SessionOut`. The user is always present on a completed sign-in. */
interface SessionResponse {
  user: User
}

export type PasskeyErrorCode =
  | 'PASSKEY_CANCELLED'
  | 'PASSKEY_ALREADY_REGISTERED'
  | 'WEBAUTHN_FAILED'

/**
 * A browser-side ceremony failure, shaped like an API error.
 *
 * Extending `ApiRequestError` means shared UI that switches on `code` (ErrorState) handles
 * these without a second code path. The status is 0 because no HTTP request failed.
 */
export class PasskeyError extends ApiRequestError {
  declare readonly code: PasskeyErrorCode

  constructor(code: PasskeyErrorCode, message: string, cause?: unknown) {
    super(0, { code, message, details: {} })
    this.name = 'PasskeyError'
    this.cause = cause
  }
}

/** True when the user dismissed the system prompt — expected, not an error. */
export function isPasskeyCancellation(error: unknown): boolean {
  return error instanceof ApiRequestError && error.code === 'PASSKEY_CANCELLED'
}

export function isPasskeySupported(): boolean {
  if (typeof window === 'undefined') return false
  if (typeof window.PublicKeyCredential !== 'function') return false
  return browserSupportsWebAuthn()
}

/**
 * Recover the underlying DOMException name.
 *
 * SimpleWebAuthn raises its own `WebAuthnError` and keeps the original exception in `cause`,
 * so the interesting name can be at either level depending on the version and the failure.
 */
function exceptionName(error: unknown): string {
  if (typeof error !== 'object' || error === null) return ''
  const candidate = error as { name?: unknown; code?: unknown; cause?: unknown }

  const cause = candidate.cause
  if (typeof cause === 'object' && cause !== null) {
    const causeName = (cause as { name?: unknown }).name
    if (typeof causeName === 'string' && causeName !== '') return causeName
  }

  // SimpleWebAuthn's own vocabulary, used when it swallows the original exception.
  if (candidate.code === 'ERROR_CEREMONY_ABORTED') return 'NotAllowedError'
  if (candidate.code === 'ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED') return 'InvalidStateError'

  return typeof candidate.name === 'string' ? candidate.name : ''
}

function toPasskeyError(error: unknown): ApiRequestError {
  // A failed begin/complete call is already the right shape; only the browser step is mapped.
  if (error instanceof ApiRequestError) return error

  const message = error instanceof Error ? error.message : String(error)
  switch (exceptionName(error)) {
    case 'NotAllowedError':
      return new PasskeyError('PASSKEY_CANCELLED', message, error)
    case 'InvalidStateError':
      return new PasskeyError('PASSKEY_ALREADY_REGISTERED', message, error)
    default:
      return new PasskeyError('WEBAUTHN_FAILED', message, error)
  }
}

async function ceremony<T>(run: () => Promise<T>): Promise<T> {
  try {
    return await run()
  } catch (error) {
    throw toPasskeyError(error)
  }
}

/**
 * Sign in with a discoverable credential: no email, no account hint. The authenticator picks
 * the account, which is why the login page needs nothing but a button.
 */
export async function startPasskeyLogin(): Promise<User> {
  const options = await api.post<AuthenticationOptionsResponse>('/auth/passkey/login/begin')
  const credential = await ceremony(() => startAuthentication({ optionsJSON: options.publicKey }))
  const session = await api.post<SessionResponse>('/auth/passkey/login/complete', { credential })
  return session.user
}

/** Add another passkey to the account that is already signed in. */
export async function startPasskeyRegistration(nickname?: string): Promise<Passkey> {
  const options = await api.post<RegistrationOptionsResponse>('/auth/passkey/register/begin')
  const credential = await ceremony(() => startRegistration({ optionsJSON: options.publicKey }))
  return api.post<Passkey>('/auth/passkey/register/complete', {
    credential,
    nickname: nickname?.trim() || null,
  })
}

/**
 * Enroll a brand-new account from an invite. The server creates the user, consumes the invite
 * and opens the session in one step, so the caller only has to refresh its own session state.
 */
export async function startInviteRegistration(
  token: string,
  displayName: string,
  locale?: string,
): Promise<User> {
  const base = `/auth/invite/${encodeURIComponent(token)}`
  const options = await api.post<RegistrationOptionsResponse>(`${base}/begin`)
  const credential = await ceremony(() => startRegistration({ optionsJSON: options.publicKey }))
  const session = await api.post<SessionResponse>(`${base}/complete`, {
    credential,
    displayName: displayName.trim() || null,
    locale: locale ?? null,
  })
  return session.user
}
