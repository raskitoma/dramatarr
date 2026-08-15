/** Language picker.
 *
 * Switches i18next immediately, remembers the choice locally, and — when there is a session —
 * writes it to the profile so the same person gets their language on another device and in
 * the emails the server sends them.
 */

import { useMutation } from '@tanstack/react-query'
import type { ChangeEvent } from 'react'
import { useTranslation } from 'react-i18next'

import { api } from '@/api/client'
import { useAuth } from '@/hooks/useAuth'
import { LOCALE_LABELS, SUPPORTED_LOCALES, persistLocale, type Locale } from '@/i18n'

import { Select } from '@/components/Select'

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

  const persist = useMutation({
    mutationFn: (next: Locale) => api.patch<unknown>('/me', { locale: next }),
  })

  function handleChange(event: ChangeEvent<HTMLSelectElement>): void {
    const next = event.target.value
    if (!isSupported(next)) return

    void i18n.changeLanguage(next)
    persistLocale(next)

    if (!user) return
    patchUser({ locale: next })
    // Losing this write only costs cross-device sync; the local switch already happened.
    persist.mutate(next)
  }

  return (
    <Select aria-label={t('locale.label')} value={baseLocale(i18n.language)} onChange={handleChange}>
      {SUPPORTED_LOCALES.map((locale) => (
        <option key={locale} value={locale}>
          {LOCALE_LABELS[locale]}
        </option>
      ))}
    </Select>
  )
}
