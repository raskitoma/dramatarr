import { useTranslation } from 'react-i18next'

import { cx } from './cx'
import styles from './Spinner.module.css'

export type SpinnerSize = 'sm' | 'md' | 'lg'

export interface SpinnerProps {
  size?: SpinnerSize
  /**
   * Accessible label, announced through a `role="status"` wrapper.
   * Defaults to the generic "loading" string. Pass `''` when the spinner sits inside a
   * control that already announces its own busy state (see Button) — an extra label there
   * would leak into that control's accessible name.
   */
  label?: string
  className?: string
}

export function Spinner({ size = 'md', label, className }: SpinnerProps) {
  const { t } = useTranslation()
  const text = label ?? t('common.loading')

  if (text === '') {
    return <span className={cx(styles.spinner, styles[size], className)} aria-hidden="true" />
  }

  return (
    <span className={cx(styles.root, className)} role="status">
      <span className={cx(styles.spinner, styles[size])} aria-hidden="true" />
      <span className="sr-only">{text}</span>
    </span>
  )
}

export default Spinner
