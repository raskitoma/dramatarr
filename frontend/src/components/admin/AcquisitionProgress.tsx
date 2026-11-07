import { useTranslation } from 'react-i18next'

import type { Job } from '@/api/types'
import { Badge } from '@/components/Badge'
import { ProgressBar } from '@/components/ProgressBar'
import { formatBytes, formatPercent } from '@/lib/format'
import styles from './Acquisition.module.css'

type Details = {
  stage?: string
  episodesTotal?: number
  episodesCompleted?: number
  episodesFailed?: number
  currentEpisode?: number
  currentEpisodePercent?: number
  downloadedBytes?: number
  currentEpisodeTotalBytes?: number
  currentEpisodeBytes?: number
}

const phases = ['searching', 'awaiting_review', 'search_failed', 'no_match', 'queued', 'extracting', 'downloading', 'importing', 'complete']

export function AcquisitionProgress({ job }: { job: Job }) {
  const { t } = useTranslation()
  if (job.type !== 'download_series' && job.type !== 'find_source') return null

  const info = (job.payload.acquisition || {}) as Details
  const phase = info.stage && phases.includes(info.stage) ? info.stage : 'queued'
  if (job.type === 'find_source') {
    return <p className={styles.muted} role="status">{t(`acquisitionFlow.phase.${phase}`)}</p>
  }

  const running = job.status === 'running'
  const completed = info.episodesCompleted ?? 0
  const failed = info.episodesFailed ?? 0
  const total = info.episodesTotal
  const percent = Math.min(100, Math.max(0, job.progressPercent || 0))
  const episodePercent = info.currentEpisodePercent

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <strong>{running ? t(`acquisitionFlow.phase.${phase}`) : t(`admin.jobStatus.${job.status}`)}</strong>
        <span className={styles.percent}>{formatPercent(percent)}</span>
      </div>
      <ProgressBar percent={percent} tone={job.status === 'succeeded' ? 'success' : 'accent'} label={t('acquisitionFlow.totalProgress')} />
      <div className={styles.meta}>
        {job.payload.provider ? <Badge tone="neutral">{String(job.payload.provider)}</Badge> : null}
        {job.payload.language ? <Badge tone="neutral">{String(job.payload.language)}</Badge> : null}
        {total ? <span>{t('acquisitionFlow.episodes', { done: completed, total, pending: Math.max(0, total - completed - failed), failed })}</span> : null}
        {info.downloadedBytes != null && <span>{t('acquisitionFlow.downloadedBytes', { bytes: formatBytes(info.downloadedBytes) })}</span>}
      </div>
      {running && phase === 'downloading' && (
        <div className={styles.episode}>
          <div className={styles.header}>
            {info.currentEpisode != null && <span>{t('acquisitionFlow.currentEpisode', { episode: info.currentEpisode })}</span>}
            {episodePercent != null && <span className={styles.percent}>{formatPercent(episodePercent)}</span>}
          </div>
          {episodePercent != null && <ProgressBar size="sm" percent={episodePercent} label={t('acquisitionFlow.episodeProgress')} />}
          <p className={styles.muted}>
            {info.currentEpisodeTotalBytes
              ? t('acquisitionFlow.episodeBytes', { done: formatBytes(info.currentEpisodeBytes ?? 0), total: formatBytes(info.currentEpisodeTotalBytes) })
              : t('acquisitionFlow.unknownSize')}
          </p>
        </div>
      )}
    </div>
  )
}
