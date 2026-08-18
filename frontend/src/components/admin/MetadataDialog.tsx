/** "Actualizar metadata" — what it would change, before it changes anything.
 *
 * The button this replaces went away and came back having done something, with a job log as the
 * only account of what. Now the admin sees the rows first, unticks what they do not want, and
 * presses apply. Nothing is written until they do — not even by picking a different record.
 *
 * **The poster is one of the rows.** That was the owner's request in capitals, and it is why the
 * diff carries an `image` kind rather than treating artwork as a separate errand.
 *
 * Two screens, one dialog, entered at whichever the state calls for:
 *
 *   - **bound** → straight to the diff. This is nearly every case, and charging it a "pick the
 *     record" step first would be a click for nothing.
 *   - **unbound, or the record has gone** → the search, seeded with the series' own title and
 *     editable, because an unbound series is precisely the one whose folder name is wrong.
 *
 * Two rows arrive unticked and explained rather than hidden: a synopsis somebody wrote, and
 * artwork somebody pinned. Hiding them would be a different lie — and ticking one anyway does
 * apply it, because the note is a warning and not an argument with somebody who has decided.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'

import {
  adminKeys,
  applyDramadex,
  metadataPreview,
  searchDramadex,
  type MetadataDiffRow,
  type MetadataPreview,
} from '@/api/admin'
import type { SeriesDetail } from '@/api/types'
import { Badge } from '@/components/Badge'
import { Button } from '@/components/Button'
import { Dialog } from '@/components/Dialog'
import { ErrorState } from '@/components/ErrorState'
import { Field } from '@/components/Field'
import { Input } from '@/components/Input'
import { Poster } from '@/components/Poster'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { cx } from '@/components/cx'
import { ExternalLinkIcon, SearchIcon } from '@/components/icons'

import styles from './MetadataDialog.module.css'

interface MetadataDialogProps {
  open: boolean
  onClose: () => void
  detail: SeriesDetail
  /**
   * Called after a successful apply, so the editor can reseed its form.
   *
   * Not optional politeness: the page seeds `form` and `baseline` once per series id, and the
   * id does not change when the metadata does. Without this the inputs keep their pre-apply
   * values, the next save is diffed against the stale baseline, and editing anything silently
   * reverts what DramaDex just wrote — permanently, because that save also sets
   * `metadata_locked`.
   */
  onApplied?: () => void
}

