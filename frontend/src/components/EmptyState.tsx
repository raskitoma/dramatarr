import type { ReactNode } from 'react'

import { cx } from './cx'
import styles from './EmptyState.module.css'

export interface EmptyStateProps {
  title: string
  description?: string
  /** Usually a single Button. */
  action?: ReactNode
  icon?: ReactNode
  className?: string
}

export function EmptyState({ title, description, action, icon, className }: EmptyStateProps) {
  return (
    <div className={cx(styles.empty, className)}>
      {icon ? (
        <div className={styles.icon} aria-hidden="true">
          {icon}
        </div>
      ) : null}
      <p className={styles.title}>{title}</p>
      {description ? <p className={styles.description}>{description}</p> : null}
      {action ? <div className={styles.action}>{action}</div> : null}
    </div>
  )
}

export default EmptyState
