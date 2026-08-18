/** A poster tile linking to a series. Used by the browse grid, the home rows and search. */

import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import type { SeriesSummary } from '@/api/types'
import { Poster } from '@/components/Poster'
import { declaredIsPlausible } from '@/lib/format'

import styles from './SeriesCard.module.css'

interface SeriesCardProps {
  series: SeriesSummary
  /** A secondary line under the title — search uses it to explain a cross-language match. */
  note?: string
  size?: 'sm' | 'md' | 'lg'
}

export function SeriesCard({ series, note, size = 'md' }: SeriesCardProps) {
  const { t } = useTranslation()

  /* Finished means finished, which needs both numbers on the payload rather than the nearer
     one. Watching every file the library holds is not the same as watching the series when the
     indexer knows of one more — and that is the ordinary case here, not the exotic one: twelve
     of nineteen series tip into it the moment their held files are watched. The card used to
     print a green tick and "Visto" on a title whose own page said an episode was missing, which
     is the same contradiction one screen earlier with the disclosure removed. */
  const held = declaredIsPlausible(series.availableEpisodeCount, series.declaredEpisodeCount)
    ? series.declaredEpisodeCount
    : series.availableEpisodeCount
  const fullyWatched =
    series.availableEpisodeCount > 0 &&
    series.watchedEpisodeCount >= series.availableEpisodeCount &&
    series.availableEpisodeCount >= held

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
          {/* When the indexer knows of more than the library holds, the card says both numbers
              — the same sentence the series page shows — instead of a bare total that reads as
              the whole run. */}
          <span>
            {series.availableEpisodeCount < held
              ? t('series.availableOf', {
                  available: series.availableEpisodeCount,
                  declared: held,
                })
              : t('series.availableCount', { count: series.availableEpisodeCount })}
          </span>
        </span>
        {note ? <span className={styles.note}>{note}</span> : null}
      </Link>
    </article>
  )
}
