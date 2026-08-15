/** Rename this series' files on disk to match its metadata.
 *
 * The only control in dramatarr that changes files on the NAS, so it is built as
 * **preview, then confirm** and never as a single button. The plan is loaded on demand, lists
 * every file including the ones that will be skipped and why, and the confirm names the number
 * of files that will actually move. An admin who reads nothing still cannot run this without
 * seeing that number.
 *
 * The skipped rows are the point of showing the plan at all. A collision means two files want
 * the same name, and the app refuses rather than choosing — that refusal is safety, but it is
 * only *useful* if it is visible before the run rather than buried in a job log afterwards.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  adminKeys,
  getRenamePlan,
  renameSeriesFiles,
  type RenameItem,
  type RenameStatus,
} from '@/api/admin'
import { Badge, type BadgeTone } from '@/components/Badge'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'

import styles from './RenamePanel.module.css'

const TONE: Record<RenameStatus, BadgeTone> = {
  ok: 'accent',
  unchanged: 'neutral',
  collision: 'warning',
  missing: 'warning',
  failed: 'danger',
}

/** The rows worth reading first: what will change, then what will not. */
const ORDER: RenameStatus[] = ['ok', 'collision', 'missing', 'failed', 'unchanged']

export function RenamePanel({ seriesId }: { seriesId: string }) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)

  const plan = useQuery({
    queryKey: adminKeys.renamePlan(seriesId),
    queryFn: () => getRenamePlan(seriesId),
    // Only once the admin asks: it stats every file of the series on a network mount.
    enabled: open,
    staleTime: 0,
    gcTime: 0,
  })

  const run = useMutation({
    mutationFn: () => renameSeriesFiles(seriesId),
    onSuccess: async () => {
      show(t('admin.library.renameQueued'), 'success')
      // The job rewrites `media_files.path`, so anything showing a path is now stale.
      await queryClient.invalidateQueries({ queryKey: adminKeys.series() })
      setOpen(false)
    },
  })

  const items = plan.data?.items ?? []
  const sorted = [...items].sort(
    (a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status),
  )
  const count = plan.data?.renamableCount ?? 0

  return (
    <section className={styles.panel} id="series-rename">
      <h2 className={styles.title}>{t('admin.library.renameTitle')}</h2>
      <p className={styles.hint}>{t('admin.library.renameHint')}</p>

      {!open ? (
        <Button onClick={() => setOpen(true)}>{t('admin.library.renamePreview')}</Button>
      ) : null}

      {open ? (
        <>
          {plan.isFetching ? <Spinner label={t('common.loading')} /> : null}
          {plan.isError ? (
            <ErrorState error={plan.error} onRetry={() => void plan.refetch()} />
          ) : null}
          {run.isError ? <ErrorState error={run.error} /> : null}

          {plan.data ? (
            <>
              <ul className={styles.list}>
                {sorted.map((item) => (
                  <RenameRow key={item.mediaFileId} item={item} />
                ))}
              </ul>

              <div className={styles.actions}>
                <Button
                  variant="primary"
                  loading={run.isPending}
                  disabled={count === 0}
                  onClick={() => run.mutate()}
                >
                  {count === 0
                    ? t('admin.library.renameNothing')
                    : t('admin.library.renameConfirm', { count })}
                </Button>
                <Button onClick={() => setOpen(false)}>{t('common.cancel')}</Button>
              </div>
            </>
          ) : null}
        </>
      ) : null}
    </section>
  )
}

function RenameRow({ item }: { item: RenameItem }) {
  const { t } = useTranslation()
  const changing = item.status === 'ok'

  return (
    <li className={styles.row}>
      <Badge tone={TONE[item.status]}>{t(`admin.library.renameStatus.${item.status}`)}</Badge>
      <div className={styles.paths}>
        <code className={styles.from}>{item.currentPath}</code>
        {changing ? <code className={styles.to}>{item.proposedPath}</code> : null}
        {item.reason ? <span className={styles.reason}>{item.reason}</span> : null}
      </div>
    </li>
  )
}

export default RenamePanel
