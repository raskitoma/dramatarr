/** The signed-in user's own profile, credentials and sessions.
 *
 * Split from `library.ts` because the cache lifetimes have nothing in common: a passkey list
 * changes only when the user acts on this screen, while watch state churns constantly. Keeping
 * a separate key root also stops `invalidateWatchState` from refetching sessions every time an
 * episode is marked watched.
 */

import { api } from './client'
import type { AuthSessionInfo, Passkey, User } from './types'

export const accountKeys = {
  all: ['account'] as const,
  passkeys: () => [...accountKeys.all, 'passkeys'] as const,
  sessions: () => [...accountKeys.all, 'sessions'] as const,
} as const

/** Exactly the fields `PATCH /me` accepts (§5). */
export type ProfilePatch = Partial<
  Pick<
    User,
    | 'displayName'
    | 'locale'
    | 'theme'
    | 'preferredAudioLang'
    | 'preferredSubtitleLang'
    | 'autoplayNext'
  >
>

export function getProfile(): Promise<User> {
  return api.get<User>('/me')
}

export function updateProfile(patch: ProfilePatch): Promise<User> {
  return api.patch<User>('/me', patch)
}

// --- Profile picture ------------------------------------------------------------------------

/**
 * Uploads a new profile picture and returns the updated profile.
 *
 * The server resizes the image and re-encodes it, which drops every EXIF block along the way —
 * including the GPS tags a phone writes into a photo. The account screen says so before the
 * file is chosen, because "my avatar told the household where I live" is not a surprise anyone
 * should have to discover afterwards.
 */
export function uploadAvatar(file: File): Promise<User> {
  const form = new FormData()
  form.append('file', file, file.name)
  return api.upload<User>('/me/avatar', form)
}

/** Removes the picture; the initials-on-colour fallback takes over again. */
export function deleteAvatar(): Promise<User> {
  return api.delete<User>('/me/avatar')
}

export function listSessions(): Promise<AuthSessionInfo[]> {
  return api.get<AuthSessionInfo[]>('/me/sessions')
}

export function revokeSession(id: string): Promise<void> {
  return api.delete<void>(`/me/sessions/${encodeURIComponent(id)}`)
}

export function listPasskeys(): Promise<Passkey[]> {
  return api.get<Passkey[]>('/auth/passkeys')
}

export function renamePasskey(id: string, nickname: string): Promise<Passkey> {
  return api.patch<Passkey>(`/auth/passkeys/${encodeURIComponent(id)}`, { nickname })
}

export function deletePasskey(id: string): Promise<void> {
  return api.delete<void>(`/auth/passkeys/${encodeURIComponent(id)}`)
}
