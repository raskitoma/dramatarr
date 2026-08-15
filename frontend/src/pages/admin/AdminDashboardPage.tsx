/** Admin landing: the six numbers that describe the library, plus the state of the queue.
 *
 * Deliberately quiet — big figures, muted labels, no chrome. The queue card shares the SSE feed
 * with the queue page, so a scan started from here shows its progress without a reload.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import { adminKeys, apiErrorKeys, createJob, getQueue, getStats } from '@/api/admin'
import { Badge } from '@/components/Badge'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { ProgressBar } from '@/components/ProgressBar'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { useQueueEvents } from '@/hooks/useQueueEvents'
import { formatBytes, formatPercent } from '@/lib/format'
import styles from './AdminDashboardPage.module.css'

export function AdminDashboardPage() {
  const { t, i18n } = useTranslation()
  const client = useQueryClient()
  const toast = useToast()
  useQueueEvents()

  const statsQuery = useQuery({ queryKey: adminKeys.stats(), queryFn: getStats })
  const queueQuery = useQuery({ queryKey: adminKeys.queue(), queryFn: getQueue })

  const scanMutation = useMutation({
    mutationFn: () => createJob({ type: 'library_scan' }),
    onSuccess: (job) => {
      void client.invalidateQueries({ queryKey: adminKeys.jobLists() })
      void client.invalidateQueries({ queryKey: adminKeys.queue() })
      toast.show(t('admin.queue.jobQueued', { type: t(`admin.jobType.${job.type}`) }), 'success')
    },
    onError: (error: unknown) => {
      toast.show(t(apiErrorKeys(error)), 'error')
    },
  })

  const numberFormat = useMemo(() => new Intl.NumberFormat(i18n.language), [i18n.language])
  const stats = statsQuery.data
  const queue = queueQuery.data
  const unmatched = stats?.unmatchedCount ?? 0

  const tiles: { key: string; label: string; value: string; alert?: boolean }[] = stats
    ? [
        {
          key: 'series',
          label: t('admin.stats.series'),
          value: numberFormat.format(stats.seriesCount),
        },
        {
          key: 'episodes',
          label: t('admin.stats.episodes'),
          value: numberFormat.format(stats.episodeCount),
        },
        {
          key: 'available',
          label: t('admin.stats.available'),
          value: numberFormat.format(stats.availableEpisodeCount),
        },
        {
          key: 'unmatched',
          label: t('admin.stats.unmatched'),
          value: numberFormat.format(stats.unmatchedCount),
          alert: stats.unmatchedCount > 0,
        },
        {
          key: 'users',
          label: t('admin.stats.users'),
          value: numberFormat.format(stats.userCount),
        },
        {
          // The size of the *library* — the media itself, summed from the scan records. This
          // used to show `dataDirBytes`, which is the app's own directory (database, cached
          // artwork, sprites): a number in the tens of megabytes sitting under a heading that
          // reads "storage", next to tiles counting series and episodes. It answered a
          // question nobody was asking on this screen.
          key: 'storage',
          label: t('admin.stats.storage'),
          value: formatBytes(stats.libraryBytes),
        },
        {
          // Kept, but named for what it is. It is the number that actually grows on the
          // application's disk rather than on the NAS share, so it is the one worth watching
          // when `/mnt/apps` fills up.
          key: 'appData',
          label: t('admin.stats.appData'),
          value: formatBytes(stats.dataDirBytes),
        },
      ]
    : []

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>{t('admin.dashboard')}</h1>

      {statsQuery.isLoading ? <Spinner label={t('common.loading')} /> : null}
      {statsQuery.isError ? (
        <ErrorState error={statsQuery.error} onRetry={() => void statsQuery.refetch()} />
      ) : null}

      {tiles.length > 0 ? (
        <dl className={styles.tiles}>
          {tiles.map((tile) => (
            <div key={tile.key} className={styles.tile} data-alert={tile.alert ? 'true' : 'false'}>
              <dt className={styles.tileLabel}>{tile.label}</dt>
              <dd className={styles.tileValue}>{tile.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      <section className={styles.card} aria-labelledby="dashboard-queue">
        <div className={styles.cardHead}>
          <h2 className={styles.cardTitle} id="dashboard-queue">
            {t('admin.queue.title')}
          </h2>
          {queue?.paused ? <Badge tone="warning">{t('admin.queue.paused')}</Badge> : null}
          <Link href="/admin/queue" className={styles.cardLink}>
            {t('nav.queue')}
          </Link>
        </div>

        {queueQuery.isLoading ? <Spinner size="sm" label={t('common.loading')} /> : null}
        {queueQuery.isError ? (
          <ErrorState error={queueQuery.error} onRetry={() => void queueQuery.refetch()} />
        ) : null}

        {queue?.current ? (
          <div className={styles.running}>
            <div className={styles.runningHead}>
              <span className={styles.runningType}>
                {t(`admin.jobType.${queue.current.type}`)}
              </span>
              <span className={styles.runningPercent}>
                {formatPercent(queue.current.progressPercent)}
              </span>
            </div>
            <ProgressBar
              percent={queue.current.progressPercent}
              tone="accent"
              label={t('admin.queue.running')}
            />
            {queue.current.progressMessage ? (
              <p className={styles.runningMessage}>{queue.current.progressMessage}</p>
            ) : null}
          </div>
        ) : null}

        {queue ? (
          <p className={styles.queueCount}>
            <span className={styles.queueCountLabel}>{t('admin.queue.queued')}</span>
            <span className={styles.queueCountValue}>
              {numberFormat.format(queue.queuedCount)}
            </span>
          </p>
        ) : null}

        <div className={styles.actions}>
          <Button variant="primary" onClick={() => scanMutation.mutate()} loading={scanMutation.isPending}>
            {t('admin.queue.enqueueScan')}
          </Button>
          {unmatched > 0 ? (
            <Link href="/admin/library" className={styles.unmatchedLink}>
              {t('admin.library.unmatched')}
              <Badge tone="warning">{numberFormat.format(unmatched)}</Badge>
            </Link>
          ) : null}
        </div>
      </section>
    </div>
  )
}
