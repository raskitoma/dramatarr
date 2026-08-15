/** One job in the queue page: summary line, optional live progress, and an expandable log.
 *
 * The log is fetched lazily — `GET /admin/jobs/{id}` returns the full log with the job, and a
 * queue with a few hundred rows of history must not pull all of that up front. While a running
 * job is expanded the detail query polls slowly: SSE carries progress but not log lines, so the
 * poll is the only way to see them arrive, and it only runs for the one row the admin opened.
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { adminKeys, getJob, jobStatusTone } from '@/api/admin'
import type { Job, JobLogEntry } from '@/api/types'
import { Badge } from '@/components/Badge'
import { ErrorState } from '@/components/ErrorState'
import { IconButton } from '@/components/IconButton'
import { ProgressBar } from '@/components/ProgressBar'
import { Spinner } from '@/components/Spinner'
import { formatDuration, formatPercent, formatRelativeTime } from '@/lib/format'
import styles from './JobRow.module.css'

const LOG_POLL_MS = 3_000

export interface JobRowProps {
  job: Job
  expanded: boolean
  onToggleExpand: (jobId: string) => void
  /** Progress bar, ticking elapsed time and progress message — the running job only. */
  live?: boolean
  /** Show the queue position weighting; only meaningful while a job is waiting. */
  showPriority?: boolean
  /** Cancel / retry / reorder controls, rendered at the end of the summary line. */
  actions?: ReactNode
}

/** Re-renders once a second so the running job's elapsed time actually moves. */
function useElapsedSeconds(startedAt: string | null, active: boolean): number {
  const [now, setNow] = useState<number>(() => Date.now())

  useEffect(() => {
    if (!active || startedAt === null) return
    const id = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(id)
  }, [active, startedAt])

  if (startedAt === null) return 0
  const started = Date.parse(startedAt)
  if (Number.isNaN(started)) return 0
  return Math.max(0, Math.floor((now - started) / 1_000))
}

const LEVEL_CLASS: Record<JobLogEntry['level'], string | undefined> = {
  debug: styles.levelDebug,
  info: styles.levelInfo,
  warning: styles.levelWarning,
  error: styles.levelError,
}

export function JobRow({
  job,
  expanded,
  onToggleExpand,
  live = false,
  showPriority = false,
  actions,
}: JobRowProps) {
  const { t, i18n } = useTranslation()
  const logRef = useRef<HTMLOListElement>(null)

  const detail = useQuery({
    queryKey: adminKeys.job(job.id),
    queryFn: () => getJob(job.id),
    enabled: expanded,
    refetchInterval: expanded && job.status === 'running' ? LOG_POLL_MS : false,
  })

  const logs = detail.data?.logs
  const elapsed = useElapsedSeconds(job.startedAt, live && job.status === 'running')

  // Newest lines are appended, so the interesting end is the bottom.
  useEffect(() => {
    const node = logRef.current
    if (!node) return
    node.scrollTop = node.scrollHeight
  }, [logs, expanded])

  const panelId = `job-log-${job.id}`
  const timestamp = job.finishedAt ?? job.startedAt ?? job.createdAt
  // The running row re-renders on every progress tick; building a formatter each time is waste.
  const timeFormatter = useMemo(
    () => new Intl.DateTimeFormat(i18n.language, { timeStyle: 'medium' }),
    [i18n.language],
  )

  return (
    <li className={styles.row} data-status={job.status}>
      <div className={styles.summary}>
        <IconButton
          label={t('admin.queue.logs')}
          size="sm"
          variant="ghost"
          aria-expanded={expanded}
          aria-controls={panelId}
          onClick={() => onToggleExpand(job.id)}
        >
          <span aria-hidden="true" className={styles.chevron} data-open={expanded}>
            ›
          </span>
        </IconButton>

        <div className={styles.identity}>
          <span className={styles.type}>{t(`admin.jobType.${job.type}`)}</span>
          {job.seriesTitle !== null && job.seriesTitle !== '' ? (
            <span className={styles.target}>{job.seriesTitle}</span>
          ) : null}
        </div>

        <div className={styles.meta}>
          {showPriority ? (
            <span className={styles.priority}>
              {t('admin.queue.priority')} {job.priority}
            </span>
          ) : null}
          {job.attempts > 1 ? (
            <span className={styles.attempts}>
              {t('admin.queue.attempts', { current: job.attempts, max: job.maxAttempts })}
            </span>
          ) : null}
          <time className={styles.time} dateTime={timestamp}>
            {formatRelativeTime(timestamp, t)}
          </time>
          <Badge tone={jobStatusTone(job.status)}>{t(`admin.jobStatus.${job.status}`)}</Badge>
        </div>

        {actions ? <div className={styles.actions}>{actions}</div> : null}
      </div>

      {live && job.status === 'running' ? (
        <div className={styles.progress}>
          <ProgressBar
            percent={job.progressPercent}
            tone="accent"
            label={t(`admin.jobType.${job.type}`)}
          />
          <div className={styles.progressMeta}>
            <span className={styles.percent}>{formatPercent(job.progressPercent)}</span>
            {job.progressMessage ? (
              <span className={styles.message}>{job.progressMessage}</span>
            ) : null}
            <span className={styles.elapsed}>
              {t('admin.queue.elapsed', { time: formatDuration(elapsed) })}
            </span>
          </div>
        </div>
      ) : null}

      {job.error !== null && job.error !== '' ? (
        <p className={styles.error}>{job.error}</p>
      ) : null}

      {/* The wrapper always exists so `aria-controls` resolves; its contents mount on expand,
          which is also what makes the scroll-to-bottom effect measure a laid-out element. */}
      <div className={styles.panel} id={panelId} hidden={!expanded}>
        {expanded ? (
          <>
            {detail.isLoading ? <Spinner size="sm" label={t('common.loading')} /> : null}
            {detail.isError ? (
              <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
            ) : null}
            {logs && logs.length > 0 ? (
              <ol className={styles.log} ref={logRef} tabIndex={0}>
                {logs.map((entry) => (
                  <li
                    key={entry.id}
                    className={`${styles.logLine} ${LEVEL_CLASS[entry.level] ?? ''}`}
                  >
                    <span className={styles.logTime}>
                      {timeFormatter.format(new Date(entry.at))}
                    </span>
                    <span className={styles.logMessage}>{entry.message}</span>
                  </li>
                ))}
              </ol>
            ) : null}
            {logs && logs.length === 0 ? (
              <p className={styles.logEmpty}>{t('common.empty')}</p>
            ) : null}
          </>
        ) : null}
      </div>
    </li>
  )
}
