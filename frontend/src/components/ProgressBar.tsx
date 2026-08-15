import { useTranslation } from 'react-i18next'

import { cx } from './cx'
import styles from './ProgressBar.module.css'

export interface ProgressBarProps {
  /** 0–100. Values outside the range are clamped rather than overflowing the track. */
  percent: number
  tone?: 'accent' | 'success'
  size?: 'sm' | 'md'
  /** Accessible name. Defaults to the generic "progress" string. */
  label?: string
  className?: string
}

export function ProgressBar({
  percent,
  tone = 'accent',
  size = 'md',
  label,
  className,
}: ProgressBarProps) {
  const { t } = useTranslation()
  const safe = Number.isFinite(percent) ? percent : 0
  const clamped = Math.min(100, Math.max(0, safe))
  const rounded = Math.round(clamped)

  return (
    <div
      className={cx(styles.track, styles[tone], styles[size], className)}
      role="progressbar"
      aria-valuenow={rounded}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label ?? t('common.progress')}
    >
      <div className={styles.fill} style={{ inlineSize: `${clamped}%` }} />
    </div>
  )
}

export default ProgressBar
