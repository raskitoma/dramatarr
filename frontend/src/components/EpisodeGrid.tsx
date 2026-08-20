/** A season as a matrix of numbered tiles.
 *
 * A short vertical drama is fifty-eight five-minute instalments with no titles and no
 * per-episode artwork. As rows that is fifty-eight identical strips, and the only thing telling
 * one from the next is its number — so the tile makes the NUMBER the subject and the series
 * poster the texture behind it, and packs the whole season into something you can take in at a
 * glance rather than scroll through.
 *
 * Watch state has to survive that shrinking, so it is carried by three separate channels rather
 * than one: watched desaturates the art and shows a check, in progress gets a bar pinned to the
 * bottom edge plus its percent, and an episode the NAS does not have is dashed, art-less and
 * inert. Each is legible at a hundred pixels wide, and none of them depends on hovering.
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import { useClearEpisodeProgress, useMarkEpisodeWatched } from '@/api/library'
import type { EpisodeSummary } from '@/api/types'
import { IconButton } from '@/components/IconButton'
import { ProgressBar } from '@/components/ProgressBar'
import { cx } from '@/components/cx'
import { CheckCircleIcon, CircleIcon, UnwatchIcon } from '@/components/icons'

import { usePlayable } from '@/offline/useOffline'

import styles from './EpisodeGrid.module.css'

/**
 * A CSS `url()` token for an arbitrary URL.
 *
 * The value is written into a custom property, which is a stylesheet context: a quote inside the
 * path would close the string early and everything after it would parse as CSS. Quotes and
 * backslashes are escaped, and newlines — which cannot appear inside a CSS string at all — are
 * dropped rather than escaped.
 */
function cssUrl(src: string): string {
  const escaped = src.replace(/[\r\n]/g, '').replace(/["\\]/g, '\\$&')
  return `url("${escaped}")`
}

/**
 * The series poster, fetched exactly once for the entire grid.
 *
 * Every tile shows the same image, so fifty-eight `<img>` elements would put fifty-eight
 * entries in the loading queue for one file — the obvious trap here, and a real one on a phone
 * where the browser's per-host connection budget is small. Instead the URL is loaded a single
 * time and then handed to CSS as a custom property: identical `background-image` URLs resolve
 * to one shared image resource, so every tile paints from what this one load already decoded.
 *
 * The load waits until the grid is near the viewport, which is what `loading="lazy"` would have
 * done for a real image. Where `IntersectionObserver` is missing it simply starts immediately;
 * a missing poster is a texture that never arrives, never a broken grid.
 */
function useSharedPoster(src: string | null): {
  attach: (node: HTMLElement | null) => void
  loaded: boolean
} {
  const [node, setNode] = useState<HTMLElement | null>(null)
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null)

  useEffect(() => {
    if (src === null || node === null) return

    let cancelled = false
    const start = (): void => {
      const image = new Image()
      image.decoding = 'async'
      image.addEventListener('load', () => {
        if (!cancelled) setLoadedSrc(src)
      })
      image.src = src
    }

    if (typeof IntersectionObserver !== 'function') {
      start()
      return () => {
        cancelled = true
      }
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return
        observer.disconnect()
        start()
      },
      { rootMargin: '300px' },
    )
    observer.observe(node)

    return () => {
      cancelled = true
      observer.disconnect()
    }
  }, [src, node])

  return { attach: setNode, loaded: src !== null && loadedSrc === src }
}

/**
 * How many tiles the current row holds, read from the rendered grid.
 *
 * `auto-fill` decides the column count from the available width, so it is not knowable from the
 * markup and must be measured. Rects are used rather than `offsetTop` because the focusable face
 * is absolutely positioned inside its tile, which makes every `offsetTop` zero.
 */
function columnCount(nodes: Array<HTMLElement | null>): number {
  const first = nodes[0]
  if (!first) return 1

  const top = Math.round(first.getBoundingClientRect().top)
  let count = 0
  for (const node of nodes) {
    if (!node || Math.round(node.getBoundingClientRect().top) !== top) break
    count += 1
  }
  return Math.max(1, count)
}

