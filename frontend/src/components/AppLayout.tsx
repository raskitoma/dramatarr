/** The page container every route renders into.
 *
 * It owns the single `#main` landmark the skip link targets, so exactly one exists per screen
 * and pages never have to remember to declare it.
 */

import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { Spinner } from '@/components/Spinner'

import styles from './AppLayout.module.css'

interface AppLayoutProps {
  children: ReactNode
  /** Opts out of the max-width and gutters — the player wants the whole viewport. */
  bleed?: boolean
}

export function AppLayout({ children, bleed = false }: AppLayoutProps) {
  return (
    // tabIndex={-1} makes the element programmatically focusable so "skip to content"
    // actually moves the keyboard caret, not just the scroll position.
    <main id="main" tabIndex={-1} className={bleed ? styles.bleed : styles.main}>
      {children}
    </main>
  )
}

/** Centered busy state for route-level waits: session bootstrap and lazy chunk loads. */
export function PageLoader() {
  const { t } = useTranslation()
  return (
    <div className={styles.loader} role="status" aria-live="polite">
      <Spinner size="lg" label={t('common.loading')} />
    </div>
  )
}
