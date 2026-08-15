/** Routing table and the chrome every page sits inside.
 *
 * Two rules shape this file. First, nothing renders until the session question is answered —
 * flashing the login page at someone who is already signed in is worse than a beat of
 * spinner. Second, the two heavy corners of the app (the player and the whole admin area)
 * are split out of the initial bundle; a viewer who only ever browses never downloads them.
 */

import { Suspense, lazy, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, Redirect, Route, Switch, useSearch } from 'wouter'

import { AppHeader } from '@/components/AppHeader'
import { AppLayout, PageLoader } from '@/components/AppLayout'
import { EmptyState } from '@/components/EmptyState'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { ProtectedRoute } from '@/components/ProtectedRoute'
import { ToastProvider } from '@/components/Toast'
import { AdminNav } from '@/components/admin/AdminNav'
import { useAuth } from '@/hooks/useAuth'
import { useServerPreferences } from '@/hooks/useServerPreferences'

import { AccountPage } from '@/pages/AccountPage'
import { BrowsePage } from '@/pages/BrowsePage'
import { HomePage } from '@/pages/HomePage'
import { InvitePage } from '@/pages/InvitePage'
import { LoginPage } from '@/pages/LoginPage'
import { RequestsPage } from '@/pages/RequestsPage'
import { SearchPage } from '@/pages/SearchPage'
import { SeriesPage } from '@/pages/SeriesPage'

const WatchPage = lazy(async () => ({ default: (await import('@/pages/WatchPage')).WatchPage }))

const AdminDashboardPage = lazy(async () => ({
  default: (await import('@/pages/admin/AdminDashboardPage')).AdminDashboardPage,
}))
const AdminQueuePage = lazy(async () => ({
  default: (await import('@/pages/admin/AdminQueuePage')).AdminQueuePage,
}))
const AdminLibraryPage = lazy(async () => ({
  default: (await import('@/pages/admin/AdminLibraryPage')).AdminLibraryPage,
}))
const AdminSeriesEditPage = lazy(async () => ({
  default: (await import('@/pages/admin/AdminSeriesEditPage')).AdminSeriesEditPage,
}))
const AdminImportPage = lazy(async () => ({
  default: (await import('@/pages/admin/AdminImportPage')).AdminImportPage,
}))
const AdminUsersPage = lazy(async () => ({
  default: (await import('@/pages/admin/AdminUsersPage')).AdminUsersPage,
}))
const AdminRequestsPage = lazy(async () => ({
  default: (await import('@/pages/admin/AdminRequestsPage')).AdminRequestsPage,
}))
const AdminSettingsPage = lazy(async () => ({
  default: (await import('@/pages/admin/AdminSettingsPage')).AdminSettingsPage,
}))

/** Header + main. Everything except the player and the two public pages renders inside it. */
function Chrome({ children }: { children: ReactNode }) {
  return (
    <>
      <AppHeader />
      <AppLayout>{children}</AppLayout>
    </>
  )
}

/**
 * Every `/admin/*` screen, with the section's own sub-navigation above it. The header links
 * only to `/admin`, so without this the users and settings screens have no inbound link at
 * all — putting it here rather than in each page means a new admin route cannot forget it.
 */
function AdminPage({ children }: { children: ReactNode }) {
  return (
    <ProtectedRoute adminOnly>
      <Chrome>
        <AdminNav />
        <Suspense fallback={<PageLoader />}>{children}</Suspense>
      </Chrome>
    </ProtectedRoute>
  )
}

/** `?next=` comes off a link anyone can craft, so only same-origin absolute paths are kept. */
function safeInternalPath(raw: string | null): string {
  if (!raw || !raw.startsWith('/')) return '/'
  if (raw.startsWith('//') || raw.startsWith('/\\')) return '/'
  return raw
}

/**
 * `/login` is also where emailed sign-in links land (`/login?token=…`), so a signed-in
 * visitor is only bounced away when there is no token to consume — otherwise switching
 * accounts on a shared device would be impossible.
 */
