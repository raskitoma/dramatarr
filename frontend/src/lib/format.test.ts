import type { TFunction } from 'i18next'
import { describe, expect, it } from 'vitest'

import {
  formatBytes,
  formatDate,
  formatDuration,
  formatPercent,
  formatRelativeTime,
  formatTimecode,
  initialsOf,
} from './format'

/** i18next is not initialised in tests, so every helper falls back to the `es` locale. */

/**
 * Intl unit abbreviations vary by locale and ICU version (`min` vs `m`, thin vs regular
 * space), so duration assertions check structure rather than an exact byte sequence.
 */
function compact(value: string): string {
  return value.replace(/\s+/gu, '')
}

const HOUR = 3600
const MINUTE = 60

describe('formatDuration', () => {
  it('renders zero as seconds rather than an empty string', () => {
    expect(compact(formatDuration(0))).toMatch(/^0s$/u)
  })

  it('renders sub-minute values in seconds only', () => {
    const result = compact(formatDuration(45))
    expect(result).toMatch(/^45s$/u)
  })

  it('renders minutes and seconds under an hour', () => {
    const result = compact(formatDuration(24 * MINUTE + 10))
    expect(result).toMatch(/^24m(in)?10s$/u)
  })

  it('drops the seconds when they are zero', () => {
    expect(compact(formatDuration(24 * MINUTE))).toMatch(/^24m(in)?$/u)
  })

  it('renders exactly one hour without a trailing zero-minute part', () => {
    expect(compact(formatDuration(HOUR))).toMatch(/^1h$/u)
  })

  it('renders hours and minutes, never seconds, above an hour', () => {
    const result = compact(formatDuration(HOUR + 24 * MINUTE + 30))
    expect(result).toMatch(/^1h24m(in)?$/u)
  })

  it('keeps counting hours past a day instead of rolling over', () => {
    expect(compact(formatDuration(25 * HOUR))).toMatch(/^25h$/u)
  })

  it('rounds to the nearest second', () => {
    expect(compact(formatDuration(59.6))).toMatch(/^1m(in)?$/u)
  })

  it('treats NaN, Infinity and negatives as zero', () => {
    for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, -90]) {
      expect(compact(formatDuration(bad))).toMatch(/^0s$/u)
    }
  })
})

describe('formatTimecode', () => {
  it('formats zero', () => {
    expect(formatTimecode(0)).toBe('0:00')
  })

  it('pads seconds under ten', () => {
    expect(formatTimecode(5)).toBe('0:05')
    expect(formatTimecode(65)).toBe('1:05')
  })

  it('omits the hour segment under an hour', () => {
    expect(formatTimecode(4 * MINUTE + 5)).toBe('4:05')
    expect(formatTimecode(59 * MINUTE + 59)).toBe('59:59')
  })

  it('adds the hour segment and zero-pads minutes at exactly one hour', () => {
    expect(formatTimecode(HOUR)).toBe('1:00:00')
    expect(formatTimecode(HOUR + 5)).toBe('1:00:05')
    expect(formatTimecode(HOUR + 24 * MINUTE + 5)).toBe('1:24:05')
  })

  it('keeps accumulating hours past a day', () => {
    expect(formatTimecode(25 * HOUR + MINUTE + 1)).toBe('25:01:01')
    expect(formatTimecode(100 * HOUR)).toBe('100:00:00')
  })

  it('truncates rather than rounds, so the clock never jumps ahead of the video', () => {
    expect(formatTimecode(59.9)).toBe('0:59')
  })

  it('never produces NaN for a missing or nonsensical duration', () => {
    for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, -1]) {
      expect(formatTimecode(bad)).toBe('0:00')
      expect(formatTimecode(bad)).not.toMatch(/NaN/u)
    }
  })
})

describe('formatBytes', () => {
  it('formats zero in bytes', () => {
    expect(compact(formatBytes(0))).toMatch(/^0/u)
  })

  it('stays in bytes below the first step', () => {
    expect(compact(formatBytes(512))).toMatch(/^512/u)
  })

  it('steps up through the decimal units', () => {
    expect(compact(formatBytes(1500))).toMatch(/^1[.,]5kB$/u)
    expect(compact(formatBytes(2_400_000))).toMatch(/^2[.,]4MB$/u)
    expect(compact(formatBytes(5_000_000_000))).toMatch(/^5GB$/u)
    expect(compact(formatBytes(3_000_000_000_000))).toMatch(/^3TB$/u)
  })

  it('drops the fraction once the value has two digits', () => {
    expect(compact(formatBytes(45_600_000))).toMatch(/^46MB$/u)
  })

  it('clamps at the largest unit instead of running off the end of the table', () => {
    expect(compact(formatBytes(9e21))).toMatch(/PB$/u)
  })

  it('treats NaN and negatives as zero', () => {
    expect(compact(formatBytes(Number.NaN))).toMatch(/^0/u)
    expect(compact(formatBytes(-1))).toMatch(/^0/u)
  })
})

