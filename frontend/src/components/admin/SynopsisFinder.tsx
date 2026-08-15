/** Find the page that describes this series, on the platforms the household configured.
 *
 * This replaces the Wikipedia synopsis fetcher, which was removed for the plain reason that it
 * did not work here: Wikipedia has articles about television, and almost none about vertical
 * short-form drama. Asking it about this library returned nothing, over and over.
 *
 * What replaces it is deliberately **less automatic**, and that is not a downgrade — it is the
 * only honest option. The pages that describe these shows are the platforms' own, and what a
 * web search returns for them is a *snippet*: someone else's page cut mid-sentence, sometimes
 * with a cookie banner in it. Wikipedia could be fetched and stored because it publishes an
 * extract meant to be read by a program, with a licence that says so. A search snippet is
 * neither, so this finds the page and stops. The admin opens it, reads it, and writes the
 * synopsis in their own words — which is also the only version that will be in the right
 * language for the household.
 *
 * So: one click to find, one tab to read, one paste. The screen says exactly that, because a
 * panel that looked like it would fill the field and then did not would be worse than no panel.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  adminKeys,
  discoverSeries,
  discoveryLookup,
  applyImdbMetadata,
  lookupTmdb,
  matchSeries,
  type DiscoveryResult,
} from '@/api/admin'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { ExternalLinkIcon, SearchIcon } from '@/components/icons'

import styles from './SynopsisFinder.module.css'

export interface SynopsisFinderProps {
  seriesId: string
  seriesTitle: string
  /** Called once a result has been applied, so the editor can reload the filled-in fields. */
  onApplied?: () => void
}

