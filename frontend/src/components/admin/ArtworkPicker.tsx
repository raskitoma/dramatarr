/** Choose the artwork a series shows everywhere in the app.
 *
 * The picker exists because automatic artwork is right most of the time and badly wrong the
 * rest: the metadata job takes whichever poster TMDB lists first, and for a drama with six
 * regional posters that is a coin toss. So every candidate carries its **dimensions** and its
 * **rating**, which is the whole basis for choosing between two thumbnails that look identical
 * at 7rem wide — a 300×450 upscale and a 2000×3000 scan do not.
 *
 * Three sources, in the order they are worth trying:
 *   - TMDB's own gallery for the matched entry, already curated for this exact series;
 *   - a web image search, which is a wider net and needs configuring first;
 *   - an image the admin has in hand, pasted as a URL or uploaded from disk. Both preview
 *     before they commit: a URL that resolves to a 404 page or a screenshot of the wrong show
 *     should be visible here, not after it is the series' face.
 *
 * A source that contributes nothing is **not** an error, and the server says which of the
 * ordinary reasons applies — the series is not linked to TMDB, the web search was never
 * configured, the provider failed. Each becomes its own sentence with the action attached,
 * never an empty grid the admin is left to interpret and never a red box for something nobody
 * broke. That matters most on a fresh install, where this screen is where the admin is trying
 * to fix exactly the thing that is missing.
 *
 * The consequence — choosing artwork locks the series' metadata against automatic refreshes —
 * is stated once, at the top, in the same terms the metadata editor uses. Once is deliberate:
 * repeating it on every card would train the eye to skip it.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, type ChangeEvent, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import {
  adminKeys,
  artworkSource,
  candidateEngine,
  imageSearchProviderKey,
  listArtwork,
  selectArtwork,
  uploadArtwork,
  type ArtworkCandidate,
  type ArtworkKind,
  type ArtworkSource,
} from '@/api/admin'
import Badge from '@/components/Badge'
import Button from '@/components/Button'
import Dialog from '@/components/Dialog'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import Input from '@/components/Input'
import Poster from '@/components/Poster'
import Select from '@/components/Select'
import Spinner from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { formatBytes, languageName } from '@/lib/format'
import { IMAGE_ACCEPT, IMAGE_MAX_BYTES, isAcceptedImageType } from '@/lib/images'

import styles from './ArtworkPicker.module.css'

/** Sentinel for "every language", distinct from `''`, which the wire never sends. */
const ANY_LANGUAGE = '*'

const KINDS: readonly ArtworkKind[] = ['poster', 'backdrop']

interface ArtworkPickerProps {
  open: boolean
  onClose: () => void
  seriesId: string
  seriesTitle: string
  /** Called once an image is committed, so the caller can refetch the series it is showing. */
  onChanged?: () => void
}

