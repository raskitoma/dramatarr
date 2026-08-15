/** i18n bootstrap.
 *
 * `es` is the source of truth: every key must exist there first, and `en` mirrors it. A test
 * asserts the two key sets are identical, so a missing translation fails CI rather than
 * silently rendering a raw key in the UI.
 */

import i18next from 'i18next'
import { initReactI18next } from 'react-i18next'

import en from './en.json'
import es from './es.json'

export const SUPPORTED_LOCALES = ['es', 'en'] as const
export type Locale = (typeof SUPPORTED_LOCALES)[number]

export const LOCALE_LABELS: Record<Locale, string> = {
  es: 'Español',
  en: 'English',
}

const STORAGE_KEY = 'dramatarr.locale'

export function detectLocale(fallback = 'es'): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored && (SUPPORTED_LOCALES as readonly string[]).includes(stored)) return stored as Locale
  } catch {
    // Storage unavailable.
  }
  for (const candidate of navigator.languages ?? []) {
    const base = candidate.split('-')[0]
    if (base && (SUPPORTED_LOCALES as readonly string[]).includes(base)) return base as Locale
  }
  return (SUPPORTED_LOCALES as readonly string[]).includes(fallback) ? (fallback as Locale) : 'es'
}

/**
 * True when this device has already chosen a language.
 *
 * The signed-in profile's `locale` is only adopted when this returns false — a choice made
 * here must not be overwritten by what the account happened to be set to elsewhere.
 */
export function hasStoredLocale(): boolean {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return Boolean(stored) && (SUPPORTED_LOCALES as readonly string[]).includes(stored ?? '')
  } catch {
    return false
  }
}

export function persistLocale(locale: Locale): void {
  try {
    localStorage.setItem(STORAGE_KEY, locale)
  } catch {
    // Preference will not survive a reload.
  }
  document.documentElement.setAttribute('lang', locale)
}

export async function setupI18n(initialLocale?: string): Promise<typeof i18next> {
  const locale = (initialLocale as Locale) ?? detectLocale()
  await i18next.use(initReactI18next).init({
    resources: {
      es: { translation: es },
      en: { translation: en },
    },
    lng: locale,
    fallbackLng: 'es',
    interpolation: { escapeValue: false },
    returnNull: false,
  })
  document.documentElement.setAttribute('lang', locale)
  return i18next
}

export default i18next
