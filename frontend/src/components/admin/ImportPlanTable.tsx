/** The review surface: every file in a folder next to the episode it would become.
 *
 * This is what an admin actually approves, so it is a table and not a summary — 58 rows, all
 * of them readable, scrollable inside the card instead of pushing the Import button off the
 * screen. The left column is exactly what is on disk today and the right column is exactly
 * what the catalogue would say about it; nothing between them is abbreviated away.
 *
 * Per-row confidence only appears when the rows disagree. When the whole plan is one uniform
 * number, the card's badge already says it and a column repeating it 58 times would be noise.
 */

import { useTranslation } from 'react-i18next'

import { importConfidenceTone, type ImportPlanItem } from '@/api/admin'
import Badge from '@/components/Badge'
import { episodeLabelParts } from '@/components/EpisodeRow'
import EmptyState from '@/components/EmptyState'
import { formatPercent } from '@/lib/format'

import styles from './ImportPlanTable.module.css'

export interface ImportPlanTableProps {
  items: ImportPlanItem[]
  /** Names the table for assistive tech — normally the folder the plan belongs to. */
  caption: string
}

/** Rounded to whole percent first: 0.9199 and 0.92 are the same number to a reader. */
function confidenceVaries(items: ImportPlanItem[]): boolean {
  const seen = new Set(items.map((item) => Math.round(item.confidence * 100)))
  return seen.size > 1
}

export function ImportPlanTable({ items, caption }: ImportPlanTableProps) {
  const { t } = useTranslation()

  if (items.length === 0) {
    return <EmptyState title={t('admin.import.planEmpty')} />
  }

  const showConfidence = confidenceVaries(items)

  return (
    <div className={styles.scroller} tabIndex={0} role="group" aria-label={caption}>
      <table className={styles.table}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">{t('admin.import.currentName')}</th>
            {/* The arrow column is decoration; the two real headers carry the meaning. */}
            <th scope="col" className={styles.arrowCell}>
              <span className="sr-only">{t('admin.import.becomes')}</span>
            </th>
            <th scope="col">{t('admin.import.proposed')}</th>
            {showConfidence ? (
              <th scope="col" className={styles.confidenceCell}>
                {t('admin.import.confidence')}
              </th>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.mediaFileId}>
              <th scope="row" className={styles.nameCell}>
                <span className={styles.filename}>{item.filename}</span>
                {/* What the strategy actually sorted on. Showing it turns the explanation
                    from a claim into something the admin can check row by row. */}
                {item.orderKey !== null ? (
                  <span className={styles.orderKey}>
                    {t('admin.import.orderKey', { value: item.orderKey })}
                  </span>
                ) : null}
              </th>
              <td className={styles.arrowCell} aria-hidden="true">
                →
              </td>
              <td className={styles.targetCell}>
                <span className={styles.episodeCode}>
                  {t('series.episodeLabel', episodeLabelParts(item))}
                </span>
              </td>
              {showConfidence ? (
                <td className={styles.confidenceCell}>
                  <Badge tone={importConfidenceTone(item.confidence)}>
                    {formatPercent(item.confidence * 100)}
                  </Badge>
                </td>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default ImportPlanTable
