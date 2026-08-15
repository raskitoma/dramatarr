import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import { ApiRequestError } from './api/client'
import { AuthProvider } from './hooks/useAuth'
import { ThemeProvider } from './hooks/useTheme'
import { setupI18n } from './i18n'
import './theme/base.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
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
