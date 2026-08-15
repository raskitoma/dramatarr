/** Remove a series from the catalogue — and only from the catalogue.
 *
 * Two things have to be true before the admin presses the button, or this dialog has failed:
 *
 *  1. They know the video files are untouched. dramatarr mounts the library read-only and
 *     never deletes, moves or writes a media file; what goes away is rows.
 *  2. They know whether the deletion will actually last. If the files are still on the NAS the
 *     next scan indexes them right back, so the honest answer is "move the folder out of the
 *     library first" — said here, before the confirm, rather than discovered a scan later when
 *     the series reappears and the app looks broken.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { adminKeys, deleteSeries, getDeletionPreview, type SeriesDeleted } from '@/api/admin'
import { libraryKeys } from '@/api/library'
import Button from '@/components/Button'
import Dialog from '@/components/Dialog'
import ErrorState from '@/components/ErrorState'
import Spinner from '@/components/Spinner'
import { useToast } from '@/components/Toast'

import styles from './DeleteSeriesDialog.module.css'

interface DeleteSeriesDialogProps {
  open: boolean
  onClose: () => void
  seriesId: string
  seriesTitle: string
  /** Called after the rows are gone, so the caller can drop its own row or navigate away. */
  onDeleted?: () => void
}

export function DeleteSeriesDialog({
  open,
  onClose,
  seriesId,
  seriesTitle,
  onDeleted,
}: DeleteSeriesDialogProps) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const [acknowledged, setAcknowledged] = useState(false)

  // A reopen — same series or another — must never inherit the previous confirmation.
  const [subject, setSubject] = useState(seriesId)
  const [wasOpen, setWasOpen] = useState(open)
  if (subject !== seriesId || open !== wasOpen) {
    setSubject(seriesId)
    setWasOpen(open)
    setAcknowledged(false)
  }

  const preview = useQuery({
    queryKey: adminKeys.deletionPreview(seriesId),
    queryFn: () => getDeletionPreview(seriesId),
    enabled: open,
    // What is on disk right now is the whole point; a cached answer could be a lie.
    staleTime: 0,
    gcTime: 0,
    retry: false,
  })

  const remove = useMutation({
    mutationFn: () => deleteSeries(seriesId),
    onSuccess: async (result: SeriesDeleted) => {
      // The warning survives the deletion: if the files are still there, this is temporary.
      if (result.filesStillOnDisk) {
        show(t('admin.library.deleteDoneWillReturn', { title: seriesTitle }), 'info')
      } else {
        show(t('admin.library.deleteDone', { title: seriesTitle }), 'success')
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: adminKeys.series() }),
        queryClient.invalidateQueries({ queryKey: adminKeys.stats() }),
        queryClient.invalidateQueries({ queryKey: libraryKeys.all }),
      ])
      onDeleted?.()
      onClose()
    },
  })

  const data = preview.data
  const confirmDisabled = !acknowledged || data === undefined || preview.isError

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t('admin.library.deleteTitle')}
      description={t('admin.library.deleteSubtitle')}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="danger"
            loading={remove.isPending}
            disabled={confirmDisabled}
            onClick={() => remove.mutate()}
          >
            {t('admin.library.deleteConfirm')}
          </Button>
        </>
      }
    >
      <p className={styles.subject}>{seriesTitle}</p>

      {preview.isPending ? <Spinner label={t('common.loading')} /> : null}

      {preview.isError ? (
        <ErrorState error={preview.error} onRetry={() => void preview.refetch()} />
      ) : null}

      {data ? (
        <>
          <dl className={styles.stats}>
            <div className={styles.stat}>
              <dt className={styles.statLabel}>{t('admin.library.deleteEpisodes')}</dt>
              <dd className={styles.statValue}>{data.episodeCount}</dd>
            </div>
            <div className={styles.stat}>
              <dt className={styles.statLabel}>{t('admin.library.deleteFiles')}</dt>
              <dd className={styles.statValue}>{data.mediaFileCount}</dd>
              {data.missingFileCount > 0 ? (
                <dd className={styles.statNote}>
                  {t('admin.library.deleteFilesMissing', { count: data.missingFileCount })}
                </dd>
              ) : null}
            </div>
            <div className={styles.stat}>
              <dt className={styles.statLabel}>{t('admin.library.deleteViewers')}</dt>
              <dd className={styles.statValue}>{data.usersWithProgress}</dd>
              <dd className={styles.statNote}>{t('admin.library.deleteViewersNote')}</dd>
            </div>
          </dl>

          <p className={styles.readOnly}>{t('admin.library.deleteFilesKept')}</p>

          {data.filesStillOnDisk ? (
            <div className={styles.warning} role="alert">
              <p className={styles.warningTitle}>{t('admin.library.deleteWillReturnTitle')}</p>
              <p>{t('admin.library.deleteWillReturnBody')}</p>
              {data.presentFileCount > 0 ? (
                <p className={styles.warningCount}>
                  {t('admin.library.deleteWillReturnFiles', { count: data.presentFileCount })}
                </p>
              ) : null}
            </div>
          ) : (
            <p className={styles.gone}>{t('admin.library.deleteGone')}</p>
          )}

          <label className={styles.confirm}>
            <input
              type="checkbox"
              className={styles.checkbox}
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            <span>
              {data.filesStillOnDisk
                ? t('admin.library.deleteAckWillReturn')
                : t('admin.library.deleteAck')}
            </span>
          </label>
        </>
      ) : null}

      {remove.isError ? <ErrorState error={remove.error} /> : null}
    </Dialog>
  )
}

export default DeleteSeriesDialog
