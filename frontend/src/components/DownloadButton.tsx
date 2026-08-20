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

import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { SeriesDetail } from '@/api/types'
import { Button } from '@/components/Button'
import { Dialog } from '@/components/Dialog'
import { ProgressBar } from '@/components/ProgressBar'
import { useToast } from '@/components/Toast'
import { DownloadIcon, TrashIcon } from '@/components/icons'
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

  return (
    <>
      {done ? (
        <Button
          variant="secondary"
          title={t('downloads.remove')}
          onClick={() => {
            void erase(detail.id).then(() => toast.show(t('downloads.removed'), 'success'))
          }}
        >
          <TrashIcon />
          <span className={styles.label}>{t('downloads.downloaded')}</span>
        </Button>
      ) : busy ? (
        <div className={styles.running}>
          <ProgressBar
            className={styles.bar}
            percent={total ? (stored / total) * 100 : 0}
            size="sm"
            label={t('downloads.progressLabel', { title: detail.title })}
          />
          <button type="button" className={styles.pause} onClick={() => pause(detail.id)}>
            {t('downloads.episodesOf', { done: stored, total })} · {t('downloads.pause')}
          </button>
        </div>
      ) : (
        <Button variant="secondary" onClick={() => void ask()} loading={measuring}>
          <DownloadIcon />
          <span className={styles.label}>
            {row?.state === 'paused' ? t('downloads.resume') : t('downloads.download')}
          </span>
        </Button>
      )}

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