export function MetadataDialog({ open, onClose, detail, onApplied }: MetadataDialogProps) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  /** The record being previewed. Null means "whatever the series is bound to". */
  const [candidate, setCandidate] = useState<number | null>(null)
  /** Forced onto the search screen by the admin, rather than by the state. */
  const [picking, setPicking] = useState(false)
  const [query, setQuery] = useState(detail.title)
  const [searched, setSearched] = useState(false)
  const [checked, setChecked] = useState<Set<string> | null>(null)
  const [showUnchanged, setShowUnchanged] = useState(false)

  // Each visit starts clean. A record considered last time is not a question being asked now.
  const [wasOpen, setWasOpen] = useState(open)
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) {
      setCandidate(null)
      setPicking(false)
      setQuery(detail.title)
      setSearched(false)
      setChecked(null)
      setShowUnchanged(false)
    }
  }

  const preview = useQuery({
    queryKey: adminKeys.metadataPreview(detail.id, candidate),
    queryFn: () => metadataPreview(detail.id, candidate),
    enabled: open,
    staleTime: 30_000,
  })

  const hits = useQuery({
    queryKey: adminKeys.dramadex(query.trim()),
    queryFn: () => searchDramadex(query.trim(), 12),
    enabled: open && searched && query.trim().length > 0,
  })

  const data: MetadataPreview | undefined = preview.data
  // Ticks follow the server's defaults until the admin touches one; `null` means untouched, so
  // re-previewing a different record re-seeds rather than carrying the last record's choices.
  const ticks = useMemo(() => {
    if (checked !== null) return checked
    return new Set((data?.rows ?? []).filter((row) => row.defaultChecked).map((row) => row.field))
  }, [checked, data])

  const apply = useMutation({
    mutationFn: () =>
      applyDramadex(detail.id, { dramadexId: candidate, fields: [...ticks] }),
    onSuccess: (fresh) => {
      queryClient.setQueryData(adminKeys.seriesDetail(detail.id), fresh)
      void queryClient.invalidateQueries({ queryKey: adminKeys.series() })
      // Counts what will actually be written, not what is ticked. `declared_episode_count`
      // below the file count is refused by the writer whatever the checkbox says — the row
      // carries `fewerEpisodes` to say so — and "actualizamos 2 campos" when one was declined
      // is the screen asserting something it did not do.
      const refused = (data?.rows ?? []).filter(
        (row) => row.note === 'fewerEpisodes' && ticks.has(row.field),
      ).length
      show(
        t('admin.library.metadataApplied', {
          count: Math.max(0, ticks.size - refused),
          title: detail.title,
        }),
        'success',
      )
      onApplied?.()
      onClose()
    },
    onError: () => show(t('admin.library.metadataFailed'), 'error'),
  })

  const changed = (data?.rows ?? []).filter((row) => row.changed)
  const unchanged = (data?.rows ?? []).filter((row) => !row.changed)

  /** The search screen shows when the admin asked for it, or when there is nothing to diff. */
  const onSearchScreen =
    picking || (data !== undefined && (!data.bound || !data.found))

  function toggle(field: string): void {
    const next = new Set(ticks)
    if (next.has(field)) next.delete(field)
    else next.add(field)
    setChecked(next)
  }

  function runSearch(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    setSearched(true)
    void hits.refetch()
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t('admin.library.metadataTitle')}
      description={t('admin.library.metadataSubtitle')}
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t('common.close')}
          </Button>
          {!onSearchScreen && data?.found ? (
            <Button
              variant="primary"
              loading={apply.isPending}
              // Deliberately not disabled at zero. The binding is not a field, and an admin who
              // picked the right record and wants none of its values still needs the link to
              // land — `test_an_empty_selection_writes_no_fields_but_still_binds` exists for
              // exactly that case, and disabling here reintroduced it on the client.
              onClick={() => apply.mutate()}
            >
              {t('admin.library.metadataApply', { count: ticks.size })}
            </Button>
          ) : null}
        </>
      }
    >
      <p className={styles.subject}>{detail.title}</p>

      {preview.isPending ? <Spinner label={t('admin.library.metadataLoading')} /> : null}

      {/* A transport failure is the one thing here that *is* an error. */}
      {preview.isError ? (
        <div className={styles.state}>
          <p>{t('admin.library.metadataUnreachable')}</p>
          <Button onClick={() => void preview.refetch()}>{t('admin.library.metadataRetry')}</Button>
        </div>
      ) : null}

      {data && !data.configured ? (
        <p className={styles.state}>{t('admin.library.dramadexUnconfigured')}</p>
      ) : null}

      {data?.configured && onSearchScreen ? (
        <section className={styles.screen}>
          <h3 className={styles.screenTitle}>{t('admin.library.metadataPickTitle')}</h3>
          {!data.bound ? <p className={styles.note}>{t('admin.library.metadataUnbound')}</p> : null}
          {data.bound && !data.found ? (
            <p className={styles.note}>{t('admin.library.metadataGone')}</p>
          ) : null}

          <form className={styles.searchForm} onSubmit={runSearch}>
            <div className={styles.searchField}>
              <Field
                label={t('admin.library.metadataSearchLabel')}
                htmlFor="metadata-query"
                hint={t('admin.library.metadataSearchHint')}
              >
                <Input
                  id="metadata-query"
                  value={query}
                  spellCheck={false}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </Field>
            </div>
            <Button type="submit" loading={hits.isFetching}>
              <SearchIcon size="1em" aria-hidden="true" />
              {t('admin.library.metadataSearchAction')}
            </Button>
          </form>

          {hits.isError ? <ErrorState error={hits.error} /> : null}

          {searched && hits.data && hits.data.items.length === 0 ? (
            <p className={styles.note}>{t('admin.library.dramadexNoResults')}</p>
          ) : null}

          <ul className={styles.hits}>
            {(hits.data?.items ?? []).map((hit) => (
              <li key={hit.id} className={styles.hit}>
                <span className={styles.hitPoster}>
                  <Poster src={hit.posterUrl} alt="" size="sm" fluid />
                </span>
                <span className={styles.hitBody}>
                  <span className={styles.hitTitle}>{hit.title}</span>
                  <span className={styles.hitMeta}>
                    {[hit.year, hit.studio].filter(Boolean).join(' · ')}
                  </span>
                  {hit.id === detail.dramadexId ? (
                    <Badge tone="success">{t('admin.library.metadataCurrentBind')}</Badge>
                  ) : null}
                  {hit.inLibrary && hit.librarySlug !== detail.slug ? (
                    <span className={styles.note}>
                      {t('admin.library.metadataInLibraryElsewhere', { title: hit.title })}
                    </span>
                  ) : null}
                </span>
                <Button
                  size="sm"
                  onClick={() => {
                    setCandidate(hit.id)
                    setChecked(null)
                    setPicking(false)
                  }}
                >
                  {t('admin.library.metadataSee')}
                </Button>
              </li>
            ))}
          </ul>

          {data.bound && data.found ? (
            <Button variant="ghost" onClick={() => setPicking(false)}>
              {t('admin.library.metadataBackToDiff')}
            </Button>
          ) : null}
        </section>
      ) : null}

      {data?.found && !onSearchScreen ? (
        <section className={styles.screen}>
          <p className={styles.source}>
            {/* Two different sentences, because the alias fix makes the difference real: until
                it landed the candidate was never previewed, so this always described the true
                binding. Now it can describe a record you are only looking at. */}
            {t(
              candidate !== null && candidate !== detail.dramadexId
                ? 'admin.library.metadataLookingAt'
                : 'admin.library.metadataBoundTo',
              { title: data.dramadexTitle ?? '' },
            )}
            {data.dramadexEpisodeCount !== null ? (
              <span className={styles.sourceMeta}>
                {' · '}
                {t('admin.library.metadataDeclared', { count: data.dramadexEpisodeCount })}
              </span>
            ) : null}
            {data.watchUrl && data.watchStudio ? (
              <a
                className={styles.sourceLink}
                href={data.watchUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                <ExternalLinkIcon size="1em" aria-hidden="true" />
                {t('admin.library.metadataOpenSource', { studio: data.watchStudio })}
              </a>
            ) : null}
          </p>

          {changed.length === 0 ? (
            <div className={styles.state}>
              <p>{t('admin.library.metadataNothing')}</p>
              <p className={styles.note}>{t('admin.library.metadataNothingHint')}</p>
            </div>
          ) : (
            <ul className={styles.rows}>
              {changed.map((row) => (
                <DiffRow
                  key={row.field}
                  row={row}
                  checked={ticks.has(row.field)}
                  onToggle={() => toggle(row.field)}
                />
              ))}
            </ul>
          )}

          {unchanged.length > 0 ? (
            <>
              <Button variant="ghost" size="sm" onClick={() => setShowUnchanged((v) => !v)}>
                {showUnchanged
                  ? t('admin.library.metadataUnchangedHide')
                  : t('admin.library.metadataUnchangedShow', { count: unchanged.length })}
              </Button>
              {showUnchanged ? (
                <ul className={styles.rows}>
                  {unchanged.map((row) => (
                    <DiffRow key={row.field} row={row} checked={false} onToggle={null} />
                  ))}
                </ul>
              ) : null}
            </>
          ) : null}

          <div className={styles.rebind}>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setPicking(true)
                setSearched(false)
              }}
            >
              {t('admin.library.metadataRebind')}
            </Button>
            <p className={styles.note}>{t('admin.library.metadataRebindHint')}</p>
          </div>
        </section>
      ) : null}
    </Dialog>
  )
}

