/** Locale-aware formatting helpers.
 *
 * Everything user-visible that is a number, a date or a duration goes through here so the
 * app never hand-rolls a string that only reads correctly in one language. The active locale
 * is read from the live i18next instance rather than passed in, because these are called from
 * deep inside render trees where threading a locale through would be pure noise; `formatDate`
 * is the exception since callers there already have it.
 *
 * Every entry point is total: garbage in (NaN, Infinity, negatives, unparseable ISO strings)
 * produces a sane string, never "NaN:NaN".
 */

import i18next, { type TFunction } from 'i18next'

const FALLBACK_LOCALE = 'es'

/** Seconds thresholds for the relative-time buckets, in the order they are tested. */
const SECONDS_PER_MINUTE = 60
const MINUTES_PER_HOUR = 60
const HOURS_PER_DAY = 24
/** Past this, an absolute date is more useful than "43 days ago". */
const RELATIVE_DAY_LIMIT = 30

/** Decimal, not binary: the unit names Intl prints (kB, MB, GB) are the decimal ones. */
const BYTE_STEP = 1000
const BYTE_UNITS = [
  'byte',
  'kilobyte',
  'megabyte',
  'gigabyte',
  'terabyte',
  'petabyte',
] as const

function activeLocale(): string {
  return i18next.language || FALLBACK_LOCALE
}

// Intl constructors are comparatively expensive and these run inside lists, so memoize them.
const numberFormatters = new Map<string, Intl.NumberFormat>()
const dateFormatters = new Map<string, Intl.DateTimeFormat>()

function numberFormatter(locale: string, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `${locale}|${JSON.stringify(options)}`
  const cached = numberFormatters.get(key)
  if (cached) return cached
  const created = new Intl.NumberFormat(locale, options)
  numberFormatters.set(key, created)
  return created
}

