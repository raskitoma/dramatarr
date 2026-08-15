/** Bind a series to its TMDB entry — or let go of the one it has.
 *
 * Four tabs, because there are exactly four situations and they need different tools.
 *
 * *By title* is the common case, and it carries a **search language** rather than inheriting
 * the household's UI language: a Korean drama is findable by its Korean title only when TMDB is
 * asked in `ko`, and which alphabet the admin is typing has nothing to do with which language
 * the app is rendered in.
 *
 * *On the web* is what to reach for when the title search comes back empty, which for this
 * library's short-form Asian drama is often. TMDB is bad at finding these by name; a web search
 * is not, and the page it turns up usually carries the TMDB or IMDb id in its own URL. So the
 * web is used to locate the record, and the record is still TMDB's — see `DiscoveryPanel`.
 *
 * *By id* is the escape hatch for everything search cannot do — a remake that outranks the
 * original, a title stored under a transliteration nobody would guess. The admin usually has
 * the TMDB or IMDb page open already; pasting the id is both faster and exact.
 *
 * *Unlink* exists because a wrong match is worse than no match: it keeps overwriting good
 * manual metadata on every refresh. Undoing it must be as reachable as making it.
 *
 * Every result card shows poster, title, original title, year, an overview snippet and the vote
 * count. The vote count is not decoration: three votes and four thousand votes look identical
 * in a list of names, and it is usually the only thing that separates the real show from the
 * fan entry with the same title.
 *
 * A missing TMDB key is a configuration problem, not an error the admin should have to decode
 * from a red box: when the proxy reports it, this points straight at the settings page.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import {
  TMDB_SEARCH_LANGUAGES,
  adminKeys,
  lookupTmdb,
  matchSeries,
  searchTmdb,
  unlinkSeries,
  type TmdbSearchResult,
} from '@/api/admin'
import { ApiRequestError } from '@/api/client'
import DiscoveryPanel from '@/components/admin/DiscoveryPanel'
import Badge from '@/components/Badge'
import Button from '@/components/Button'
import Dialog from '@/components/Dialog'
import EmptyState from '@/components/EmptyState'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import Input from '@/components/Input'
import Poster from '@/components/Poster'
import Select from '@/components/Select'
import Spinner from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { languageName } from '@/lib/format'

import styles from './TmdbMatchDialog.module.css'

type MatchTab = 'search' | 'web' | 'byId' | 'unlink'

/** In the order they are worth trying: the title, then the web, then a hand-typed id. */
const TABS: readonly MatchTab[] = ['search', 'web', 'byId', 'unlink']

const TAB_LABEL_KEYS: Record<MatchTab, string> = {
  search: 'admin.library.matchTabSearch',
  web: 'admin.library.matchTabWeb',
  byId: 'admin.library.matchTabById',
  unlink: 'admin.library.matchTabUnlink',
}

interface TmdbMatchDialogProps {
  open: boolean
  onClose: () => void
  seriesId: string
  seriesTitle: string
  initialQuery?: string
  initialYear?: number | null
  /** What the series is bound to right now — the unlink tab's whole subject. */
  currentTmdbId?: number | null
  currentImdbId?: string | null
  /** Called after the match (or the unlink) is accepted, so the caller can refresh its view. */
  onMatched?: () => void
}

interface SubmittedSearch {
  q: string
  year: number | null
  language: string
}

interface SubmittedLookup {
  tmdbId: number | null
  imdbId: string
}

function codeOf(error: unknown): string | null {
  return error instanceof ApiRequestError ? error.code : null
}

function isTmdbUnconfigured(error: unknown): boolean {
  const code = codeOf(error)
  return code === 'TMDB_NOT_CONFIGURED' || code === 'TMDB_KEY_MISSING'
}

/**
 * `tt` + digits is IMDb's own format, and the server enforces the same shape because the
 * value is interpolated into a request path. Checking it here saves a round trip and answers
 * in the app's own words rather than with a validation error.
 */
