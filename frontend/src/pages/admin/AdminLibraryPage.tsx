/** Library curation: the series table and the pile of files the scanner could not place.
 *
 * Two tabs rather than two routes — an admin fixing a bad scan bounces between "what do I
 * have" and "what could not be matched" constantly, and a tab keeps both one keystroke away.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  useEffect,
  useRef,
  useState,
  type ComponentProps,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import {
  adminKeys,
  assignUnmatched,
  cleanupLibrary,
  isEmptyCleanup,
  listAdminSeries,
  listUnmatched,
  refreshSeries,
  type AdminSeriesQuery,
  type AdminSeriesRow,
  type LibraryCleanupResult,
} from '@/api/admin'
import { libraryKeys } from '@/api/library'
import type { SeriesStatus, UnmatchedFile } from '@/api/types'
import DeleteSeriesDialog from '@/components/admin/DeleteSeriesDialog'
import TmdbMatchDialog from '@/components/admin/TmdbMatchDialog'
import UnmatchedTable from '@/components/admin/UnmatchedTable'
import Badge from '@/components/Badge'
import Button from '@/components/Button'
import Dialog from '@/components/Dialog'
import EmptyState from '@/components/EmptyState'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import IconButton from '@/components/IconButton'
import Input from '@/components/Input'
import Pagination from '@/components/Pagination'
import Poster from '@/components/Poster'
import Select from '@/components/Select'
import Spinner from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { cx } from '@/components/cx'
import { EditIcon, LinkIcon, RefreshIcon, SearchIcon, TrashIcon } from '@/components/icons'
import { declaredIsPlausible } from '@/lib/format'

import styles from './AdminLibraryPage.module.css'

type TabId = 'series' | 'unmatched'

const PAGE_SIZE = 20
const UNMATCHED_PAGE_SIZE = 25
const STATUS_OPTIONS: SeriesStatus[] = ['ongoing', 'completed', 'unknown']

function statusLabelKey(status: SeriesStatus): string {
  if (status === 'ongoing') return 'series.statusOngoing'
  if (status === 'completed') return 'series.statusCompleted'
  return 'series.statusUnknown'
}

/**
 * A filter box with the magnifier laid over its leading edge.
 *
 * A component rather than two siblings inside `Field`, because `Field` only wires
 * `aria-describedby` and friends when it is handed exactly one child: the wrapper has to be
 * that child and pass those props on to the real `<input>`.
 */
function SearchInput(props: ComponentProps<typeof Input>) {
  return (
    <span className={styles.searchControl}>
      <SearchIcon className={styles.searchIcon} aria-hidden="true" />
      <Input {...props} />
    </span>
  )
}

/**
 * A data cell that names its own column.
 *
 * Below 720px the table stops being a table and each row becomes a card; the header row is
 * gone by then, and a bare "2019" has lost the thing that said it was a year. The label is
 * real text rather than generated content so a screen reader reads it once the layout has
 * stacked — above the breakpoint CSS takes it back out of the document.
 */
function Cell({
  label,
  className,
  children,
}: {
  label: string
  className?: string
  children: ReactNode
}) {
  return (
    <td className={cx(styles.cell, className)}>
      <span className={styles.cellLabel}>{label}</span>
      <div className={styles.cellValue}>{children}</div>
    </td>
  )
}

