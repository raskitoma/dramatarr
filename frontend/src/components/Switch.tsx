import { useId, type ChangeEvent } from 'react'

import { cx } from './cx'
import styles from './Switch.module.css'

export interface SwitchProps {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  hint?: string
  disabled?: boolean
  className?: string
}

/**
 * A labelled on/off control.
 *
 * Built on a real `<input type="checkbox" role="switch">` inside its `<label>`: the browser
 * gives us the accessible name, the checked state, Space to toggle and click-on-label for
 * free, and the visual track is a sibling driven by `:checked` / `:focus-visible`.
 */
export function Switch({
  checked,
  onChange,
  label,
  hint,
  disabled = false,
  className,
}: SwitchProps) {
  const labelId = useId()
  const hintId = useId()

  const handleChange = (event: ChangeEvent<HTMLInputElement>): void => {
    onChange(event.target.checked)
  }

  return (
    <label className={cx(styles.root, disabled && styles.disabled, className)}>
      <input
        type="checkbox"
        role="switch"
        className={styles.input}
        checked={checked}
        disabled={disabled}
        onChange={handleChange}
        // The hint lives inside the <label>, so name it explicitly — otherwise the hint text
        // would be concatenated into the switch's accessible name instead of its description.
        aria-labelledby={labelId}
        aria-describedby={hint ? hintId : undefined}
      />
      <span className={styles.track} aria-hidden="true">
        <span className={styles.thumb} />
      </span>
      <span className={styles.text}>
        <span id={labelId} className={styles.label}>
          {label}
        </span>
        {hint ? (
          <span id={hintId} className={styles.hint}>
            {hint}
          </span>
        ) : null}
      </span>
    </label>
  )
}

export default Switch
