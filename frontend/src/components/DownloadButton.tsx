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
import { erase, measure, pause, start } from '@/offline/downloadManager'
import { offlineSupported } from '@/offline/register'
import { useSeriesDownload } from '@/offline/useDownloads'
import { formatBytes } from '@/lib/format'

import styles from './DownloadButton.module.css'

export function DownloadButton({ detail }: { detail: SeriesDetail }) {
  const { t } = useTranslation()
  const toast = useToast()
  const row = useSeriesDownload(detail.id)
  const [asking, setAsking] = useState(false)
  const [measuring, setMeasuring] = useState(false)
  const [plan, setPlan] = useState<{ episodeIds: string[]; bytes: number } | null>(null)

  const supported = offlineSupported()
  const done = row?.state === 'done'
  const busy = row?.state === 'running'
  const stored = row?.storedIds.length ?? 0
  const total = row?.episodeIds.length ?? 0

  async function ask(): Promise<void> {
    setAsking(true)
    setMeasuring(true)
    try {
      setPlan(await measure(detail))
    } catch {
      toast.show(t('downloads.measureFailed'), 'error')
      setAsking(false)
    } finally {
      setMeasuring(false)
    }
  }

  if (!supported) {
    return (
      <Button variant="secondary" disabled title={t('downloads.unsupportedHint')}>
        <DownloadIcon />
        <span className={styles.label}>{t('downloads.unsupported')}</span>
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
        loading={measuring}
        onClick={() => {
          if (done) {
            void erase(detail.id).then(() => toast.show(t('downloads.removed'), 'success'))
          } else if (busy) {
            pause(detail.id)
          } else {
            void ask()
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
        open={asking && plan !== null}
        onClose={() => setAsking(false)}
        title={t('downloads.confirmTitle', { title: detail.title })}
        description={t('downloads.confirmBody', {
          count: plan?.episodeIds.length ?? 0,
          size: formatBytes(plan?.bytes ?? 0),
        })}
        footer={
          <>
            <Button variant="ghost" onClick={() => setAsking(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              onClick={() => {
                setAsking(false)
                void start(detail)
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
