import { Children, cloneElement, isValidElement, type ReactNode } from 'react'

import { cx } from './cx'
import styles from './Field.module.css'

/** The subset of props Field injects into its control. All ARIA, so it is safe on any element. */
interface ControlAria {
  'aria-describedby'?: string
  'aria-invalid'?: boolean | 'true' | 'false'
  'aria-required'?: boolean
}

export interface FieldProps {
  label: string
  /** id of the control this labels. Hint/error ids are derived from it. */
  htmlFor: string
  hint?: string
  error?: string
  required?: boolean
  children: ReactNode
  className?: string
}

/**
 * Label + hint + error around a single form control.
 *
 * When `children` is exactly one element, Field wires `aria-describedby`, `aria-invalid` and
 * `aria-required` into it, so callers only have to give the control a matching `id`. Anything
 * a caller set explicitly is preserved (describedby ids are merged, not replaced).
 */
export function Field({
  label,
  htmlFor,
  hint,
  error,
  required = false,
  children,
  className,
}: FieldProps) {
  const hintId = hint ? `${htmlFor}-hint` : undefined
  const errorId = error ? `${htmlFor}-error` : undefined
  const describedBy = cx(hintId, errorId) || undefined

  let control = children
  if (Children.count(children) === 1 && isValidElement<ControlAria>(children)) {
    control = cloneElement(children, {
      'aria-describedby': cx(children.props['aria-describedby'], describedBy) || undefined,
      'aria-invalid': error ? true : children.props['aria-invalid'],
      'aria-required': required ? true : children.props['aria-required'],
    })
  }

  return (
    <div className={cx(styles.field, className)}>
      <label htmlFor={htmlFor} className={cx(styles.label, required && styles.required)}>
        {label}
      </label>
      <div className={styles.control}>{control}</div>
      {hint ? (
        <p id={hintId} className={styles.hint}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
    </div>
  )
}

export default Field
