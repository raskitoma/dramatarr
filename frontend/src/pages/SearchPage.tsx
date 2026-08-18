/** Search — the visible payoff of the multilingual index (§7).
 *
 * When a result matched something other than its display title — a native title, a
 * romanization, an alternate — that is stated right under the title. Without it a hit on
 * `miqing` looks like a bug rather than the feature it is.
 */

import { useEffect, useId, useMemo, useRef, useState, type ComponentProps } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useSearch as useLocationSearch } from 'wouter'

import {
  SEARCH_DEBOUNCE_MS,
  SEARCH_MIN_QUERY_LENGTH,
  SERIES_STATUSES,
  isSeriesStatus,
  useGenres,
  useSearch,
  useYears,
  type SeriesFacets,
} from '@/api/library'
import type { EpisodeHit, SeriesHit, SeriesStatus } from '@/api/types'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { episodeLabelParts } from '@/components/EpisodeRow'
import { Field } from '@/components/Field'
import { Input } from '@/components/Input'
import { Select } from '@/components/Select'
import { SeriesCard } from '@/components/SeriesCard'
import { Spinner } from '@/components/Spinner'
import { Switch } from '@/components/Switch'
import { episodeHref, matchNote, matchedVia } from '@/components/searchHits'
import { SearchIcon } from '@/components/icons'

import styles from './SearchPage.module.css'

const STATUS_LABEL_KEY: Record<SeriesStatus, string> = {
  ongoing: 'series.statusOngoing',
  completed: 'series.statusCompleted',
  unknown: 'series.statusUnknown',
}

/** Everything in the query string except the query itself. Read from the URL rather than from
 *  state for the same reason Explorar does it: a narrowed search is then a link. */
function facetsFrom(params: URLSearchParams): SeriesFacets {
  const status = params.get('status')
  const year = Number.parseInt(params.get('year') ?? '', 10)
  return {
    genre: params.get('genre') || undefined,
    status: isSeriesStatus(status) ? status : undefined,
    year: Number.isFinite(year) && year > 0 ? year : undefined,
    availableOnly: params.get('availableOnly') === '1',
  }
}


/**
 * The query box with a magnifier laid over its leading edge.
 *
 * It is a component rather than two siblings inside `Field` on purpose: `Field` only wires
 * `aria-describedby` and friends when it is handed exactly one child, so the wrapper has to be
 * that child and forward those props down to the real `<input>`.
 */
function SearchInput(props: ComponentProps<typeof Input>) {
  return (
    <span className={styles.searchControl}>
      <SearchIcon className={styles.searchIcon} aria-hidden="true" />
      <Input {...props} />
    </span>
  )
}

