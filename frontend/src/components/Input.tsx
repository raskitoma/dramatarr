import { forwardRef, type InputHTMLAttributes } from 'react'

import { cx } from './cx'
import styles from './Input.module.css'

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { invalid = false, className, ...rest },
  ref,
) {
  return (
    <input
      {...rest}
      ref={ref}
      aria-invalid={invalid || rest['aria-invalid']}
      className={cx(styles.input, invalid && styles.invalid, className)}
    />
  )
})

export default Input
