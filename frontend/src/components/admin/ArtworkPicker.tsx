/** Choose the artwork a series shows everywhere in the app.
 *
 * This is a manual override, and now it is only that. Two galleries used to feed it — TMDB's
 * own images for the matched entry, and a web image search — and both left with the sources
 * they belonged to. DramaDex supplies a poster, but it arrives with the metadata fill rather
 * than as a menu: there is exactly one per title, so there is nothing to choose between.
 *
 * What is left is the path that never depended on a third party. An image the admin has in
 * hand, pasted as a URL or uploaded from disk. Both preview before they commit: a URL that
 * resolves to a 404 page, or a screenshot of the wrong show, should be visible here and not
 * after it is the series' face.
 *
 * The consequence — choosing artwork locks the series' metadata against automatic refreshes —
 * is stated once, at the top, in the same terms the metadata editor uses. Once is deliberate:
 * repeating it on every card would train the eye to skip it.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type ChangeEvent, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'

import { adminKeys, selectArtwork, uploadArtwork, type ArtworkKind } from '@/api/admin'
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

  // Each visit starts clean: a URL typed last time is not a choice made this time.
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) {
      setKind('poster')
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
      </div>

      {chooseUrl.isError ? <ErrorState error={chooseUrl.error} /> : null}
      {chooseFile.isError ? <ErrorState error={chooseFile.error} /> : null}

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
