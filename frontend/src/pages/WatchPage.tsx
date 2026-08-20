/** `/watch/:episodeId` — the player page.
 *
 * Everything about *what* to play comes from `EpisodeDetail`; this page only frames it. In
 * particular `resumePositionSeconds` is already null when the backend has decided a resume
 * should not be offered (§4: under 15s in, or completed), so the rule is not re-derived here.
 *
 * The one structural rule: **advancing an episode is not a navigation.** The library is a
 * 58-part serial of five-minute episodes, so the player advances constantly, and a route
 * change would tear down the `<video>` element every time. A media element only keeps the
 * right to start playing on its own for as long as it stays the same element — a new one has
 * no user gesture behind it and `play()` is refused. So the episode is held in state, the URL
 * is rewritten with `history.replaceState` (which is what wouter's `navigate(..., {replace})`
 * does) purely so refresh and sharing land in the right place, and `VideoPlayer` is never
 * keyed or unmounted between episodes.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useParams } from 'wouter'

import {
  useEpisodeDetail,
  useInvalidateWatchState,
  useMarkEpisodeWatched,
  useSeriesDetail,
} from '@/api/library'
import type { EpisodeDetail } from '@/api/types'
import ErrorState from '@/components/ErrorState'
import IconButton from '@/components/IconButton'
import Spinner from '@/components/Spinner'
import VideoPlayer from '@/components/VideoPlayer'
import { ChevronLeftIcon, CloseIcon } from '@/components/icons'
import { useAuth } from '@/hooks/useAuth'
import { localResume } from '@/offline/progress'

import styles from './WatchPage.module.css'

export function WatchPage() {
  const { t } = useTranslation()
  const { user } = useAuth()
  const params = useParams<{ episodeId: string }>()
  const [, navigate] = useLocation()
  const routeEpisodeId = params.episodeId

  /**
   * The episode on screen.
   *
   * Seeded from the route and thereafter driven by the player. It follows the route again
   * whenever the route changes for a reason other than our own `replaceState` — a back/forward
   * press, or a fresh link — which is a no-op in the common case because we set both.
   */
  const [activeEpisodeId, setActiveEpisodeId] = useState(routeEpisodeId)
  useEffect(() => {
    setActiveEpisodeId(routeEpisodeId)
  }, [routeEpisodeId])

  // Keyed by episode so advancing to the next one re-warns about a different file.
  const [warningDismissedFor, setWarningDismissedFor] = useState<string | null>(null)

  // Both of these come from `@/api/library` rather than being written here: it owns the cache
  // keys, so a completed episode actually clears Home's continue-watching row and the series
  // page's progress bar instead of invalidating key roots nothing is stored under.
  const episodeQuery = useEpisodeDetail(activeEpisodeId)
  const completeMutation = useMarkEpisodeWatched()
  const invalidateWatchState = useInvalidateWatchState()

  /**
   * Hold the last episode we managed to load.
   *
   * A swap must not blink through a spinner — that would unmount the player and undo the whole
   * point of swapping in place. React Query serves the next episode from cache instantly when
   * it has been prefetched below; this covers the case where it has not.
   */
  const [shownEpisode, setShownEpisode] = useState<EpisodeDetail | null>(null)
  useEffect(() => {
    if (episodeQuery.data) setShownEpisode(episodeQuery.data)
  }, [episodeQuery.data])

  const episode = episodeQuery.data ?? shownEpisode

  /* The position written on this device, which is the only one that exists while the server is
     off. Read once per episode; `null` both while it loads and when there is nothing local, and
     in both cases the cached server value is used. */
  const [localPosition, setLocalPosition] = useState<number | null>(null)
  useEffect(() => {
    let alive = true
    setLocalPosition(null)
    if (!activeEpisodeId) return undefined
    void localResume(activeEpisodeId).then((position) => {
      if (alive) setLocalPosition(position)
    })
    return () => {
      alive = false
    }
  }, [activeEpisodeId])

  /**
   * Warm the next episode's detail while this one plays.
   *
   * Five-minute episodes mean the swap happens every few minutes and has to be instant: by the
   * time the countdown ends, `streamUrl` for the next file is already in the cache, so
   * `loadSource` runs in the same tick the viewer asked for it and the element keeps its right
   * to play.
   */
  useEpisodeDetail(episode?.nextEpisodeId ?? undefined)

  // Only for the "Episode 12 of 58" line; the player works fine before it arrives.
  const seriesQuery = useSeriesDetail(episode?.seriesSlug)
  const seasonEpisodeCount = useMemo(() => {
    if (!episode) return null
    const season = seriesQuery.data?.seasons.find(
      (group) => group.seasonNumber === episode.seasonNumber,
    )
    return season?.episodes.length ?? null
  }, [seriesQuery.data, episode])

  const nextEpisodeTitle = useMemo(() => {
    const nextId = episode?.nextEpisodeId
    if (!nextId || !seriesQuery.data) return null
    for (const season of seriesQuery.data.seasons) {
      const found = season.episodes.find((item) => item.id === nextId)
      if (found) {
        return (
          found.title ??
          t('player.episodeLabel', {
            season: found.seasonNumber,
            episode: found.episodeNumber,
          })
        )
      }
    }
    return null
  }, [seriesQuery.data, episode?.nextEpisodeId, t])

  const goToSeries = useCallback(() => {
    if (episode) navigate(`/series/${episode.seriesSlug}`)
  }, [episode, navigate])

  /**
   * Switch episodes without leaving the page.
   *
   * `navigate(..., { replace: true })` is `history.replaceState` — the URL and the browser's
   * history entry are corrected so a refresh or a shared link resolves to what is playing,
   * while the route pattern is unchanged and React reconciles the same `VideoPlayer` element
   * in place. No remount, so no lost autoplay.
   */
  const selectEpisode = useCallback(
    (episodeId: string) => {
      setActiveEpisodeId(episodeId)
      navigate(`/watch/${episodeId}`, { replace: true })
    },
    [navigate],
  )

  const completedRef = useRef<Set<string>>(new Set())
  const handleEpisodeComplete = useCallback(
    (episodeId: string) => {
      if (completedRef.current.has(episodeId)) return
      completedRef.current.add(episodeId)
      // The mutation refreshes the caches on success; this covers the failure path so the
      // series page at least re-reads what the server does think.
      completeMutation.mutate(episodeId, { onError: () => invalidateWatchState() })
    },
    [completeMutation, invalidateWatchState],
  )

  const codecSummary = useMemo(() => {
    if (!episode) return ''
    return [episode.videoCodec, episode.audioCodec].filter(Boolean).join(' / ')
  }, [episode])

  if (!episode && episodeQuery.isLoading) {
    return (
      <div className={styles.centered}>
        <Spinner size="lg" label={t('common.loading')} />
      </div>
    )
  }

  if (!episode) {
    return (
      <div className={styles.centered}>
        <ErrorState error={episodeQuery.error} onRetry={() => void episodeQuery.refetch()} />
      </div>
    )
  }

  const showWarning = !episode.directPlayOk && warningDismissedFor !== episode.id

  return (
    <div className={styles.page}>
      <header className={styles.bar}>
        <Link href={`/series/${episode.seriesSlug}`} className={styles.back}>
          <span className={styles.backGlyph} aria-hidden="true">
            <ChevronLeftIcon />
          </span>
          {t('player.back')}
        </Link>
        <div className={styles.titles}>
          <h1 className={styles.seriesTitle}>{episode.seriesTitle}</h1>
          <p className={styles.episodeLine}>
            <span className={styles.episodeCode}>
              {t('player.episodeLabel', {
                season: episode.seasonNumber,
                episode: episode.episodeNumber,
              })}
            </span>
            {episode.title ? <span className={styles.episodeTitle}>{episode.title}</span> : null}
          </p>
        </div>
      </header>

      {showWarning ? (
        <div className={styles.warning} role="status">
          <span className={styles.warningGlyph} aria-hidden="true">
            <WarningMark />
          </span>
          <div className={styles.warningText}>
            <p className={styles.warningTitle}>{t('player.notDirectPlayable')}</p>
            <p className={styles.warningHint}>
              {t('player.notDirectPlayableHint', {
                codec: codecSummary || t('common.unknown'),
                container: episode.container ?? t('common.unknown'),
              })}
            </p>
          </div>
          <IconButton
            label={t('common.close')}
            variant="ghost"
            size="sm"
            onClick={() => setWarningDismissedFor(episode.id)}
          >
            <CloseIcon />
          </IconButton>
        </div>
      ) : null}

      <div className={styles.stage}>
        {/* Deliberately unkeyed: a key on the episode would remount the `<video>` on every
         * advance and the browser would refuse to start the next episode. */}
        <VideoPlayer
          episode={episode}
          seasonEpisodeCount={seasonEpisodeCount}
          nextEpisodeTitle={nextEpisodeTitle}
          onSelectEpisode={selectEpisode}
          onEpisodeComplete={handleEpisodeComplete}
          onLeave={goToSeries}
          autoplayNext={user?.autoplayNext ?? false}
          /* Local wins when it exists. Offline, `episode.resumePositionSeconds` is whatever
             was true the night the series was downloaded, so without this every episode
             watched on the plane would jump back to that frozen position. */
          initialPositionSeconds={localPosition ?? episode.resumePositionSeconds}
        />
      </div>
    </div>
  )
}

function WarningMark() {
  return (
    <svg viewBox="0 0 24 24" className={styles.glyph} aria-hidden="true" focusable="false">
      <path
        d="M12 4.5l8.5 15h-17z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
      <path
        d="M12 10v4.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <circle cx="12" cy="16.9" r="1" fill="currentColor" />
    </svg>
  )
}

export default WatchPage