// --- One row ----------------------------------------------------------------------------------

interface DiffRowProps {
  row: MetadataDiffRow
  checked: boolean
  /** Null for the unchanged list, which is read-only: there is nothing to choose. */
  onToggle: (() => void) | null
}

function DiffRow({ row, checked, onToggle }: DiffRowProps) {
  const { t } = useTranslation()
  const label = t(`admin.library.metadataField.${row.field}`, { defaultValue: row.field })

  return (
    <li className={cx(styles.row, row.kind === 'image' && styles.imageRow)}>
      <div className={styles.rowHead}>
        {onToggle ? (
          <label className={styles.check}>
            <input type="checkbox" checked={checked} onChange={onToggle} />
            <span className="sr-only">{t('admin.library.metadataTakeRow', { field: label })}</span>
          </label>
        ) : null}
        <span className={styles.rowLabel}>{label}</span>
      </div>

      <div className={styles.pair}>
        <div className={styles.side}>
          <span className={styles.sideLabel}>{t('admin.library.metadataColumnCurrent')}</span>
          <Value values={row.current} kind={row.kind} />
        </div>
        <div className={styles.side}>
          <span className={styles.sideLabel}>{t('admin.library.metadataColumnIncoming')}</span>
          <Value values={row.incoming} kind={row.kind} />
        </div>
      </div>

      {row.note ? (
        <p className={styles.rowNote}>
          {t(
            `admin.library.metadataNote${row.note.charAt(0).toUpperCase()}${row.note.slice(1)}`,
            row.noteArgs,
          )}
        </p>
      ) : null}

      {/* Said under the poster specifically: taking the automatic source is not pinning it —
          and it releases a pin that was there, which is what makes the sentence true rather
          than aspirational. */}
      {row.field === 'poster' ? (
        <p className={styles.rowNote}>{t('admin.library.metadataNotePosterSource')}</p>
      ) : null}
    </li>
  )
}

function Value({ values, kind }: { values: string[]; kind: string }) {
  const { t } = useTranslation()

  if (kind === 'image') {
    const src = values[0] ?? null
    return (
      <span className={styles.imageValue}>
        <Poster src={src} alt="" size="md" fluid />
      </span>
    )
  }
  if (values.length === 0) {
    return <span className={styles.empty}>{t('admin.library.metadataEmptyValue')}</span>
  }
  if (kind === 'list') {
    return (
      <span className={styles.chips}>
        {values.map((value) => (
          <span key={value} className={styles.chip}>
            {value}
          </span>
        ))}
      </span>
    )
  }
  return <span className={cx(styles.text, kind === 'long_text' && styles.longText)}>{values[0]}</span>
}

export default MetadataDialog
