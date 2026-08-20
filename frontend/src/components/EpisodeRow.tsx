/** One episode in a season list.
 *
 * Watch state is the point of this row: a check when watched, a bar and a percent when in
 * progress, and a muted, non-playable treatment when the catalogue declares an episode that
 * has no file on the NAS. A watched episode is still a link — rewatching is never gated (§9).
 *
 * This is the detailed view. It is worth its height when episodes have their own stills, titles
 * and air dates; a season of fifty-eight untitled five-minute instalments is better read as the
 * matrix in `EpisodeGrid`, and the series page lets the reader pick.
 */

import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import { useClearEpisodeProgress, useMarkEpisodeWatched } from '@/api/library'
import type { EpisodeSummary } from '@/api/types'
import { Badge } from '@/components/Badge'
import { IconButton } from '@/components/IconButton'
import { ProgressBar } from '@/components/ProgressBar'
import { cx } from '@/components/cx'
import {
  CheckCircleIcon,
  CheckIcon,
  MoreIcon,
  PlayIcon,
  ReplayIcon,
  UnwatchIcon,
} from '@/components/icons'
import { formatDate, formatDuration } from '@/lib/format'

import { usePlayable } from '@/offline/useOffline'

import styles from './EpisodeRow.module.css'

/** Zero-padded parts for the `series.episodeLabel` interpolation, shared with the home row. */
export function episodeLabelParts(episode: {
  seasonNumber: number
  episodeNumber: number
}): { season: string; episode: string } {
  return {
    season: String(episode.seasonNumber).padStart(2, '0'),
    episode: String(episode.episodeNumber).padStart(2, '0'),
  }
}

interface EpisodeRowProps {
  episode: EpisodeSummary
  /** `EpisodeSummary` carries no overview; pass one when the caller has the detail payload. */
  overview?: string | null
}

export function EpisodeRow({ episode, overview }: EpisodeRowProps) {
  /* Not `episode.available`, which answers "the NAS holds a file" — the right answer online and
     the wrong one on a plane. Offline this becomes "downloaded on this device", so the row
     agrees with what the Play button will actually open. */
  const canPlay = usePlayable()(episode)
  const { t, i18n } = useTranslation()
  const hintId = useId()

  const markWatched = useMarkEpisodeWatched()
  const clearProgress = useClearEpisodeProgress()

  const title = episode.title ?? t('series.episodeNumber', { number: episode.episodeNumber })
  const hasProgress = episode.percent > 0 && !episode.watched
  const busy = markWatched.isPending || clearProgress.isPending

  const body = (
    <>
      <span className={styles.number} aria-hidden="true">
        {episode.episodeNumber}
      </span>

      <span className={styles.thumb}>
        {episode.stillUrl !== null ? (
          <>
            <img className={styles.still} src={episode.stillUrl} alt="" loading="lazy" decoding="async" />
            {/* Only over a real still: it says the picture is the play control. Decorative —
                the link around it is already named. The placeholder needs no such hint, and
                would rather spend its middle on the number. */}
            {canPlay ? (
              <span className={styles.playMark} aria-hidden="true">
                <PlayIcon />
              </span>
            ) : null}
          </>
        ) : (
          /* No still, so the number does the identifying — the same job it does in the grid,
             and the only identity a row has on a phone, where the number column is dropped. */
          <span className={styles.stillPlaceholder} aria-hidden="true">
            {episode.episodeNumber}
          </span>
        )}

        {episode.watched ? (
          <span className={styles.watchedMark} aria-hidden="true">
            <CheckIcon />
          </span>
        ) : null}
      </span>

      <span className={styles.body}>
        <span className={styles.titleLine}>
          <span className={styles.title}>{title}</span>
          {/* The visible number column is aria-hidden, so restate it for the accessible name. */}
          <span className="sr-only">{t('series.episodeNumber', { number: episode.episodeNumber })}</span>
        </span>

        <span className={styles.meta}>
          {episode.runtimeSeconds !== null ? <span>{formatDuration(episode.runtimeSeconds)}</span> : null}
          {episode.airDate !== null ? <span>{formatDate(episode.airDate, i18n.language)}</span> : null}
          {episode.playCount > 0 ? (
            <span>{t('series.playedTimes', { count: episode.playCount })}</span>
          ) : null}
        </span>

        {overview ? <span className={styles.overview}>{overview}</span> : null}

        {hasProgress ? (
          <span className={styles.progress}>
            <ProgressBar
              percent={episode.percent}
              tone="accent"
              size="sm"
              label={t('series.inProgress')}
            />
            <span className={styles.percent}>
              {t('series.percentWatched', { percent: Math.round(episode.percent) })}
            </span>
          </span>
        ) : null}
      </span>
    </>
  )

  return (
    <li
      className={cx(
        styles.row,
        canPlay ? styles.playable : styles.unavailable,
        episode.watched && styles.isWatched,
      )}
    >
      {canPlay ? (
        <Link href={`/watch/${encodeURIComponent(episode.id)}`} className={styles.main}>
          {body}
        </Link>
      ) : (
        /* No file, so nothing to navigate to. The reason is spelled out rather than left as a
           dead row the user has to guess about. */
        <div className={styles.main} aria-describedby={hintId}>
          {body}
        </div>
      )}

      <div className={styles.aside}>
        {episode.watched ? (
          <Badge tone="success">{t('series.watched')}</Badge>
        ) : hasProgress ? (
          <Badge tone="info">{t('series.inProgress')}</Badge>
        ) : null}

        {!canPlay ? (
          <span className={styles.unavailableTag} title={t('series.notAvailableHint')}>
            <Badge tone="neutral">{t('series.notAvailable')}</Badge>
            <span className="sr-only" id={hintId}>
              {t('series.notAvailableHint')}
            </span>
          </span>
        ) : (
          <RowMenu
            label={t('series.moreActions')}
            busy={busy}
            items={[
              episode.watched
                ? {
                    key: 'unwatch',
                    label: t('series.markUnwatched'),
                    icon: <UnwatchIcon />,
                    onSelect: () => clearProgress.mutate(episode.id),
                  }
                : {
                    key: 'watch',
                    label: t('series.markWatched'),
                    icon: <CheckCircleIcon />,
                    onSelect: () => markWatched.mutate(episode.id),
                  },
              ...(hasProgress
                ? [
                    {
                      key: 'clear',
                      label: t('series.clearProgress'),
                      icon: <ReplayIcon />,
                      onSelect: () => clearProgress.mutate(episode.id),
                    },
                  ]
                : []),
            ]}
          />
        )}
      </div>
    </li>
  )
}