/** `http(s)` only — and the server checks it again, plus every redirect hop, before fetching. */
function isFetchableUrl(value: string): boolean {
  try {
    const url = new URL(value.trim())
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

/** The i18n key for why a source contributed nothing, or null when it did contribute. */
function reasonKey(source: ArtworkSource | null, channel: 'tmdb' | 'web'): string | null {
  if (source === null) return null
  if (source.available) return null
  if (source.reason === 'NOT_LINKED') return 'admin.library.artworkTmdbNotLinked'
  if (source.reason === 'PROVIDER_ERROR') return 'admin.library.artworkSourceFailed'
  if (source.reason === 'NOT_CONFIGURED') {
    return channel === 'tmdb'
      ? 'admin.library.artworkTmdbUnconfigured'
      : 'admin.library.artworkWebUnconfigured'
  }
  return null
}

export function ArtworkPicker({
  open,
  onClose,
  seriesId,
  seriesTitle,
  onChanged,
}: ArtworkPickerProps) {
  const { t, i18n } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const [kind, setKind] = useState<ArtworkKind>('poster')
  const [language, setLanguage] = useState<string>(ANY_LANGUAGE)
  const [urlDraft, setUrlDraft] = useState('')
  const [urlPreview, setUrlPreview] = useState<string | null>(null)
  const [urlError, setUrlError] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [filePreview, setFilePreview] = useState<string | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const [wasOpen, setWasOpen] = useState(open)

  const artwork = useQuery({
    queryKey: adminKeys.artwork(seriesId),
    queryFn: () => listArtwork(seriesId),
    enabled: open,
    staleTime: 60_000,
  })

  // Each visit starts clean: a URL typed last time is not a choice made this time.
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) {
      setKind('poster')
      setLanguage(ANY_LANGUAGE)
      setUrlDraft('')
      setUrlPreview(null)
      setUrlError(null)
      setFile(null)
      setFileError(null)
    }
  }

  // One object URL per chosen file, revoked when the choice changes or the dialog unmounts.
  useEffect(() => {
    if (file === null) {
      setFilePreview(null)
      return undefined
    }
    const objectUrl = URL.createObjectURL(file)
    setFilePreview(objectUrl)
    return () => {
      URL.revokeObjectURL(objectUrl)
    }
  }, [file])

  const commit = () => {
    // The response says what was stored, not what the series now looks like, so the series is
    // refetched rather than patched from it — and its cached list rows go with it.
    void queryClient.invalidateQueries({ queryKey: adminKeys.series() })
    show(t(kind === 'poster' ? 'admin.library.artworkSaved' : 'admin.library.artworkSavedBackdrop'), 'success')
    onChanged?.()
    onClose()
  }

  const chooseUrl = useMutation({
    mutationFn: (url: string) => selectArtwork(seriesId, kind, url),
    onSuccess: commit,
  })

  const chooseFile = useMutation({
    mutationFn: (chosen: File) => uploadArtwork(seriesId, kind, chosen),
    onSuccess: commit,
  })

  const tmdbSource = artworkSource(artwork.data, 'tmdb')
  const webSource = artworkSource(artwork.data, 'web')

  /**
   * Which search answered, named on screen.
   *
   * The results are only as good as the thing that produced them — a household's own SearXNG
   * and a Google engine return different pictures for the same drama — so the admin should not
   * have to open settings to find out which one they are looking at. A provider this build has
   * no name for shows its own id rather than nothing.
   */
  const webProvider = webSource?.provider ?? ''
  const webProviderKey = imageSearchProviderKey(webProvider)
  const webProviderLabel =
    webProvider === '' ? '' : webProviderKey === '' ? webProvider : t(webProviderKey)

  /** TMDB's candidates for the slot being chosen. Web hits declare no slot, so they suit both. */
  const fromTmdb = useMemo(
    () => (kind === 'poster' ? (artwork.data?.posters ?? []) : (artwork.data?.backdrops ?? [])),
    [artwork.data, kind],
  )
  const fromWeb = artwork.data?.web ?? []

  /** Every language present in the current candidates, so the filter offers only what exists. */
  const languages = useMemo(() => {
    const seen = new Set<string>()
    for (const candidate of [...fromTmdb, ...fromWeb]) {
      if (candidate.language !== null) seen.add(candidate.language)
    }
    return [...seen].sort((a, b) =>
      languageName(a, i18n.language).localeCompare(languageName(b, i18n.language), i18n.language),
    )
  }, [fromTmdb, fromWeb, i18n.language])

  const filter = (items: ArtworkCandidate[]) =>
    language === ANY_LANGUAGE ? items : items.filter((item) => item.language === language)

  const busy = chooseUrl.isPending || chooseFile.isPending

  const onSubmitUrl = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const value = urlDraft.trim()
    if (!isFetchableUrl(value)) {
      setUrlPreview(null)
      setUrlError(t('admin.library.artworkUrlInvalid'))
      return
    }
    setUrlError(null)
    setUrlPreview(value)
  }

  const onChooseFile = (event: ChangeEvent<HTMLInputElement>) => {
    chooseFile.reset()
    const chosen = event.target.files?.[0] ?? null
    if (chosen === null) {
      setFile(null)
      setFileError(null)
      return
    }
    if (!isAcceptedImageType(chosen)) {
      setFile(null)
      setFileError(t('admin.library.artworkBadType'))
      return
    }
    if (chosen.size > IMAGE_MAX_BYTES) {
      setFile(null)
      setFileError(t('admin.library.artworkTooLarge', { size: formatBytes(IMAGE_MAX_BYTES) }))
      return
    }
    setFileError(null)
    setFile(chosen)
  }

  /** A source's grid, or the one sentence that explains why it is empty. */
  const renderSource = (
    items: ArtworkCandidate[],
    source: ArtworkSource | null,
    channel: 'tmdb' | 'web',
    emptyKey: string,
  ) => {
    const key = reasonKey(source, channel)
    if (key !== null) {
      return (
        <p className={styles.configureNote}>
          {t(key)}{' '}
          {source?.reason === 'NOT_CONFIGURED' ? (
            <Link href="/admin/settings" onClick={onClose}>
              {t('nav.settings')}
            </Link>
          ) : null}
        </p>
      )
    }
    const visible = filter(items)
    if (visible.length === 0) return <p className={styles.emptyNote}>{t(emptyKey)}</p>
    return (
      <ul className={kind === 'poster' ? styles.grid : styles.wideGrid}>
        {visible.map((candidate) => (
          <CandidateCard
            key={`${candidate.source}-${candidate.url}`}
            candidate={candidate}
            kind={kind}
            busy={busy}
            selecting={chooseUrl.isPending && chooseUrl.variables === candidate.url}
            onSelect={() => chooseUrl.mutate(candidate.url)}
          />
        ))}
      </ul>
    )
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t('admin.library.artworkTitle')}
      description={t('admin.library.artworkHint')}
      size="lg"
      footer={
        <Button variant="ghost" onClick={onClose}>
          {t('common.close')}
        </Button>
      }
    >
      <p className={styles.subject}>{seriesTitle}</p>

      {/* Said once, where a choice is about to be made. */}
      <p className={styles.lockNote}>{t('admin.library.artworkLockNote')}</p>

      <div className={styles.filter}>
        <Field label={t('admin.library.artworkKind')} htmlFor="artwork-kind">
          <Select
            id="artwork-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value as ArtworkKind)}
          >
            {KINDS.map((option) => (
              <option key={option} value={option}>
                {t(`admin.library.artworkKinds.${option}`)}
              </option>
            ))}
          </Select>
        </Field>

        {languages.length > 0 ? (
          <>
            <Field label={t('admin.library.artworkLanguage')} htmlFor="artwork-language">
              <Select
                id="artwork-language"
                value={language}
                onChange={(event) => setLanguage(event.target.value)}
              >
                <option value={ANY_LANGUAGE}>{t('common.all')}</option>
                {languages.map((code) => (
                  <option key={code} value={code}>
                    {languageName(code, i18n.language)}
                  </option>
                ))}
              </Select>
            </Field>
            <p className={styles.filterHint}>{t('admin.library.artworkLanguageHint')}</p>
          </>
        ) : null}
      </div>

      {artwork.isPending ? <Spinner label={t('common.loading')} /> : null}

      {artwork.isError ? (
        <ErrorState error={artwork.error} onRetry={() => void artwork.refetch()} />
      ) : null}

      {chooseUrl.isError ? <ErrorState error={chooseUrl.error} /> : null}
      {chooseFile.isError ? <ErrorState error={chooseFile.error} /> : null}

      {artwork.isSuccess ? (
        <>
          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>{t('admin.library.artworkFromTmdb')}</h3>
            {renderSource(fromTmdb, tmdbSource, 'tmdb', 'admin.library.artworkNoneTmdb')}
          </section>

          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>{t('admin.library.artworkFromWeb')}</h3>
            {webProviderLabel === '' ? null : (
              <p className={styles.providerNote}>
                {t('admin.library.artworkWebProvider', { provider: webProviderLabel })}
              </p>
            )}
            {renderSource(fromWeb, webSource, 'web', 'admin.library.artworkNoneWeb')}
          </section>
        </>
      ) : null}

      {/* --- From a URL ------------------------------------------------------------------ */}
      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>{t('admin.library.artworkFromUrl')}</h3>
        <form className={styles.inlineForm} onSubmit={onSubmitUrl}>
          <div className={styles.urlField}>
            <Field
              label={t('admin.library.artworkUrl')}
              htmlFor="artwork-url"
              hint={t('admin.library.artworkUrlHint')}
              error={urlError ?? undefined}
            >
              <Input
                id="artwork-url"
                // Deliberately not `type="url"`: native constraint validation would block the
                // submit and answer with the browser's own bubble, so this field would have
                // two different error languages depending on which check fired first.
                inputMode="url"
                spellCheck={false}
                value={urlDraft}
                invalid={urlError !== null}
                onChange={(event) => {
                  setUrlDraft(event.target.value)
                  setUrlError(null)
                }}
              />
            </Field>
          </div>
          <Button type="submit" disabled={urlDraft.trim() === ''}>
            {t('admin.library.artworkPreviewAction')}
          </Button>
        </form>

        {urlPreview !== null ? (
          <div className={styles.preview}>
            <Poster src={urlPreview} alt={t('admin.library.artworkPreview')} size="sm" />
            <div className={styles.previewBody}>
              <p className={styles.previewNote}>{t('admin.library.artworkPreviewNote')}</p>
              <Button
                variant="primary"
                size="sm"
                loading={chooseUrl.isPending && chooseUrl.variables === urlPreview}
                disabled={busy}
                onClick={() => chooseUrl.mutate(urlPreview)}
              >
                {t('admin.library.artworkUse')}
              </Button>
            </div>
          </div>
        ) : null}
      </section>

      {/* --- From a file ----------------------------------------------------------------- */}
      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>{t('admin.library.artworkFromFile')}</h3>
        <Field
          label={t('admin.library.artworkFile')}
          htmlFor="artwork-file"
          hint={t('admin.library.artworkFileHint', { size: formatBytes(IMAGE_MAX_BYTES) })}
          error={fileError ?? undefined}
        >
          <input
            id="artwork-file"
            className={styles.fileInput}
            type="file"
            accept={IMAGE_ACCEPT}
            disabled={busy}
            onChange={onChooseFile}
          />
        </Field>

        {filePreview !== null ? (
          <div className={styles.preview}>
            <Poster src={filePreview} alt={t('admin.library.artworkPreview')} size="sm" />
            <div className={styles.previewBody}>
              <p className={styles.previewNote}>{t('admin.library.artworkPreviewNote')}</p>
              <Button
                variant="primary"
                size="sm"
                loading={chooseFile.isPending}
                disabled={busy}
                onClick={() => {
                  if (file) chooseFile.mutate(file)
                }}
              >
                {t('admin.library.artworkUse')}
              </Button>
            </div>
          </div>
        ) : null}
      </section>
    </Dialog>
  )
}

