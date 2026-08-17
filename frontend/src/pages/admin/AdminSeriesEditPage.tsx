/** The metadata editor for one series.
 *
 * The spec's rule is "the admin can edit, but the program fetches when it can": any edit sets
 * `metadata_locked`, which makes automatic refreshes skip this row. That trade-off is stated
 * on screen — before the first edit as a warning, afterwards as a banner with an explicit
 * unlock — because a silent toggle would make a series quietly stop updating forever.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useParams } from 'wouter'

import { useGenres } from '@/api/library'

import {
  addSeriesTitle,
  adminKeys,
  deleteSeriesTitle,
  getAdminEpisode,
  getAdminSeries,
  getVocabulary,
  patchEpisode,
  patchSeries,
  refreshSeries,
  type ContentRatingGroup,
  type SeriesPatch,
} from '@/api/admin'
import type {
  EpisodeSummary,
  SeriesDetail,
  SeriesStatus,
  SeriesTitleOut,
} from '@/api/types'
import ArtworkPicker from '@/components/admin/ArtworkPicker'
import RenamePanel from '@/components/admin/RenamePanel'
import DramadexPanel from '@/components/admin/DramadexPanel'
import { TocLayout } from '@/components/admin/TocLayout'
import Badge from '@/components/Badge'
import Button from '@/components/Button'
import Dialog from '@/components/Dialog'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import IconButton from '@/components/IconButton'
import Input from '@/components/Input'
import Select from '@/components/Select'
import Poster from '@/components/Poster'
import Spinner from '@/components/Spinner'
import Textarea from '@/components/Textarea'
import { useToast } from '@/components/Toast'
import { formatBytes, formatDuration } from '@/lib/format'

import styles from './AdminSeriesEditPage.module.css'

/** Sentinel for the "not on the list" option in the two pickers below. */
const OTHER = '__other__'

/**
 * Which platform the show came from.
 *
 * A picker rather than a text box because the value is a proper noun with one correct
 * spelling, and free text produced exactly what this household's database holds: `Dramabox`
 * and `Reelshort` for platforms that write themselves DramaBox and ReelShort. Two spellings of
 * one platform are two entries in every filter that groups by it.
 *
 * It is still open-ended. A value the list does not carry — a new platform, a real
 * broadcaster — selects "other" and gets a text box, and the server preserves it. A stored
 * value that is not on the list is added to the options so that merely opening the editor
 * cannot silently discard it.
 */