function dateFormatter(locale: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${locale}|${JSON.stringify(options)}`
  const cached = dateFormatters.get(key)
  if (cached) return cached
  const created = new Intl.DateTimeFormat(locale, options)
  dateFormatters.set(key, created)
  return created
}

/** NaN, Infinity, null-ish and negative inputs all collapse to zero. */
function safeSeconds(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 0
}

function unitPart(locale: string, value: number, unit: 'hour' | 'minute' | 'second'): string {
  return numberFormatter(locale, {
    style: 'unit',
    unit,
    unitDisplay: 'narrow',
    maximumFractionDigits: 0,
  }).format(value)
}

function pad2(value: number): string {
  return value < 10 ? `0${value}` : String(value)
}

/**
 * Human duration: `1h 24min` / `24min 10s` / `45s`.
 *
 * Two units at most — a third is noise at the sizes this app deals with. The exact unit
 * abbreviations come from Intl, so they differ per locale (es says `min`, en says `m`).
 */
export function formatDuration(seconds: number): string {
  const locale = activeLocale()
  const total = Math.round(safeSeconds(seconds))
  const hours = Math.floor(total / (SECONDS_PER_MINUTE * MINUTES_PER_HOUR))
  const minutes = Math.floor((total % (SECONDS_PER_MINUTE * MINUTES_PER_HOUR)) / SECONDS_PER_MINUTE)
  const rest = total % SECONDS_PER_MINUTE

  const parts: string[] = []
  if (hours > 0) {
    parts.push(unitPart(locale, hours, 'hour'))
    if (minutes > 0) parts.push(unitPart(locale, minutes, 'minute'))
  } else if (minutes > 0) {
    parts.push(unitPart(locale, minutes, 'minute'))
    if (rest > 0) parts.push(unitPart(locale, rest, 'second'))
  } else {
    parts.push(unitPart(locale, rest, 'second'))
  }
  return parts.join(' ')
}

/**
 * Player timecode: `4:05` under an hour, `1:24:05` over it, `25:01:01` past a day.
 *
 * Deliberately not localized — a timecode next to a seek bar is a fixed notation, and the
 * player calls this on every `timeupdate`, so it stays allocation-cheap.
 */
export function formatTimecode(seconds: number): string {
  const total = Math.floor(safeSeconds(seconds))
  const hours = Math.floor(total / (SECONDS_PER_MINUTE * MINUTES_PER_HOUR))
  const minutes = Math.floor((total % (SECONDS_PER_MINUTE * MINUTES_PER_HOUR)) / SECONDS_PER_MINUTE)
  const rest = total % SECONDS_PER_MINUTE
  if (hours > 0) return `${hours}:${pad2(minutes)}:${pad2(rest)}`
  return `${minutes}:${pad2(rest)}`
}

/** File / disk size: `0 byte`, `1.5GB`. Decimal steps so the Intl unit names stay honest. */
export function formatBytes(bytes: number): string {
  const value = Number.isFinite(bytes) && bytes > 0 ? bytes : 0
  let index = 0
  let scaled = value
  while (scaled >= BYTE_STEP && index < BYTE_UNITS.length - 1) {
    scaled /= BYTE_STEP
    index += 1
  }
  // noUncheckedIndexedAccess: the loop guarantees the index is in range, but prove it anyway.
  const unit = BYTE_UNITS[index] ?? 'byte'
  return numberFormatter(activeLocale(), {
    style: 'unit',
    unit,
    unitDisplay: 'narrow',
    maximumFractionDigits: index === 0 || scaled >= 10 ? 0 : 1,
  }).format(scaled)
}

/** Medium-length absolute date. Returns an empty string for an unparseable input. */
export function formatDate(iso: string, locale: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return dateFormatter(locale || FALLBACK_LOCALE, { dateStyle: 'medium' }).format(date)
}

/**
 * "just now" / "3 minutes ago" / "2 days ago", falling back to an absolute date past a month.
 *
 * Takes `t` rather than reaching for the global instance so it re-renders with the component
 * when the language changes. Future timestamps (clock skew between the NAS and the browser)
 * read as "just now" instead of a negative count.
 */
export function formatRelativeTime(iso: string, t: TFunction): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''

  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000))
  if (seconds < SECONDS_PER_MINUTE) return t('time.justNow')

  const minutes = Math.floor(seconds / SECONDS_PER_MINUTE)
  if (minutes < MINUTES_PER_HOUR) return t('time.minutesAgo', { count: minutes })

  const hours = Math.floor(minutes / MINUTES_PER_HOUR)
  if (hours < HOURS_PER_DAY) return t('time.hoursAgo', { count: hours })

  const days = Math.floor(hours / HOURS_PER_DAY)
  if (days < RELATIVE_DAY_LIMIT) return t('time.daysAgo', { count: days })

  return formatDate(iso, activeLocale())
}

/**
 * The name of a language, in the reader's language: `ko` → «coreano» / "Korean".
 *
 * Read from `Intl.DisplayNames` rather than from the locale files on purpose — a language name
 * is data the platform already ships in every locale, and hand-translating a dozen of them
 * twice is how the two bundles drift apart. Falls back to the upper-cased code on engines with
 * no data for that pair, which is still readable.
 */
export function languageName(code: string, locale?: string): string {
  const trimmed = code.trim()
  if (trimmed === '') return ''
  try {
    return (
      new Intl.DisplayNames([locale || activeLocale()], { type: 'language' }).of(trimmed) ??
      trimmed.toUpperCase()
    )
  } catch {
    return trimmed.toUpperCase()
  }
}

/** Takes 0–100 (what the API sends), not 0–1. Out-of-range values are clamped. */
export function formatPercent(value: number): string {
  const clamped = Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0
  return numberFormatter(activeLocale(), {
    style: 'percent',
    maximumFractionDigits: 0,
  }).format(clamped / 100)
}

/**
 * Up to two letters for an avatar, from a display name falling back to an email.
 *
 * Sliced by code point, not by UTF-16 unit: `name.charAt(0)` cuts a CJK ideograph outside the
 * BMP — or any emoji — in half and renders a replacement glyph. In a library built around
 * multilingual titles and household members who may have non-Latin names, that is a visible
 * bug rather than a theoretical one.
 */
export function initialsOf(name: string, fallback: string): string {
  const source = name.trim() || fallback.trim()
  const words = source.split(/\s+/u).filter(Boolean)
  const first = words[0]
  const last = words.length > 1 ? words[words.length - 1] : undefined
  const head = first ? (Array.from(first)[0] ?? '') : ''
  const tail = last ? (Array.from(last)[0] ?? '') : ''
  return (head + tail).toLocaleUpperCase()
}

/**
 * Whether a series' declared episode count can be shown as the total it claims to be.
 *
 * It usually can. But this library is vertical short drama, which TMDB does not carry, so a
 * match is often to a *different* show with a similar name — and the live catalogue proved it:
 * two series were bound to TMDB 324289, which declares **one** episode, while their folders
 * hold sixty-six each. The screen printed "66 de 1 disponibles", which is not a rounding
 * problem or a scan that has not finished. It is the application repeating a wrong claim with
 * a straight face.
 *
 * So the rule is the only one that survives a bad match: a declared total below what is
 * already on the NAS is not a total. Say how many there are and stop, and — where an admin can
 * act on it — say that the metadata disagrees, because the fix is to re-match the series, not
 * to edit the number.
 */
export function declaredIsPlausible(
  available: number,
  declared: number | null | undefined,
): declared is number {
  return typeof declared === 'number' && declared >= available
}