describe('formatDate', () => {
  it('formats an ISO timestamp in the requested locale', () => {
    expect(formatDate('2024-03-05T12:00:00Z', 'en')).toContain('2024')
    expect(formatDate('2024-03-05T12:00:00Z', 'es')).toContain('2024')
  })

  it('produces different output for different locales', () => {
    expect(formatDate('2024-03-05T12:00:00Z', 'en')).not.toBe(
      formatDate('2024-03-05T12:00:00Z', 'es'),
    )
  })

  it('returns an empty string for an unparseable value', () => {
    expect(formatDate('not-a-date', 'es')).toBe('')
    expect(formatDate('', 'es')).toBe('')
  })

  it('falls back to a default locale rather than throwing on an empty locale', () => {
    expect(formatDate('2024-03-05T12:00:00Z', '')).toContain('2024')
  })
})

describe('formatRelativeTime', () => {
  /** Echoes the key and count so assertions read as "which bucket did it pick". */
  const t = ((key: string, options?: { count?: number }) =>
    options?.count === undefined ? key : `${key}:${options.count}`) as unknown as TFunction

  function ago(seconds: number): string {
    return formatRelativeTime(new Date(Date.now() - seconds * 1000).toISOString(), t)
  }

  it('uses the just-now bucket under a minute', () => {
    expect(ago(0)).toBe('time.justNow')
    expect(ago(59)).toBe('time.justNow')
  })

  it('switches to minutes at exactly one minute', () => {
    expect(ago(60)).toBe('time.minutesAgo:1')
    expect(ago(59 * MINUTE)).toBe('time.minutesAgo:59')
  })

  it('switches to hours at exactly one hour', () => {
    expect(ago(HOUR)).toBe('time.hoursAgo:1')
    expect(ago(23 * HOUR)).toBe('time.hoursAgo:23')
  })

  it('switches to days at exactly one day', () => {
    expect(ago(24 * HOUR)).toBe('time.daysAgo:1')
    expect(ago(29 * 24 * HOUR)).toBe('time.daysAgo:29')
  })

  it('falls back to an absolute date past a month', () => {
    const result = ago(200 * 24 * HOUR)
    expect(result).not.toContain('time.')
    expect(result).not.toBe('')
  })

  it('reads a future timestamp as just now instead of a negative count', () => {
    const future = new Date(Date.now() + 10 * MINUTE * 1000).toISOString()
    expect(formatRelativeTime(future, t)).toBe('time.justNow')
  })

  it('returns an empty string for an unparseable value', () => {
    expect(formatRelativeTime('nonsense', t)).toBe('')
  })
})

describe('formatPercent', () => {
  it('formats the ends of the range', () => {
    expect(compact(formatPercent(0))).toBe('0%')
    expect(compact(formatPercent(100))).toBe('100%')
  })

  it('formats a mid value without decimals', () => {
    expect(compact(formatPercent(42.4))).toBe('42%')
  })

  it('clamps out-of-range values', () => {
    expect(compact(formatPercent(150))).toBe('100%')
    expect(compact(formatPercent(-20))).toBe('0%')
  })

  it('treats NaN as zero', () => {
    expect(compact(formatPercent(Number.NaN))).toBe('0%')
    expect(compact(formatPercent(Number.POSITIVE_INFINITY))).toBe('0%')
  })
})

describe('initialsOf', () => {
  it('takes the first letter of the first and last words', () => {
    expect(initialsOf('Ana María Pérez', 'ana@example.com')).toBe('AP')
  })

  it('takes a single letter from a one-word name', () => {
    expect(initialsOf('Ana', 'ana@example.com')).toBe('A')
  })

  it('falls back to the email when there is no display name', () => {
    expect(initialsOf('   ', 'zoe@example.com')).toBe('Z')
  })

  /** The reason this helper is shared: `charAt(0)` splits these in half. */
  it('slices by code point, not UTF-16 unit', () => {
    // U+20BB7, an ideograph outside the BMP: one code point, two UTF-16 units.
    expect(initialsOf('\u{20BB7}\u{20BB7}', 'x@example.com')).toBe('\u{20BB7}')
    expect(initialsOf('迷情 记事', 'x@example.com')).toBe('迷记')
  })

  it('returns an empty string when there is nothing to work with', () => {
    expect(initialsOf('', '')).toBe('')
  })
})
