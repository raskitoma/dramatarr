/** Choose the artwork a series shows everywhere in the app.
 *
 * It **opens already searching**, because the owner's complaint was exact: "no necesito estar
 * copiando y pegando". Two grids populate on mount — the bound record's own key art, and the
 * posters of similarly-named titles — and the paste-a-URL and upload-a-file paths move below
 * them as the fallbacks they always were.
 *
 * This is not the return of the gallery that was deleted. That one offered *sources* — TMDB's
 * images and an open web image search, both of which left with the services they belonged to.
 * This is one source's own artwork, fetched **server-side** with an API key the browser never
 * sees, which is also why picking a thumbnail sends a title id rather than a URL.
 *
 * Both manual paths still preview before they commit: a URL that resolves to a 404 page, or a
 * screenshot of the wrong show, should be visible here and not after it is the series' face.
 *
 * Choosing anything here pins it — `artwork_locked`, which is *not* the metadata lock it used
 * to set. Wanting this picture and wanting the synopsis kept current are compatible wishes, and
 * conflating them meant uploading a poster opted the series out of every future update.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type ChangeEvent, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'

import {
  adminKeys,
  chooseDramadexArtwork,
  patchSeries,
  searchDramadex,
  selectArtwork,
  uploadArtwork,
  type ArtworkKind,
} from '@/api/admin'
import Button from '@/components/Button'
import Dialog from '@/components/Dialog'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import Input from '@/components/Input'
import Poster from '@/components/Poster'
import Select from '@/components/Select'
import { useToast } from '@/components/Toast'
import { formatBytes } from '@/lib/format'
import { IMAGE_ACCEPT, IMAGE_MAX_BYTES, isAcceptedImageType } from '@/lib/images'

import styles from './ArtworkPicker.module.css'

const KINDS: readonly ArtworkKind[] = ['poster', 'backdrop']

interface ArtworkPickerProps {
  open: boolean
  onClose: () => void
  seriesId: string
  seriesTitle: string
  /** The record this series is bound to, when it is bound. Its key art leads the grid. */
  dramadexId?: number | null
  /** True when this poster was picked by hand, and refreshes are leaving it alone. */
  artworkLocked?: boolean
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

export function ArtworkPicker({
  open,
  onClose,
  seriesId,
  seriesTitle,
  dramadexId = null,
  artworkLocked = false,
  onChanged,
}: ArtworkPickerProps) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const [kind, setKind] = useState<ArtworkKind>('poster')
  const [urlDraft, setUrlDraft] = useState('')
  const [urlPreview, setUrlPreview] = useState<string | null>(null)
  const [urlError, setUrlError] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [filePreview, setFilePreview] = useState<string | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const [wasOpen, setWasOpen] = useState(open)
  const [query, setQuery] = useState(seriesTitle)

  /**
   * Posters from titles with a similar name, asked for the moment the dialog opens.
   *
   * No gate and no button: the whole complaint was having to type a search before being shown
   * anything. The query field below is for correcting it, not for starting it.
   */
  const suggestions = useQuery({
    queryKey: adminKeys.dramadex(query.trim()),
    queryFn: () => searchDramadex(query.trim(), 12),
    enabled: open && query.trim().length > 1,
    staleTime: 60_000,
  })

  // Each visit starts clean: a URL typed last time is not a choice made this time.
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) {
      setKind('poster')
      setQuery(seriesTitle)
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

  const chooseDramadex = useMutation({
    mutationFn: (titleId: number) => chooseDramadexArtwork(seriesId, titleId, kind),
    onSuccess: commit,
  })

  /** Hand the poster back to the indexer. Until this existed, a chosen one was permanent. */
  const release = useMutation({
    mutationFn: () => patchSeries(seriesId, { artworkLocked: false }),
    onSuccess: () => {
      show(t('admin.library.artworkReleased'), 'success')
      onChanged?.()
    },
    onError: () => show(t('errors.generic'), 'error'),
  })

  const busy = chooseUrl.isPending || chooseFile.isPending || chooseDramadex.isPending

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

      {/* Already pinned: say so, and offer the way back. A chosen poster used to be permanent
          — the flag was not even on the payload — so the only undo was uploading another. */}
      {artworkLocked ? (
        <div className={styles.pinned}>
          <p>{t('admin.library.artworkPinned')}</p>
          <Button size="sm" loading={release.isPending} onClick={() => release.mutate()}>
            {t('admin.library.artworkRelease')}
          </Button>
        </div>
      ) : null}

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
      </div>

      {chooseUrl.isError ? <ErrorState error={chooseUrl.error} /> : null}
      {chooseFile.isError ? <ErrorState error={chooseFile.error} /> : null}

      {/* --- From DramaDex ---------------------------------------------------------------- */}
      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>{t('admin.library.artworkFromDramadex')}</h3>

        {dramadexId !== null ? (
          <>
            <p className={styles.sourceLabel}>{t('admin.library.artworkDramadexBound')}</p>
            <button
              type="button"
              className={styles.boundArt}
              disabled={busy}
              onClick={() => chooseDramadex.mutate(dramadexId)}
            >
              <Poster
                src={`/api/library/dramadex/titles/${dramadexId}/poster`}
                alt=""
                size="md"
                fluid
              />
              <span className={styles.useThis}>{t('admin.library.artworkUseThis')}</span>
            </button>
          </>
        ) : (
          <p className={styles.emptyNote}>{t('admin.library.artworkDramadexUnbound')}</p>
        )}

        <p className={styles.sourceLabel}>{t('admin.library.artworkDramadexOthers')}</p>
        <form className={styles.inlineForm} onSubmit={(event) => event.preventDefault()}>
          <div className={styles.urlField}>
            <Field label={t('admin.library.artworkQueryLabel')} htmlFor="artwork-query">
              <Input
                id="artwork-query"
                value={query}
                spellCheck={false}
                onChange={(event) => setQuery(event.target.value)}
              />
            </Field>
          </div>
        </form>

        {suggestions.isFetching ? (
          <p className={styles.emptyNote}>{t('admin.library.artworkDramadexSearching')}</p>
        ) : null}
        {suggestions.isError ? (
          <p className={styles.emptyNote}>{t('admin.library.artworkDramadexFailed')}</p>
        ) : null}
        {suggestions.data && suggestions.data.items.length === 0 && !suggestions.isFetching ? (
          <p className={styles.emptyNote}>{t('admin.library.artworkDramadexNone')}</p>
        ) : null}

        <ul className={styles.grid}>
          {(suggestions.data?.items ?? [])
            .filter((hit) => hit.id !== dramadexId)
            .map((hit) => (
              <li key={hit.id}>
                <button
                  type="button"
                  className={styles.card}
                  disabled={busy}
                  title={hit.title}
                  onClick={() => chooseDramadex.mutate(hit.id)}
                >
                  <span className={styles.thumb}>
                    <img
                      className={styles.image}
                      src={`/api/library/dramadex/titles/${hit.id}/poster`}
                      alt=""
                      loading="lazy"
                      decoding="async"
                      draggable={false}
                    />
                  </span>
                  <span className={styles.cardMeta}>
                    <span className={styles.cardTitle}>{hit.title}</span>
                  </span>
                  <span className="sr-only">{t('admin.library.artworkUseThis')}</span>
                </button>
              </li>
            ))}
        </ul>
      </section>

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

export default ArtworkPicker
