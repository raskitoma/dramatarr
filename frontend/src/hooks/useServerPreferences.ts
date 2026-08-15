/** Reads the signed-in profile's appearance preferences back onto this device.
 *
 * `ThemeToggle` and `LocaleSelect` write `theme` and `locale` to the profile so they follow
 * the user to another device — but nothing was applying them on arrival, which made the
 * round trip pointless. This closes it.
 *
 * The rule in both directions is the same: a choice made *on this device* always wins. The
 * profile value is a seed for a device that has never expressed one, never an override. Both
 * owners of the relevant localStorage key (`useTheme`, `i18n`) expose that test themselves
 * rather than having this module reach for their keys.
 *
 * It runs once per signed-in user. Re-adopting on every session refetch would fight anyone
 * who changed the setting in this tab, since the local write and the profile write are not
 * simultaneous.
 */

import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'

import { useAuth } from '@/hooks/useAuth'
import { useTheme } from '@/hooks/useTheme'
import { SUPPORTED_LOCALES, hasStoredLocale, persistLocale, type Locale } from '@/i18n'

function isSupportedLocale(value: string): value is Locale {
  return (SUPPORTED_LOCALES as readonly string[]).includes(value)
}

export function useServerPreferences(): void {
  const { user } = useAuth()
  const { adoptServerPreference } = useTheme()
  const { i18n } = useTranslation()
  const adoptedFor = useRef<string | null>(null)

  useEffect(() => {
    if (!user) {
      // Let the next sign-in adopt again: it may be a different person on a shared device.
      adoptedFor.current = null
      return
    }
    if (adoptedFor.current === user.id) return
    adoptedFor.current = user.id

    adoptServerPreference(user.theme)

    const locale = user.locale.split('-')[0] ?? ''
    if (!hasStoredLocale() && isSupportedLocale(locale) && i18n.language !== locale) {
      void i18n.changeLanguage(locale)
      persistLocale(locale)
    }
  }, [user, adoptServerPreference, i18n])
}
