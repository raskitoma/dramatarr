/** "Download this series to this device", and everything that follows from pressing it.
 *
 * The confirmation is not ceremony: this is one to two gigabytes going onto whatever the person
 * is holding, and the exact figure is not known until every episode record has been read. So the
 * button measures first, shows the real total, and only then moves video.
 *
 * On an origin with no service worker — `http://<nas-ip>:8090`, which is not a secure context —
 * the button is disabled and says why. Offering it there would produce a download that appears
 * to work and is invisible to the app on the plane, because storage is per-origin.
 */

import { useState, type CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'

import type { SeriesDetail } from '@/api/types'
import { Button } from '@/components/Button'
import { Dialog } from '@/components/Dialog'
import { useToast } from '@/components/Toast'
import { CheckIcon, DownloadIcon } from '@/components/icons'
import {
  erase,
  measure,
  pause,
  resume,
  start,
  type Measured,
} from '@/offline/downloadManager'
import { offlineSupported, useOfflineBlocked } from '@/offline/register'
import { useSeriesDownload } from '@/offline/useDownloads'
import { formatBytes } from '@/lib/format'

import styles from './DownloadButton.module.css'

export function DownloadButton({ detail }: { detail: SeriesDetail }) {
  const { t } = useTranslation()
  const toast = useToast()
  const row = useSeriesDownload(detail.id)
  const [asking, setAsking] = useState(false)
  const [measuring, setMeasuring] = useState(false)
  const [plan, setPlan] = useState<Measured | null>(null)

  /* Two different noes. `offlineSupported` is false where there is no API at all — the LAN
     address, which is not a secure context. `blocked` is a browser that has the API and refused
     to register: Edge and Chrome on iPhone, where Apple exposes service workers only to Safari
     and Home Screen web apps. The second is the one that would otherwise take a 1.4 GB
     download and quietly produce nothing openable. */
  const blocked = useOfflineBlocked()
  const supported = offlineSupported() && !blocked
  const done = row?.state === 'done'
  const busy = row?.state === 'running' || row?.state === 'queued'
  const available = (detail.seasons ?? [])
    .flatMap((season) => season.episodes)
    .filter((episode) => episode.available).length
  const stored = row?.storedIds.length ?? 0
  const total = row?.episodes.length ?? 0

  /* Opens the dialog *first*, then measures into it.
   *
   * It used to measure first and open after, and the wait was long enough — sixty-seven
   * records, one after the other — that the spinner read as a download in progress. Pressing
   * Descargar and then being asked to confirm, a minute later, looks like nothing happened. */
  function ask(): void {
    setPlan(null)
    setAsking(true)
    setMeasuring(true)
    measure(detail)
      .then(setPlan)
      .catch(() => {
        toast.show(t('downloads.measureFailed'), 'error')
        setAsking(false)
      })
      .finally(() => setMeasuring(false))
  }

  if (!supported) {
    return (
      <Button
        variant="secondary"
        disabled
        title={blocked ? t('downloads.blockedHint') : t('downloads.unsupportedHint')}
      >
        <DownloadIcon />
        <span className={styles.label}>
          {blocked ? t('downloads.blocked') : t('downloads.unsupported')}
        </span>
      </Button>
    )
  }

  /* One square, four states. Its three neighbours in this row are glyphs, so this one is too —
     the progress lives in the accessible name and in a hairline across the bottom of the
     button, rather than in a word that spills out of the box. */
  const label = done
    ? t('downloads.downloadedRemove')
    : busy
      ? `${t('downloads.progressLabel', { title: detail.title })} · ${t('downloads.episodesOf', { done: stored, total })} · ${t('downloads.pause')}`
      : row?.state === 'paused'
        ? t('downloads.resume')
        : t('downloads.download')

  return (
    <>
      <Button
        variant="secondary"
        className={styles.trigger}
        title={label}
        onClick={() => {
          if (done) {
            void erase(detail.id).then(() => toast.show(t('downloads.removed'), 'success'))
          } else if (busy) {
            pause(detail.id)
          } else if (row?.state === 'paused' || row?.state === 'error') {
            // Already measured; the row knows its episodes, so this skips straight back to
            // pulling bytes rather than re-reading sixty-seven episode records.
            resume(detail.id)
          } else {
            ask()
          }
        }}
      >
        {done ? <CheckIcon /> : <DownloadIcon />}
        <span className={styles.label}>{label}</span>
        {busy || row?.state === 'paused' ? (
          <span
            className={styles.fill}
            style={{ '--fill': `${total ? (stored / total) * 100 : 0}%` } as CSSProperties}
            aria-hidden="true"
          />
        ) : null}
      </Button>

      <Dialog
        open={asking}
        onClose={() => setAsking(false)}
        title={t('downloads.confirmTitle', { title: detail.title })}
        description={
          plan
            ? t('downloads.confirmBody', {
                count: plan.episodes.length,
                size: formatBytes(plan.bytes),
              })
            : t('downloads.confirmMeasuring', { count: available })
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setAsking(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              loading={measuring}
              disabled={measuring}
              onClick={() => {
                setAsking(false)
                if (!plan) return
                // Never `void`: this used to swallow every failure, so a download that died on
                // IndexedDB or a rejected quota looked exactly like one that had never been
                // pressed. The row records it too; this is what puts it on screen.
                start(detail, plan).catch((error: unknown) => {
                  toast.show(
                    t('downloads.startFailed', {
                      reason: error instanceof Error ? error.message : '',
                    }),
                    'error',
                  )
                })
              }}
            >
              {t('downloads.download')}
            </Button>
          </>
        }
      >
        <p className={styles.note}>{t('downloads.confirmNote')}</p>
      </Dialog>
    </>
  )
}
