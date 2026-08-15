/** The live job queue: what is running now, what is waiting, and what already finished.
 *
 * Concurrency is exactly 1 by design (§6), so the page is deliberately shaped around that fact
 * rather than showing a generic pool of workers: one running card, one ordered waiting list the
 * admin can re-prioritize, and a filterable history.
 *
 * `useQueueEvents` keeps the running job's progress bar moving by writing SSE payloads into the
 * query cache, so nothing here polls.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  JOB_TYPES,
  TERMINAL_JOB_STATUSES,
  adminKeys,
  apiErrorKeys,
  cancelJob,
  clearCompletedJobs,
  createJob,
  isRetryableJob,
  isTerminalJob,
  listJobs,
  pauseQueue,
  reorderJob,
  resumeQueue,
  retryJob,
  getQueue,
  type JobQuery,
} from '@/api/admin'
import type { JobStatus, JobType } from '@/api/types'
import { Badge } from '@/components/Badge'
import { Button } from '@/components/Button'
import { Dialog } from '@/components/Dialog'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { Field } from '@/components/Field'
import { IconButton } from '@/components/IconButton'
import { Pagination } from '@/components/Pagination'
import { Select } from '@/components/Select'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import {
  ChevronRightIcon,
  CloseIcon,
  PauseIcon,
  PlayIcon,
  RefreshIcon,
  TrashIcon,
} from '@/components/icons'
import { JobRow } from '@/components/admin/JobRow'
import { TocLayout } from '@/components/admin/TocLayout'
import { useQueueEvents } from '@/hooks/useQueueEvents'
import styles from './AdminQueuePage.module.css'

const HISTORY_PAGE_SIZE = 20
/** The waiting list is an ordered whole — paginating it would make "move up" meaningless. */
const WAITING_PAGE_SIZE = 100

function toStatusFilter(value: string): JobStatus | '' {
  return (TERMINAL_JOB_STATUSES as readonly string[]).includes(value) ? (value as JobStatus) : ''
}

function toTypeFilter(value: string): JobType | '' {
  return (JOB_TYPES as readonly string[]).includes(value) ? (value as JobType) : ''
}

