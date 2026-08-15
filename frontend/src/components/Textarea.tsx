import { forwardRef, type TextareaHTMLAttributes } from 'react'

import { cx } from './cx'
import styles from './Textarea.module.css'

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { invalid = false, className, rows = 4, ...rest },
  ref,
) {
  return (
    <textarea
      {...rest}
      ref={ref}
      rows={rows}
      aria-invalid={invalid || rest['aria-invalid']}
      className={cx(styles.textarea, invalid && styles.invalid, className)}
    />
  )
})

export default Textarea
