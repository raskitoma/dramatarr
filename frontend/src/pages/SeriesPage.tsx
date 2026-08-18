/** Series detail — `/series/:slug`.
 *
 * The spec's checklist for this view (§9): title, available-of-declared episode count, which
 * episodes are watched, which one is in progress and at what percent, an aggregate bar, and a
 * primary action that knows whether you are resuming, starting or rewatching. Alternate titles
 * are shown because in a multilingual library they are how you confirm this is the right show.
 *
 * Everything on that checklist stays in the header; everything else was moved out of it. The
 * episodes now sit directly under it rather than below the synopsis, and the synopsis, the
 * genres and the alternate titles follow — on a phone the point of opening a series you are
 * fifty episodes into is the episode list, not the blurb you read the first time.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useParams } from 'wouter'

import { useRefreshSeriesMetadata, useSeriesDetail, useSetSeriesWatched } from '@/api/library'
import type { EpisodeSummary, SeasonGroup, SeriesDetail, SeriesStatus, SeriesTitleOut } from '@/api/types'
import { Badge } from '@/components/Badge'
import { Button } from '@/components/Button'
import { Dialog } from '@/components/Dialog'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { EpisodeGrid } from '@/components/EpisodeGrid'
import { EpisodeRow, episodeLabelParts } from '@/components/EpisodeRow'
import { IconButton } from '@/components/IconButton'
import { Poster } from '@/components/Poster'
import { ProgressBar } from '@/components/ProgressBar'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { cx } from '@/components/cx'
import {
  CheckCircleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  EditIcon,
  ExternalLinkIcon,
  GridIcon,
  InfoIcon,
  ListIcon,
  PlayIcon,
  RefreshIcon,
  UnwatchIcon,
} from '@/components/icons'
import { useAuth } from '@/hooks/useAuth'
import { declaredIsPlausible, formatTimecode, initialsOf } from '@/lib/format'

import styles from './SeriesPage.module.css'

const STATUS_LABEL_KEY: Record<SeriesStatus, string> = {
  ongoing: 'series.statusOngoing',
  completed: 'series.statusCompleted',
  unknown: 'series.statusUnknown',
}

const STATUS_TONE: Record<SeriesStatus, 'info' | 'success' | 'neutral'> = {
  ongoing: 'info',
  completed: 'success',
  unknown: 'neutral',
}

// --- Episode layout ------------------------------------------------------------------------

type EpisodeView = 'grid' | 'list'

const VIEW_STORAGE_KEY = 'dramatarr.episodeView'

/** Null when this device has never chosen, which is what lets the series pick its own default. */
function readStoredView(): EpisodeView | null {
  try {
    const value = localStorage.getItem(VIEW_STORAGE_KEY)
    if (value === 'grid' || value === 'list') return value
  } catch {
    // Storage unavailable; the per-series default stands for this session.
  }
  return null
}

/**
 * What to show a reader who has never picked.
 *
 * Rows earn their height when episodes have their own stills and titles. When every still is
 * null and no episode is named — a short vertical drama, dozens of five-minute instalments —
 * the rows are identical strips and the matrix is strictly more readable, so that is the
 * default there. An explicit choice always wins over this.
 */
function defaultViewFor(episodes: EpisodeSummary[]): EpisodeView {
  const detailed = episodes.some((episode) => episode.stillUrl !== null || episode.title !== null)
  return detailed ? 'list' : 'grid'
}

/** Machine-generated romanizations ("miqing", "mq") are search fuel, not something to read;
 *  "Also known as" lists the titles a person would recognize. */
function humanTitles(detail: SeriesDetail): SeriesTitleOut[] {
  const seen = new Set<string>([detail.title.trim().toLocaleLowerCase()])
  if (detail.originalTitle) seen.add(detail.originalTitle.trim().toLocaleLowerCase())

  const out: SeriesTitleOut[] = []
  for (const entry of detail.titles) {
    if (entry.kind === 'romanization' || entry.kind === 'acronym') continue
    const key = entry.title.trim().toLocaleLowerCase()
    if (!key || seen.has(key)) continue
    seen.add(key)
    out.push(entry)
  }
  return out
}

