/** Three-way appearance control: auto / light / dark.
 *
 * Modelled as a radio group rather than three buttons because "auto" is a peer choice, not
 * the absence of a choice — the group has exactly one selected value at all times. Roving
 * tabindex keeps it a single tab stop; arrow keys move between options, as ARIA expects.
 *
 * The preference is written to the profile when there is a session so it follows the user to
 * their phone; the local (localStorage) value applies immediately either way.
 */

import { useMutation } from '@tanstack/react-query'
import { useRef, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'

import { api } from '@/api/client'
import type { ThemePreference } from '@/api/types'
import { useAuth } from '@/hooks/useAuth'
import { useTheme } from '@/hooks/useTheme'

import styles from './ThemeToggle.module.css'

const THEME_OPTIONS = ['auto', 'light', 'dark'] as const satisfies readonly ThemePreference[]

function ThemeIcon({ option }: { option: ThemePreference }) {
  const common = {
    className: styles.icon,
    viewBox: '0 0 16 16',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.5,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  }

  if (option === 'light') {
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="3.1" />
        <path d="M8 1.4v1.4M8 13.2v1.4M1.4 8h1.4M13.2 8h1.4M3.3 3.3l1 1M11.7 11.7l1 1M12.7 3.3l-1 1M4.3 11.7l-1 1" />
      </svg>
    )
  }

  if (option === 'dark') {
    return (
      <svg {...common}>
        <path d="M13.4 9.6A5.8 5.8 0 0 1 6.4 2.6a5.8 5.8 0 1 0 7 7Z" />
      </svg>
    )
  }

  // Auto: a disc split down the middle, the usual "follows the system" mark.
  return (
    <svg {...common}>
      <circle cx="8" cy="8" r="6" />
      <path d="M8 2a6 6 0 0 1 0 12Z" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function ThemeToggle() {
  const { t } = useTranslation()
  const { theme, setTheme } = useTheme()
  const { user, patchUser } = useAuth()
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([])

  const persist = useMutation({
    mutationFn: (next: ThemePreference) => api.patch<unknown>('/me', { theme: next }),
  })

  function select(next: ThemePreference): void {
    setTheme(next)
    if (!user) return
    // Reflect it in the cached profile straight away; the account page reads the same value.
    // A failed PATCH only costs cross-device sync, so it is not worth a toast here.
    patchUser({ theme: next })
    persist.mutate(next)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const count = THEME_OPTIONS.length
    const current = THEME_OPTIONS.indexOf(theme)
    let target: number
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        target = (current + 1) % count
        break
      case 'ArrowLeft':
      case 'ArrowUp':
        target = (current - 1 + count) % count
        break
      case 'Home':
        target = 0
        break
      case 'End':
        target = count - 1
        break
      default:
        return
    }
    event.preventDefault()
    const next = THEME_OPTIONS[target]
    if (!next) return
    select(next)
    optionRefs.current[target]?.focus()
  }

  return (
    <div
      role="radiogroup"
      aria-label={t('theme.label')}
      className={styles.group}
      onKeyDown={handleKeyDown}
    >
      {THEME_OPTIONS.map((option, index) => {
        const selected = option === theme
        const label = t(`theme.${option}`)
        return (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={selected}
            // The only name this control has now that the words are gone. `title` puts the
            // same string in the tooltip, so a glyph nobody recognizes is still one hover from
            // explaining itself.
            aria-label={label}
            title={label}
            // Only the selected option is in the tab order; arrows reach the others.
            tabIndex={selected ? 0 : -1}
            className={styles.option}
            ref={(element) => {
              optionRefs.current[index] = element
            }}
            onClick={() => {
              select(option)
            }}
          >
            <ThemeIcon option={option} />
          </button>
        )
      })}
    </div>
  )
}
