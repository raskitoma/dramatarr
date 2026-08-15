/** Find the record behind a title TMDB cannot find.
 *
 * The household's library is short-form Asian drama, and TMDB's title search misses those often
 * enough that the admin regularly ends up holding a folder name and nothing else. A plain web
 * search does not miss them — it turns up the show's TMDB, IMDb, Wikipedia or MyDramaList page
 * on the first screen. So this panel treats the web as a way to **locate the authoritative
 * record**, never as a source of text.
 *
 * That distinction is the whole design of the row:
 *
 *  - A hit whose URL carries a TMDB or IMDb id is *actionable*. Its primary action resolves the
 *    id through the same lookup the "by id" tab uses and binds the series, so recognizing the
 *    right link and matching it are one act rather than two plus a copy-paste. The id is printed
 *    on the row because it is the reason that row has a button at all.
 *  - A hit with no id is still listed and still opens, in a new tab. A MyDramaList page is how
 *    the admin learns the show's real title, which is what makes the *next* search work. Hiding
 *    it would throw away the lead.
 *
 * The snippet is quoted, muted and captioned with its domain because of what it actually is: a
 * truncated extract of someone else's page, cut wherever the engine stopped and occasionally
 * full of navigation furniture. It is here to help choose between links, it is never saved as an
 * overview, and it should not look like it could be.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import {
  adminKeys,
  discoverSeries,
  discoveryLookup,
  lookupTmdb,
  matchSeries,
  type DiscoveryResult,
} from '@/api/admin'
import Button from '@/components/Button'
import EmptyState from '@/components/EmptyState'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import Input from '@/components/Input'
import Spinner from '@/components/Spinner'
import { Switch } from '@/components/Switch'

import styles from './DiscoveryPanel.module.css'

/** Where the search provider is configured, and the sentence that names it must agree. */
const SEARCH_SETTINGS_HREF = '/admin/settings#settings-metadata'

export interface DiscoveryPanelProps {
  seriesId: string
  /** Prefills the box. Editable, because the folder name is frequently the reason we are here. */
  initialQuery: string
  /** Called once a hit has been resolved and the series bound, so the caller can close. */
  onLinked: () => void
  /** Lets the caller dismiss itself when the admin follows the link to the settings screen. */
  onNavigate?: () => void
}

/** How the row's id should be printed, or null when the row carries none. */
function idBadge(result: DiscoveryResult): { key: string; id: string | number } | null {
  if (result.tmdbId !== null) return { key: 'admin.library.discoverTmdbId', id: result.tmdbId }
  if (result.imdbId !== null) return { key: 'admin.library.discoverImdbId', id: result.imdbId }
  return null
}