export function AdminQueuePage() {
  const { t } = useTranslation()
  const client = useQueryClient()
  const toast = useToast()
  const { connected } = useQueueEvents()

  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [newJobOpen, setNewJobOpen] = useState(false)
  const [cancelId, setCancelId] = useState<string | null>(null)
  const [clearOpen, setClearOpen] = useState(false)
  const [historyPage, setHistoryPage] = useState(1)
  const [historyStatus, setHistoryStatus] = useState<JobStatus | ''>('')
  const [historyType, setHistoryType] = useState<JobType | ''>('')

  const waitingParams: JobQuery = { status: 'queued', page: 1, pageSize: WAITING_PAGE_SIZE }
  const historyParams: JobQuery = {
    status: historyStatus,
    type: historyType,
    page: historyPage,
    pageSize: HISTORY_PAGE_SIZE,
  }

  const queueQuery = useQuery({ queryKey: adminKeys.queue(), queryFn: getQueue })

  const waitingQuery = useQuery({
    queryKey: adminKeys.jobList(waitingParams),
    queryFn: () => listJobs(waitingParams),
  })

  const historyQuery = useQuery({
    queryKey: adminKeys.jobList(historyParams),
    queryFn: () => listJobs(historyParams),
  })

  const onError = useCallback(
    (error: unknown) => {
      toast.show(t(apiErrorKeys(error)), 'error')
    },
    [toast, t],
  )

  const refreshQueue = useCallback(() => {
    void client.invalidateQueries({ queryKey: adminKeys.jobLists() })
    void client.invalidateQueries({ queryKey: adminKeys.queue() })
  }, [client])

  const pauseMutation = useMutation({
    mutationFn: (next: boolean) => (next ? pauseQueue() : resumeQueue()),
    onSuccess: (state) => {
      client.setQueryData(adminKeys.queue(), state)
    },
    onError,
  })

  const enqueueMutation = useMutation({
    mutationFn: (full: boolean) =>
      createJob({ type: 'library_scan', payload: full ? { full: true } : {} }),
    onSuccess: (job) => {
      setNewJobOpen(false)
      refreshQueue()
      toast.show(
        t('admin.queue.jobQueued', { type: t(`admin.jobType.${job.type}`) }),
        'success',
      )
    },
    onError,
  })

  const cancelMutation = useMutation({
    mutationFn: (jobId: string) => cancelJob(jobId),
    onSuccess: () => {
      setCancelId(null)
      refreshQueue()
    },
    onError,
  })

  const retryMutation = useMutation({
    mutationFn: (jobId: string) => retryJob(jobId),
    onSuccess: refreshQueue,
    onError,
  })

  const reorderMutation = useMutation({
    mutationFn: (vars: { jobId: string; priority: number }) => reorderJob(vars.jobId, vars.priority),
    onSuccess: refreshQueue,
    onError,
  })

  const clearMutation = useMutation({
    mutationFn: () => clearCompletedJobs(),
    onSuccess: () => {
      setClearOpen(false)
      setHistoryPage(1)
      void client.invalidateQueries({ queryKey: adminKeys.jobLists() })
      void client.invalidateQueries({ queryKey: adminKeys.stats() })
    },
    onError,
  })

  const toggleExpand = useCallback((jobId: string) => {
    setExpandedId((current) => (current === jobId ? null : jobId))
  }, [])

  const paused = queueQuery.data?.paused ?? false
  const current = queueQuery.data?.current ?? null
  const waiting = waitingQuery.data?.items ?? []
  // Queued and running jobs have their own regions above; the history must not repeat them.
  const history = (historyQuery.data?.items ?? []).filter((job) => isTerminalJob(job.status))

  /* Priorities are sparse integers, so stepping just past the neighbour is enough to reorder
     without rewriting the whole list. Zero is the floor the API accepts. */
  function moveUp(index: number): void {
    const job = waiting[index]
    const previous = waiting[index - 1]
    if (!job || !previous) return
    reorderMutation.mutate({ jobId: job.id, priority: Math.max(0, previous.priority - 1) })
  }

  function moveDown(index: number): void {
    const job = waiting[index]
    const next = waiting[index + 1]
    if (!job || !next) return
    reorderMutation.mutate({ jobId: job.id, priority: next.priority + 1 })
  }

  const nothingPending = current === null && waiting.length === 0

  /* Computed rather than constant: the "running" heading only exists while something is
     running, and an index that links to a heading which is not on the page is worse than no
     index. With nothing running this drops to two entries and still earns its column. */
  const sections = useMemo(
    () => [
      ...(current ? [{ id: 'queue-running', labelKey: 'admin.queue.running' }] : []),
      { id: 'queue-waiting', labelKey: 'admin.queue.queued' },
      { id: 'queue-history', labelKey: 'admin.queue.history' },
    ],
    [current],
  )

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headline}>
          <h1 className={styles.title}>{t('admin.queue.title')}</h1>
          {paused ? <Badge tone="warning">{t('admin.queue.paused')}</Badge> : null}
        </div>
        <div className={styles.headerActions}>
          {/* Glyph beside the word, not instead of it: this one control both stops and starts
              the queue, and which of the two it is about to do is worth stating twice. */}
          <Button onClick={() => pauseMutation.mutate(!paused)} loading={pauseMutation.isPending}>
            {paused ? <PlayIcon /> : <PauseIcon />}
            {paused ? t('admin.queue.resume') : t('admin.queue.pause')}
          </Button>
          <Button variant="primary" onClick={() => setNewJobOpen(true)}>
            {t('admin.queue.newJob')}
          </Button>
        </div>
      </header>

      {paused ? <p className={styles.hint}>{t('admin.queue.pausedHint')}</p> : null}

      <p className={styles.connection} role="status" aria-live="polite">
        {connected ? null : t('admin.queue.disconnected')}
      </p>

      {queueQuery.isLoading ? <Spinner label={t('common.loading')} /> : null}
      {queueQuery.isError ? (
        <ErrorState error={queueQuery.error} onRetry={() => void queueQuery.refetch()} />
      ) : null}

      <TocLayout sections={sections} labelKey="admin.queue.tocLabel">
      {current ? (
        <section className={styles.section} aria-labelledby="queue-running">
          <h2 className={styles.sectionTitle} id="queue-running">
            {t('admin.queue.running')}
          </h2>
          <ul className={styles.list}>
            <JobRow
              job={current}
              live
              expanded={expandedId === current.id}
              onToggleExpand={toggleExpand}
              actions={
                <IconButton
                  variant="danger"
                  label={t('admin.queue.cancelJobFor', {
                    type: t(`admin.jobType.${current.type}`),
                  })}
                  onClick={() => setCancelId(current.id)}
                  disabled={current.cancelRequested}
                >
                  <CloseIcon />
                </IconButton>
              }
            />
          </ul>
        </section>
      ) : null}

      <section className={styles.section} aria-labelledby="queue-waiting">
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle} id="queue-waiting">
            {t('admin.queue.queued')}
          </h2>
          <span className={styles.count}>
            {t('common.results', { count: waitingQuery.data?.total ?? waiting.length })}
          </span>
        </div>
        <p className={styles.hint}>{t('admin.queue.hint')}</p>

        {waitingQuery.isLoading ? <Spinner size="sm" label={t('common.loading')} /> : null}
        {waitingQuery.isError ? (
          <ErrorState error={waitingQuery.error} onRetry={() => void waitingQuery.refetch()} />
        ) : null}

        {waiting.length > 0 ? (
          <ul className={styles.list}>
            {waiting.map((job, index) => (
              <JobRow
                key={job.id}
                job={job}
                showPriority
                expanded={expandedId === job.id}
                onToggleExpand={toggleExpand}
                actions={
                  <>
                    {/* The set has no vertical chevron, so the horizontal one is turned. A
                        rotated glyph beats a bare "↑" character, which inherits the text font
                        and lands at a different weight and size on every platform. */}
                    <IconButton
                      label={t('admin.queue.moveUp')}
                      variant="ghost"
                      disabled={index === 0 || reorderMutation.isPending}
                      onClick={() => moveUp(index)}
                    >
                      <ChevronRightIcon className={styles.chevronUp} />
                    </IconButton>
                    <IconButton
                      label={t('admin.queue.moveDown')}
                      variant="ghost"
                      disabled={index === waiting.length - 1 || reorderMutation.isPending}
                      onClick={() => moveDown(index)}
                    >
                      <ChevronRightIcon className={styles.chevronDown} />
                    </IconButton>
                    <IconButton
                      label={t('admin.queue.cancelJobFor', {
                        type: t(`admin.jobType.${job.type}`),
                      })}
                      variant="ghost"
                      onClick={() => setCancelId(job.id)}
                    >
                      <CloseIcon />
                    </IconButton>
                  </>
                }
              />
            ))}
          </ul>
        ) : null}

        {nothingPending && !waitingQuery.isLoading && !waitingQuery.isError ? (
          <EmptyState
            title={t('admin.queue.empty')}
            action={
              <Button variant="primary" onClick={() => setNewJobOpen(true)}>
                {t('admin.queue.newJob')}
              </Button>
            }
          />
        ) : null}
      </section>

      <section className={styles.section} aria-labelledby="queue-history">
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle} id="queue-history">
            {t('admin.queue.history')}
          </h2>
          {/* Keeps its words: it empties a list rather than acting on one row, and a lone
              bin glyph beside a section heading names nothing. */}
          <Button
            size="sm"
            variant="danger"
            onClick={() => setClearOpen(true)}
            disabled={history.length === 0}
          >
            <TrashIcon size="1em" />
            {t('admin.queue.clearHistory')}
          </Button>
        </div>

        <div className={styles.filters}>
          <Field label={t('admin.queue.filterStatus')} htmlFor="queue-history-status">
            <Select
              id="queue-history-status"
              value={historyStatus}
              onChange={(event) => {
                setHistoryStatus(toStatusFilter(event.currentTarget.value))
                setHistoryPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {TERMINAL_JOB_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {t(`admin.jobStatus.${status}`)}
                </option>
              ))}
            </Select>
          </Field>

          <Field label={t('admin.queue.filterType')} htmlFor="queue-history-type">
            <Select
              id="queue-history-type"
              value={historyType}
              onChange={(event) => {
                setHistoryType(toTypeFilter(event.currentTarget.value))
                setHistoryPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {JOB_TYPES.map((type) => (
                <option key={type} value={type}>
                  {t(`admin.jobType.${type}`)}
                </option>
              ))}
            </Select>
          </Field>
        </div>

        {historyQuery.isLoading ? <Spinner size="sm" label={t('common.loading')} /> : null}
        {historyQuery.isError ? (
          <ErrorState error={historyQuery.error} onRetry={() => void historyQuery.refetch()} />
        ) : null}

        {history.length > 0 ? (
          <ul className={styles.list}>
            {history.map((job) => (
              <JobRow
                key={job.id}
                job={job}
                expanded={expandedId === job.id}
                onToggleExpand={toggleExpand}
                actions={
                  isRetryableJob(job.status) ? (
                    <IconButton
                      variant="secondary"
                      label={t('admin.queue.retryJobFor', {
                        type: t(`admin.jobType.${job.type}`),
                      })}
                      onClick={() => retryMutation.mutate(job.id)}
                      loading={retryMutation.isPending && retryMutation.variables === job.id}
                    >
                      <RefreshIcon />
                    </IconButton>
                  ) : null
                }
              />
            ))}
          </ul>
        ) : null}

        {history.length === 0 && !historyQuery.isLoading && !historyQuery.isError ? (
          <EmptyState title={t('common.empty')} />
        ) : null}

        <Pagination
          page={historyPage}
          pageSize={HISTORY_PAGE_SIZE}
          total={historyQuery.data?.total ?? 0}
          onPage={setHistoryPage}
        />
      </section>
      </TocLayout>

      <Dialog
        open={newJobOpen}
        onClose={() => setNewJobOpen(false)}
        title={t('admin.queue.newJob')}
        size="sm"
      >
        <div className={styles.newJob}>
          <Button
            variant="primary"
            full
            onClick={() => enqueueMutation.mutate(false)}
            loading={enqueueMutation.isPending && enqueueMutation.variables === false}
          >
            {t('admin.queue.enqueueScan')}
          </Button>
          <div className={styles.newJobOption}>
            <Button
              full
              onClick={() => enqueueMutation.mutate(true)}
              loading={enqueueMutation.isPending && enqueueMutation.variables === true}
            >
              {t('admin.queue.enqueueFullScan')}
            </Button>
            <p className={styles.hint}>{t('admin.queue.enqueueFullScanHint')}</p>
          </div>
        </div>
      </Dialog>

      <Dialog
        open={cancelId !== null}
        onClose={() => setCancelId(null)}
        title={t('admin.queue.cancel')}
        size="sm"
        footer={
          <>
            <Button onClick={() => setCancelId(null)}>{t('common.close')}</Button>
            <Button
              variant="danger"
              loading={cancelMutation.isPending}
              onClick={() => {
                if (cancelId !== null) cancelMutation.mutate(cancelId)
              }}
            >
              {t('common.confirm')}
            </Button>
          </>
        }
      >
        <p>{t('admin.queue.cancelConfirm')}</p>
      </Dialog>

      <Dialog
        open={clearOpen}
        onClose={() => setClearOpen(false)}
        title={t('admin.queue.clearHistory')}
        size="sm"
        footer={
          <>
            <Button onClick={() => setClearOpen(false)}>{t('common.close')}</Button>
            <Button
              variant="danger"
              loading={clearMutation.isPending}
              onClick={() => clearMutation.mutate()}
            >
              {t('common.confirm')}
            </Button>
          </>
        }
      >
        <p>{t('admin.queue.clearHistoryConfirm')}</p>
      </Dialog>
    </div>
  )
}
