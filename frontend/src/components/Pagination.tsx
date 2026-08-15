import { useTranslation } from 'react-i18next'

import { Button } from './Button'
import { cx } from './cx'
import styles from './Pagination.module.css'

export interface PaginationProps {
  /** 1-based. */
  page: number
  pageSize: number
  total: number
  onPage: (page: number) => void
  className?: string
}

type Slot = number | 'gapBefore' | 'gapAfter'

/** First, last, and a window around the current page; gaps stand in for the rest. */
function buildSlots(current: number, totalPages: number): Slot[] {
  const slots: Slot[] = [1]
  const window = [current - 1, current, current + 1].filter((n) => n > 1 && n < totalPages)

  const first = window[0]
  if (first !== undefined && first > 2) slots.push('gapBefore')

  for (const n of window) slots.push(n)

  const last = window[window.length - 1]
  if (last !== undefined && last < totalPages - 1) slots.push('gapAfter')

  if (totalPages > 1) slots.push(totalPages)
  return slots
}

export function Pagination({ page, pageSize, total, onPage, className }: PaginationProps) {
  const { t } = useTranslation()

  const size = pageSize > 0 ? pageSize : 1
  const totalPages = Math.max(1, Math.ceil(total / size))
  const current = Math.min(Math.max(1, Math.trunc(page)), totalPages)

  if (totalPages <= 1) return null

  return (
    <nav className={cx(styles.pagination, className)} aria-label={t('common.pagination')}>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => onPage(current - 1)}
        disabled={current <= 1}
      >
        {t('common.previous')}
      </Button>

      <ul className={styles.pages}>
        {buildSlots(current, totalPages).map((slot) =>
          typeof slot === 'string' ? (
            <li key={slot} className={styles.gap} aria-hidden="true" />
          ) : (
            <li key={slot}>
              <Button
                variant={slot === current ? 'primary' : 'ghost'}
                size="sm"
                className={styles.page}
                aria-current={slot === current ? 'page' : undefined}
                aria-label={t('common.page', { page: slot, total: totalPages })}
                onClick={() => onPage(slot)}
              >
                {slot}
              </Button>
            </li>
          ),
        )}
      </ul>

      <p className={styles.summary}>{t('common.page', { page: current, total: totalPages })}</p>

      <Button
        variant="ghost"
        size="sm"
        onClick={() => onPage(current + 1)}
        disabled={current >= totalPages}
      >
        {t('common.next')}
      </Button>
    </nav>
  )
}

export default Pagination