// --- Candidate --------------------------------------------------------------------------------

interface CandidateCardProps {
  candidate: ArtworkCandidate
  kind: ArtworkKind
  busy: boolean
  selecting: boolean
  onSelect: () => void
}

/**
 * One image on offer.
 *
 * The whole card is the button: at this size a separate "use" control would be larger than the
 * thumbnail it belongs to. Dimensions and rating sit under the image rather than over it, so
 * they stay legible on a dark poster.
 */
function CandidateCard({ candidate, kind, busy, selecting, onSelect }: CandidateCardProps) {
  const { t, i18n } = useTranslation()

  const size =
    candidate.width !== null && candidate.height !== null
      ? `${candidate.width}×${candidate.height}`
      : t('common.unknown')
  const label = candidate.language
    ? languageName(candidate.language, i18n.language)
    : t('admin.library.artworkNoLanguage')
  // Only a web result has an engine worth naming — "tmdb" under a TMDB poster is noise. For
  // the rest it is the second half of the judgement the dimensions start: 1000×1500 from a
  // poster database and 1000×1500 from a wallpaper site are not the same bet.
  const engine = candidate.source === 'tmdb' ? '' : candidateEngine(candidate)

  return (
    <li>
      <button
        type="button"
        className={styles.card}
        disabled={busy}
        aria-busy={selecting || undefined}
        onClick={onSelect}
      >
        <span className={kind === 'poster' ? styles.thumb : styles.wideThumb}>
          <img
            className={styles.image}
            src={candidate.thumbnailUrl ?? candidate.url}
            alt=""
            loading="lazy"
            decoding="async"
            draggable={false}
          />
        </span>
        <span className={styles.cardMeta}>
          <span className={styles.cardSize}>{size}</span>
          <span className={styles.cardLanguage}>{label}</span>
          {/* The engine's own name for itself, not ours to translate. */}
          {engine === '' ? null : <span className={styles.cardEngine}>{engine}</span>}
          {candidate.voteAverage === null ? null : (
            <Badge tone="neutral">
              {t('admin.library.artworkRating', { rating: candidate.voteAverage.toFixed(1) })}
            </Badge>
          )}
        </span>
        <span className="sr-only">{t('admin.library.artworkUse')}</span>
      </button>
    </li>
  )
}

export default ArtworkPicker
