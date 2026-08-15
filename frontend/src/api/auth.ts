/** Sign-in methods, and the Google half of them.
 *
 * Shared by the login screen (unauthenticated) and the account screen (authenticated), which
 * is why it is its own module rather than living in `account.ts` next to the profile calls.
 *
 * Google is deliberately *not* a fetch flow. The start endpoints only mint the state and hand
 * back the authorization URL; the browser itself has to travel to Google, so the caller
 * performs a real navigation. Nothing token-shaped ever passes through this file: the callback
 * lands back on the API, which sets the session cookie and redirects — and on failure sends
 * the browser to `/login?error=CODE`, which the login screen localizes.
 */

import { api } from './client'
import type { SignInMethods } from './types'

export const authKeys = {
  all: ['auth'] as const,
  methods: () => [...authKeys.all, 'methods'] as const,
} as const

/** `GoogleAuthUrlOut` — an absolute Google URL with state, nonce and PKCE challenge in it. */
interface GoogleAuthUrl {
  authorizationUrl: string
}

export interface GoogleLoginOptions {
  /** Same-origin path to land on afterwards. Re-validated server-side. */
  next?: string
  /** Raw invite token, when the person is accepting an invitation instead of signing in. */
  invite?: string
}

export function getSignInMethods(): Promise<SignInMethods> {
  return api.get<SignInMethods>('/auth/methods')
}

/**
 * Asks the API where to send the browser, then sends it.
 *
 * Resolves only if the navigation somehow does not happen — callers can treat a resolved
 * promise as "still here", and a rejection as an ordinary API error worth showing.
 */
export async function startGoogleSignIn(options: GoogleLoginOptions = {}): Promise<void> {
  const { authorizationUrl } = await api.get<GoogleAuthUrl>('/auth/google/start', {
    next: options.next,
    invite: options.invite,
  })
  window.location.assign(authorizationUrl)
}

/**
 * Attaches Google to the account of the session making the call.
 *
 * A separate endpoint from the login start, and a POST, because it acts on behalf of a signed-in
 * user rather than merely describing where to go next.
 */
export async function startGoogleLink(next?: string): Promise<void> {
  const { authorizationUrl } = await api.post<GoogleAuthUrl>(
    '/auth/google/link/start',
    undefined,
    { next },
  )
  window.location.assign(authorizationUrl)
}

/** Detaches Google. Refuses with `LAST_SIGN_IN_METHOD` when it is the only way into the account. */
export function disconnectGoogle(): Promise<void> {
  return api.delete<void>('/auth/google/link')
}