export interface EpisodeGridProps {
  episodes: EpisodeSummary[]
  /** The series poster. Episodes in a library like this one carry no stills of their own. */
  posterUrl: string | null
  /** Accessible name for the grid, e.g. "Episodes". */
  label: string
}

export function EpisodeGrid({ episodes, posterUrl, label }: EpisodeGridProps) {
  const poster = useSharedPoster(posterUrl)

  /* One mutation pair for the whole grid rather than one per tile: fifty-eight tiles would
     otherwise mean fifty-eight idle mutation observers for a button pressed once. */
  const markWatched = useMarkEpisodeWatched()
  const clearProgress = useClearEpisodeProgress()

  const pendingId: string | null =
    (markWatched.isPending ? (markWatched.variables ?? null) : null) ??
    (clearProgress.isPending ? (clearProgress.variables ?? null) : null)

  const faceRefs = useRef<Array<HTMLElement | null>>([])
  const [focusIndex, setFocusIndex] = useState<number | null>(null)

  useEffect(() => {
    /* Refs are written by index, so a season that shrank would keep dead nodes past its end. */
    faceRefs.current.length = episodes.length
  }, [episodes.length])

  /** Tab lands on the episode you would actually resume, not on number 1 of fifty-eight. */
  const resumeIndex = useMemo(() => {
    const started = episodes.findIndex((e) => e.available && !e.watched && e.percent > 0)
    if (started >= 0) return started
    const fresh = episodes.findIndex((e) => e.available && !e.watched)
    return fresh >= 0 ? fresh : 0
  }, [episodes])

  const activeIndex =
    focusIndex !== null && focusIndex >= 0 && focusIndex < episodes.length ? focusIndex : resumeIndex

  const move = useCallback(
    (from: number, delta: number) => {
      const clamped = Math.min(Math.max(from + delta, 0), episodes.length - 1)
      const node = faceRefs.current[clamped]
      if (!node) return
      setFocusIndex(clamped)
      node.focus()
    },
    [episodes.length],
  )

  /* Arrow keys move a roving tabindex. Fifty-eight tiles in the tab order would bury whatever
     follows the grid; one tab stop in and out, arrows within, is the grid's own convention. */
  const onKeyDown = (event: KeyboardEvent<HTMLUListElement>): void => {
    const current = faceRefs.current.findIndex(
      (node) => node !== null && node === document.activeElement,
    )
    if (current < 0) return

    if (event.key === 'ArrowRight') move(current, 1)
    else if (event.key === 'ArrowLeft') move(current, -1)
    else if (event.key === 'ArrowDown') move(current, columnCount(faceRefs.current))
    else if (event.key === 'ArrowUp') move(current, -columnCount(faceRefs.current))
    else if (event.key === 'Home') move(current, -current)
    else if (event.key === 'End') move(current, episodes.length - 1 - current)
    else return

    event.preventDefault()
  }

  return (
    <ul
      className={styles.grid}
      aria-label={label}
      ref={poster.attach}
      onKeyDown={onKeyDown}
      data-art={poster.loaded ? 'true' : 'false'}
      style={
        poster.loaded && posterUrl !== null
          ? ({ '--tile-art': cssUrl(posterUrl) } as CSSProperties)
          : undefined
      }
    >
      {episodes.map((episode, index) => (
        <EpisodeTile
          key={episode.id}
          episode={episode}
          tabbable={index === activeIndex}
          busy={pendingId === episode.id}
          onFocus={() => setFocusIndex(index)}
          onMarkWatched={() => markWatched.mutate(episode.id)}
          onClearProgress={() => clearProgress.mutate(episode.id)}
          faceRef={(node) => {
            faceRefs.current[index] = node
          }}
        />
      ))}
    </ul>
  )
}

// --- Tile ----------------------------------------------------------------------------------