/**
 * True when a CSS-clamped element is actually hiding something.
 *
 * Measured rather than guessed from the character count, because "too long" is a question about
 * the rendered width: the same synopsis fits in three lines on a desktop and spills over six on
 * a phone. Counting characters would put an "info" button on a paragraph that is fully visible
 * — a control that opens a dialog showing exactly what is already on screen — and withhold it
 * from one that is genuinely cut off.
 *
 * Re-measured on resize for the same reason. `contents` is in the dependency list so switching
 * series re-runs it; the ref itself is stable and would not.
 */
function useClipped<T extends HTMLElement>(contents: string | null | undefined) {
  const ref = useRef<T>(null)
  const [clipped, setClipped] = useState(false)

  const measure = useCallback(() => {
    const element = ref.current
    // The +1 absorbs sub-pixel line-height rounding, which otherwise reports a one-line
    // paragraph as overflowing on some zoom levels.
    setClipped(element !== null && element.scrollHeight > element.clientHeight + 1)
  }, [])

  useEffect(() => {
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [measure, contents])

  return [ref, clipped] as const
}

function EpisodeBoard({
  season,
  posterUrl,
  view,
}: {
  season: SeasonGroup
  posterUrl: string | null
  view: EpisodeView
}) {
  const { t } = useTranslation()

  if (view === 'grid') {
    return (
      <EpisodeGrid episodes={season.episodes} posterUrl={posterUrl} label={t('series.episodes')} />
    )
  }

  return (
    <ul className={styles.episodes}>
      {season.episodes.map((episode) => (
        <EpisodeRow key={episode.id} episode={episode} />
      ))}
    </ul>
  )
}

export function SeriesPage() {
  const { t } = useTranslation()
  const params = useParams()
  const [, navigate] = useLocation()
  const { isAdmin } = useAuth()
  const toast = useToast()

  const rawSlug = params['slug']
  const slug = typeof rawSlug === 'string' && rawSlug.length > 0 ? rawSlug : undefined
  const query = useSeriesDetail(slug)
  const detail = query.data

  const [selectedSeason, setSelectedSeason] = useState<number | null>(null)
  const [chosenView, setChosenView] = useState<EpisodeView | null>(readStoredView)
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const [overviewOpen, setOverviewOpen] = useState(false)
  const [overviewRef, overviewClipped] = useClipped<HTMLParagraphElement>(detail?.overview)

  const refreshMetadata = useRefreshSeriesMetadata()
  const setSeriesWatched = useSetSeriesWatched()

  const alsoKnownAs = useMemo(() => (detail ? humanTitles(detail) : []), [detail])

  const seasons: SeasonGroup[] = detail?.seasons ?? []
  /* Land on the season you would actually resume in, unless a tab was clicked. */
  const fallbackSeason = detail?.nextEpisode?.seasonNumber ?? seasons[0]?.seasonNumber ?? null
  const activeNumber =
    selectedSeason !== null && seasons.some((season) => season.seasonNumber === selectedSeason)
      ? selectedSeason
      : fallbackSeason
  const activeSeason = seasons.find((season) => season.seasonNumber === activeNumber)
  const view = chosenView ?? defaultViewFor(activeSeason?.episodes ?? [])

  const chooseView = (next: EpisodeView): void => {
    setChosenView(next)
    try {
      localStorage.setItem(VIEW_STORAGE_KEY, next)
    } catch {
      // The choice simply will not survive a reload.
    }
  }

  if (query.isLoading) {
    return (
      <div className={styles.centered}>
        <Spinner size="lg" label={t('common.loading')} />
      </div>
    )
  }

  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />
  }

  if (!detail) {
    return <EmptyState title={t('errors.NOT_FOUND')} />
  }

  const next = detail.nextEpisode
  const fullyWatched =
    detail.availableEpisodeCount > 0 && detail.watchedEpisodeCount >= detail.availableEpisodeCount

  const primaryLabel = next
    ? next.resumePositionSeconds !== null
      ? t('series.resumeFrom', { time: formatTimecode(next.resumePositionSeconds) })
      : fullyWatched
        ? t('series.rewatch')
        : t('series.play')
    : null

  // "X of Y" only while Y is still a credible total. A wrong TMDB match can declare one
  // episode for a folder holding sixty-six, and printing that would be the page asserting
  // something plainly false to a viewer who cannot fix it.
  const episodeCountText = declaredIsPlausible(
    detail.availableEpisodeCount,
    detail.declaredEpisodeCount,
  )
    ? t('series.availableOf', {
        available: detail.availableEpisodeCount,
        declared: detail.declaredEpisodeCount,
      })
    : t('series.availableCount', { count: detail.availableEpisodeCount })

  const seriesWatchedLabel = fullyWatched
    ? t('series.markSeriesUnwatched')
    : t('series.markSeriesWatched')

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number): void => {
    let target = index
    if (event.key === 'ArrowRight') target = index + 1
    else if (event.key === 'ArrowLeft') target = index - 1
    else if (event.key === 'Home') target = 0
    else if (event.key === 'End') target = seasons.length - 1
    else return

    event.preventDefault()
    const count = seasons.length
    if (count === 0) return
    const wrapped = ((target % count) + count) % count
    const season = seasons[wrapped]
    if (!season) return
    setSelectedSeason(season.seasonNumber)
    tabRefs.current[wrapped]?.focus()
  }


