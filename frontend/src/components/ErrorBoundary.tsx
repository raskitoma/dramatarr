/** Last line of defence for render-time crashes.
 *
 * Sits outside the router so a throw in any page still leaves a usable screen instead of a
 * blank document. Retry just clears the boundary: React re-mounts the subtree, which is
 * enough for a transient failure and harmless for a permanent one (it simply throws again).
 */

import { Component, type ErrorInfo, type ReactNode } from 'react'

import { AppLayout } from '@/components/AppLayout'
import { ErrorState } from '@/components/ErrorState'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  error: unknown
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error }
  }

  override componentDidCatch(error: unknown, info: ErrorInfo): void {
    // Nothing collects client logs in a self-hosted install, so the console is the only sink.
    console.error('dramatarr: unhandled UI error', error, info.componentStack)
  }

  private readonly handleRetry = (): void => {
    this.setState({ error: null })
  }

  override render(): ReactNode {
    if (this.state.error !== null) {
      return (
        <AppLayout>
          <ErrorState error={this.state.error} onRetry={this.handleRetry} />
        </AppLayout>
      )
    }
    return this.props.children
  }
}
