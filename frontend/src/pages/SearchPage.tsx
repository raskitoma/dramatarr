/** Search — the visible payoff of the multilingual index (§7).
 *
 * When a result matched something other than its display title — a native title, a
 * romanization, an alternate — that is stated right under the title. Without it a hit on
 * `miqing` looks like a bug rather than the feature it is.
 */

import { useEffect, useId, useMemo, useRef, useState, type ComponentProps } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useSearch as useLocationSearch } from 'wouter'

import { SEARCH_DEBOUNCE_MS, SEARCH_MIN_QUERY_LENGTH, useSearch } from '@/api/library'
import type { EpisodeHit, SeriesHit } from '@/api/types'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { episodeLabelParts } from '@/components/EpisodeRow'
import { Field } from '@/components/Field'
import { Input } from '@/components/Input'
import { SeriesCard } from '@/components/SeriesCard'
import { Spinner } from '@/components/Spinner'
import { episodeHref, matchedVia } from '@/components/searchHits'
import { SearchIcon } from '@/components/icons'

import styles from './SearchPage.module.css'


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

  const urlQuery = useMemo(() => new URLSearchParams(searchString).get('q') ?? '', [searchString])

  const [input, setInput] = useState(urlQuery)
  const [query, setQuery] = useState(urlQuery)
  /* Remembers what this page last pushed into the URL, so its own writes are not mistaken for
     an external navigation and echoed back over what the user is currently typing. */
  const lastWritten = useRef(urlQuery)

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(input.trim()), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [input])

  useEffect(() => {
    if (query === lastWritten.current) return
    lastWritten.current = query
    // Replace, so a session of typing leaves one history entry, not thirty.
    navigate(query ? `/search?q=${encodeURIComponent(query)}` : '/search', { replace: true })
  }, [query, navigate])

  useEffect(() => {
    if (urlQuery === lastWritten.current) return
    lastWritten.current = urlQuery
    setInput(urlQuery)
    setQuery(urlQuery)
  }, [urlQuery])

  const results = useSearch(query)
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
  const alternate = matchedVia(hit.matchedTitle, hit.series.title)
  return (
    <SeriesCard
      series={hit.series}
      note={alternate ? t('search.matchedVia', { title: alternate }) : undefined}
    />
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
