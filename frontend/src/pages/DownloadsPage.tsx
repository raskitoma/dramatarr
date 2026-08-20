/** What is stored on *this* device, and whether it will still be here on the plane.
 *
 * The device panel is the honest half of this feature. Browser storage is per-origin and
 * per-device, silently, so the two questions that decide whether a trip works — "did the
 * service worker actually take control here" and "did the browser agree to protect this" — are
 * printed as plain answers rather than assumed.
 *
 * There is deliberately **no "space remaining"**. `navigator.storage.estimate().quota` is a
 * fabricated number on Chromium (usage plus 10 GiB whenever the disk has that much), so a
 * remaining figure derived from it would be a confident lie. `usage` is real and is shown.
 */

import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import { Badge } from '@/components/Badge'
import { Button } from '@/components/Button'
import { EmptyState } from '@/components/EmptyState'
import { Poster } from '@/components/Poster'
import { ProgressBar } from '@/components/ProgressBar'
import { CheckIcon, CircleIcon, DownloadIcon, PauseIcon, TrashIcon } from '@/components/icons'
import { formatBytes } from '@/lib/format'
import { erase, pause, reconcile, resume } from '@/offline/downloadManager'
import { pendingCount } from '@/offline/progress'
import { offlineReady, offlineSupported } from '@/offline/register'
import { useDownloads } from '@/offline/useDownloads'
import type { DownloadRow } from '@/offline/db'

import styles from './DownloadsPage.module.css'

interface DeviceFacts {
  persisted: boolean
}

export function DownloadsPage() {
  const { t } = useTranslation()
  const rows = useDownloads()
  const [facts, setFacts] = useState<DeviceFacts | null>(null)
  const [pending, setPending] = useState(0)

  useEffect(() => {
    void reconcile()
    void pendingCount().then(setPending)
    void (async () => {
      if (!offlineSupported()) return
      const persisted = (await navigator.storage?.persisted?.()) ?? false
      setFacts({ persisted })
    })()
  }, [])

  /* One number, from the ledger, which counts the episodes actually written to the cache.
     `navigator.storage.estimate().usage` used to sit beside it labelled "in total", and it
     reported *less* — 46MB against 99MB — because browsers update that figure lazily and
     quantise it. A part larger than its whole is the same defect as "62 de 62 vistos · falta
     1": two true-ish numbers, adjacent, saying something false. */
  const mine = rows.reduce((sum, row) => sum + row.bytesDone, 0)

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>{t('downloads.title')}</h1>

      <section className={styles.device} aria-labelledby="device-facts">
        <h2 className={styles.deviceTitle} id="device-facts">
          {t('downloads.deviceTitle')}
        </h2>
        <dl className={styles.facts}>
          <div className={styles.fact}>
            <dt>{t('downloads.deviceOrigin')}</dt>
            <dd className={styles.origin}>{window.location.origin}</dd>
          </div>
          <div className={styles.fact}>
            <dt>{t('downloads.deviceOfflineReady')}</dt>
            <dd>
              <Badge tone={offlineReady() ? 'success' : 'warning'}>
                {offlineReady() ? t('downloads.yes') : t('downloads.no')}
              </Badge>
            </dd>
          </div>
          <div className={styles.fact}>
            <dt>{t('downloads.devicePersisted')}</dt>
            <dd>
              <Badge tone={facts?.persisted ? 'success' : 'warning'}>
                {facts?.persisted ? t('downloads.yes') : t('downloads.no')}
              </Badge>
            </dd>
          </div>
          <div className={styles.fact}>
            <dt>{t('downloads.deviceUsage')}</dt>
            <dd className={styles.number}>{formatBytes(mine)}</dd>
          </div>
        </dl>
        {!offlineReady() ? <p className={styles.hint}>{t('downloads.offlineHint')}</p> : null}
        {!facts?.persisted ? <p className={styles.hint}>{t('downloads.persistHint')}</p> : null}
        {pending > 0 ? (
          <p className={styles.hint}>{t('downloads.pendingProgress', { count: pending })}</p>
        ) : null}
      </section>

      {rows.length === 0 ? (
        <EmptyState title={t('downloads.empty')} description={t('downloads.emptyHint')} />
      ) : (
        <ul className={styles.list}>
          {rows.map((row) => (
            <DownloadRowView key={row.seriesId} row={row} />
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * One series, and — when you open it — every episode in it.
 *
 * The list is the point rather than decoration: a partially downloaded series is the normal
 * state, not a failure, and "48 de 67" does not tell you *which* forty-eight. On a plane that
 * is the difference between knowing you can watch tonight's episode and finding out you
 * cannot.
 */
function DownloadRowView({ row }: { row: DownloadRow }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const stored = new Set(row.storedIds)
  const listId = `downloads-${row.seriesId}`
  const running = row.state === 'running'

  return (
    <li className={styles.row}>
      <div className={styles.summary}>
        <Link href={`/series/${encodeURIComponent(row.slug)}`} className={styles.poster}>
          <Poster src={row.posterUrl} alt={row.title} size="sm" />
        </Link>

        <button
          type="button"
          className={styles.body}
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          onClick={() => setOpen((previous) => !previous)}
        >
          <span className={styles.name}>{row.title}</span>
          <ProgressBar
            percent={row.episodes.length ? (stored.size / row.episodes.length) * 100 : 0}
            tone={row.state === 'done' ? 'success' : 'accent'}
            size="sm"
            label={t('downloads.progressLabel', { title: row.title })}
          />
          <span className={styles.meta}>
            {/* The state in words, because "0 de 67" cannot tell waiting apart from stopped
                apart from failed — and those want different reactions. */}
            <Badge tone={row.state === 'error' ? 'warning' : 'neutral'}>
              {t(`downloads.state.${row.state}`)}
            </Badge>{' '}
            {t('downloads.episodesOf', { done: stored.size, total: row.episodes.length })}
            {' · '}
            {formatBytes(row.bytesDone)}
            {row.bytesTotal > 0 ? ` / ${formatBytes(row.bytesTotal)}` : ''}
            {row.error ? ` · ${row.error}` : ''}
          </span>
        </button>

        <div className={styles.rowActions}>
          {row.state !== 'done' ? (
            <Button
              variant="ghost"
              iconOnly
              aria-label={running ? t('downloads.pauseSeries') : t('downloads.resumeSeries')}
              title={running ? t('downloads.pauseSeries') : t('downloads.resumeSeries')}
              onClick={() => (running ? pause(row.seriesId) : resume(row.seriesId))}
            >
              {running ? <PauseIcon /> : <DownloadIcon />}
            </Button>
          ) : null}
          <Button
            variant="ghost"
            iconOnly
            aria-label={t('downloads.remove')}
            title={t('downloads.remove')}
            onClick={() => void erase(row.seriesId)}
          >
            <TrashIcon />
          </Button>
        </div>
      </div>

      {open ? (
        <ul className={styles.episodes} id={listId}>
          {row.episodes.map((episode) => {
            const here = stored.has(episode.id)
            return (
              <li key={episode.id} className={styles.episode} data-stored={here ? 'true' : undefined}>
                <span className={styles.tick} aria-hidden="true">
                  {here ? <CheckIcon /> : <CircleIcon />}
                </span>
                <span className={styles.episodeLabel}>{episode.label}</span>
                <span className={styles.episodeSize}>{formatBytes(episode.sizeBytes)}</span>
                <span className={styles.srOnly}>
                  {here ? t('downloads.state.done') : t('downloads.state.queued')}
                </span>
              </li>
            )
          })}
        </ul>
      ) : null}
    </li>
  )
}
