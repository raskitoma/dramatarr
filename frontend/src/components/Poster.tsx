import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ProgressBar } from './ProgressBar'
import { cx } from './cx'
import { CheckIcon } from './icons'
import styles from './Poster.module.css'

export type PosterSize = 'sm' | 'md' | 'lg'

export interface PosterProps {
  src: string | null
  /** The title this poster stands for. Also feeds the placeholder glyph. */
  alt: string
  size?: PosterSize
  /** 0–100. Renders a thin bar pinned to the bottom edge. */
  progressPercent?: number
  watched?: boolean
  /** Let the parent grid dictate the width instead of the size preset. */
  fluid?: boolean
  className?: string
}

/**
 * First user-perceived character of a title.
 *
 * `str[0]` would split a surrogate pair and `Array.from` still splits grapheme clusters, so
 * segment properly where the engine can — otherwise "𝒜" or a combining-mark title renders as
 * a replacement box. CJK titles need no uppercasing but it is a no-op for them.
 */
function firstGlyph(text: string): string {
  const trimmed = text.trim()
  if (trimmed === '') return ''
  if (typeof Intl.Segmenter === 'function') {
    const segments = new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(trimmed)
    const first = segments[Symbol.iterator]().next()
    if (!first.done) return first.value.segment.toLocaleUpperCase()
  }
  return (Array.from(trimmed)[0] ?? '').toLocaleUpperCase()
}

export function Poster({
  src,
  alt,
  size = 'md',
  progressPercent,
  watched = false,
  fluid = false,
  className,
}: PosterProps) {
  const { t } = useTranslation()
  /**
   * A URL that fails to load falls back to the same placeholder as no URL at all.
   *
   * Without this the placeholder only ever covered `src === null` — "we know there is no
   * poster" — and left the other case, "there is an address and nothing behind it", rendering
   * the browser's broken-image glyph with the alt text spilled beside it. That is what a
   * series shows after its image cache is wiped, after a restore that missed `DATA_PATH`, or
   * while a freshly promoted request is still being fetched, and it looks like the app is
   * broken rather than like a picture is missing.
   *
   * Keyed by `src` so that changing the poster retries rather than staying stuck on the
   * failure of the previous one.
   */
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const usable = src !== null && failedSrc !== src ? src : null

  const percent =
    typeof progressPercent === 'number' && Number.isFinite(progressPercent)
      ? Math.min(100, Math.max(0, progressPercent))
      : 0

  return (
    <div className={cx(styles.poster, styles[size], fluid && styles.fluid, className)}>
      {usable === null ? (
        <>
          <div className={styles.placeholder} aria-hidden="true">
            {firstGlyph(alt)}
          </div>
          {/* The image would have carried the title; keep it available without one. */}
          <span className="sr-only">{alt}</span>
        </>
      ) : (
        <img
          className={styles.image}
          src={usable}
          alt={alt}
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => setFailedSrc(usable)}
        />
      )}

      {watched ? (
        <span className={styles.watched} title={t('series.watched')}>
          {/* Heavier stroke than the set's default: at 14px on artwork the 1.8 weight
              disappears against a busy poster. */}
          <CheckIcon className={styles.check} strokeWidth={3.5} />
          <span className="sr-only">{t('series.watched')}</span>
        </span>
      ) : null}

      {percent > 0 ? (
        <div className={styles.progress}>
          <ProgressBar
            percent={percent}
            size="sm"
            tone={watched ? 'success' : 'accent'}
            label={t('series.percentWatched', { percent: Math.round(percent) })}
          />
        </div>
      ) : null}
    </div>
  )
}

export default Poster
