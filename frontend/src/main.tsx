import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import { ApiRequestError } from './api/client'
import { AuthProvider } from './hooks/useAuth'
import { ThemeProvider } from './hooks/useTheme'
import { setupI18n } from './i18n'
import { registerServiceWorker } from './offline/register'
import './theme/base.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      /* The one line that makes the app work on a plane.
       *
       * TanStack's default is `networkMode: 'online'`, which *pauses* a query when the browser
       * reports no connection: it never fires, `isLoading` stays false and `data` stays
       * undefined. `useAuth` then reads that as "no user" and `ProtectedRoute` redirects to the
       * login page — so a fully downloaded library would have been unreachable behind a login
       * form that cannot submit. With 'always' the request is actually made, the service worker
       * answers it from its cache, and the session is real. A genuine failure still surfaces
       * through the existing error states. */
      networkMode: 'always',
      retry: (failureCount, error) => {
        // Auth and permission failures are terminal; retrying only delays the redirect.
        if (error instanceof ApiRequestError && error.status >= 400 && error.status < 500) {
          return false
        }
        return failureCount < 2
      },
    },
  },
})

async function bootstrap(): Promise<void> {
  await setupI18n()
  registerServiceWorker()

  const container = document.getElementById('root')
  if (!container) throw new Error('#root is missing from index.html')

  createRoot(container).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </StrictMode>,
  )
}

void bootstrap()