function ProviderField({
  value,
  providers,
  onChange,
}: {
  value: string
  providers: readonly string[]
  onChange: (value: string) => void
}) {
  const { t } = useTranslation()
  const known = value !== '' && providers.includes(value)
  const [custom, setCustom] = useState(!known && value !== '')

  return (
    <Field label={t('series.network')} htmlFor="series-network" hint={t('admin.library.networkHint')}>
      {custom ? (
        <div className={styles.tagInput}>
          <Input
            id="series-network"
            value={value}
            placeholder={t('admin.library.networkOtherPlaceholder')}
            onChange={(event) => onChange(event.target.value)}
          />
          <Button
            type="button"
            onClick={() => {
              setCustom(false)
              onChange('')
            }}
          >
            {t('admin.library.networkFromList')}
          </Button>
        </div>
      ) : (
        <Select
          id="series-network"
          value={value}
          onChange={(event) => {
            if (event.target.value === OTHER) {
              setCustom(true)
              onChange('')
              return
            }
            onChange(event.target.value)
          }}
        >
          <option value="">{t('common.none')}</option>
          {providers.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
          <option value={OTHER}>{t('admin.library.networkOther')}</option>
        </Select>
      )}
    </Field>
  )
}

/**
 * The age rating, as the standard set.
 *
 * Grouped into film and TV rather than listed flat: `PG-13` and `TV-14` are two scales, and one
 * list invites reading them as points on the same one. A stored value from outside the set —
 * TMDB returns per-country certifications like `R15+` or `12` — is kept as its own option
 * rather than being reset to blank by the act of opening the page.
 */
function ContentRatingField({
  value,
  groups,
  onChange,
}: {
  value: string
  groups: readonly ContentRatingGroup[]
  onChange: (value: string) => void
}) {
  const { t } = useTranslation()
  const listed = groups.some((group) => group.ratings.includes(value))

  return (
    <Field label={t('admin.library.contentRating')} htmlFor="series-rating">
      <Select
        id="series-rating"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{t('admin.library.contentRatingNone')}</option>
        {value !== '' && !listed ? <option value={value}>{value}</option> : null}
        {groups.map((group) => (
          <optgroup key={group.group} label={t(`admin.library.contentRatingGroup.${group.group}`)}>
            {group.ratings.map((rating) => (
              <option key={rating} value={rating}>
                {rating}
              </option>
            ))}
          </optgroup>
        ))}
      </Select>
    </Field>
  )
}

/** The three parts of the editor. All render whenever a series has loaded. */
const EDIT_SECTIONS = [
  { id: 'series-metadata', labelKey: 'admin.library.editSeries' },
  { id: 'series-titles', labelKey: 'admin.library.titles' },
  { id: 'series-cast', labelKey: 'series.cast' },
  { id: 'series-episodes', labelKey: 'series.episodes' },
  { id: 'series-rename', labelKey: 'admin.library.renameTitle' },
] as const

const STATUSES: SeriesStatus[] = ['ongoing', 'completed', 'unknown']
const TITLE_KINDS: SeriesTitleOut['kind'][] = [
  'official',
  'native',
  'romanization',
  'alternate',
  'acronym',
]

interface SeriesForm {
  title: string
  originalTitle: string
  overview: string
  year: string
  status: SeriesStatus
  genres: string[]
  network: string
  contentRating: string
}

const EMPTY_FORM: SeriesForm = {
  title: '',
  originalTitle: '',
  overview: '',
  year: '',
  status: 'unknown',
  genres: [],
  network: '',
  contentRating: '',
}

function toForm(detail: SeriesDetail): SeriesForm {
  return {
    title: detail.title,
    originalTitle: detail.originalTitle ?? '',
    overview: detail.overview ?? '',
    year: detail.year === null ? '' : String(detail.year),
    status: detail.status,
    genres: [...detail.genres],
    network: detail.network ?? '',
    contentRating: detail.contentRating ?? '',
  }
}

function sameForm(a: SeriesForm, b: SeriesForm): boolean {
  return (
    a.title === b.title &&
    a.originalTitle === b.originalTitle &&
    a.overview === b.overview &&
    a.year === b.year &&
    a.status === b.status &&
    a.network === b.network &&
    a.contentRating === b.contentRating &&
    a.genres.length === b.genres.length &&
    a.genres.every((genre, index) => genre === b.genres[index])
  )
}

function statusLabelKey(status: SeriesStatus): string {
  if (status === 'ongoing') return 'series.statusOngoing'
  if (status === 'completed') return 'series.statusCompleted'
  return 'series.statusUnknown'
}

function CrossIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
      <path
        d="M4 4l8 8M12 4l-8 8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  )
}

interface AdminSeriesEditPageProps {
  /** wouter passes route params here when the route is wired with `component=`. */
  params?: { id?: string }
}

