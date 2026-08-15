/** Theme state.
 *
 * `auto` deliberately removes the attribute rather than resolving it to light/dark, so the
 * page keeps following the OS if the user changes it while the tab is open.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import type { ThemePreference } from '@/api/types'

const STORAGE_KEY = 'dramatarr.theme'

interface ThemeContextValue {
  theme: ThemePreference
  /** What is actually on screen right now, with `auto` resolved against the OS. */
  resolved: 'light' | 'dark'
  setTheme: (theme: ThemePreference) => void
  /**
   * Seed the theme from the signed-in profile, for a device that has never chosen one.
   * A no-op once this device has a stored choice — see `adoptServerPreference` below.
   */
  adoptServerPreference: (theme: ThemePreference) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

/** Null when this device has never expressed a preference, which is what makes adoption safe. */
function readStoredRaw(): ThemePreference | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    if (value === 'light' || value === 'dark' || value === 'auto') return value
  } catch {
    // Storage unavailable.
  }
  return null
}

function readStored(): ThemePreference {
  return readStoredRaw() ?? 'auto'
}

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches
}

function apply(theme: ThemePreference): void {
  const root = document.documentElement
  if (theme === 'auto') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemePreference>(readStored)
  const [systemDark, setSystemDark] = useState(systemPrefersDark)

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches)
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])

  useEffect(() => {
    apply(theme)
  }, [theme])

  const setTheme = useCallback((next: ThemePreference) => {
    setThemeState(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Preference simply will not survive a reload.
    }
  }, [])

  /**
   * Adopt the profile's theme on a device that has never picked one.
   *
   * The stored value wins whenever it exists, including when it is an explicit `auto` — that
   * is a choice too, and overriding it from the server would make "follow my system" fail to
   * stick. Adoption persists, so the pre-paint script in index.html applies it on the next
   * load and there is no flash.
   */
  const adoptServerPreference = useCallback(
    (next: ThemePreference) => {
      if (readStoredRaw() !== null) return
      setTheme(next)
    },
    [setTheme],
  )

  const value = useMemo<ThemeContextValue>(
    () => ({
      theme,
      resolved: theme === 'auto' ? (systemDark ? 'dark' : 'light') : theme,
      setTheme,
      adoptServerPreference,
    }),
    [theme, systemDark, setTheme, adoptServerPreference],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('useTheme must be used inside ThemeProvider')
  return context
}
