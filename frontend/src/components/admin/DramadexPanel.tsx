/** Find this series in the household's own indexer, and fill it in one action.
 *
 * This replaces the web-search panel, and the difference is not a better search — it is that
 * there is now something to search. That panel asked a metasearch engine, scored results by
 * hostname, and could only ever hand back a link for a human to read; every synopsis in this
 * library was typed by hand afterwards. DramaDex indexes DramaBox and ReelShort directly, so
 * a row here is a record: synopsis, year, cast, platform, and the link to watch it.
 *
 * The one thing this screen must get right is **refusing to guess**. A query the index cannot
 * satisfy still returns its nearest neighbours — asking for "The Call Boy I Met in Paris"
 * offers "Call to Arms, Call to Love" — and binding one of those writes another show's
 * synopsis and another show's cast onto a series. That is not hypothetical: it is exactly how
 * the Netflix thriller *You* ended up on a Chinese surgeon drama here. So the server marks
 * the rows whose normalised title actually equals this one, those lead the list and are the
 * only ones offered as a plain "use this", and everything else is shown as a neighbour that
 * has to be chosen deliberately.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { adminKeys, applyDramadex, searchDramadex, type DramadexHit } from '@/api/admin'
import { Badge } from '@/components/Badge'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { Poster } from '@/components/Poster'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { ExternalLinkIcon, SearchIcon } from '@/components/icons'

import styles from './DramadexPanel.module.css'

export interface DramadexPanelProps {
  seriesId: string
  seriesTitle: string
  /** Called once a title has been applied, so the editor reloads the filled-in fields. */
  onApplied?: () => void
}

export function DramadexPanel({ seriesId, seriesTitle, onApplied }: DramadexPanelProps) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  /* A neighbour has to be chosen deliberately, so the "use this" on a non-matching row only
     appears after the admin says they mean it. */
  const [showNeighbours, setShowNeighbours] = useState(false)

  const search = useQuery({
    queryKey: adminKeys.dramadex(seriesTitle),
    queryFn: () => searchDramadex(seriesTitle),
    enabled: open,
    staleTime: 60_000,
  })

  const apply = useMutation({
    mutationFn: (hit: DramadexHit) => applyDramadex(seriesId, hit.id),
    onSuccess: async (detail) => {
      show(t('admin.library.dramadexApplied', { title: detail.title }), 'success')
      await queryClient.invalidateQueries({ queryKey: adminKeys.series() })
      onApplied?.()
    },
    onError: () => show(t('errors.generic'), 'error'),
  })

  const items = search.data?.items ?? []
  const matches = items.filter((hit) => hit.matches)
  const neighbours = items.filter((hit) => !hit.matches)

  function row(hit: DramadexHit, exact: boolean) {
    return (
      <li key={hit.id} className={styles.row}>
        <div className={styles.poster}>
          <Poster src={hit.posterUrl} alt="" size="sm" />
        </div>
        <div className={styles.body}>
          <p className={styles.titleLine}>
            <span className={styles.name}>{hit.title}</span>
            {hit.year === null ? null : <span className={styles.year}>{hit.year}</span>}
            {hit.studio === null ? null : <Badge tone="info">{hit.studio}</Badge>}
            {hit.inLibrary ? <Badge tone="success">{t('requests.inLibrary')}</Badge> : null}
          </p>
          {hit.episodeCount === null ? null : (
            <p className={styles.meta}>
              {t('series.episodeCount', { count: hit.episodeCount })}
              {hit.genres.length > 0 ? ` · ${hit.genres.join(', ')}` : ''}
            </p>
          )}
          {hit.synopsis ? <p className={styles.synopsis}>{hit.synopsis}</p> : null}
          {hit.cast.length > 0 ? (
            <p className={styles.cast}>{t('series.cast')}: {hit.cast.join(', ')}</p>
          ) : null}
          <div className={styles.actions}>
            <Button
              type="button"
              size="sm"
              variant={exact ? 'primary' : 'secondary'}
              loading={apply.isPending && apply.variables?.id === hit.id}
              disabled={apply.isPending}
              onClick={() => apply.mutate(hit)}
            >
              {exact ? t('admin.library.dramadexUse') : t('admin.library.dramadexUseAnyway')}
            </Button>
            {hit.watchUrl ? (
              <a
                className={styles.watch}
                href={hit.watchUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                <ExternalLinkIcon size="1em" aria-hidden="true" />
                {t('series.watchOn', { studio: hit.studio ?? 'DramaDex' })}
              </a>
            ) : null}
          </div>
        </div>
      </li>
    )
  }

  return (
    <div className={styles.panel}>
      {!open ? (
        <Button type="button" size="sm" onClick={() => setOpen(true)}>
          <SearchIcon size="1em" aria-hidden="true" />
          {t('admin.library.dramadexFind')}
        </Button>
      ) : (
        <>
          {search.isFetching ? <Spinner size="sm" label={t('common.loading')} /> : null}
          {search.isError ? (
            <ErrorState error={search.error} onRetry={() => void search.refetch()} />
          ) : null}
          {apply.isError ? <ErrorState error={apply.error} /> : null}

          {search.data && !search.data.configured ? (
            <p className={styles.note}>{t('admin.library.dramadexUnconfigured')}</p>
          ) : null}

          {search.data?.configured && items.length === 0 && !search.isFetching ? (
            <p className={styles.note}>{t('admin.library.dramadexNoResults')}</p>
          ) : null}

          {matches.length > 0 ? <ul className={styles.list}>{matches.map((h) => row(h, true))}</ul> : null}

          {neighbours.length > 0 ? (
            <div className={styles.neighbours}>
              {/* Named for what they are. A list that presented these beside a real match
                  would be inviting exactly the mistake this panel exists to prevent. */}
              <p className={styles.note}>
                {matches.length > 0
                  ? t('admin.library.dramadexAlsoFound', { count: neighbours.length })
                  : t('admin.library.dramadexOnlyNeighbours', { count: neighbours.length })}
              </p>
              {showNeighbours ? (
                <ul className={styles.list}>{neighbours.map((h) => row(h, false))}</ul>
              ) : (
                <Button type="button" size="sm" onClick={() => setShowNeighbours(true)}>
                  {t('admin.library.dramadexShowNeighbours')}
                </Button>
              )}
            </div>
          ) : null}

          <Button type="button" size="sm" onClick={() => setOpen(false)}>
            {t('common.close')}
          </Button>
        </>
      )}
    </div>
  )
}

export default DramadexPanel