export function AdminLibraryPage() {
  const { t } = useTranslation()
  const [tab, setTab] = useState<TabId>('series')
  const [cleanupOpen, setCleanupOpen] = useState(false)
  const seriesTabRef = useRef<HTMLButtonElement>(null)
  const unmatchedTabRef = useRef<HTMLButtonElement>(null)

  const onTabKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End']
    if (!keys.includes(event.key)) return
    event.preventDefault()

    const other: TabId = tab === 'series' ? 'unmatched' : 'series'
    const next: TabId =
      event.key === 'Home' ? 'series' : event.key === 'End' ? 'unmatched' : other
    setTab(next)
    const target = next === 'series' ? seriesTabRef.current : unmatchedTabRef.current
    target?.focus()
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>{t('admin.library.title')}</h1>
        {/* Missing entries are a library-wide problem, not a tab's: reachable from both. */}
        <div className={styles.headerActions}>
          <Button onClick={() => setCleanupOpen(true)}>{t('admin.library.cleanupAction')}</Button>
        </div>
      </header>

      <div
        className={styles.tabs}
        role="tablist"
        aria-label={t('admin.library.title')}
        onKeyDown={onTabKeyDown}
      >
        <button
          ref={seriesTabRef}
          type="button"
          role="tab"
          id="admin-library-tab-series"
          aria-selected={tab === 'series'}
          aria-controls="admin-library-panel-series"
          tabIndex={tab === 'series' ? 0 : -1}
          className={tab === 'series' ? styles.tabActive : styles.tab}
          onClick={() => setTab('series')}
        >
          {t('admin.library.tabSeries')}
        </button>
        <button
          ref={unmatchedTabRef}
          type="button"
          role="tab"
          id="admin-library-tab-unmatched"
          aria-selected={tab === 'unmatched'}
          aria-controls="admin-library-panel-unmatched"
          tabIndex={tab === 'unmatched' ? 0 : -1}
          className={tab === 'unmatched' ? styles.tabActive : styles.tab}
          onClick={() => setTab('unmatched')}
        >
          {t('admin.library.unmatched')}
        </button>
      </div>

      {tab === 'series' ? (
        <div
          id="admin-library-panel-series"
          role="tabpanel"
          aria-labelledby="admin-library-tab-series"
          tabIndex={-1}
        >
          <SeriesTab />
        </div>
      ) : (
        <div
          id="admin-library-panel-unmatched"
          role="tabpanel"
          aria-labelledby="admin-library-tab-unmatched"
          tabIndex={-1}
        >
          <UnmatchedTab />
        </div>
      )}

      {cleanupOpen ? <CleanupDialog onClose={() => setCleanupOpen(false)} /> : null}
    </div>
  )
}

// --- Series ---------------------------------------------------------------------------------

