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
import { TrashIcon } from '@/components/icons'
import { formatBytes } from '@/lib/format'
import { erase, reconcile } from '@/offline/downloadManager'
import { pendingCount } from '@/offline/progress'
import { offlineReady, offlineSupported } from '@/offline/register'
import { useDownloads } from '@/offline/useDownloads'

import styles from './DownloadsPage.module.css'

interface DeviceFacts {
  persisted: boolean
  originUsage: number
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
      const [persisted, estimate] = await Promise.all([
        navigator.storage?.persisted?.() ?? Promise.resolve(false),
        navigator.storage?.estimate?.() ?? Promise.resolve({ usage: 0 }),
      ])
      setFacts({ persisted, originUsage: estimate.usage ?? 0 })
    })()
  }, [])

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
          <div className={styles.fact}>
            <dt>{t('downloads.deviceOriginUsage')}</dt>
            <dd className={styles.number}>{formatBytes(facts?.originUsage ?? 0)}</dd>
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
            <li key={row.seriesId} className={styles.row}>
              <Link href={`/series/${encodeURIComponent(row.slug)}`} className={styles.poster}>
                <Poster src={row.posterUrl} alt={row.title} size="sm" />
              </Link>
              <div className={styles.body}>
                <p className={styles.name}>{row.title}</p>
                <ProgressBar
                  percent={
                    row.episodeIds.length
                      ? (row.storedIds.length / row.episodeIds.length) * 100
                      : 0
                  }
                  tone={row.state === 'done' ? 'success' : 'accent'}
                  size="sm"
                  label={t('downloads.progressLabel', { title: row.title })}
                />
                <p className={styles.meta}>
                  {t('downloads.episodesOf', {
                    done: row.storedIds.length,
                    total: row.episodeIds.length,
                  })}
                  {' · '}
                  {formatBytes(row.bytesDone)}
                  {row.error ? ` · ${row.error}` : ''}
                </p>
              </div>
              <Button
                variant="ghost"
                iconOnly
                aria-label={t('downloads.remove')}
                title={t('downloads.remove')}
                onClick={() => void erase(row.seriesId)}
              >
                <TrashIcon />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