/**
 * Step to the title before or after this one.
 *
 * The neighbours arrive **on the series payload**, so this renders correctly the first time
 * even on a cold load of a link somebody pasted — there is no list on the client to walk, and
 * asking for one would have been a second request that a shared link could not answer anyway.
 *
 * At the ends of the catalogue the control is **disabled rather than removed**. A pair that
 * loses a button at the first and last title shifts under the cursor exactly when someone is
 * clicking through quickly, and `aria-disabled` keeps the reason readable: the label says which
 * end you are at instead of going silent.
 *
 * The order is the catalogue's A–Z — the same one Explorar shows by default — never the list
 * the viewer came through. That is why the labels say "title", not "next result".
 */
function TitleNav({ detail }: { detail: SeriesDetail }) {
  const { t } = useTranslation()

  const previousLabel =
    detail.previousSeriesSlug === null
      ? t('series.previousTitleNone')
      : t('series.previousTitle', { title: detail.previousSeriesTitle ?? '' })
  const nextLabel =
    detail.nextSeriesSlug === null
      ? t('series.nextTitleNone')
      : t('series.nextTitle', { title: detail.nextSeriesTitle ?? '' })

  return (
    <nav className={styles.titleNav} aria-label={t('series.titleNav')}>
      <NavArrow
        slug={detail.previousSeriesSlug}
        label={previousLabel}
        icon={<ChevronLeftIcon />}
      />
      <NavArrow slug={detail.nextSeriesSlug} label={nextLabel} icon={<ChevronRightIcon />} />
    </nav>
  )
}

