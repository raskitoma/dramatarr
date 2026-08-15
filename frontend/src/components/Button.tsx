import { forwardRef, type ButtonHTMLAttributes, type MouseEvent } from 'react'

import { Spinner } from './Spinner'
import { cx } from './cx'
import styles from './Button.module.css'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  /** Swaps the label for a spinner without changing the button's width. */
  loading?: boolean
  /** Square button holding a single icon. Pair with IconButton so it gets a label. */
  iconOnly?: boolean
  full?: boolean
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = 'secondary',
    size = 'md',
    loading = false,
    iconOnly = false,
    full = false,
    type = 'button',
    disabled = false,
    className,
    children,
    onClick,
    ...rest
  },
  ref,
) {
  // While loading the button must not act, but taking the native `disabled` attribute away
  // from a focused element moves focus to <body> mid-interaction. `aria-disabled` plus a
  // swallowed click keeps the keyboard user exactly where they were.
  const handleClick = (event: MouseEvent<HTMLButtonElement>): void => {
    if (loading) {
      event.preventDefault()
      event.stopPropagation()
      return
    }
    onClick?.(event)
  }

  return (
    <button
      {...rest}
      ref={ref}
      type={type}
      disabled={disabled}
      aria-disabled={loading || disabled ? true : undefined}
      aria-busy={loading || undefined}
      onClick={handleClick}
      className={cx(
        styles.button,
        styles[variant],
        styles[size],
        iconOnly && styles.iconOnly,
        full && styles.full,
        loading && styles.loading,
        className,
      )}
    >
      <span className={styles.content}>{children}</span>
      {loading ? (
        <span className={styles.spinnerSlot}>
          {/* Empty label on purpose: aria-busy already reports the state, and a nested
              status string would rewrite the button's accessible name. */}
          <Spinner size={size === 'sm' ? 'sm' : 'md'} label="" />
        </span>
      ) : null}
    </button>
  )
})

export default Button
