/** The landing point of an emailed sign-in link.
 *
 * The mail carries `/login?token=…`, so LoginPage renders this component when it sees a
 * token; it also stands alone on its own route, reading the token from the query itself.
 *
 * The token is single-use, so the exchange must happen exactly once. Running it through
 * TanStack Query keyed by the token gives that for free: StrictMode's double mount is
 * de-duplicated, and nothing refetches a token that has already been spent.
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { useLocation, useSearch } from 'wouter'

import { ApiRequestError, api } from '@/api/client'
import type { User } from '@/api/types'
import { Button } from '@/components/Button'
import { EmptyState } from '@/components/EmptyState'
import { Spinner } from '@/components/Spinner'
import { useAuth } from '@/hooks/useAuth'

import styles from './LoginPage.module.css'

interface SessionResponse {
  user: User
}

interface MagicLinkPageProps {
  /** Passed by LoginPage when the token arrived on `/login?token=…`. */
  token?: string
  /** Where to land after signing in. Already sanitized by the caller. */
  next?: string
}

function queryParam(search: string, key: string): string | null {
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
  return params.get(key)
}

function safeInternalPath(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith('/')) return '/'
  if (raw.startsWith('//') || raw.startsWith('/\\')) return '/'
  return raw
}

/**
 * "Expired" and "already used" are the same story to the reader: this link is spent, ask for
 * a fresh one. Anything else is a real fault and gets the generic code mapping.
 */
function messageKey(error: unknown): string {
  if (error instanceof ApiRequestError) {
    switch (error.code) {
      case 'MAGIC_LINK_EXPIRED':
      case 'MAGIC_LINK_USED':
      case 'MAGIC_LINK_INVALID':
        return 'auth.magicLinkExpired'
      default:
        return `errors.${error.code}`
    }
  }
  return 'errors.generic'
}

export function MagicLinkPage({ token: tokenProp, next: nextProp }: MagicLinkPageProps) {
  const { t } = useTranslation()
  const { refresh } = useAuth()
  const [, navigate] = useLocation()
  const search = useSearch()

  const token = tokenProp ?? queryParam(search, 'token') ?? ''
  const next = safeInternalPath(nextProp ?? queryParam(search, 'next'))

  const consume = useQuery({
    queryKey: ['magic-link', token],
    queryFn: () => api.post<SessionResponse>('/auth/magic-link/consume', { token }),
    enabled: token !== '',
    retry: false,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const signedIn = consume.isSuccess

  useEffect(() => {
    if (!signedIn) return
    void (async () => {
      await refresh()
      navigate(next, { replace: true })
    })()
  }, [signedIn, refresh, navigate, next])

  const failed = token === '' || consume.isError
  const key = token === '' ? 'errors.MAGIC_LINK_INVALID' : messageKey(consume.error)

  return (
    <main className={styles.page}>
      <div className={styles.card}>
        {failed ? (
          <EmptyState
            title={t(key, { defaultValue: t('errors.generic') })}
            action={
              <Button variant="primary" onClick={() => navigate('/login', { replace: true })}>
                {t('auth.signIn')}
              </Button>
            }
          />
        ) : (
          <div className={styles.status} role="status" aria-live="polite">
            <Spinner size="lg" label={t('common.loading')} />
            <p className={styles.statusText}>{t('common.loading')}</p>
          </div>
        )}
      </div>
    </main>
  )
}
