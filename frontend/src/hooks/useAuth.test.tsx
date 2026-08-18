// @vitest-environment jsdom
/**
 * What signing out leaves behind.
 *
 * The report was "al cerrar sesión se queda en la última pantalla". The cause was
 * `queryClient.clear()`: it does not *empty* a query, it destroys it, and destroying one
 * orphans the mounted observer — which then renders its last result indefinitely because
 * nothing notifies it and nothing makes it re-create the query. The cache genuinely held
 * `{ user: null }` while the component went on showing the signed-in user, so
 * `ProtectedRoute` never redirected and `/login` bounced straight back out because it saw a
 * user too.
 *
 * These assertions are about the **cache**, not about a re-render, and that is deliberate
 * rather than lazy: a probe in this file established that under jsdom + `act`, plain
 * `setQueryData` does not flush a notification to a mounted `useQuery` either. A rendering
 * assertion here would pass or fail for reasons that have nothing to do with this code. The
 * server half is pinned properly in `backend/tests/test_logout.py`.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const post = vi.fn()
const get = vi.fn()

vi.mock('@/api/client', () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))

const { AuthProvider, useAuth } = await import('./useAuth')

const USER = { id: 'u1', email: 'a@b.c', role: 'admin', locale: 'es' }

/** Hands the live context out so a test can call `signOut` directly. */
function Probe({ onReady }: { onReady: (value: ReturnType<typeof useAuth>) => void }) {
  onReady(useAuth())
  return null
}

async function mount(): Promise<{ client: QueryClient; signOut: () => Promise<void> }> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  let captured: ReturnType<typeof useAuth> | null = null
  const tree: ReactNode = (
    <QueryClientProvider client={client}>
      <AuthProvider>
        <Probe
          onReady={(value) => {
            captured = value
          }}
        />
      </AuthProvider>
    </QueryClientProvider>
  )
  await act(async () => {
    render(tree)
  })
  const value = captured as ReturnType<typeof useAuth> | null
  if (value === null) throw new Error('the auth context never rendered')
  return { client, signOut: value.signOut }
}

beforeEach(() => {
  vi.clearAllMocks()
  get.mockImplementation((path: string) => {
    if (path === '/auth/session') return Promise.resolve({ user: USER })
    if (path === '/config') return Promise.resolve({})
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
  post.mockResolvedValue({ ok: true })
})

afterEach(cleanup)

describe('signOut', () => {
  it('leaves the session answered as nobody rather than removed', async () => {
    // The distinction is the entire bug. A *removed* session query looks identical to a
    // cleared one from the cache's point of view and is invisible to every consumer.
    const { client, signOut } = await mount()
    expect(client.getQueryData(['session'])).toEqual({ user: USER })

    get.mockImplementation((path: string) =>
      path === '/auth/session' ? Promise.resolve({ user: null }) : Promise.resolve({}),
    )
    await act(async () => {
      await signOut()
    })

    expect(client.getQueryData(['session'])).toEqual({ user: null })
    expect(post).toHaveBeenCalledWith('/auth/logout')
  })

  it('drops the previous member data without touching the session or the config', async () => {
    // Another person in the house may sign in on this device; their first paint must not be
    // somebody else's library. The two auth-shaped keys have to survive, because destroying
    // them is what caused the original defect.
    const { client, signOut } = await mount()
    client.setQueryData(['library', 'series', 'list', {}], [{ id: 's1' }])
    client.setQueryData(['admin', 'users'], [{ id: 'u9' }])

    get.mockImplementation((path: string) =>
      path === '/auth/session' ? Promise.resolve({ user: null }) : Promise.resolve({}),
    )
    await act(async () => {
      await signOut()
    })

    expect(client.getQueryData(['library', 'series', 'list', {}])).toBeUndefined()
    expect(client.getQueryData(['admin', 'users'])).toBeUndefined()
    expect(client.getQueryData(['session'])).toEqual({ user: null })
    expect(client.getQueryState(['public-config'])).not.toBeUndefined()
  })

  it('still signs out when the logout request fails', async () => {
    // The likeliest moment to press this button is when the cookie has already expired, which
    // is exactly when the request 401s. Rethrowing left the caller's `await` rejected, so the
    // navigation to /login on the next line never ran.
    const { client, signOut } = await mount()
    post.mockRejectedValue(new Error('401'))
    get.mockImplementation((path: string) =>
      path === '/auth/session' ? Promise.resolve({ user: null }) : Promise.resolve({}),
    )

    await act(async () => {
      await expect(signOut()).resolves.toBeUndefined()
    })

    expect(client.getQueryData(['session'])).toEqual({ user: null })
  })
})
