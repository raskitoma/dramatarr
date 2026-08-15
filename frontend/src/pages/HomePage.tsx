/** Home — continue watching first, then recently added.
 *
 * "Where was I?" is the question this page answers, so the resume row comes before anything
 * else and carries enough state (episode, bar, time left) to decide without opening the series.
 */

import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import { useContinueWatching, useEnqueueScan, useRecentlyAdded } from '@/api/library'
import type { ContinueWatchingItem } from '@/api/types'
import { Button } from '@/components/Button'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { episodeLabelParts } from '@/components/EpisodeRow'
import { Poster } from '@/components/Poster'
import { ProgressBar } from '@/components/ProgressBar'
import { SeriesCard } from '@/components/SeriesCard'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { useAuth } from '@/hooks/useAuth'
import { formatDuration } from '@/lib/format'

import styles from './HomePage.module.css'

export function HomePage() {
  const { t } = useTranslation()
  const { isAdmin } = useAuth()
  const toast = useToast()

  const continueWatching = useContinueWatching()
  const recentlyAdded = useRecentlyAdded(20)
  const enqueueScan = useEnqueueScan()

  const isLoading = continueWatching.isLoading || recentlyAdded.isLoading
  const resumeItems = continueWatching.data ?? []
  const recent = recentlyAdded.data

  if (isLoading) {
    return (
      <div className={styles.centered}>
        <Spinner size="lg" label={t('common.loading')} />
      </div>
    )
  }

  if (recentlyAdded.isError) {
    return <ErrorState error={recentlyAdded.error} onRetry={() => void recentlyAdded.refetch()} />
  }

  /* A failed continue-watching fetch is not an empty library — show the failure, don't invite
     the admin to rescan a library that is probably fine. */
  const showContinue = resumeItems.length > 0 || continueWatching.isError
  const libraryIsEmpty = !showContinue && (recent?.total ?? 0) === 0

  if (libraryIsEmpty) {
    return (
      <EmptyState
        title={t('common.empty')}
        description={isAdmin ? t('home.emptyLibraryAdmin') : t('home.emptyLibrary')}
        action={
          isAdmin ? (
            <Button
              variant="primary"
              loading={enqueueScan.isPending}
              onClick={() => {
                enqueueScan.mutate(false, {
                  onSuccess: () => toast.show(t('home.scanQueued'), 'success'),
                  onError: () => toast.show(t('errors.generic'), 'error'),
                })
              }}
            >
              {t('home.runScan')}
            </Button>
          ) : undefined
        }
      />
    )
  }

  return (
    <div className={styles.page}>
      {showContinue ? (
        <section className={styles.section} aria-labelledby="home-continue">
          <h2 className={styles.heading} id="home-continue">
            {t('home.continueWatching')}
          </h2>
          {continueWatching.isError ? (
            <ErrorState
              error={continueWatching.error}
              onRetry={() => void continueWatching.refetch()}
            />
          ) : (
            <ul className={styles.scroller}>
              {resumeItems.map((item) => (
                <ResumeCard key={item.episode.id} item={item} />
              ))}
            </ul>
          )}
        </section>
      ) : null}

      {recent && recent.items.length > 0 ? (
        <section className={styles.section} aria-labelledby="home-recent">
          <h2 className={styles.heading} id="home-recent">
            {t('home.recentlyAdded')}
          </h2>
          <ul className={styles.grid}>
            {recent.items.map((series) => (
              <li key={series.id}>
                <SeriesCard series={series} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}

function ResumeCard({ item }: { item: ContinueWatchingItem }) {
  const { t } = useTranslation()
  const { series, episode } = item

  const label = t('series.episodeLabel', episodeLabelParts(episode))
  const title = episode.title ?? t('series.episodeNumber', { number: episode.episodeNumber })

  /* Time left is the useful number when deciding what to finish tonight. Position is
     authoritative when the server offered a resume point; otherwise derive it from percent. */
  const remainingSeconds =
    episode.runtimeSeconds !== null
      ? Math.max(
          0,
          Math.round(
            episode.runtimeSeconds -
              (episode.resumePositionSeconds ?? (episode.runtimeSeconds * episode.percent) / 100),
          ),
        )
      : null

  return (
    <li className={styles.scrollItem}>
      <Link href={`/watch/${encodeURIComponent(episode.id)}`} className={styles.resumeLink}>
        {/* Poster labels the link; the visible line below repeats it for sighted users only. */}
        <Poster src={series.posterUrl} alt={series.title} size="md" fluid />
        <span className={styles.resumeSeries} aria-hidden="true">
          {series.title}
        </span>
        <span className={styles.resumeEpisode}>
          <span className={styles.resumeCode}>{label}</span>
          <span className={styles.resumeTitle}>{title}</span>
        </span>
        <ProgressBar percent={episode.percent} tone="accent" size="sm" label={t('series.inProgress')} />
        <span className={styles.resumeRemaining}>
          {remainingSeconds !== null
            ? t('time.remaining', { time: formatDuration(remainingSeconds) })
            : t('series.percentWatched', { percent: Math.round(episode.percent) })}
        </span>
      </Link>
    </li>
  )
}
