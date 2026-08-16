/** The flags the language picker is drawn with.
 *
 * Inline SVG rather than the flag emoji, which would have been three characters instead of this
 * whole file. Emoji flags are the one emoji range that simply does not render on Windows — it
 * ships no glyphs for them, and the picker would fall back to showing the letter pairs `ES` and
 * `GB`. A household control that works on the phone and breaks on the laptop is worse than one
 * that costs a few paths.
 *
 * A flag is a country and a language is not, so this mapping is a convention, not a fact:
 * Spanish is drawn with Spain and English with the United Kingdom because those are the ones
 * people read fastest. Worth knowing that dramatarr's own Spanish is Rioplatense — if the
 * household would rather see Argentina or Uruguay here, it is this file and nothing else.
 *
 * Both are simplified: no coat of arms on Spain, and the Union Jack's saltire is drawn
 * symmetrical rather than counterchanged. At 20 pixels wide neither difference is visible.
 */

import type { Locale } from '@/i18n'

export interface LocaleFlagProps {
  locale: Locale
  className?: string
}

export function LocaleFlag({ locale, className }: LocaleFlagProps) {
  // Decorative in every current use: the control that holds it carries the language name as
  // its accessible name, so announcing "flag" here would only repeat it.
  const common = {
    className,
    viewBox: '0 0 60 42',
    width: 20,
    height: 14,
    role: 'presentation' as const,
    'aria-hidden': true,
  }

  if (locale === 'en') {
    return (
      <svg {...common}>
        <defs>
          <clipPath id="flag-en-clip">
            <rect width="60" height="42" rx="2" />
          </clipPath>
        </defs>
        <g clipPath="url(#flag-en-clip)">
          <rect width="60" height="42" fill="#012169" />
          {/* Diagonals first, wide in white and narrow in red on top of it. */}
          <path d="M0 0 60 42M60 0 0 42" stroke="#fff" strokeWidth="8.4" />
          <path d="M0 0 60 42M60 0 0 42" stroke="#c8102e" strokeWidth="4.2" />
          {/* Then the upright cross, which in the real flag covers the diagonals. */}
          <path d="M30 0V42M0 21H60" stroke="#fff" strokeWidth="14" />
          <path d="M30 0V42M0 21H60" stroke="#c8102e" strokeWidth="8.4" />
        </g>
        <rect
          width="59"
          height="41"
          x="0.5"
          y="0.5"
          rx="2"
          fill="none"
          stroke="currentColor"
          strokeOpacity="0.25"
        />
      </svg>
    )
  }

  return (
    <svg {...common}>
      <defs>
        <clipPath id="flag-es-clip">
          <rect width="60" height="42" rx="2" />
        </clipPath>
      </defs>
      <g clipPath="url(#flag-es-clip)">
        <rect width="60" height="42" fill="#aa151b" />
        {/* The yellow band is half the height, not a third. */}
        <rect width="60" height="21" y="10.5" fill="#f1bf00" />
      </g>
      <rect
        width="59"
        height="41"
        x="0.5"
        y="0.5"
        rx="2"
        fill="none"
        stroke="currentColor"
        strokeOpacity="0.25"
      />
    </svg>
  )
}

export default LocaleFlag