export function AdminSeriesEditPage({ params }: AdminSeriesEditPageProps = {}) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()
  const [, navigate] = useLocation()
  const routeParams = useParams()
  const seriesId = params?.id ?? routeParams['id'] ?? ''

  const detailQuery = useQuery({
    queryKey: adminKeys.seriesDetail(seriesId),
    queryFn: () => getAdminSeries(seriesId),
    enabled: seriesId !== '',
  })
  const detail = detailQuery.data

  const [seed, setSeed] = useState<string | null>(null)
  const [form, setForm] = useState<SeriesForm>(EMPTY_FORM)
  const [baseline, setBaseline] = useState<SeriesForm>(EMPTY_FORM)
  const [genreDraft, setGenreDraft] = useState('')
  const [showArtwork, setShowArtwork] = useState(false)
  const [zoomOpen, setZoomOpen] = useState(false)
  const [leaveTo, setLeaveTo] = useState<string | null>(null)
  const [touched, setTouched] = useState(false)

  // The option lists for the two pickers. Effectively static, so it is fetched once and held.
  const vocabulary = useQuery({
    queryKey: adminKeys.vocabulary(),
    queryFn: getVocabulary,
    staleTime: Infinity,
  })

  /* Every genre already in use across the library. Without this an admin retypes a tag they
     have used ten times, and one slip makes `Secret Baby` and `Secret baby` two genres that
     look identical in a chip and filter as two — which is how the live rows got the way they
     did. The server title-cases on write, so picking an existing one is exact by construction. */
  const knownGenres = useGenres()

  // Seed the form the first time this series arrives, and whenever the route changes series.
  if (detail && seed !== detail.id) {
    const next = toForm(detail)
    setSeed(detail.id)
    setForm(next)
    setBaseline(next)
    setTouched(false)
  }

  const dirty = seed !== null && !sameForm(form, baseline)

  // The browser's own guard: covers reloads, closing the tab and following an external link.
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const save = useMutation({
    mutationFn: (patch: SeriesPatch) => patchSeries(seriesId, patch),
    onSuccess: async (updated) => {
      const next = toForm(updated)
      setForm(next)
      setBaseline(next)
      setTouched(false)
      show(t('common.saved'), 'success')
      queryClient.setQueryData(adminKeys.seriesDetail(seriesId), updated)
      await queryClient.invalidateQueries({ queryKey: adminKeys.series() })
    },
  })

  const unlock = useMutation({
    mutationFn: () => patchSeries(seriesId, { metadataLocked: false }),
    onSuccess: async (updated) => {
      queryClient.setQueryData(adminKeys.seriesDetail(seriesId), updated)
      await queryClient.invalidateQueries({ queryKey: adminKeys.series() })
    },
  })

  const refresh = useMutation({
    mutationFn: () => refreshSeries(seriesId),
    onSuccess: () => show(t('admin.library.refreshQueued'), 'success'),
    onError: () => show(t('errors.generic'), 'error'),
  })

  const yearNumber = form.year === '' ? null : Number.parseInt(form.year, 10)
  const titleError = touched && form.title.trim() === '' ? t('common.required') : undefined
  const yearError =
    touched && form.year !== '' && (yearNumber === null || Number.isNaN(yearNumber))
      ? t('admin.settings.invalidNumber')
      : undefined

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setTouched(true)
    if (form.title.trim() === '') return
    if (form.year !== '' && (yearNumber === null || Number.isNaN(yearNumber))) return

    const patch: SeriesPatch = {}
    if (form.title !== baseline.title) patch.title = form.title.trim()
    if (form.originalTitle !== baseline.originalTitle) {
      patch.originalTitle = form.originalTitle.trim() || null
    }
    if (form.overview !== baseline.overview) patch.overview = form.overview.trim() || null
    if (form.year !== baseline.year) patch.year = yearNumber
    if (form.status !== baseline.status) patch.status = form.status
    if (form.network !== baseline.network) patch.network = form.network.trim() || null
    if (form.contentRating !== baseline.contentRating) {
      patch.contentRating = form.contentRating.trim() || null
    }
    const genresChanged =
      form.genres.length !== baseline.genres.length ||
      form.genres.some((genre, index) => genre !== baseline.genres[index])
    if (genresChanged) patch.genres = form.genres

    save.mutate(patch)
  }

  const addGenre = () => {
    const value = genreDraft.trim()
    if (value === '' || form.genres.includes(value)) {
      setGenreDraft('')
      return
    }
    setForm({ ...form, genres: [...form.genres, value] })
    setGenreDraft('')
  }

  const onGenreKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== 'Enter') return
    event.preventDefault()
    addGenre()
  }

  const leave = (href: string) => {
    if (dirty) setLeaveTo(href)
    else navigate(href)
  }

  if (seriesId === '') {
    return <ErrorState error={new Error('missing id')} />
  }

  if (detailQuery.isPending) {
    return (
      <div className={styles.center}>
        <Spinner size="lg" label={t('common.loading')} />
      </div>
    )
  }

  if (detailQuery.isError || !detail) {
    return <ErrorState error={detailQuery.error} onRetry={() => void detailQuery.refetch()} />
  }

  return (
    <div className={styles.page}>
      <div className={styles.breadcrumb}>
        <Button variant="ghost" size="sm" onClick={() => leave('/admin/library')}>
          {t('common.back')}
        </Button>
      </div>

      <header className={styles.header}>
        {/* The poster is here because it is what the choice is about: the picker's grid is
            only meaningful next to the art the series shows today. At `sm` it is too small to
            judge — these covers carry the title in small type and a platform watermark, both
            of which decide whether this is the right art — so it opens full size. A button
            rather than a clickable div, because it is one. */}
        {detail.posterUrl === null ? (
          <Poster src={null} alt={detail.title} size="sm" />
        ) : (
          <button
            type="button"
            className={styles.posterZoom}
            title={t('admin.library.posterZoom')}
            aria-label={t('admin.library.posterZoom')}
            onClick={() => setZoomOpen(true)}
          >
            <Poster src={detail.posterUrl} alt={detail.title} size="sm" />
          </button>
        )}
        <div className={styles.headerText}>
          <h1 className={styles.title}>{detail.title}</h1>
          <p className={styles.path}>{detail.libraryPath}</p>
        </div>
        <div className={styles.headerActions}>
          <Button onClick={() => refresh.mutate()} loading={refresh.isPending}>
            {t('admin.library.refreshMetadata')}
          </Button>
          <Button onClick={() => setShowArtwork(true)}>{t('admin.library.artworkAction')}</Button>
        </div>
      </header>

      {detail.metadataLocked ? (
        <div className={styles.lockedBanner} role="status">
          <div>
            <p className={styles.bannerTitle}>
              <Badge tone="warning">{t('admin.library.locked')}</Badge>
            </p>
            <p className={styles.bannerBody}>{t('admin.library.lockedHint')}</p>
          </div>
          <Button onClick={() => unlock.mutate()} loading={unlock.isPending}>
            {t('admin.library.unlock')}
          </Button>
        </div>
      ) : (
        <p className={styles.lockHint}>{t('admin.library.lockOnEditHint')}</p>
      )}

      <TocLayout sections={EDIT_SECTIONS} labelKey="admin.library.tocLabel">
      {/* --- Metadata ------------------------------------------------------------------- */}
      <form className={styles.card} id="series-metadata" onSubmit={onSubmit}>
        <h2 className={styles.sectionTitle}>{t('admin.library.editSeries')}</h2>

        <div className={styles.grid}>
          <div className={styles.spanAll}>
            <Field
              label={t('admin.library.titleValue')}
              htmlFor="series-title"
              required
              error={titleError}
            >
              <Input
                id="series-title"
                value={form.title}
                invalid={Boolean(titleError)}
                onChange={(event) => setForm({ ...form, title: event.target.value })}
              />
            </Field>
          </div>

          <div className={styles.spanAll}>
            <Field label={t('series.originalTitle')} htmlFor="series-original-title">
              <Input
                id="series-original-title"
                value={form.originalTitle}
                onChange={(event) => setForm({ ...form, originalTitle: event.target.value })}
              />
            </Field>
          </div>

          <div className={styles.spanAll}>
            <Field label={t('series.overview')} htmlFor="series-overview">
              <Textarea
                id="series-overview"
                rows={5}
                value={form.overview}
                onChange={(event) => setForm({ ...form, overview: event.target.value })}
              />
            </Field>
            {/* The only finder on this screen. The web-search panel that used to sit beneath
                it, and the TMDB match dialog above, are gone from the UI: DramaDex now
                resolves all 17 series in this library where between them they resolved two,
                and offering three finders invites picking the wrong one. The code behind them
                is still in the repository — this is a removal from the interface, not a
                deletion — so the settings that configure them stay meaningful for anyone who
                needs to go back. */}
            <DramadexPanel
              seriesId={detail.id}
              seriesTitle={detail.title}
              onApplied={() => setSeed(null)}
            />
          </div>

          <Field label={t('series.year')} htmlFor="series-year" error={yearError}>
            <Input
              id="series-year"
              inputMode="numeric"
              value={form.year}
              invalid={Boolean(yearError)}
              onChange={(event) => setForm({ ...form, year: event.target.value })}
            />
          </Field>

          <Field label={t('series.status')} htmlFor="series-status">
            <Select
              id="series-status"
              value={form.status}
              onChange={(event) =>
                setForm({ ...form, status: event.target.value as SeriesStatus })
              }
            >
              {STATUSES.map((status) => (
                <option key={status} value={status}>
                  {t(statusLabelKey(status))}
                </option>
              ))}
            </Select>
          </Field>

          <ProviderField
            value={form.network}
            providers={vocabulary.data?.providers ?? []}
            onChange={(network) => setForm({ ...form, network })}
          />

          <ContentRatingField
            value={form.contentRating}
            groups={vocabulary.data?.contentRatings ?? []}
            onChange={(contentRating) => setForm({ ...form, contentRating })}
          />

          <div className={styles.spanAll}>
            <Field label={t('series.genres')} htmlFor="series-genre-draft">
              <div className={styles.tagInput}>
                <Input
                  id="series-genre-draft"
                  list="series-genre-options"
                  autoComplete="off"
                  placeholder={t('admin.library.genrePlaceholder')}
                  value={genreDraft}
                  onChange={(event) => setGenreDraft(event.target.value)}
                  onKeyDown={onGenreKeyDown}
                />
                <Button type="button" onClick={addGenre} disabled={genreDraft.trim() === ''}>
                  {t('common.add')}
                </Button>
              </div>
            </Field>
            {/* A native datalist rather than a custom combobox: it is one element, the browser
                already knows how to filter and navigate it, and it does not stop the admin
                typing a genre the library has never seen. Tags already on *this* series are
                excluded — offering one that is a no-op to add is just noise. */}
            <datalist id="series-genre-options">
              {(knownGenres.data ?? [])
                .filter((facet) => !form.genres.includes(facet.genre))
                .map((facet) => (
                  <option key={facet.genre} value={facet.genre} />
                ))}
            </datalist>

            {form.genres.length > 0 ? (
              <ul className={styles.tags}>
                {form.genres.map((genre) => (
                  <li key={genre} className={styles.tag}>
                    <span>{genre}</span>
                    <IconButton
                      type="button"
                      size="sm"
                      variant="ghost"
                      label={`${t('common.remove')} ${genre}`}
                      onClick={() =>
                        setForm({ ...form, genres: form.genres.filter((item) => item !== genre) })
                      }
                    >
                      <CrossIcon />
                    </IconButton>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </div>

        {save.isError ? <ErrorState error={save.error} /> : null}

        <div className={styles.formFooter}>
          <p className={styles.dirtyNote} aria-live="polite">
            {dirty ? t('admin.library.unsavedTitle') : ''}
          </p>
          <Button type="submit" variant="primary" loading={save.isPending} disabled={!dirty}>
            {save.isPending ? t('common.saving') : t('common.save')}
          </Button>
        </div>
      </form>

      <TitlesSection seriesId={detail.id} titles={detail.titles} />

      <CastSection detail={detail} />

      <EpisodesSection detail={detail} />

      <RenamePanel seriesId={detail.id} />
      </TocLayout>


      {showArtwork ? (
        <ArtworkPicker
          open
          onClose={() => setShowArtwork(false)}
          seriesId={detail.id}
          seriesTitle={detail.title}
          onChanged={() => void detailQuery.refetch()}
        />
      ) : null}

      {/* The poster at the size the decision needs. `lg` rather than a bare `<img>` so it
          still goes through the cached-path guard every other poster in the app uses — the
          URL is stored metadata, and metadata is not a trusted image source. */}
      <Dialog
        open={zoomOpen}
        onClose={() => setZoomOpen(false)}
        title={detail.title}
        size="md"
      >
        <div className={styles.posterFull}>
          <Poster src={detail.posterUrl} alt={detail.title} size="lg" fluid />
        </div>
      </Dialog>

      <Dialog
        open={leaveTo !== null}
        onClose={() => setLeaveTo(null)}
        title={t('admin.library.unsavedTitle')}
        description={t('admin.library.unsavedBody')}
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setLeaveTo(null)}>
              {t('admin.library.stay')}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                const href = leaveTo
                setLeaveTo(null)
                setForm(baseline)
                if (href) navigate(href)
              }}
            >
              {t('admin.library.discard')}
            </Button>
          </>
        }
      >
        <p>{t('admin.library.unsavedBody')}</p>
      </Dialog>
    </div>
  )
}

