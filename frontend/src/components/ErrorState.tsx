import { useTranslation } from 'react-i18next'

import { ApiRequestError } from '@/api/client'

import { Button } from './Button'
import { cx } from './cx'
import styles from './ErrorState.module.css'

export interface ErrorStateProps {
  error: unknown
  onRetry?: () => void
  className?: string
}

/**
 * The one place an API failure becomes a sentence.
 *
 * Backend error codes are stable SCREAMING_SNAKE strings, so `errors.<CODE>` is looked up
 * directly and anything unmapped degrades to the generic message — a new backend code can
 * never surface a raw English server string in a Spanish UI.
 */
export function ErrorState({ error, onRetry, className }: ErrorStateProps) {
  const { t, i18n } = useTranslation()

  let message = t('errors.generic')
  /* Only shown when we had no translation for the code — then it is the one clue the
   * household admin can act on. A mapped code would just be noise under its own sentence. */
  let unmappedCode: string | null = null

  if (error instanceof ApiRequestError) {
    if (error.code === 'NETWORK_ERROR') {
      message = t('errors.network')
    } else {
      const key = `errors.${error.code}`
      if (i18n.exists(key)) {
        message = t(key)
      } else {
        unmappedCode = error.code
      }
    }
  } else if (error instanceof TypeError) {
    // `fetch` rejects with a TypeError when the request never reached the server.
    message = t('errors.network')
  }

  return (
    <div className={cx(styles.error, className)} role="alert">
      <svg
        className={styles.icon}
        viewBox="0 0 24 24"
        width="20"
        height="20"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7.5v5.5M12 16.5v.01" />
      </svg>
      <div className={styles.body}>
        <p className={styles.message}>
          {message}
          {unmappedCode ? <code className={styles.code}>{unmappedCode}</code> : null}
        </p>
        {onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            {t('common.retry')}
          </Button>
        ) : null}
      </div>
    </div>
  )
}

export default ErrorState