export function SynopsisFinder({ seriesId, seriesTitle, onApplied }: SynopsisFinderProps) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  /* Starts narrowed to the household's platforms. Widening is a deliberate second action —
     the search does not do it silently, because when it did, an admin whose platform domains
     are thinly indexed got a generic web search every time and no sign that their filter had
     been dropped. */
  const [platforms, setPlatforms] = useState(true)
  /* The URL of the row whose replace is awaiting a yes. Only one at a time, and nothing is
     armed by default — replacing destroys text a person wrote, so it costs two clicks. */
  const [confirming, setConfirming] = useState<string | null>(null)

  const search = useQuery({
    queryKey: adminKeys.discover(seriesId, seriesTitle, platforms),
    queryFn: () => discoverSeries(seriesId, seriesTitle, platforms),
    // Only when asked: it reaches the household's SearXNG instance.
    enabled: open,
    staleTime: 60_000,
  })

  /**
   * Resolve the id the result's URL carries, then bind it — one action.
   *
   * This is what "pull the metadata into the fields" actually is. The row itself carries only
   * a headline and a link, and a search snippet is not a synopsis. But a row whose URL holds
   * an IMDb or TMDB id names a *record*, and binding that record hands the series to the
   * metadata job, which fills title, year, overview, genres, network, rating and artwork from
   * a source built to be read by a program.
   *
   * `null` rather than a throw when the id resolves to nothing: a well-formed IMDb id that
   * TMDB has never heard of is a sentence on that one row, not a failure of the whole list.
   */
  const apply = useMutation({
    mutationFn: async (result: DiscoveryResult) => {
      const params = discoveryLookup(result)
      if (params === null) return null

      // TMDB first: it carries more (episodes, translations, artwork galleries) and is wired
      // into the refresh job, so binding there fills far more than a single record can.
      const found = await lookupTmdb(params)
      const first = found[0]
      if (first !== undefined) {
        await matchSeries(seriesId, first.tmdbId)
        return first.name
      }

      // TMDB has never heard of it. For this library that is the *normal* case — measured on
      // all four series — so it is a fallback rather than an error, provided the row named an
      // IMDb id and a key is configured.
      if (result.imdbId) {
        const filled = await applyImdbMetadata(seriesId, result.imdbId)
        return filled.title
      }
      return null
    },
    onSuccess: async (matched) => {
      if (matched === null) return
      show(t('admin.library.synopsisApplied', { title: matched }), 'success')
      await queryClient.invalidateQueries({ queryKey: adminKeys.series() })
      onApplied?.()
    },
  })

  /**
   * The same fill, authorised to overwrite fields that already have something in them.
   *
   * Separate from `apply` on purpose, in two ways. It is a **second, explicit action**, because
   * the ordinary button fills blanks only and that default is load-bearing: this household
   * writes its synopses in Spanish, OMDb's plot is English, and coming back for the year must
   * not cost somebody the paragraph they wrote. And it goes **straight to OMDb**, skipping the
   * TMDB attempt — "replace with the IMDb record" should do exactly that, not quietly bind to
   * a TMDB row where overwriting means nothing.
   */
  const replace = useMutation({
    mutationFn: async (result: DiscoveryResult) => {
      if (!result.imdbId) return null
      const detail = await applyImdbMetadata(seriesId, result.imdbId, true)
      return detail.title
    },
    onSuccess: async (replaced) => {
      setConfirming(null)
      if (replaced === null) return
      show(t('admin.library.synopsisReplaced', { title: replaced }), 'success')
      await queryClient.invalidateQueries({ queryKey: adminKeys.series() })
      onApplied?.()
    },
  })

  const busy = apply.isPending || replace.isPending

  const results = search.data?.results ?? []
  /* Named separately because it changes what an empty list *means*: with engines down, "no
     results" is a statement about the instance, not about the world. */
  const down = search.data?.unresponsiveEngines ?? []

  return (
    <div className={styles.finder}>
      {!open ? (
        <Button type="button" size="sm" onClick={() => setOpen(true)}>
          <SearchIcon size="1em" aria-hidden="true" />
          {t('admin.library.synopsisFind')}
        </Button>
      ) : (
        <>
          <p className={styles.note}>{t('admin.library.synopsisNote')}</p>

          {search.isFetching ? <Spinner size="sm" label={t('common.loading')} /> : null}
          {search.isError ? (
            <ErrorState error={search.error} onRetry={() => void search.refetch()} />
          ) : null}

          {search.data && !search.data.configured ? (
            <p className={styles.note}>{t('admin.library.discoverUnconfigured')}</p>
          ) : null}

          {down.length > 0 ? (
            <p className={styles.warning} role="status">
              {t('admin.library.searchEnginesDown', {
                count: down.length,
                engines: down.join(', '),
              })}
            </p>
          ) : null}

          {search.data && search.data.configured && results.length === 0 && !search.isFetching ? (
            <div className={styles.empty}>
              <p className={styles.note}>
                {search.data.narrowed
                  ? t('admin.library.synopsisNoneOnPlatforms')
                  : t('admin.library.synopsisNoResults')}
              </p>
              {search.data.narrowed ? (
                <Button type="button" size="sm" onClick={() => setPlatforms(false)}>
                  {t('admin.library.synopsisWiden')}
                </Button>
              ) : null}
            </div>
          ) : null}

          {results.length > 0 && !search.data?.narrowed ? (
            <p className={styles.note}>{t('admin.library.synopsisWideNote')}</p>
          ) : null}

          {apply.isError ? <ErrorState error={apply.error} /> : null}
          {replace.isError ? <ErrorState error={replace.error} /> : null}
          {apply.isSuccess && apply.data === null ? (
            <p className={styles.note} role="status">
              {t('admin.library.matchIdNotFound')}
            </p>
          ) : null}

          {results.length > 0 ? (
            <ul className={styles.list}>
              {results.slice(0, 8).map((result) => {
                const id = result.tmdbId ?? result.imdbId
                return (
                  <li key={result.url} className={styles.row}>
                    <a
                      className={styles.link}
                      href={result.url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      <ExternalLinkIcon size="1em" aria-hidden="true" />
                      {result.title || result.domain}
                    </a>
                    <span className={styles.domain}>{result.domain}</span>
                    {/* Only rows that name a record can be applied. The rest stay links —
                        there is nothing structured behind them to pull. */}
                    {id === null || id === undefined ? null : (
                      <Button
                        type="button"
                        size="sm"
                        variant="primary"
                        loading={apply.isPending && apply.variables?.url === result.url}
                        disabled={busy}
                        onClick={() => apply.mutate(result)}
                      >
                        {t('admin.library.synopsisApply')}
                      </Button>
                    )}
                    {/* Replacing is offered only where it can actually be honoured: an IMDb
                        row, which is the one path that takes an overwrite flag. */}
                    {result.imdbId && confirming !== result.url ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => setConfirming(result.url)}
                      >
                        {t('admin.library.synopsisReplace')}
                      </Button>
                    ) : null}
                    {confirming === result.url ? (
                      <span className={styles.confirm}>
                        {/* Names the fields, because "replace" on its own does not say what
                            is about to be lost — and one of them is prose in the household's
                            own language, about to be traded for English. */}
                        <span className={styles.confirmText}>
                          {t('admin.library.synopsisReplaceConfirm')}
                        </span>
                        <Button
                          type="button"
                          size="sm"
                          variant="danger"
                          loading={replace.isPending}
                          disabled={busy}
                          onClick={() => replace.mutate(result)}
                        >
                          {t('admin.library.synopsisReplaceYes')}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          disabled={replace.isPending}
                          onClick={() => setConfirming(null)}
                        >
                          {t('common.cancel')}
                        </Button>
                      </span>
                    ) : null}
                  </li>
                )
              })}
            </ul>
          ) : null}

          <Button type="button" size="sm" onClick={() => setOpen(false)}>
            {t('common.close')}
          </Button>
        </>
      )}
    </div>
  )
}

export default SynopsisFinder