function SeriesTab() {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const [rawQuery, setRawQuery] = useState('')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<SeriesStatus | ''>('')
  const [page, setPage] = useState(1)
  const [matchTarget, setMatchTarget] = useState<AdminSeriesRow | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<AdminSeriesRow | null>(null)

  // Debounced only; the request itself still goes through TanStack Query.
  useEffect(() => {
    const handle = window.setTimeout(() => {
      setQuery(rawQuery)
      setPage(1)
    }, 250)
    return () => window.clearTimeout(handle)
  }, [rawQuery])

  const params: AdminSeriesQuery = {
    q: query,
    status,
    sort: 'title',
    page,
    pageSize: PAGE_SIZE,
  }

  const seriesQuery = useQuery({
    queryKey: adminKeys.seriesList(params),
    queryFn: () => listAdminSeries(params),
    placeholderData: (previous) => previous,
  })

  const refresh = useMutation({
    mutationFn: (seriesId: string) => refreshSeries(seriesId),
    onSuccess: async () => {
      show(t('admin.library.refreshQueued'), 'success')
      await queryClient.invalidateQueries({ queryKey: adminKeys.jobs() })
    },
    onError: () => show(t('errors.generic'), 'error'),
  })

  const rows = seriesQuery.data?.items ?? []
  const total = seriesQuery.data?.total ?? 0

  return (
    <section className={styles.panel}>
      <div className={styles.filters}>
        <div className={styles.filterGrow}>
          <Field label={t('common.search')} htmlFor="admin-series-filter">
            <SearchInput
              id="admin-series-filter"
              type="search"
              value={rawQuery}
              placeholder={t('admin.library.searchPlaceholder')}
              onChange={(event) => setRawQuery(event.target.value)}
            />
          </Field>
        </div>
        <div className={styles.filterFixed}>
          <Field label={t('browse.filterStatus')} htmlFor="admin-series-status">
            <Select
              id="admin-series-status"
              value={status}
              onChange={(event) => {
                setStatus(event.target.value as SeriesStatus | '')
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {STATUS_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {t(statusLabelKey(option))}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      </div>

      <p className={styles.count} aria-live="polite">
        {seriesQuery.isPending ? t('common.loading') : t('common.results', { count: total })}
      </p>

      {seriesQuery.isPending ? <Spinner label={t('common.loading')} /> : null}

      {seriesQuery.isError ? (
        <ErrorState error={seriesQuery.error} onRetry={() => void seriesQuery.refetch()} />
      ) : null}

      {!seriesQuery.isPending && !seriesQuery.isError && rows.length === 0 ? (
        <EmptyState
          title={query || status ? t('browse.noResults') : t('home.emptyLibrary')}
          description={query || status ? undefined : t('home.emptyLibraryAdmin')}
        />
      ) : null}

      {rows.length > 0 ? (
        <div className={styles.scroller}>
          <table className={styles.table}>
            <caption className="sr-only">{t('admin.library.title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('admin.library.titleValue')}</th>
                <th scope="col">{t('series.year')}</th>
                <th scope="col">{t('series.episodes')}</th>
                <th scope="col">{t('admin.library.tmdbColumn')}</th>
                <th scope="col" className={styles.actionsCell}>
                  {t('common.actions')}
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  {/* The flex row is an element inside the cell, not the cell itself: a `th`
                      set to `display: flex` stops stretching to the row's height, and its
                      bottom rule then floats halfway up a tall row. */}
                  <th scope="row" className={styles.seriesCell}>
                    <div className={styles.seriesLine}>
                      <Poster src={row.posterUrl} alt={row.title} size="sm" />
                      <span className={styles.seriesText}>
                        <Link href={`/admin/library/${row.id}`} className={styles.seriesLink}>
                          {row.title}
                        </Link>
                        {row.metadataLocked ? (
                          <span className={styles.lockBadge}>
                            <Badge tone="warning">{t('admin.library.locked')}</Badge>
                          </span>
                        ) : null}
                      </span>
                    </div>
                  </th>
                  <Cell label={t('series.year')} className={styles.numeric}>
                    {row.year ?? t('common.unknown')}
                  </Cell>
                  <Cell label={t('series.episodes')} className={styles.numeric}>
                    {declaredIsPlausible(row.availableEpisodeCount, row.declaredEpisodeCount) ? (
                      t('series.availableOf', {
                        available: row.availableEpisodeCount,
                        declared: row.declaredEpisodeCount,
                      })
                    ) : (
                      <>
                        {t('series.availableCount', { count: row.availableEpisodeCount })}
                        {/* Named rather than silently dropped: this is the visible symptom of a
                            match to the wrong show, and the admin is the one person who can
                            undo it. Hiding the discrepancy would hide the defect. */}
                        {row.declaredEpisodeCount === null ? null : (
                          <span
                            className={styles.mismatch}
                            title={t('admin.library.declaredMismatchHint', {
                              declared: row.declaredEpisodeCount,
                            })}
                          >
                            <Badge tone="warning">{t('admin.library.declaredMismatch')}</Badge>
                          </span>
                        )}
                      </>
                    )}
                  </Cell>
                  <Cell label={t('admin.library.tmdbColumn')}>
                    {row.tmdbId === undefined ? (
                      <span className={styles.muted}>{t('common.unknown')}</span>
                    ) : row.tmdbId === null ? (
                      <Badge tone="neutral">{t('admin.library.tmdbNotLinked')}</Badge>
                    ) : (
                      <Badge tone="success">{t('admin.library.tmdbLinked')}</Badge>
                    )}
                  </Cell>
                  <Cell label={t('common.actions')} className={styles.actionsCell}>
                    {/* Four glyphs instead of four wrapped word-buttons: this is the column
                        that decided the table could not fit a phone. Every one of them names
                        its series, because four identical "Delete"s in a column are
                        indistinguishable to anyone not looking at the row. */}
                    <div className={styles.rowActions}>
                      <Link
                        href={`/admin/library/${row.id}`}
                        className={styles.actionLink}
                        aria-label={t('admin.library.editFor', { title: row.title })}
                        title={t('admin.library.editFor', { title: row.title })}
                      >
                        <EditIcon />
                      </Link>
                      <IconButton
                        variant="secondary"
                        label={t('admin.library.refreshMetadataFor', { title: row.title })}
                        loading={refresh.isPending && refresh.variables === row.id}
                        disabled={refresh.isPending}
                        onClick={() => refresh.mutate(row.id)}
                      >
                        <RefreshIcon />
                      </IconButton>
                      <IconButton
                        variant="secondary"
                        label={t('admin.library.matchTmdbFor', { title: row.title })}
                        onClick={() => setMatchTarget(row)}
                      >
                        <LinkIcon />
                      </IconButton>
                      <IconButton
                        variant="danger"
                        label={t('admin.library.deleteFor', { title: row.title })}
                        onClick={() => setDeleteTarget(row)}
                      >
                        <TrashIcon />
                      </IconButton>
                    </div>
                  </Cell>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {total > PAGE_SIZE ? (
        <Pagination page={page} pageSize={PAGE_SIZE} total={total} onPage={setPage} />
      ) : null}

      {matchTarget ? (
        <TmdbMatchDialog
          open
          onClose={() => setMatchTarget(null)}
          seriesId={matchTarget.id}
          seriesTitle={matchTarget.title}
          initialQuery={matchTarget.title}
          initialYear={matchTarget.year}
          // The table already knows whether this row is linked; without it the dialog's
          // unlink tab would claim an unlinked series on a row the same table marks linked.
          currentTmdbId={matchTarget.tmdbId ?? null}
          onMatched={() => void seriesQuery.refetch()}
        />
      ) : null}

      {deleteTarget ? (
        <DeleteSeriesDialog
          open
          onClose={() => setDeleteTarget(null)}
          seriesId={deleteTarget.id}
          seriesTitle={deleteTarget.title}
          onDeleted={() => void seriesQuery.refetch()}
        />
      ) : null}
    </section>
  )
}

// --- Unmatched ------------------------------------------------------------------------------

function UnmatchedTab() {
  const { t } = useTranslation()
  const [page, setPage] = useState(1)
  const [assignTarget, setAssignTarget] = useState<UnmatchedFile | null>(null)

  const unmatchedQuery = useQuery({
    queryKey: adminKeys.unmatched(page, UNMATCHED_PAGE_SIZE),
    queryFn: () => listUnmatched(page, UNMATCHED_PAGE_SIZE),
    placeholderData: (previous) => previous,
  })

  const files = unmatchedQuery.data?.items ?? []
  const total = unmatchedQuery.data?.total ?? 0

  return (
    <section className={styles.panel}>
      <p className={styles.hint}>{t('admin.library.unmatchedHint')}</p>

      {/* One-at-a-time assignment is the wrong tool for a folder of 58 opaque filenames;
          when there is a pile here, point at the screen that plans a whole folder at once. */}
      {total > 0 ? (
        <p className={styles.hint}>
          {t('admin.import.fromUnmatchedHint')}{' '}
          <Link href="/admin/import">{t('admin.import.fromUnmatchedLink')}</Link>
        </p>
      ) : null}

      {unmatchedQuery.isPending ? <Spinner label={t('common.loading')} /> : null}

      {unmatchedQuery.isError ? (
        <ErrorState error={unmatchedQuery.error} onRetry={() => void unmatchedQuery.refetch()} />
      ) : null}

      {!unmatchedQuery.isPending && !unmatchedQuery.isError ? (
        <UnmatchedTable files={files} onAssign={setAssignTarget} />
      ) : null}

      {total > UNMATCHED_PAGE_SIZE ? (
        <Pagination page={page} pageSize={UNMATCHED_PAGE_SIZE} total={total} onPage={setPage} />
      ) : null}

      {assignTarget ? (
        <AssignDialog file={assignTarget} onClose={() => setAssignTarget(null)} />
      ) : null}
    </section>
  )
}

// --- Cleanup dialog -------------------------------------------------------------------------

interface CleanupDialogProps {
  onClose: () => void
}

/**
 * "Clean up missing entries", rehearsed before it is performed.
 *
 * The scanner never deletes a row on its own — an unplugged disk must not wipe the library —
 * so entries whose files vanished pile up as `missing` until somebody decides. That decision
 * is made against a dry run: the same call with `dryRun: true` reports exactly what would go,
 * and only then does the real one become available. No file is touched either way; the rows
 * here point at files the NAS no longer has.
 */
function CleanupDialog({ onClose }: CleanupDialogProps) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const dryRun = useQuery({
    queryKey: adminKeys.cleanupPreview(),
    queryFn: () => cleanupLibrary(true),
    // Never confirm against a count from an earlier visit.
    staleTime: 0,
    gcTime: 0,
    retry: false,
  })

  const cleanup = useMutation({
    mutationFn: () => cleanupLibrary(false),
    onSuccess: async (result: LibraryCleanupResult) => {
      show(t('admin.library.cleanupDone', { count: result.removedMediaFiles }), 'success')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: adminKeys.series() }),
        queryClient.invalidateQueries({ queryKey: adminKeys.stats() }),
        queryClient.invalidateQueries({ queryKey: [...adminKeys.all, 'unmatched'] }),
        queryClient.invalidateQueries({ queryKey: libraryKeys.all }),
      ])
      onClose()
    },
  })

  const preview = dryRun.data
  const nothingToDo = preview !== undefined && isEmptyCleanup(preview)
  const doomedSeries = preview?.removedSeries ?? []

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('admin.library.cleanupTitle')}
      description={t('admin.library.cleanupSubtitle')}
      footer={
        nothingToDo ? (
          <Button variant="ghost" onClick={onClose}>
            {t('common.close')}
          </Button>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="danger"
              loading={cleanup.isPending}
              disabled={preview === undefined || dryRun.isError}
              onClick={() => cleanup.mutate()}
            >
              {t('admin.library.cleanupConfirm')}
            </Button>
          </>
        )
      }
    >
      {dryRun.isPending ? <Spinner label={t('admin.library.cleanupChecking')} /> : null}

      {dryRun.isError ? (
        <ErrorState error={dryRun.error} onRetry={() => void dryRun.refetch()} />
      ) : null}

      {nothingToDo ? (
        <EmptyState
          title={t('admin.library.cleanupNothing')}
          description={t('admin.library.cleanupNothingHint')}
        />
      ) : null}

      {preview && !nothingToDo ? (
        <>
          <dl className={styles.cleanupStats}>
            <div className={styles.cleanupStat}>
              <dt className={styles.statLabel}>{t('admin.library.cleanupFiles')}</dt>
              <dd className={styles.statValue}>{preview.removedMediaFiles}</dd>
            </div>
            <div className={styles.cleanupStat}>
              <dt className={styles.statLabel}>{t('admin.library.cleanupEpisodes')}</dt>
              <dd className={styles.statValue}>{preview.removedEpisodes}</dd>
            </div>
            <div className={styles.cleanupStat}>
              <dt className={styles.statLabel}>{t('admin.library.cleanupSeries')}</dt>
              <dd className={styles.statValue}>{doomedSeries.length}</dd>
            </div>
          </dl>

          {/* Only series left with no episodes at all disappear; the rest just lose episodes,
              so naming these by title is the part worth reading twice. */}
          {doomedSeries.length > 0 ? (
            <div className={styles.cleanupAffected}>
              <p className={styles.cleanupAffectedTitle}>{t('admin.library.cleanupAffected')}</p>
              <ul className={styles.cleanupList}>
                {doomedSeries.map((title) => (
                  <li key={title} className={styles.cleanupItem}>
                    {title}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <p className={styles.cleanupNote}>{t('admin.library.cleanupFilesKept')}</p>
        </>
      ) : null}

      {cleanup.isError ? <ErrorState error={cleanup.error} /> : null}
    </Dialog>
  )
}

// --- Assign dialog --------------------------------------------------------------------------

interface AssignDialogProps {
  file: UnmatchedFile
  onClose: () => void
}

function AssignDialog({ file, onClose }: AssignDialogProps) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const [seriesFilter, setSeriesFilter] = useState('')
  const [debouncedFilter, setDebouncedFilter] = useState('')
  const [seriesId, setSeriesId] = useState('')
  const [season, setSeason] = useState('1')
  const [episode, setEpisode] = useState('')
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedFilter(seriesFilter), 250)
    return () => window.clearTimeout(handle)
  }, [seriesFilter])

  const pickerParams: AdminSeriesQuery = {
    q: debouncedFilter,
    sort: 'title',
    page: 1,
    pageSize: 50,
  }

  const seriesQuery = useQuery({
    queryKey: adminKeys.seriesList(pickerParams),
    queryFn: () => listAdminSeries(pickerParams),
  })

  const assign = useMutation({
    mutationFn: () =>
      assignUnmatched(file.id, {
        seriesId,
        seasonNumber: Number.parseInt(season, 10),
        episodeNumber: Number.parseInt(episode, 10),
      }),
    onSuccess: async () => {
      show(t('admin.library.assigned'), 'success')
      await queryClient.invalidateQueries({ queryKey: [...adminKeys.all, 'unmatched'] })
      await queryClient.invalidateQueries({ queryKey: adminKeys.series() })
      onClose()
    },
  })

  const seasonNumber = Number.parseInt(season, 10)
  const episodeNumber = Number.parseInt(episode, 10)
  const seriesError = touched && !seriesId ? t('common.required') : undefined
  const seasonError =
    touched && (Number.isNaN(seasonNumber) || seasonNumber < 0)
      ? t('admin.settings.invalidNumber')
      : undefined
  const episodeError =
    touched && (Number.isNaN(episodeNumber) || episodeNumber < 0)
      ? t('admin.settings.invalidNumber')
      : undefined
  const valid = !seriesError && !seasonError && !episodeError && seriesId !== ''

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setTouched(true)
    if (!seriesId || Number.isNaN(seasonNumber) || Number.isNaN(episodeNumber)) return
    assign.mutate()
  }

  const options = seriesQuery.data?.items ?? []

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('admin.library.assignTitle')}
      description={file.path}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button
            type="submit"
            form="assign-form"
            variant="primary"
            loading={assign.isPending}
            disabled={touched && !valid}
          >
            {t('admin.library.assign')}
          </Button>
        </>
      }
    >
      <form id="assign-form" className={styles.assignForm} onSubmit={onSubmit}>
        <Field label={t('common.search')} htmlFor="assign-filter">
          <Input
            id="assign-filter"
            type="search"
            value={seriesFilter}
            placeholder={t('admin.library.searchPlaceholder')}
            onChange={(event) => setSeriesFilter(event.target.value)}
          />
        </Field>

        <Field
          label={t('admin.library.assignSeries')}
          htmlFor="assign-series"
          required
          error={seriesError}
        >
          <Select
            id="assign-series"
            value={seriesId}
            invalid={Boolean(seriesError)}
            onChange={(event) => setSeriesId(event.target.value)}
          >
            <option value="">{t('common.none')}</option>
            {options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.year === null ? option.title : `${option.title} (${option.year})`}
              </option>
            ))}
          </Select>
        </Field>

        <div className={styles.numbersRow}>
          <Field
            label={t('admin.library.assignSeason')}
            htmlFor="assign-season"
            required
            error={seasonError}
          >
            <Input
              id="assign-season"
              inputMode="numeric"
              value={season}
              invalid={Boolean(seasonError)}
              onChange={(event) => setSeason(event.target.value)}
            />
          </Field>
          <Field
            label={t('admin.library.assignEpisode')}
            htmlFor="assign-episode"
            required
            error={episodeError}
          >
            <Input
              id="assign-episode"
              inputMode="numeric"
              value={episode}
              invalid={Boolean(episodeError)}
              onChange={(event) => setEpisode(event.target.value)}
            />
          </Field>
        </div>

        {assign.isError ? <ErrorState error={assign.error} /> : null}
      </form>
    </Dialog>
  )
}

export default AdminLibraryPage
