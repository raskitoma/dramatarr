/** Route gate: signed-in required, optionally admin.
 *
 * A missing session redirects; insufficient role does not. Bouncing a signed-in viewer away
 * from `/admin` would look like a broken link, so they get an explicit "not for you" screen
 * with the header still attached so they can navigate somewhere useful.
 */

import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, Redirect, useLocation, useSearch } from 'wouter'

import { AppHeader } from '@/components/AppHeader'
import { AppLayout, PageLoader } from '@/components/AppLayout'
import { EmptyState } from '@/components/EmptyState'
import { useAuth } from '@/hooks/useAuth'

interface ProtectedRouteProps {
  children: ReactNode
  adminOnly?: boolean
}

export function ProtectedRoute({ children, adminOnly = false }: ProtectedRouteProps) {
  const { user, isAdmin, isLoading } = useAuth()
  const { t } = useTranslation()
  const [location] = useLocation()
  const search = useSearch()

  // App already gates on this, but a direct mount (tests, future entry points) must not
  // read a not-yet-loaded session as "signed out" and redirect.
  if (isLoading) return <PageLoader />

  // Carry the destination through the sign-in. Someone opening a shared link to an episode
  // should land on that episode once they are in, not on the home page wondering where it
  // went. LoginPage already reads `?next=` and re-sanitizes it before navigating.
  if (!user) {
    const target = search ? `${location}?${search}` : location
    const next = target === '/' ? '' : `?next=${encodeURIComponent(target)}`
    return <Redirect to={`/login${next}`} replace />
  }

  if (adminOnly && !isAdmin) {
    return (
      <>
        <AppHeader />
        <AppLayout>
          <EmptyState
            title={t('errors.ADMIN_REQUIRED')}
            description={t('errors.FORBIDDEN')}
            action={<Link href="/">{t('nav.home')}</Link>}
          />
        </AppLayout>
      </>
    )
  }

  return <>{children}</>
}
