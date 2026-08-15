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
  const percent =
    typeof progressPercent === 'number' && Number.isFinite(progressPercent)
      ? Math.min(100, Math.max(0, progressPercent))
      : 0

  return (
    <div className={cx(styles.poster, styles[size], fluid && styles.fluid, className)}>
      {src === null ? (
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
          src={src}
          alt={alt}
          loading="lazy"
          decoding="async"
          draggable={false}
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
