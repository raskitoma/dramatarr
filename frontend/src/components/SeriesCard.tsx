/** A poster tile linking to a series. Used by the browse grid, the home rows and search. */

import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import type { SeriesSummary } from '@/api/types'
import { Poster } from '@/components/Poster'

import styles from './SeriesCard.module.css'

interface SeriesCardProps {
  series: SeriesSummary
  /** A secondary line under the title — search uses it to explain a cross-language match. */
  note?: string
  size?: 'sm' | 'md' | 'lg'
}

export function SeriesCard({ series, note, size = 'md' }: SeriesCardProps) {
  const { t } = useTranslation()

  const fullyWatched =
    series.availableEpisodeCount > 0 && series.watchedEpisodeCount >= series.availableEpisodeCount

  return (
    <article className={styles.card}>
      {/* One link wraps the tile. Poster carries the title as its label — that is also what
          feeds its placeholder glyph — so the visible copy below is hidden from assistive tech
          to keep the link's accessible name from saying the title twice. */}
      <Link href={`/series/${encodeURIComponent(series.slug)}`} className={styles.link}>
        <Poster
          src={series.posterUrl}
          alt={series.title}
          size={size}
          progressPercent={series.progressPercent}
          watched={fullyWatched}
          fluid
        />
        <span className={styles.title} aria-hidden="true">
          {series.title}
        </span>
        <span className={styles.meta}>
          {series.year !== null ? <span>{series.year}</span> : null}
          <span>{t('series.availableCount', { count: series.availableEpisodeCount })}</span>
        </span>
        {note ? <span className={styles.note}>{note}</span> : null}
      </Link>
    </article>
  )
}
