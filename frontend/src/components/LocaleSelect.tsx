/** Language picker, drawn as flags.
 *
 * Switches i18next immediately, remembers the choice locally, and — when there is a session —
 * writes it to the profile so the same person gets their language on another device and in
 * the emails the server sends them.
 *
 * A radio group rather than a `<select>`, for one reason that decided it: a native `<option>`
 * renders text and nothing else, so a select could only have shown flags as emoji — the one
 * emoji range Windows ships no glyphs for. Modelled on `ThemeToggle`, which sits beside it in
 * the header and is the same shape of control: a small fixed set, exactly one of them chosen,
 * one tab stop with arrow keys inside.
 */

import { useMutation } from '@tanstack/react-query'
import { useRef, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'

import { api } from '@/api/client'
import { useAuth } from '@/hooks/useAuth'
import { LOCALE_LABELS, SUPPORTED_LOCALES, persistLocale, type Locale } from '@/i18n'

import { LocaleFlag } from '@/components/LocaleFlag'

import styles from './LocaleSelect.module.css'

function isSupported(value: string): value is Locale {
  return (SUPPORTED_LOCALES as readonly string[]).includes(value)
}

/** i18next may hold a regional tag (`es-AR`); the picker only offers base languages. */
function baseLocale(language: string | undefined): Locale {
  const base = (language ?? '').split('-')[0] ?? ''
  return isSupported(base) ? base : 'es'
}

export function LocaleSelect() {
  const { t, i18n } = useTranslation()
  const { user, patchUser } = useAuth()
  const current = baseLocale(i18n.language)
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([])

  const persist = useMutation({
    mutationFn: (next: Locale) => api.patch<unknown>('/me', { locale: next }),
  })

  function select(next: Locale): void {
    if (next === current) return

    void i18n.changeLanguage(next)
    persistLocale(next)

    if (!user) return
    patchUser({ locale: next })
    // Losing this write only costs cross-device sync; the local switch already happened.
    persist.mutate(next)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const count = SUPPORTED_LOCALES.length
    const index = SUPPORTED_LOCALES.indexOf(current)
    let target: number
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        target = (index + 1) % count
        break
      case 'ArrowLeft':
      case 'ArrowUp':
        target = (index - 1 + count) % count
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
    const next = SUPPORTED_LOCALES[target]
    if (!next) return
    select(next)
    optionRefs.current[target]?.focus()
  }

  return (
    <div
      role="radiogroup"
      aria-label={t('locale.label')}
      className={styles.group}
      onKeyDown={handleKeyDown}
    >
      {SUPPORTED_LOCALES.map((locale, index) => {
        const selected = locale === current
        return (
          <button
            key={locale}
            type="button"
            role="radio"
            aria-checked={selected}
            // The language's own name is the accessible name and the tooltip. A flag alone
            // names a country, and never unambiguously names a language.
            aria-label={LOCALE_LABELS[locale]}
            title={LOCALE_LABELS[locale]}
            tabIndex={selected ? 0 : -1}
            className={styles.option}
            ref={(element) => {
              optionRefs.current[index] = element
            }}
            onClick={() => {
              select(locale)
            }}
          >
            <LocaleFlag locale={locale} className={styles.flag} />
          </button>
        )
      })}
    </div>
  )
}

export default LocaleSelect
