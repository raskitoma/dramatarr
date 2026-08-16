/** Apply one edit to every selected series.
 *
 * The fields here are exactly the ones no metadata source will ever fill for this library —
 * status, year, network, age rating — and which arrive identical for a whole batch of imports.
 * Correcting them one screen at a time is work people abandon halfway, which leaves a
 * catalogue that is *partly* fixed and harder to reason about than one nobody touched.
 *
 * **Empty means "leave it alone", never "clear it".** Every control starts on a "no change"
 * value and a field the admin did not touch is not sent at all. The consequence is deliberate:
 * this bar cannot blank a field across a selection. Emptying a column on forty rows at once is
 * the one edit here with no undo and no warning sign afterwards — the rows simply look like
 * they were never filled in — so it stays a per-series edit, where the admin sees the value
 * they are erasing.
 *
 * Nothing is applied until Apply is pressed, and the button says how many rows it will touch,
 * because "40" is the number that should give someone pause, not the field they just changed.
 */

import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { ContentRatingGroup, SeriesPatch } from '@/api/admin'
import type { SeriesStatus } from '@/api/types'
import { Button } from '@/components/Button'
import { Input } from '@/components/Input'
import { Select } from '@/components/Select'

import styles from './BulkEditBar.module.css'

/** Sent for `metadataLocked`, which is a tri-state here: unchanged, lock, unlock. */
const KEEP = ''

export interface BulkEditBarProps {
  count: number
  pending: boolean
  providers: readonly string[]
  ratingGroups: readonly ContentRatingGroup[]
  onApply: (patch: SeriesPatch) => void
  onClear: () => void
}

export function BulkEditBar({
  count,
  pending,
  providers,
  ratingGroups,
  onApply,
  onClear,
}: BulkEditBarProps) {
  const { t } = useTranslation()
  const [status, setStatus] = useState<SeriesStatus | ''>('')
  const [year, setYear] = useState('')
  const [network, setNetwork] = useState('')
  const [rating, setRating] = useState('')
  const [locked, setLocked] = useState(KEEP)

  const parsedYear = Number.parseInt(year, 10)
  const yearValid = year === '' || (Number.isInteger(parsedYear) && parsedYear >= 1800 && parsedYear <= 2200)

  const patch: SeriesPatch = {}
  if (status !== '') patch.status = status
  if (year !== '' && yearValid) patch.year = parsedYear
  if (network !== '') patch.network = network
  if (rating !== '') patch.contentRating = rating
  if (locked !== KEEP) patch.metadataLocked = locked === 'locked'

  const changes = Object.keys(patch).length

  function apply(): void {
    if (changes === 0 || !yearValid) return
    onApply(patch)
    setStatus('')
    setYear('')
    setNetwork('')
    setRating('')
    setLocked(KEEP)
  }

  return (
    <div className={styles.bar} role="group" aria-label={t('admin.library.bulkTitle')}>
      <p className={styles.count}>{t('admin.library.bulkSelected', { count })}</p>

      <div className={styles.fields}>
        <label className={styles.field}>
          <span className={styles.label}>{t('series.status')}</span>
          <Select
            value={status}
            onChange={(event) => setStatus(event.target.value as SeriesStatus | '')}
          >
            <option value="">{t('admin.library.bulkKeep')}</option>
            <option value="ongoing">{t('series.statusOngoing')}</option>
            <option value="completed">{t('series.statusCompleted')}</option>
            <option value="unknown">{t('series.statusUnknown')}</option>
          </Select>
        </label>

        <label className={styles.field}>
          <span className={styles.label}>{t('series.year')}</span>
          <Input
            inputMode="numeric"
            value={year}
            placeholder={t('admin.library.bulkKeep')}
            aria-invalid={yearValid ? undefined : true}
            onChange={(event) => setYear(event.target.value)}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>{t('series.network')}</span>
          <Select value={network} onChange={(event) => setNetwork(event.target.value)}>
            <option value="">{t('admin.library.bulkKeep')}</option>
            {providers.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
        </label>

        <label className={styles.field}>
          <span className={styles.label}>{t('admin.library.contentRating')}</span>
          <Select value={rating} onChange={(event) => setRating(event.target.value)}>
            <option value="">{t('admin.library.bulkKeep')}</option>
            {ratingGroups.map((group) => (
              <optgroup
                key={group.group}
                label={t(`admin.library.contentRatingGroup.${group.group}`)}
              >
                {group.ratings.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </optgroup>
            ))}
          </Select>
        </label>

        <label className={styles.field}>
          <span className={styles.label}>{t('admin.library.locked')}</span>
          <Select value={locked} onChange={(event) => setLocked(event.target.value)}>
            <option value={KEEP}>{t('admin.library.bulkKeep')}</option>
            <option value="locked">{t('admin.library.bulkLock')}</option>
            <option value="unlocked">{t('admin.library.bulkUnlock')}</option>
          </Select>
        </label>
      </div>

      <div className={styles.actions}>
        <Button
          type="button"
          variant="primary"
          loading={pending}
          // Disabled until something would actually change, so the button cannot be the thing
          // that locks forty rows against the metadata jobs by doing "nothing".
          disabled={changes === 0 || !yearValid || pending}
          onClick={apply}
        >
          {t('admin.library.bulkApply', { count })}
        </Button>
        <Button type="button" onClick={onClear} disabled={pending}>
          {t('admin.library.bulkClear')}
        </Button>
      </div>

      <p className={styles.note}>{t('admin.library.bulkNote')}</p>
    </div>
  )
}

export default BulkEditBar