/** One arrow: a link when there is somewhere to go, a disabled button when there is not. */
function NavArrow({
  slug,
  label,
  icon,
}: {
  slug: string | null
  label: string
  icon: ReactNode
}) {
  if (slug === null) {
    return (
      <span className={cx(styles.navArrow, styles.navArrowOff)} aria-disabled="true" title={label}>
        {icon}
        <span className="sr-only">{label}</span>
      </span>
    )
  }
  return (
    <Link href={`/series/${slug}`} className={styles.navArrow} title={label} aria-label={label}>
      {icon}
    </Link>
  )
}

  return (
    <article className={styles.page}>
      <header
        className={styles.hero}
        style={
          detail.backdropUrl !== null
            ? ({ '--backdrop': `url("${detail.backdropUrl}")` } as CSSProperties)
            : undefined
        }
        data-has-backdrop={detail.backdropUrl !== null ? 'true' : 'false'}
      >
        {/* Inside the card rather than floating above it. As its own row it read as belonging
            to the page instead of to this title, and it opened a band of whitespace between the
            header and everything else. Absolutely positioned so it costs the hero no layout —
            the two-column grid underneath is already carrying the poster, the counts and the
            action row. */}
        <TitleNav detail={detail} />

        <div className={styles.heroInner}>
          <div className={styles.heroPoster}>
            {/* The <h1> beside it already names the series, so this poster is decorative. */}
            <Poster src={detail.posterUrl} alt="" size="lg" fluid />
          </div>

          {/* Two columns on a wide screen: what the show *is* on the left, where you are in it
              and what you can do about it on the right. As one column the hero left most of a
              desktop's width empty, because a synopsis has to stop at a readable measure and
              nothing was using the rest. */}
          <div className={styles.heroText}>
            <div className={styles.heroMain}>
              <h1 className={styles.title}>{detail.title}</h1>

              <p className={styles.meta}>
                {detail.year !== null ? <span>{detail.year}</span> : null}
                {detail.network !== null ? <span>{detail.network}</span> : null}
                {detail.contentRating !== null ? <span>{detail.contentRating}</span> : null}
                <Badge tone={STATUS_TONE[detail.status]}>
                  {t(STATUS_LABEL_KEY[detail.status])}
                </Badge>
              </p>

              {/* In the hero rather than in a section below the episode list. It is the one
                  thing a person reads to decide whether to press play, and it was the only such
                  thing that required scrolling past ninety episodes to reach. */}
              <div className={styles.overviewBlock}>
                <p className={styles.heroOverview} ref={overviewRef}>
                  {detail.overview ?? t('series.noOverview')}
                </p>
                {/* Only when the clamp is actually hiding something — a button that opens a
                    dialog showing the paragraph already on screen is worse than no button. */}
                {overviewClipped ? (
                  <IconButton
                    // IconButton makes this both the accessible name and the tooltip, which is
                    // the whole budget for explaining a control with no words on it.
                    label={t('series.overviewFull')}
                    className={styles.overviewMore}
                    onClick={() => setOverviewOpen(true)}
                  >
                    <InfoIcon />
                  </IconButton>
                ) : null}
              </div>
            </div>

            <div className={styles.heroSide}>
            <p className={styles.counts}>{episodeCountText}</p>

            <div className={styles.progress}>
              <ProgressBar
                percent={detail.progressPercent}
                tone={fullyWatched ? 'success' : 'accent'}
                size="md"
                label={t('series.progressOverall')}
              />
              <p className={styles.progressLabel}>
                {t('series.watchedCount', {
                  watched: detail.watchedEpisodeCount,
                  total: detail.availableEpisodeCount,
                })}
              </p>
            </div>

            {/* Glyphs only, at every width. Every one of these keeps its words in the
                accessible name and in the tooltip — `.actionLabel` hides text from sight
                without hiding it from a screen reader, which a bare icon would. */}
            <div className={styles.actions}>
              {next && primaryLabel ? (
                <Button
                  variant="primary"
                  title={primaryLabel}
                  onClick={() => navigate(`/watch/${encodeURIComponent(next.id)}`)}
                >
                  <PlayIcon />
                  <span className={styles.actionLabel}>{primaryLabel}</span>
                </Button>
              ) : null}

              <Button
                variant="secondary"
                title={seriesWatchedLabel}
                loading={setSeriesWatched.isPending}
                onClick={() =>
                  setSeriesWatched.mutate(
                    { seriesId: detail.id, watched: !fullyWatched },
                    { onError: () => toast.show(t('errors.generic'), 'error') },
                  )
                }
              >
                {fullyWatched ? <UnwatchIcon /> : <CheckCircleIcon />}
                <span className={styles.actionLabel}>{seriesWatchedLabel}</span>
              </Button>

              {isAdmin ? (
                <>
                  <Link
                    href={`/admin/library/${encodeURIComponent(detail.id)}`}
                    className={styles.adminLink}
                    title={t('admin.library.editSeries')}
                  >
                    <EditIcon />
                    <span className={styles.actionLabel}>{t('admin.library.editSeries')}</span>
                  </Link>
                  <Button
                    // Secondary, not ghost. Four icon buttons in a row where one has no border
                    // reads as three controls and a mistake.
                    variant="secondary"
                    title={t('admin.library.refreshMetadata')}
                    loading={refreshMetadata.isPending}
                    onClick={() =>
                      refreshMetadata.mutate(detail.id, {
                        onSuccess: () => toast.show(t('series.refreshQueued'), 'success'),
                        onError: () => toast.show(t('errors.generic'), 'error'),
                      })
                    }
                  >
                    <RefreshIcon />
                    <span className={styles.actionLabel}>{t('admin.library.refreshMetadata')}</span>
                  </Button>
                </>
              ) : null}
            </div>

            {(detail.links ?? []).length > 0 ? (
              <p className={styles.watchLinks}>
                {(detail.links ?? []).map((link) => (
                  <a
                    key={link.studioKey}
                    className={styles.watchLink}
                    href={link.url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <ExternalLinkIcon size="1em" aria-hidden="true" />
                    {t('series.watchOn', { studio: link.studioName })}
                  </a>
                ))}
              </p>
            ) : null}

            {next ? (
              <p className={styles.nextUp}>
                {t('series.episodeLabel', episodeLabelParts(next))}
                {' · '}
                {next.title ?? t('series.episodeNumber', { number: next.episodeNumber })}
              </p>
            ) : null}
            </div>
          </div>
        </div>
      </header>

      <Dialog
        open={overviewOpen}
        onClose={() => setOverviewOpen(false)}
        title={detail.title}
        size="md"
      >
        <p className={styles.overviewFull}>{detail.overview ?? t('series.noOverview')}</p>
      </Dialog>

      <div className={styles.body}>
        {/* The record, above the episode list.
            It used to sit below, on the reasoning that somebody fifty episodes into a show
            wants the grid and not the blurb. That holds for the person watching and not for the
            person *deciding* — who is here to read the cast, the genres and what else the show
            is called, and had to scroll past sixty-four numbered tiles to reach any of it.
            Each section still disappears entirely when it is empty, rather than standing as a
            heading with nothing under it. */}
        {detail.originalTitle !== null || detail.genres.length > 0 ? (
        <section className={styles.block} aria-labelledby="series-details">
          <h2 className={styles.blockHeading} id="series-details">
            {t('series.details')}
          </h2>

          {detail.originalTitle !== null ? (
            <p className={styles.detailLine}>
              <span className={styles.detailLabel}>{t('series.originalTitle')}</span>
              <span>{detail.originalTitle}</span>
            </p>
          ) : null}

          {detail.genres.length > 0 ? (
            <ul className={styles.genres} aria-label={t('series.genres')}>
              {detail.genres.map((genre) => (
                <li key={genre}>
                  <Link href={`/browse?genre=${encodeURIComponent(genre)}`} className={styles.genre}>
                    {genre}
                  </Link>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
        ) : null}

        {(detail.cast ?? []).length > 0 ? (
          <section className={styles.block} aria-labelledby="series-cast">
            <h2 className={styles.blockHeading} id="series-cast">
              {t('series.cast')}
            </h2>
            {/* A strip of faces, not a row of chips.
                Each is a link, and that is the feature: this genre has no cast data anywhere
                else, so "what else is this actor in" had no answer until the indexer existed.

                The disc is initials on a tint because **DramaDex carries no person images** —
                `PersonDetail` is id, name, bio, external_ids, titles, and there is no route
                like `/titles/{id}/poster` for people. So this is the finished treatment rather
                than a placeholder waiting on a download. The shape is built for the photograph
                anyway: when the indexer starts serving one, it drops into the same disc and
                nothing else here moves. The tint is keyed off the person id, so one performer
                keeps one colour on this page and on their own. */}
            <ul className={styles.castList}>
              {(detail.cast ?? []).map((member) => (
                <li key={member.dramadexPersonId} className={styles.castItem}>
                  <Link
                    href={`/people/${member.dramadexPersonId}`}
                    className={styles.castLink}
                  >
                    <span
                      className={styles.castFace}
                      aria-hidden="true"
                      data-tint={member.dramadexPersonId % 6}
                    >
                      {initialsOf(member.name, '?')}
                    </span>
                    <span className={styles.castName}>{member.name}</span>
                    {/* Everything DramaDex has said so far is "actor", so a role line would be
                        the same word under every face. It renders only when it says something. */}
                    {member.role && member.role.toLowerCase() !== 'actor' ? (
                      <span className={styles.castRole}>{member.role}</span>
                    ) : null}
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {alsoKnownAs.length > 0 ? (
          <section className={styles.block} aria-labelledby="series-aka">
            <h2 className={styles.blockHeading} id="series-aka">
              {t('series.alsoKnownAs')}
            </h2>
            <ul className={styles.akaList}>
              {alsoKnownAs.map((entry) => (
                <li key={entry.id} className={styles.akaItem}>
                  <span>{entry.title}</span>
                  {entry.language ? <span className={styles.akaLang}>{entry.language}</span> : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        <section className={styles.block} aria-labelledby="series-episodes">
          <div className={styles.blockHeader}>
            <h2 className={styles.blockHeading} id="series-episodes">
              {t('series.episodes')}
            </h2>

            {/* Pressed state rides the design system's own filled/ghost pair rather than a
                bespoke highlight, so it survives both themes without new colors. */}
            {activeSeason && activeSeason.episodes.length > 0 ? (
              <div className={styles.viewToggle} role="group" aria-label={t('series.episodeView')}>
                <IconButton
                  label={t('series.viewGrid')}
                  variant={view === 'grid' ? 'secondary' : 'ghost'}
                  aria-pressed={view === 'grid'}
                  className={styles.viewButton}
                  onClick={() => chooseView('grid')}
                >
                  <GridIcon />
                </IconButton>
                <IconButton
                  label={t('series.viewList')}
                  variant={view === 'list' ? 'secondary' : 'ghost'}
                  aria-pressed={view === 'list'}
                  className={styles.viewButton}
                  onClick={() => chooseView('list')}
                >
                  <ListIcon />
                </IconButton>
              </div>
            ) : null}
          </div>

          {seasons.length > 1 ? (
            <div className={styles.tabs} role="tablist" aria-label={t('series.seasons')}>
              {seasons.map((season, index) => {
                const selected = season.seasonNumber === activeNumber
                return (
                  <button
                    key={season.seasonNumber}
                    type="button"
                    role="tab"
                    id={`season-tab-${season.seasonNumber}`}
                    aria-selected={selected}
                    aria-controls={`season-panel-${season.seasonNumber}`}
                    tabIndex={selected ? 0 : -1}
                    className={cx(styles.tab, selected && styles.tabActive)}
                    ref={(node) => {
                      tabRefs.current[index] = node
                    }}
                    onClick={() => setSelectedSeason(season.seasonNumber)}
                    onKeyDown={(event) => onTabKeyDown(event, index)}
                  >
                    {t('series.season', { number: season.seasonNumber })}
                  </button>
                )
              })}
            </div>
          ) : null}

          {activeSeason ? (
            seasons.length > 1 ? (
              <div
                role="tabpanel"
                id={`season-panel-${activeSeason.seasonNumber}`}
                aria-labelledby={`season-tab-${activeSeason.seasonNumber}`}
              >
                <EpisodeBoard season={activeSeason} posterUrl={detail.posterUrl} view={view} />
              </div>
            ) : (
              <EpisodeBoard season={activeSeason} posterUrl={detail.posterUrl} view={view} />
            )
          ) : (
            <EmptyState title={t('common.empty')} />
          )}
        </section>

      </div>
    </article>
  )
}
