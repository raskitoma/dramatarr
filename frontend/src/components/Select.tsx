import { forwardRef, type SelectHTMLAttributes } from 'react'

import { cx } from './cx'
import styles from './Select.module.css'

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  invalid?: boolean
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { invalid = false, className, children, ...rest },
  ref,
) {
  return (
    <span className={styles.wrapper}>
      <select
        {...rest}
        ref={ref}
        aria-invalid={invalid || rest['aria-invalid']}
        className={cx(styles.select, invalid && styles.invalid, className)}
      >
        {children}
      </select>
      <span className={styles.chevron} aria-hidden="true" />
    </span>
  )
})

export default Select
