/** Files the scanner kept but could not attach to an episode (§8).
 *
 * Every unparseable file still produces a `media_files` row, and this is where an admin sees
 * them: never silently dropped, always one click away from being assigned by hand.
 */

import { useTranslation } from 'react-i18next'

import type { UnmatchedFile } from '@/api/types'
import Badge from '@/components/Badge'
import Button from '@/components/Button'
import EmptyState from '@/components/EmptyState'
import { formatBytes, formatDuration, formatRelativeTime } from '@/lib/format'

import styles from './UnmatchedTable.module.css'

interface UnmatchedTableProps {
  files: UnmatchedFile[]
  onAssign: (file: UnmatchedFile) => void
  /** Id of the file whose assignment is in flight, so only that row shows a spinner. */
  assigningId?: string | null
}

export function UnmatchedTable({ files, onAssign, assigningId = null }: UnmatchedTableProps) {
  const { t } = useTranslation()

  if (files.length === 0) {
    return <EmptyState title={t('admin.library.unmatchedEmpty')} />
  }

  return (
    <div className={styles.scroller}>
      <table className={styles.table}>
        <caption className="sr-only">{t('admin.library.unmatched')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('admin.library.libraryPath')}</th>
            <th scope="col" className={styles.numeric}>
              {t('admin.library.size')}
            </th>
            <th scope="col" className={styles.numeric}>
              {t('admin.library.duration')}
            </th>
            <th scope="col" className={styles.actionsCell}>
              {t('common.actions')}
            </th>
          </tr>
        </thead>
        <tbody>
          {files.map((file) => (
            <tr key={file.id}>
              <th scope="row" className={styles.pathCell}>
                <span className={styles.path}>{file.path}</span>
                <span className={styles.meta}>
                  <span>{formatRelativeTime(file.firstSeenAt, t)}</span>
                  {file.container ? <span className={styles.chip}>{file.container}</span> : null}
                </span>
                {file.scanError ? (
                  <span className={styles.error}>
                    <Badge tone="danger">{file.scanError}</Badge>
                  </span>
                ) : null}
              </th>
              {/* Real text, not `::before` content: once the header row is hidden on a phone
                  this is the only thing naming the column, so it has to be in the a11y tree. */}
              <td className={styles.numeric}>
                <span className={styles.cellLabel}>{t('admin.library.size')}</span>
                {formatBytes(file.sizeBytes)}
              </td>
              <td className={styles.numeric}>
                <span className={styles.cellLabel}>{t('admin.library.duration')}</span>
                {file.durationSeconds === null
                  ? t('common.unknown')
                  : formatDuration(file.durationSeconds)}
              </td>
              <td className={styles.actionsCell}>
                <Button
                  size="sm"
                  variant="primary"
                  loading={assigningId === file.id}
                  onClick={() => onAssign(file)}
                >
                  {t('admin.library.assign')}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default UnmatchedTable