// --- Alternate titles -----------------------------------------------------------------------

interface TitlesSectionProps {
  seriesId: string
  titles: SeriesTitleOut[]
}

/**
 * Who is in it, on the screen where an admin is asking that question.
 *
 * The cast was rendering on the public series page and nowhere in the editor, so an admin
 * looking at the metadata of a series with four credited actors saw none of them and quite
 * reasonably concluded there were none. The data was there the whole time.
 *
 * Read-only on purpose. This is a cache of the indexer's answer — `apply_dramadex_title`
 * replaces it wholesale on every refresh — so an edit here would be silently discarded the
 * next time somebody pressed the button above. The section says where it comes from instead,
 * and each name links to everything else that person is in.
 */
function CastSection({ detail }: { detail: SeriesDetail }) {
  const { t } = useTranslation()
  const cast = detail.cast ?? []

  return (
    <section className={styles.card} id="series-cast">
      <h2 className={styles.sectionTitle}>{t('series.cast')}</h2>
      <p className={styles.sectionHint}>{t('admin.library.castHint')}</p>

      {cast.length === 0 ? (
        /* Not an error, and worth saying which of the two it is: the series is bound and the
           indexer simply has no credits for it. Six of this library's titles are in that state
           and no amount of pressing refresh will change it. */
        <p className={styles.sectionHint}>
          {detail.dramadexId === null || detail.dramadexId === undefined
            ? t('admin.library.castNotLinked')
            : t('admin.library.castNone')}
        </p>
      ) : (
        <ul className={styles.castList}>
          {cast.map((member) => (
            <li key={member.dramadexPersonId} className={styles.castItem}>
              <Link href={`/people/${member.dramadexPersonId}`} className={styles.castLink}>
                {member.name}
              </Link>
              {member.role ? <span className={styles.castRole}>{member.role}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function TitlesSection({ seriesId, titles }: TitlesSectionProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [language, setLanguage] = useState('')
  const [title, setTitle] = useState('')
  const [kind, setKind] = useState<SeriesTitleOut['kind']>('alternate')

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: adminKeys.seriesDetail(seriesId) })
  }

  const add = useMutation({
    mutationFn: () =>
      addSeriesTitle(seriesId, { title: title.trim(), language: language.trim(), kind }),
    onSuccess: async () => {
      setTitle('')
      setLanguage('')
      await invalidate()
    },
  })

  const remove = useMutation({
    mutationFn: (titleId: string) => deleteSeriesTitle(seriesId, titleId),
    onSuccess: invalidate,
  })

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (title.trim() === '') return
    add.mutate()
  }

  return (
    <section className={styles.card} id="series-titles">
      <h2 className={styles.sectionTitle}>{t('admin.library.titles')}</h2>
      <p className={styles.sectionHint}>{t('admin.library.titlesHint')}</p>

      {titles.length > 0 ? (
        <ul className={styles.titleList}>
          {titles.map((entry) => (
            <li key={entry.id} className={styles.titleRow}>
              <span className={styles.titleValue}>{entry.title}</span>
              <span className={styles.titleMeta}>
                {entry.language ? entry.language : t('common.none')}
              </span>
              <Badge tone="neutral">{t(`admin.library.titleKinds.${entry.kind}`)}</Badge>
              <IconButton
                type="button"
                size="sm"
                variant="ghost"
                label={`${t('common.remove')} ${entry.title}`}
                loading={remove.isPending && remove.variables === entry.id}
                onClick={() => remove.mutate(entry.id)}
              >
                <CrossIcon />
              </IconButton>
            </li>
          ))}
        </ul>
      ) : null}

      <form className={styles.titleForm} onSubmit={onSubmit}>
        <Field label={t('admin.library.titleValue')} htmlFor="new-title-value" required>
          <Input
            id="new-title-value"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </Field>
        <Field
          label={t('admin.library.titleLanguage')}
          htmlFor="new-title-language"
          hint={t('common.optional')}
        >
          <Input
            id="new-title-language"
            value={language}
            onChange={(event) => setLanguage(event.target.value)}
          />
        </Field>
        <Field label={t('admin.library.titleKind')} htmlFor="new-title-kind">
          <Select
            id="new-title-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value as SeriesTitleOut['kind'])}
          >
            {TITLE_KINDS.map((option) => (
              <option key={option} value={option}>
                {t(`admin.library.titleKinds.${option}`)}
              </option>
            ))}
          </Select>
        </Field>
        <Button type="submit" loading={add.isPending} disabled={title.trim() === ''}>
          {t('admin.library.addTitle')}
        </Button>
      </form>

      {add.isError ? <ErrorState error={add.error} /> : null}
      {remove.isError ? <ErrorState error={remove.error} /> : null}
    </section>
  )
}

// --- Episodes -------------------------------------------------------------------------------

function EpisodesSection({ detail }: { detail: SeriesDetail }) {
  const { t } = useTranslation()
  const [openEpisode, setOpenEpisode] = useState<string | null>(null)

  return (
    <section className={styles.card} id="series-episodes">
      <h2 className={styles.sectionTitle}>{t('series.episodes')}</h2>

      {detail.seasons.length === 0 ? (
        <p className={styles.sectionHint}>{t('common.empty')}</p>
      ) : null}

      {detail.seasons.map((season) => (
        <div key={season.seasonNumber} className={styles.season}>
          <h3 className={styles.seasonTitle}>
            {t('series.season', { number: season.seasonNumber })}
          </h3>
          <ul className={styles.episodeList}>
            {season.episodes.map((episode) => (
              <li key={episode.id} className={styles.episodeItem}>
                <div className={styles.episodeRow}>
                  <span className={styles.episodeNumber}>{episode.episodeNumber}</span>
                  <span className={styles.episodeTitle}>
                    {episode.title ?? t('series.episodeNumber', { number: episode.episodeNumber })}
                  </span>
                  {episode.available ? null : (
                    <Badge tone="neutral">{t('series.notAvailable')}</Badge>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-expanded={openEpisode === episode.id}
                    aria-controls={`episode-editor-${episode.id}`}
                    onClick={() =>
                      setOpenEpisode(openEpisode === episode.id ? null : episode.id)
                    }
                  >
                    {t('admin.library.editEpisode')}
                  </Button>
                </div>
                {openEpisode === episode.id ? (
                  <div id={`episode-editor-${episode.id}`} className={styles.episodeEditor}>
                    <EpisodeEditor episode={episode} seriesId={detail.id} />
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  )
}

interface EpisodeEditorProps {
  episode: EpisodeSummary
  seriesId: string
}

function EpisodeEditor({ episode, seriesId }: EpisodeEditorProps) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const episodeQuery = useQuery({
    queryKey: adminKeys.episode(episode.id),
    queryFn: () => getAdminEpisode(episode.id),
  })
  const data = episodeQuery.data

  const [seed, setSeed] = useState<string | null>(null)
  const [title, setTitle] = useState(episode.title ?? '')
  const [overview, setOverview] = useState('')
  // `<input type="date">` only accepts YYYY-MM-DD, so trim anything longer the API sends.
  const [airDate, setAirDate] = useState((episode.airDate ?? '').slice(0, 10))

  if (data && seed !== data.id) {
    setSeed(data.id)
    setTitle(data.title ?? '')
    setOverview(data.overview ?? '')
    setAirDate((data.airDate ?? '').slice(0, 10))
  }

  const save = useMutation({
    mutationFn: () =>
      patchEpisode(episode.id, {
        title: title.trim() || null,
        overview: overview.trim() || null,
        airDate: airDate === '' ? null : airDate,
      }),
    onSuccess: async (updated) => {
      show(t('common.saved'), 'success')
      queryClient.setQueryData(adminKeys.episode(episode.id), updated)
      await queryClient.invalidateQueries({ queryKey: adminKeys.seriesDetail(seriesId) })
    },
  })

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    save.mutate()
  }

  if (episodeQuery.isPending) return <Spinner label={t('common.loading')} />
  if (episodeQuery.isError || !data) {
    return <ErrorState error={episodeQuery.error} onRetry={() => void episodeQuery.refetch()} />
  }

  const resolution =
    data.width !== null && data.height !== null ? `${data.width}×${data.height}` : null
  const codecs = [data.container, data.videoCodec, data.audioCodec].filter(
    (part): part is string => Boolean(part),
  )

  return (
    <form className={styles.episodeForm} onSubmit={onSubmit}>
      <Field label={t('admin.library.titleValue')} htmlFor={`ep-title-${episode.id}`}>
        <Input
          id={`ep-title-${episode.id}`}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
        />
      </Field>

      <Field label={t('admin.library.airDate')} htmlFor={`ep-air-${episode.id}`}>
        <Input
          id={`ep-air-${episode.id}`}
          type="date"
          value={airDate}
          onChange={(event) => setAirDate(event.target.value)}
        />
      </Field>

      <div className={styles.spanAll}>
        <Field label={t('series.overview')} htmlFor={`ep-overview-${episode.id}`}>
          <Textarea
            id={`ep-overview-${episode.id}`}
            rows={4}
            value={overview}
            onChange={(event) => setOverview(event.target.value)}
          />
        </Field>
      </div>

      <div className={styles.spanAll}>
        <h4 className={styles.fileTitle}>{t('admin.library.fileDetails')}</h4>
        <dl className={styles.fileGrid}>
          <div>
            <dt>{t('admin.library.libraryPath')}</dt>
            <dd className={styles.mono}>{data.mediaFile?.path ?? t('series.notAvailable')}</dd>
          </div>
          <div>
            <dt>{t('admin.library.resolution')}</dt>
            <dd>{resolution ?? t('common.unknown')}</dd>
          </div>
          <div>
            <dt>{t('admin.library.duration')}</dt>
            <dd>
              {data.durationSeconds === null
                ? t('common.unknown')
                : formatDuration(data.durationSeconds)}
            </dd>
          </div>
          <div>
            <dt>{t('admin.library.size')}</dt>
            <dd>
              {data.mediaFile ? formatBytes(data.mediaFile.sizeBytes) : t('common.unknown')}
            </dd>
          </div>
          <div>
            <dt>{t('admin.library.codecs')}</dt>
            <dd>{codecs.length > 0 ? codecs.join(' · ') : t('common.unknown')}</dd>
          </div>
        </dl>
      </div>

      {save.isError ? <ErrorState error={save.error} /> : null}

      <div className={styles.episodeFooter}>
        <Button type="submit" variant="primary" size="sm" loading={save.isPending}>
          {save.isPending ? t('common.saving') : t('common.save')}
        </Button>
      </div>
    </form>
  )
}

export default AdminSeriesEditPage
