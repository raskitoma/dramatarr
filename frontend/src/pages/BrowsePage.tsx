/** Browse — the whole catalogue as a poster grid.
 *
 * Every filter lives in the query string rather than in component state: a filtered view is
 * then shareable, survives a reload, and the back button walks through filter changes.
 */

import { useId, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useLocation, useSearch } from 'wouter'

import {
  DEFAULT_PAGE_SIZE,
  SERIES_SORTS,
  SERIES_STATUSES,
  isSeriesSort,
  isSeriesStatus,
  useGenres,
  useSeriesList,
  useYears,
  type SeriesSort,
} from '@/api/library'
import type { SeriesStatus } from '@/api/types'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { Field } from '@/components/Field'
import { Pagination } from '@/components/Pagination'
import { Select } from '@/components/Select'
import { SeriesCard } from '@/components/SeriesCard'
import { Spinner } from '@/components/Spinner'
import { Switch } from '@/components/Switch'

import styles from './BrowsePage.module.css'

const STATUS_LABEL_KEY: Record<SeriesStatus, string> = {
  ongoing: 'series.statusOngoing',
  completed: 'series.statusCompleted',
  unknown: 'series.statusUnknown',
}

const SORT_LABEL_KEY: Record<SeriesSort, string> = {
  title: 'browse.sortTitle',
  added: 'browse.sortAdded',
  recent: 'browse.sortRecent',
  progress: 'browse.sortProgress',
}

function parsePage(raw: string | null): number {
  const parsed = Number.parseInt(raw ?? '', 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 1
}

export function BrowsePage() {
  const { t } = useTranslation()
  const [, navigate] = useLocation()
  const searchString = useSearch()
  const fieldId = useId()

  const params = useMemo(() => new URLSearchParams(searchString), [searchString])

  const genre = params.get('genre') ?? ''
  const statusParam = params.get('status')
  const status = isSeriesStatus(statusParam) ? statusParam : undefined
  const sortParam = params.get('sort')
  const sort: SeriesSort = isSeriesSort(sortParam) ? sortParam : 'title'
  const availableOnly = params.get('availableOnly') === '1'
  const yearParam = Number.parseInt(params.get('year') ?? '', 10)
  const year = Number.isFinite(yearParam) && yearParam > 0 ? yearParam : undefined
  const page = parsePage(params.get('page'))

  const genres = useGenres()
  const years = useYears()

  /* Both facet endpoints tally the whole `series` table — no q, no sibling filter, no
     availableOnly — so a count is only true while it is the only thing narrowing the grid.
     With two filters active, 23 of this catalogue's 45 genre×year pairs put a non-zero count
     above an empty grid. Being right when alone is what teaches the reader to trust it, so
     the number goes away rather than becoming a number that is sometimes wrong. */
  const narrowed = [genre, status, year ? String(year) : '', availableOnly ? '1' : ''].filter(
    Boolean,
  ).length > 1
  const list = useSeriesList({
    genre: genre || undefined,
    status,
    year,
    sort,
    availableOnly,
    page,
    pageSize: DEFAULT_PAGE_SIZE,
  })

  /** Writes the filter change back to the URL. Any filter change resets to page 1, because
   *  page 7 of the previous filter is meaningless under the new one. */
  const update = (changes: Record<string, string | null>, keepPage = false): void => {
    const next = new URLSearchParams(searchString)
    for (const [key, value] of Object.entries(changes)) {
      if (value === null || value === '') next.delete(key)
      else next.set(key, value)
    }
    if (!keepPage) next.delete('page')
    const qs = next.toString()
    navigate(qs ? `/browse?${qs}` : '/browse')
  }

  const total = list.data?.total ?? 0
  const items = list.data?.items ?? []

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>{t('browse.title')}</h1>

      <div className={styles.filters}>
        <Field label={t('browse.filterGenre')} htmlFor={`${fieldId}-genre`}>
          <Select
            id={`${fieldId}-genre`}
            value={genre}
            onChange={(event) => update({ genre: event.target.value })}
          >
            <option value="">{t('common.all')}</option>
            {(genres.data ?? []).map((facet) => (
              <option key={facet.genre} value={facet.genre}>
                {narrowed ? facet.genre : `${facet.genre} (${facet.count})`}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={t('search.filterYear')} htmlFor={`${fieldId}-year`}>
          <Select
            id={`${fieldId}-year`}
            value={year ? String(year) : ''}
            onChange={(event) => update({ year: event.target.value })}
          >
            <option value="">{t('common.all')}</option>
            {(years.data ?? []).map((facet) => (
              <option key={facet.year} value={String(facet.year)}>
                {narrowed ? String(facet.year) : `${facet.year} (${facet.count})`}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={t('browse.filterStatus')} htmlFor={`${fieldId}-status`}>
          <Select
            id={`${fieldId}-status`}
            value={status ?? ''}
            onChange={(event) => update({ status: event.target.value })}
          >
            <option value="">{t('common.all')}</option>
            {SERIES_STATUSES.map((value) => (
              <option key={value} value={value}>
                {t(STATUS_LABEL_KEY[value])}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={t('browse.sort')} htmlFor={`${fieldId}-sort`}>
          <Select
            id={`${fieldId}-sort`}
            value={sort}
            onChange={(event) => update({ sort: event.target.value })}
          >
            {SERIES_SORTS.map((value) => (
              <option key={value} value={value}>
                {t(SORT_LABEL_KEY[value])}
              </option>
            ))}
          </Select>
        </Field>

        <div className={styles.switchCell}>
          <Switch
            className={styles.switch}
            checked={availableOnly}
            onChange={(checked) => update({ availableOnly: checked ? '1' : null })}
            label={t('browse.filterAvailable')}
          />
        </div>
      </div>

      <p className={styles.count} aria-live="polite">
        {list.isFetching ? t('common.loading') : t('common.results', { count: total })}
      </p>

      {list.isError ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : list.isLoading ? (
        <div className={styles.centered}>
          <Spinner size="lg" label={t('common.loading')} />
        </div>
      ) : items.length === 0 ? (
        <EmptyState title={t('browse.noResults')} />
      ) : (
        <>
          <ul className={styles.grid}>
            {items.map((series) => (
              <li key={series.id}>
                <SeriesCard series={series} />
              </li>
            ))}
          </ul>

          <div className={styles.pagination}>
            <Pagination
              page={page}
              pageSize={list.data?.pageSize ?? DEFAULT_PAGE_SIZE}
              total={total}
              onPage={(next) => update({ page: next > 1 ? String(next) : null }, true)}
            />
          </div>
        </>
      )}
    </div>
  )
}