function LoginRoute() {
  const { user } = useAuth()
  const params = new URLSearchParams(useSearch())

  if (user && !params.has('token')) {
    return <Redirect to={safeInternalPath(params.get('next'))} replace />
  }
  return <LoginPage />
}

function NotFound() {
  const { t } = useTranslation()
  return (
    <ProtectedRoute>
      <Chrome>
        <EmptyState
          title={t('errors.NOT_FOUND')}
          action={<Link href="/">{t('nav.home')}</Link>}
        />
      </Chrome>
    </ProtectedRoute>
  )
}

function Routes() {
  const { isLoading } = useAuth()

  // Applies the profile's theme/locale on a device that has none of its own. Sits here rather
  // than in a page so it runs on the first screen the user sees, whichever one that is.
  useServerPreferences()

  // The whole app waits on this one question. `GET /auth/session` never 401s, so this
  // resolves on every load, signed in or not.
  if (isLoading) {
    return (
      <AppLayout>
        <PageLoader />
      </AppLayout>
    )
  }

  return (
    <Switch>
      {/* --- Public --- */}
      <Route path="/login">
        <LoginRoute />
      </Route>
      <Route path="/invite/:token">
        <InvitePage />
      </Route>

      {/* --- Signed in --- */}
      <Route path="/">
        <ProtectedRoute>
          <Chrome>
            <HomePage />
          </Chrome>
        </ProtectedRoute>
      </Route>
      <Route path="/browse">
        <ProtectedRoute>
          <Chrome>
            <BrowsePage />
          </Chrome>
        </ProtectedRoute>
      </Route>
      <Route path="/search">
        <ProtectedRoute>
          <Chrome>
            <SearchPage />
          </Chrome>
        </ProtectedRoute>
      </Route>
      <Route path="/series/:slug">
        <ProtectedRoute>
          <Chrome>
            <SeriesPage />
          </Chrome>
        </ProtectedRoute>
      </Route>
      <Route path="/requests">
        <ProtectedRoute>
          <Chrome>
            <RequestsPage />
          </Chrome>
        </ProtectedRoute>
      </Route>
      <Route path="/account">
        <ProtectedRoute>
          <Chrome>
            <AccountPage />
          </Chrome>
        </ProtectedRoute>
      </Route>

      {/* The player owns the viewport: no header, no gutters. */}
      <Route path="/watch/:episodeId">
        <ProtectedRoute>
          <AppLayout bleed>
            <Suspense fallback={<PageLoader />}>
              <WatchPage />
            </Suspense>
          </AppLayout>
        </ProtectedRoute>
      </Route>

      {/* --- Admin --- */}
      <Route path="/admin">
        <AdminPage>
          <AdminDashboardPage />
        </AdminPage>
      </Route>
      <Route path="/admin/queue">
        <AdminPage>
          <AdminQueuePage />
        </AdminPage>
      </Route>
      <Route path="/admin/library">
        <AdminPage>
          <AdminLibraryPage />
        </AdminPage>
      </Route>
      <Route path="/admin/library/:id">
        <AdminPage>
          <AdminSeriesEditPage />
        </AdminPage>
      </Route>
      <Route path="/admin/import">
        <AdminPage>
          <AdminImportPage />
        </AdminPage>
      </Route>
      <Route path="/admin/requests">
        <AdminPage>
          <AdminRequestsPage />
        </AdminPage>
      </Route>
      <Route path="/admin/users">
        <AdminPage>
          <AdminUsersPage />
        </AdminPage>
      </Route>
      <Route path="/admin/settings">
        <AdminPage>
          <AdminSettingsPage />
        </AdminPage>
      </Route>

      <Route>
        <NotFound />
      </Route>
    </Switch>
  )
}

export function App() {
  return (
    <ErrorBoundary>
      <ToastProvider>
        <Routes />
      </ToastProvider>
    </ErrorBoundary>
  )
}
