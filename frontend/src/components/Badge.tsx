import type { HTMLAttributes, ReactNode } from 'react'

import { cx } from './cx'
import styles from './Badge.module.css'

export type BadgeTone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'info'

export interface BadgeProps extends Omit<HTMLAttributes<HTMLSpanElement>, 'children'> {
  tone?: BadgeTone
  children: ReactNode
}

export function Badge({ tone = 'neutral', className, children, ...rest }: BadgeProps) {
  return (
    <span {...rest} className={cx(styles.badge, styles[tone], className)}>
      {children}
    </span>
  )
}

export default Badge