export function SearchPage() {
  const { t } = useTranslation()
  const [, navigate] = useLocation()
  const searchString = useLocationSearch()
  const inputId = useId()

  const params = useMemo(() => new URLSearchParams(searchString), [searchString])
  const urlQuery = params.get('q') ?? ''
  const facets = useMemo(() => facetsFrom(params), [params])
  const genres = useGenres()
  const years = useYears()

  const [input, setInput] = useState(urlQuery)
  const [query, setQuery] = useState(urlQuery)
  /* Remembers what this page last pushed into the URL, so its own writes are not mistaken for
     an external navigation and echoed back over what the user is currently typing. */
  const lastWritten = useRef(urlQuery)

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(input.trim()), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [input])

  /** Writes one part of the query string and keeps the rest.
   *
   *  It has to read `window.location.search` rather than close over `searchString`: the
   *  debounce means the query is written from an effect while a filter may be written from a
   *  click, and a stale copy of the parameters would silently drop whichever one landed first.
   */
  const update = (changes: Record<string, string | null>, replace = false): void => {
    const next = new URLSearchParams(window.location.search)
    for (const [key, value] of Object.entries(changes)) {
      if (value === null || value === '') next.delete(key)
      else next.set(key, value)
    }
    const qs = next.toString()
    navigate(qs ? `/search?${qs}` : '/search', { replace })
  }

  useEffect(() => {
    if (query === lastWritten.current) return
    lastWritten.current = query
    // Replace, so a session of typing leaves one history entry, not thirty.
    update({ q: query || null }, true)
    // `update` is recreated every render; the effect is keyed on the query alone on purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  useEffect(() => {
    if (urlQuery === lastWritten.current) return
    lastWritten.current = urlQuery
    setInput(urlQuery)
    setQuery(urlQuery)
  }, [urlQuery])

  const results = useSearch(query, facets)
  const active = query.length >= SEARCH_MIN_QUERY_LENGTH
  /* `keepPreviousData` would otherwise leave the last full result set on screen while the
     query is too short to run — results that no longer match what the box says. */
  const data = active ? results.data : undefined
  const seriesHits = data?.series ?? []
  const episodeHits = data?.episodes ?? []
  const total = seriesHits.length + episodeHits.length

  return (
    <div className={styles.page}>
      <form className={styles.form} role="search" onSubmit={(event) => event.preventDefault()}>
        <Field label={t('common.search')} htmlFor={inputId} hint={t('search.hint')}>
          <SearchInput
            id={inputId}
            type="search"
            autoComplete="off"
            value={input}
            placeholder={t('search.placeholder')}
            onChange={(event) => setInput(event.target.value)}
          />
        </Field>
      </form>

      {/* Always on screen rather than revealed once there are results: a member who wants
          "2026 romances" sets the year and then types, and a panel that appears underneath
          them mid-sentence moves the box they are typing into. */}
      <div className={styles.filters}>
        <Field label={t('browse.filterGenre')} htmlFor={`${inputId}-genre`}>
          <Select
            id={`${inputId}-genre`}
            value={facets.genre ?? ''}
            onChange={(event) => update({ genre: event.target.value })}
          >
            <option value="">{t('common.all')}</option>
            {(genres.data ?? []).map((facet) => (
              <option key={facet.genre} value={facet.genre}>
                {`${facet.genre} (${facet.count})`}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={t('search.filterYear')} htmlFor={`${inputId}-year`}>
          <Select
            id={`${inputId}-year`}
            value={facets.year ? String(facets.year) : ''}
            onChange={(event) => update({ year: event.target.value })}
          >
            <option value="">{t('common.all')}</option>
            {(years.data ?? []).map((facet) => (
              <option key={facet.year} value={String(facet.year)}>
                {`${facet.year} (${facet.count})`}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={t('browse.filterStatus')} htmlFor={`${inputId}-status`}>
          <Select
            id={`${inputId}-status`}
            value={facets.status ?? ''}
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

        <div className={styles.switchCell}>
          <Switch
            className={styles.switch}
            checked={facets.availableOnly ?? false}
            onChange={(checked) => update({ availableOnly: checked ? '1' : null })}
            label={t('browse.filterAvailable')}
          />
        </div>
      </div>

      <p className={styles.status} aria-live="polite">
        {active && data ? (
          <>
            <span>{t('search.resultsFor', { query: data.query || query })}</span>
            <span className={styles.statusMeta}>
              {t('common.results', { count: total })}
              {' · '}
              {t('search.tookMs', { ms: Math.round(data.tookMs) })}
            </span>
          </>
        ) : null}
      </p>

      {results.isError ? (
        <ErrorState error={results.error} onRetry={() => void results.refetch()} />
      ) : active && results.isLoading ? (
        <div className={styles.centered}>
          <Spinner size="lg" label={t('common.loading')} />
        </div>
      ) : active && data && total === 0 ? (
        <EmptyState
          title={t('search.noResults', { query })}
          description={t('search.noResultsHint')}
        />
      ) : (
        <>
          {seriesHits.length > 0 ? (
            <section className={styles.section} aria-labelledby="search-series">
              <h2 className={styles.heading} id="search-series">
                {t('search.seriesSection')}
              </h2>
              <ul className={styles.grid}>
                {seriesHits.map((hit) => (
                  <li key={hit.series.id}>
                    <SeriesHitCard hit={hit} />
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {episodeHits.length > 0 ? (
            <section className={styles.section} aria-labelledby="search-episodes">
              <h2 className={styles.heading} id="search-episodes">
                {t('search.episodesSection')}
              </h2>
              <ul className={styles.episodes}>
                {episodeHits.map((hit) => (
                  <EpisodeHitRow key={hit.episode.id} hit={hit} />
                ))}
              </ul>
            </section>
          ) : null}
        </>
      )}
    </div>
  )
}

function SeriesHitCard({ hit }: { hit: SeriesHit }) {
  const { t } = useTranslation()
  const note = matchNote(hit)
  return (
    <SeriesCard series={hit.series} note={note ? t(note.key, { title: note.title }) : undefined} />
  )
}

function EpisodeHitRow({ hit }: { hit: EpisodeHit }) {
  const { t } = useTranslation()
  const { episode } = hit

  const title = episode.title ?? t('series.episodeNumber', { number: episode.episodeNumber })
  const alternate = matchedVia(hit.matchedTitle, title)
  // Shared with the header's dropdown, which is the point: it used to build this by hand and
  // fall back to `seriesId`, which the `/series/:slug` route resolves too — so it worked by
  // accident and put a hex string in the address bar.
  const href = episodeHref(hit)

  return (
    <li className={styles.episode}>
      <Link href={href} className={styles.episodeLink}>
        <span className={styles.episodeSeries}>{hit.seriesTitle}</span>
        <span className={styles.episodeTitle}>
          <span className={styles.episodeCode}>{t('series.episodeLabel', episodeLabelParts(episode))}</span>
          <span>{title}</span>
        </span>
        {alternate ? (
          <span className={styles.episodeNote}>{t('search.matchedVia', { title: alternate })}</span>
        ) : null}
        {!episode.available ? (
          <span className={styles.episodeUnavailable}>{t('series.notAvailable')}</span>
        ) : null}
      </Link>
    </li>
  )
}
