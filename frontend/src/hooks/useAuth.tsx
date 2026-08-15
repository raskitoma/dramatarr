/** Session state, shared by the whole app.
 *
 * The session lives in a httpOnly cookie, so "am I signed in?" is answered by asking the
 * server, never by reading a token. `GET /api/auth/session` is specified never to 401, which
 * is what lets this run unconditionally on mount without a redirect loop.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react'

import { api } from '@/api/client'
import type { PublicConfig, User } from '@/api/types'

interface SessionResponse {
  user: User | null
}

interface AuthContextValue {
  user: User | null
  config: PublicConfig | null
  isLoading: boolean
  isAdmin: boolean
  refresh: () => Promise<void>
  signOut: () => Promise<void>
  /** Applied locally after a profile PATCH so the UI updates without a refetch. */
  patchUser: (patch: Partial<User>) => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  const sessionQuery = useQuery({
    queryKey: ['session'],
    queryFn: () => api.get<SessionResponse>('/auth/session'),
    staleTime: 60_000,
    retry: false,
  })

  const configQuery = useQuery({
    queryKey: ['public-config'],
    queryFn: () => api.get<PublicConfig>('/config'),
    staleTime: 5 * 60_000,
    retry: false,
  })

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ['session'] })
  }, [queryClient])

  const signOut = useCallback(async () => {
    try {
      await api.post('/auth/logout')
    } finally {
      // Clear everything: another household member may sign in on this same device, and
      // stale cached library/progress data would briefly show them the previous user's state.
      queryClient.clear()
      await queryClient.invalidateQueries({ queryKey: ['session'] })
    }
  }, [queryClient])

  const patchUser = useCallback(
    (patch: Partial<User>) => {
      queryClient.setQueryData<SessionResponse>(['session'], (previous) =>
        previous?.user ? { user: { ...previous.user, ...patch } } : previous,
      )
    },
    [queryClient],
  )

  const user = sessionQuery.data?.user ?? null

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      config: configQuery.data ?? null,
      isLoading: sessionQuery.isLoading || configQuery.isLoading,
      isAdmin: user?.role === 'admin',
      refresh,
      signOut,
      patchUser,
    }),
    [user, configQuery.data, sessionQuery.isLoading, configQuery.isLoading, refresh, signOut, patchUser],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