export function DiscoveryPanel({
  seriesId,
  initialQuery,
  onLinked,
  onNavigate,
}: DiscoveryPanelProps) {
  const { t } = useTranslation()

  const [query, setQuery] = useState(initialQuery)
  // Prefilled *and* run: the admin opened this tab because the title search already failed.
  const [submitted, setSubmitted] = useState(initialQuery.trim())
  /* On by default. The reason this panel exists is that TMDB has never heard of most of this
     library, and the pages that *do* describe a vertical drama are on the platform that made
     it — which a general web search buries under aggregators. Off gives the wider search back
     for anything conventional. */
  const [platforms, setPlatforms] = useState(true)

  const discovery = useQuery({
    queryKey: adminKeys.discover(seriesId, submitted, platforms),
    queryFn: () => discoverSeries(seriesId, submitted, platforms),
    enabled: submitted !== '',
    staleTime: 60_000,
  })

  /**
   * Resolve, then bind — one click.
   *
   * `null` rather than a throw for an id TMDB does not know: a well-formed id that resolves to
   * nothing means the page we found is not in TMDB, which is a sentence on that row, not a red
   * box over the whole list. The other rows are still worth trying.
   */
  const link = useMutation({
    mutationFn: async (result: DiscoveryResult) => {
      const params = discoveryLookup(result)
      // Unreachable: a row without an id never renders this control.
      if (params === null) return null
      const found = await lookupTmdb(params)
      const first = found[0]
      if (first === undefined) return null
      await matchSeries(seriesId, first.tmdbId)
      return first
    },
    onSuccess: (matched) => {
      if (matched !== null) onLinked()
    },
  })

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    link.reset()
    setSubmitted(query.trim())
  }

  const data = discovery.data
  const results = data?.results ?? []
  const unconfigured = data !== undefined && !data.configured
  /** The search answered, and what it answered is a real result set worth reading. */
  const settled = !discovery.isFetching && !discovery.isError && !unconfigured
  /** The row whose lookup came back empty, so the message sits on that row and nowhere else. */
  const unresolved = link.isSuccess && link.data === null ? (link.variables ?? null) : null

  // Announced only once there is something to announce: an unconfigured provider is reported by
  // the note below, and "0 results" before anything was searched would contradict both.
  const status = discovery.isFetching
    ? t('common.loading')
    : settled && submitted !== ''
      ? t('common.results', { count: results.length })
      : ''

  return (
    <div className={styles.panel}>
      <form className={styles.form} onSubmit={onSubmit}>
        <div className={styles.queryField}>
          <Field
            label={t('admin.library.discoverQuery')}
            htmlFor="discover-query"
            hint={t('admin.library.discoverQueryHint')}
          >
            <Input
              id="discover-query"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </Field>
        </div>
        <Button type="submit" variant="primary" disabled={query.trim() === ''}>
          {t('common.search')}
        </Button>
      </form>

      <Switch
        className={styles.platformToggle}
        checked={platforms}
        onChange={setPlatforms}
        label={t('admin.library.discoverPlatforms')}
        hint={t('admin.library.discoverPlatformsHint')}
      />

      {unconfigured ? (
        <div className={styles.notice} role="status">
          <p>{t('admin.library.discoverUnconfigured')}</p>
          <Link href={SEARCH_SETTINGS_HREF} onClick={onNavigate}>
            {t('admin.library.discoverSettingsPath')}
          </Link>
        </div>
      ) : null}

      <div className={styles.results} aria-busy={discovery.isFetching}>
        <p className="sr-only" aria-live="polite">
          {status}
        </p>

        {discovery.isFetching ? <Spinner label={t('common.loading')} /> : null}

        {discovery.isError ? (
          <ErrorState error={discovery.error} onRetry={() => void discovery.refetch()} />
        ) : null}

        {link.isError ? <ErrorState error={link.error} /> : null}

        {settled && results.length > 0 ? (
          /* Said once, above the list: every snippet below belongs to someone else's page. */
          <p className={styles.snippetNote}>{t('admin.library.discoverSnippetNote')}</p>
        ) : null}

        {settled && submitted !== '' && results.length === 0 ? (
          <EmptyState
            title={t('admin.library.discoverNoResults')}
            description={t('admin.library.discoverNoResultsHint')}
          />
        ) : null}

        {results.length > 0 ? (
          <ul className={styles.list}>
            {results.map((result) => (
              <ResultRow
                key={result.url}
                result={result}
                busy={link.isPending}
                linking={link.isPending && link.variables?.url === result.url}
                unresolved={unresolved?.url === result.url}
                onLink={() => link.mutate(result)}
              />
            ))}
          </ul>
        ) : null}
      </div>
    </div>
  )
}

// --- Row --------------------------------------------------------------------------------------

interface ResultRowProps {
  result: DiscoveryResult
  /** Any resolve in flight: the rest go inert so two matches cannot race. */
  busy: boolean
  linking: boolean
  /** This row's id resolved to nothing in TMDB. */
  unresolved: boolean
  onLink: () => void
}

function ResultRow({ result, busy, linking, unresolved, onLink }: ResultRowProps) {
  const { t } = useTranslation()
  const badge = idBadge(result)
  const label = badge === null ? null : t(badge.key, { id: badge.id })

  return (
    <li className={styles.row}>
      <div className={styles.rowBody}>
        {/* The title is the link. `noopener` because these hosts are search results, not ours. */}
        <a
          className={styles.rowTitle}
          href={result.url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {result.title}
        </a>

        <p className={styles.rowMeta}>
          <span className={styles.domain}>{result.domain}</span>
          {/* The engine names itself; that name is not ours to translate. */}
          {result.engine === null ? null : <span className={styles.engine}>{result.engine}</span>}
          {label === null ? null : <span className={styles.idChip}>{label}</span>}
        </p>

        {result.snippet === '' ? null : (
          <blockquote className={styles.snippet}>
            <p className={styles.snippetText}>«{result.snippet}»</p>
            <cite className={styles.snippetCite}>
              {t('admin.library.discoverSnippetFrom', { domain: result.domain })}
            </cite>
          </blockquote>
        )}

        {unresolved ? (
          <p className={styles.rowError} role="status">
            {t('admin.library.matchIdNotFound')}
          </p>
        ) : null}
      </div>

      <div className={styles.rowActions}>
        {label === null ? (
          /* No id, so no one-step match — but the page itself is the lead worth following. */
          <a
            className={styles.openLink}
            href={result.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('admin.library.discoverOpen')}
          </a>
        ) : (
          <Button
            variant="primary"
            size="sm"
            loading={linking}
            disabled={busy}
            aria-label={t('admin.library.discoverLinkWith', { id: label })}
            onClick={onLink}
          >
            {t('admin.library.discoverLink')}
          </Button>
        )}
      </div>
    </li>
  )
}

export default DiscoveryPanel