// --- Row menu ------------------------------------------------------------------------------

interface MenuItem {
  key: string
  label: string
  /** Decorative: it sits beside the visible label, which is what names the item. */
  icon: ReactNode
  onSelect: () => void
}

function RowMenu({ label, items, busy }: { label: string; items: MenuItem[]; busy: boolean }) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const triggerWrapRef = useRef<HTMLSpanElement | null>(null)
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])
  const menuId = useId()

  /* The trigger is a shared IconButton, whose ref forwarding is not part of its contract, so
     focus is returned through the wrapper element instead of a ref on the component. */
  const closeAndRestoreFocus = useCallback(() => {
    setOpen(false)
    triggerWrapRef.current?.querySelector('button')?.focus()
  }, [])

  useEffect(() => {
    if (!open) return
    itemRefs.current[0]?.focus()

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && containerRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  const focusItem = (index: number) => {
    const count = items.length
    if (count === 0) return
    itemRefs.current[((index % count) + count) % count]?.focus()
  }

  const onMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const current = itemRefs.current.findIndex((node) => node === document.activeElement)
    if (event.key === 'Escape') {
      event.stopPropagation()
      closeAndRestoreFocus()
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      focusItem(current + 1)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      focusItem(current - 1)
    } else if (event.key === 'Home') {
      event.preventDefault()
      focusItem(0)
    } else if (event.key === 'End') {
      event.preventDefault()
      focusItem(items.length - 1)
    } else if (event.key === 'Tab') {
      setOpen(false)
    }
  }

  return (
    <div className={styles.menuWrap} ref={containerRef}>
      <span ref={triggerWrapRef} className={styles.menuTrigger}>
        <IconButton
          label={label}
          variant="ghost"
          className={styles.menuButton}
          loading={busy}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={open ? menuId : undefined}
          onClick={() => setOpen((value) => !value)}
        >
          <MoreIcon />
        </IconButton>
      </span>

      {open ? (
        <div className={styles.menu} role="menu" id={menuId} aria-label={label} onKeyDown={onMenuKeyDown}>
          {items.map((item, index) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              className={styles.menuItem}
              ref={(node) => {
                itemRefs.current[index] = node
              }}
              onClick={() => {
                item.onSelect()
                closeAndRestoreFocus()
              }}
            >
              <span className={styles.menuIcon} aria-hidden="true">
                {item.icon}
              </span>
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