interface EpisodeTileProps {
  episode: EpisodeSummary
  /** Roving tabindex: exactly one tile is in the tab order at a time. */
  tabbable: boolean
  busy: boolean
  onFocus: () => void
  onMarkWatched: () => void
  onClearProgress: () => void
  faceRef: (node: HTMLElement | null) => void
}

function EpisodeTile({
  episode,
  tabbable,
  busy,
  onFocus,
  onMarkWatched,
  onClearProgress,
  faceRef,
}: EpisodeTileProps) {
  /* Same question as the row asks, answered the same way: offline, "available" has to mean
     "downloaded here", or the grid offers episodes the player cannot open. */
  const canPlay = usePlayable()(episode)
  const { t } = useTranslation()
  const hintId = useId()

  const hasProgress = episode.percent > 0 && !episode.watched
  const percent = Math.round(episode.percent)

  const numberLabel = t('series.episodeNumber', { number: episode.episodeNumber })
  const named = episode.title !== null ? `${numberLabel} · ${episode.title}` : numberLabel
  /* The big number is decorative — it is aria-hidden — so the whole state is restated here,
     otherwise the link's accessible name would be the bare digits. */
  const accessibleName = episode.watched ? `${named} · ${t('series.watched')}` : named

  const face = (
    <>
      <span className={styles.art} aria-hidden="true" />
      <span className={styles.number} aria-hidden="true">
        {episode.episodeNumber}
      </span>
      <span className="sr-only">{accessibleName}</span>
    </>
  )

  return (
    <li
      className={cx(
        styles.tile,
        episode.watched && styles.isWatched,
        !canPlay && styles.isUnavailable,
      )}
    >
      {canPlay ? (
        <Link
          href={`/watch/${encodeURIComponent(episode.id)}`}
          className={styles.face}
          ref={faceRef}
          tabIndex={tabbable ? 0 : -1}
          onFocus={onFocus}
          title={named}
        >
          {face}
        </Link>
      ) : (
        /* No file, so nothing to navigate to. It stays focusable — skipping tiles would make
           the matrix lie about its own shape — but it is announced and painted as inert. */
        <button
          type="button"
          className={styles.face}
          ref={faceRef}
          tabIndex={tabbable ? 0 : -1}
          aria-disabled="true"
          aria-describedby={hintId}
          onFocus={onFocus}
          title={t('series.notAvailableHint')}
        >
          {face}
          <span className="sr-only" id={hintId}>
            {t('series.notAvailable')} — {t('series.notAvailableHint')}
          </span>
        </button>
      )}

      {canPlay ? (
        /* Doubles as the watched indicator, so it costs no extra room: a ringed check when the
           episode is done, an empty ring when it is not, and one tap flips it either way. */
        <IconButton
          label={episode.watched ? t('series.markUnwatched') : t('series.markWatched')}
          className={cx(styles.state, episode.watched && styles.stateOn)}
          loading={busy}
          onClick={() => (episode.watched ? onClearProgress() : onMarkWatched())}
        >
          {episode.watched ? <CheckCircleIcon /> : <CircleIcon />}
        </IconButton>
      ) : null}

      {canPlay && hasProgress ? (
        <>
          {/* The percent has to be on screen anyway, so it is also the control that resets it —
              one tap, always visible, rather than a menu hidden behind a hover. */}
          <button
            type="button"
            className={styles.clear}
            title={t('series.clearProgress')}
            aria-disabled={busy ? true : undefined}
            onClick={() => {
              if (!busy) onClearProgress()
            }}
          >
            <span className={styles.clearPill}>
              <UnwatchIcon />
              <span>{t('series.percentShort', { percent })}</span>
            </span>
            <span className="sr-only">{t('series.clearProgress')}</span>
          </button>

          <ProgressBar
            className={styles.bar}
            percent={episode.percent}
            tone="accent"
            size="sm"
            label={t('series.percentWatched', { percent })}
          />
        </>
      ) : null}
    </li>
  )
}

export default EpisodeGrid