function isImdbId(value: string): boolean {
  return /^tt\d{5,12}$/i.test(value.trim())
}

export function TmdbMatchDialog({
  open,
  onClose,
  seriesId,
  seriesTitle,
  initialQuery = '',
  initialYear = null,
  currentTmdbId = null,
  currentImdbId = null,
  onMatched,
}: TmdbMatchDialogProps) {
  const { t, i18n } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const [tab, setTab] = useState<MatchTab>('search')
  const [query, setQuery] = useState(initialQuery)
  const [year, setYear] = useState(initialYear === null ? '' : String(initialYear))
  const [language, setLanguage] = useState('')
  const [tmdbIdDraft, setTmdbIdDraft] = useState('')
  const [imdbIdDraft, setImdbIdDraft] = useState('')
  const [idError, setIdError] = useState<string | null>(null)
  const [submitted, setSubmitted] = useState<SubmittedSearch | null>(
    initialQuery ? { q: initialQuery, year: initialYear, language: '' } : null,
  )
  const [lookup, setLookup] = useState<SubmittedLookup | null>(null)
  const [wasOpen, setWasOpen] = useState(open)

  // Reopening the dialog starts from the series' own title again rather than the last search.
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) {
      setTab('search')
      setQuery(initialQuery)
      setYear(initialYear === null ? '' : String(initialYear))
      setLanguage('')
      setTmdbIdDraft('')
      setImdbIdDraft('')
      setIdError(null)
      setLookup(null)
      setSubmitted(initialQuery ? { q: initialQuery, year: initialYear, language: '' } : null)
    }
  }

  const search = useQuery({
    queryKey: adminKeys.tmdbSearch(
      submitted?.q ?? '',
      submitted?.year ?? null,
      submitted?.language ?? '',
    ),
    queryFn: () => searchTmdb(submitted?.q ?? '', submitted?.year ?? null, submitted?.language),
    enabled: open && submitted !== null && submitted.q.trim().length > 0,
    staleTime: 60_000,
  })

  const byId = useQuery({
    queryKey: adminKeys.tmdbLookup(lookup?.tmdbId ?? null, lookup?.imdbId ?? ''),
    queryFn: () => lookupTmdb({ tmdbId: lookup?.tmdbId ?? null, imdbId: lookup?.imdbId ?? '' }),
    enabled: open && lookup !== null,
    staleTime: 60_000,
  })

  /** Every route into a match ends here — the card, the id lookup, and the web panel. */
  const afterMatch = async () => {
    show(t('admin.library.matched'), 'success')
    await queryClient.invalidateQueries({ queryKey: adminKeys.series() })
    onMatched?.()
    onClose()
  }

  const match = useMutation({
    mutationFn: (tmdbId: number) => matchSeries(seriesId, tmdbId),
    onSuccess: afterMatch,
  })

  const unlink = useMutation({
    mutationFn: () => unlinkSeries(seriesId),
    onSuccess: async () => {
      show(t('admin.library.matchUnlinked'), 'success')
      await queryClient.invalidateQueries({ queryKey: adminKeys.series() })
      onMatched?.()
      onClose()
    },
  })

  const onSubmitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const parsedYear = Number.parseInt(year, 10)
    setSubmitted({
      q: query.trim(),
      year: Number.isNaN(parsedYear) ? null : parsedYear,
      language,
    })
  }

  const onSubmitLookup = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const rawTmdb = tmdbIdDraft.trim()
    const rawImdb = imdbIdDraft.trim()

    if (rawTmdb !== '') {
      const parsed = Number.parseInt(rawTmdb, 10)
      if (Number.isNaN(parsed) || parsed <= 0) {
        setIdError(t('admin.library.matchIdInvalid'))
        return
      }
      setIdError(null)
      setLookup({ tmdbId: parsed, imdbId: '' })
      return
    }

    if (rawImdb !== '') {
      if (!isImdbId(rawImdb)) {
        setIdError(t('admin.library.matchIdInvalid'))
        return
      }
      setIdError(null)
      setLookup({ tmdbId: null, imdbId: rawImdb.toLowerCase() })
      return
    }

    setIdError(t('admin.library.matchIdRequired'))
  }

  // Roving tabs: ←/→ moves between them, which is what a tablist is expected to do.
  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
    if (delta === 0) return
    event.preventDefault()
    const index = TABS.indexOf(tab)
    const next = TABS[(index + delta + TABS.length) % TABS.length]
    if (!next) return
    setTab(next)
    // Roving tabindex: the tab that is selected is the one that holds focus, otherwise Tab
    // would land back on a control the arrow keys just moved away from.
    document.getElementById(`tmdb-tab-${next}`)?.focus()
  }

  const results: TmdbSearchResult[] = search.data ?? []
  // A list of one for a TMDB id, several for an IMDb id that TMDB's `/find` maps loosely.
  const found: TmdbSearchResult[] = byId.data ?? []
  const unconfigured =
    isTmdbUnconfigured(search.error) ||
    isTmdbUnconfigured(byId.error) ||
    isTmdbUnconfigured(match.error)
  const linked = currentTmdbId !== null || (currentImdbId !== null && currentImdbId !== '')

  const languageOptions = TMDB_SEARCH_LANGUAGES.map((code) => ({
    code,
    label: languageName(code, i18n.language),
  })).sort((a, b) => a.label.localeCompare(b.label, i18n.language))

  const selectResult = (result: TmdbSearchResult) => {
    match.mutate(result.tmdbId)
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t('admin.library.matchTmdb')}
      description={t('admin.library.matchTmdbHint')}
      size="lg"
      footer={
        <Button variant="ghost" onClick={onClose}>
          {t('common.close')}
        </Button>
      }
    >
      <p className={styles.subject}>{seriesTitle}</p>

      <div className={styles.tabs} role="tablist" aria-label={t('admin.library.matchTmdb')}>
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            id={`tmdb-tab-${name}`}
            aria-selected={tab === name}
            aria-controls={`tmdb-panel-${name}`}
            tabIndex={tab === name ? 0 : -1}
            className={tab === name ? `${styles.tab} ${styles.tabActive}` : styles.tab}
            onKeyDown={onTabKeyDown}
            onClick={() => setTab(name)}
          >
            {t(TAB_LABEL_KEYS[name])}
          </button>
        ))}
      </div>

      {unconfigured ? (
        <div className={styles.notice} role="status">
          <p>{t('errors.TMDB_NOT_CONFIGURED')}</p>
          <Link href="/admin/settings" onClick={onClose}>
            {t('nav.settings')}
          </Link>
        </div>
      ) : null}

      {/* --- By title ------------------------------------------------------------------- */}
      {tab === 'search' ? (
        <div id="tmdb-panel-search" role="tabpanel" aria-labelledby="tmdb-tab-search">
          <form className={styles.form} onSubmit={onSubmitSearch}>
            <div className={styles.queryField}>
              <Field label={t('common.search')} htmlFor="tmdb-query">
                <Input
                  id="tmdb-query"
                  type="search"
                  value={query}
                  placeholder={t('admin.library.matchSearchPlaceholder')}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </Field>
            </div>
            <div className={styles.languageField}>
              <Field
                label={t('admin.library.matchLanguage')}
                htmlFor="tmdb-language"
                hint={t('admin.library.matchLanguageHint')}
              >
                <Select
                  id="tmdb-language"
                  value={language}
                  onChange={(event) => setLanguage(event.target.value)}
                >
                  <option value="">{t('admin.library.matchLanguageDefault')}</option>
                  {languageOptions.map((option) => (
                    <option key={option.code} value={option.code}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <div className={styles.yearField}>
              <Field label={t('series.year')} htmlFor="tmdb-year" hint={t('common.optional')}>
                <Input
                  id="tmdb-year"
                  inputMode="numeric"
                  value={year}
                  onChange={(event) => setYear(event.target.value)}
                />
              </Field>
            </div>
            <Button type="submit" variant="primary" disabled={query.trim().length === 0}>
              {t('common.search')}
            </Button>
          </form>

          <div className={styles.results} aria-busy={search.isFetching}>
            <p className="sr-only" aria-live="polite">
              {search.isFetching
                ? t('common.loading')
                : t('common.results', { count: results.length })}
            </p>

            {search.isFetching ? <Spinner label={t('common.loading')} /> : null}

            {!unconfigured && search.isError ? (
              <ErrorState error={search.error} onRetry={() => void search.refetch()} />
            ) : null}

            {!unconfigured && match.isError ? <ErrorState error={match.error} /> : null}

            {!search.isFetching && !search.isError && submitted !== null && results.length === 0 ? (
              <EmptyState title={t('admin.library.matchNoResults')} />
            ) : null}

            {results.length > 0 ? (
              <ul className={styles.list}>
                {results.map((result) => (
                  <ResultCard
                    key={result.tmdbId}
                    result={result}
                    busy={match.isPending}
                    selecting={match.isPending && match.variables === result.tmdbId}
                    current={result.tmdbId === currentTmdbId}
                    onSelect={() => selectResult(result)}
                  />
                ))}
              </ul>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* --- On the web ----------------------------------------------------------------- */}
      {tab === 'web' ? (
        <div id="tmdb-panel-web" role="tabpanel" aria-labelledby="tmdb-tab-web">
          <DiscoveryPanel
            seriesId={seriesId}
            // The series' own title, which is what the failed TMDB search was about.
            initialQuery={initialQuery || seriesTitle}
            onLinked={() => void afterMatch()}
            onNavigate={onClose}
          />
        </div>
      ) : null}

      {/* --- By id ---------------------------------------------------------------------- */}
      {tab === 'byId' ? (
        <div id="tmdb-panel-byId" role="tabpanel" aria-labelledby="tmdb-tab-byId">
          <form className={styles.form} onSubmit={onSubmitLookup}>
            <div className={styles.queryField}>
              <Field
                label={t('admin.library.matchTmdbId')}
                htmlFor="tmdb-id"
                hint={t('admin.library.matchTmdbIdHint')}
              >
                <Input
                  id="tmdb-id"
                  inputMode="numeric"
                  autoComplete="off"
                  spellCheck={false}
                  value={tmdbIdDraft}
                  invalid={idError !== null && tmdbIdDraft.trim() !== ''}
                  onChange={(event) => {
                    setTmdbIdDraft(event.target.value)
                    setIdError(null)
                  }}
                />
              </Field>
            </div>
            <div className={styles.queryField}>
              <Field
                label={t('admin.library.matchImdbId')}
                htmlFor="imdb-id"
                hint={t('admin.library.matchImdbIdHint')}
              >
                <Input
                  id="imdb-id"
                  autoComplete="off"
                  spellCheck={false}
                  autoCapitalize="none"
                  value={imdbIdDraft}
                  invalid={idError !== null && imdbIdDraft.trim() !== ''}
                  onChange={(event) => {
                    setImdbIdDraft(event.target.value)
                    setIdError(null)
                  }}
                />
              </Field>
            </div>
            <Button type="submit" variant="primary">
              {t('admin.library.matchLookup')}
            </Button>
          </form>

          {idError ? (
            <p className={styles.idError} role="alert">
              {idError}
            </p>
          ) : null}

          <div className={styles.results} aria-busy={byId.isFetching}>
            {byId.isFetching ? <Spinner label={t('common.loading')} /> : null}

            {!unconfigured && byId.isError ? (
              <ErrorState error={byId.error} onRetry={() => void byId.refetch()} />
            ) : null}

            {!unconfigured && match.isError ? <ErrorState error={match.error} /> : null}

            {!byId.isFetching && !byId.isError && lookup !== null && found.length === 0 ? (
              <EmptyState
                title={t('admin.library.matchIdNotFound')}
                description={t('admin.library.matchIdNotFoundHint')}
              />
            ) : null}

            {found.length > 0 ? (
              <ul className={styles.list}>
                {found.map((result) => (
                  <ResultCard
                    key={result.tmdbId}
                    result={result}
                    busy={match.isPending}
                    selecting={match.isPending && match.variables === result.tmdbId}
                    current={result.tmdbId === currentTmdbId}
                    onSelect={() => selectResult(result)}
                  />
                ))}
              </ul>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* --- Unlink --------------------------------------------------------------------- */}
      {tab === 'unlink' ? (
        <div
          id="tmdb-panel-unlink"
          role="tabpanel"
          aria-labelledby="tmdb-tab-unlink"
          className={styles.unlinkPanel}
        >
          <dl className={styles.currentLink}>
            <div>
              <dt>{t('admin.library.matchTmdbId')}</dt>
              <dd>{currentTmdbId === null ? t('common.none') : String(currentTmdbId)}</dd>
            </div>
            <div>
              <dt>{t('admin.library.matchImdbId')}</dt>
              <dd>{currentImdbId ? currentImdbId : t('common.none')}</dd>
            </div>
          </dl>

          {linked ? (
            <>
              <p className={styles.unlinkBody}>{t('admin.library.matchUnlinkBody')}</p>
              <p className={styles.unlinkBody}>{t('admin.library.matchUnlinkKeeps')}</p>
              {unlink.isError ? <ErrorState error={unlink.error} /> : null}
              <div className={styles.unlinkActions}>
                <Button
                  variant="danger"
                  loading={unlink.isPending}
                  onClick={() => unlink.mutate()}
                >
                  {t('admin.library.matchUnlink')}
                </Button>
              </div>
            </>
          ) : (
            <EmptyState
              title={t('admin.library.matchNotLinked')}
              description={t('admin.library.matchNotLinkedHint')}
            />
          )}
        </div>
      ) : null}
    </Dialog>
  )
}

// --- Result card ------------------------------------------------------------------------------

interface ResultCardProps {
  result: TmdbSearchResult
  /** Any match in flight: every other button goes inert so two cannot race. */
  busy: boolean
  selecting: boolean
  /** Already the series' match — worth saying, so nobody re-binds what is bound. */
  current: boolean
  onSelect: () => void
}

function ResultCard({ result, busy, selecting, current, onSelect }: ResultCardProps) {
  const { t, i18n } = useTranslation()

  const votes = result.voteCount ?? null
  const original =
    result.originalName && result.originalName !== result.name ? result.originalName : null

  return (
    <li className={styles.item}>
      <Poster src={result.posterUrl} alt={result.name} size="sm" />
      <div className={styles.itemBody}>
        <p className={styles.itemTitle}>{result.name}</p>
        {original ? (
          <p className={styles.original}>
            {original}
            {result.originalLanguage
              ? ` · ${languageName(result.originalLanguage, i18n.language)}`
              : ''}
          </p>
        ) : null}
        <p className={styles.itemMeta}>
          <span>
            {typeof result.year === 'number' ? String(result.year) : t('common.unknown')}
          </span>
          <span>
            {votes === null || votes === 0
              ? t('admin.library.matchNoVotes')
              : t('admin.library.matchVotes', { count: votes })}
          </span>
          <span className={styles.idChip}>#{result.tmdbId}</span>
          {current ? <Badge tone="success">{t('admin.library.matchCurrent')}</Badge> : null}
        </p>
        {result.overview ? (
          <p className={styles.overview}>{result.overview}</p>
        ) : (
          <p className={styles.overviewMuted}>{t('series.noOverview')}</p>
        )}
      </div>
      <Button variant="primary" size="sm" loading={selecting} disabled={busy} onClick={onSelect}>
        {t('admin.library.matchSelect')}
      </Button>
    </li>
  )
}

export default TmdbMatchDialog
