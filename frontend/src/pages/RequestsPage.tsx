/** Ask for something the library does not have.
 *
 * The flow is three steps and no more: type a title, recognize it among the cards, tap it.
 * What comes back is a note on a list an admin works through by hand — nothing here starts a
 * download, and none of the copy on this screen implies one. That restraint is the feature's
 * whole contract with the person using it, so the wording matters as much as the code.
 *
 * Two design decisions are worth stating because they look like omissions.
 *
 * **Asking is one tap, with no dialog.** A form between "that one" and "asked" would double
 * the length of the interaction for a field almost nobody fills in. The free-text fallback at
 * the bottom is where a note belongs — a member typing a title by hand is already typing.
 *
 * **A card whose show is already in the library becomes a link to it.** That is the single
 * most valuable answer this screen can give, it costs nothing to compute, and giving it later
 * — after the request has sat in a queue for a day — would be strictly worse.
 */

import { useEffect, useId, useMemo, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import {
  externalLinks,
  requestFromCandidate,
  requestStatusKey,
  useCancelRequest,
  useCreateRequest,
  useMyRequests,
  useRequestSearch,
  type RequestCandidate,
  type RequestStatus,
  type SeriesRequest,
} from '@/api/requests'
import { ApiRequestError } from '@/api/client'
import { Badge, type BadgeTone } from '@/components/Badge'
import { Button } from '@/components/Button'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { Field } from '@/components/Field'
import { IconButton } from '@/components/IconButton'
import { Input } from '@/components/Input'
import { Poster } from '@/components/Poster'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import {
  BanIcon,
  CheckCircleIcon,
  CheckIcon,
  ClockIcon,
  ExternalLinkIcon,
  PlusIcon,
  RequestIcon,
  SearchIcon,
  TrashIcon,
} from '@/components/icons'

import styles from './RequestsPage.module.css'

const DEBOUNCE_MS = 300
const MIN_QUERY_LENGTH = 2

/** Chip appearance per status. One table, shared with the admin screen's vocabulary. */
const STATUS_TONE: Record<RequestStatus, BadgeTone> = {
  pending: 'warning',
  approved: 'info',
  fulfilled: 'success',
  declined: 'neutral',
}

function StatusIcon({ status }: { status: RequestStatus }) {
  if (status === 'fulfilled') return <CheckCircleIcon size="1em" />
  if (status === 'approved') return <CheckIcon size="1em" />
  if (status === 'declined') return <BanIcon size="1em" />
  return <ClockIcon size="1em" />
}

function StatusChip({ status }: { status: RequestStatus }) {
  const { t } = useTranslation()
  return (
    <Badge tone={STATUS_TONE[status]} className={styles.statusChip}>
      <StatusIcon status={status} />
      {t(requestStatusKey(status))}
    </Badge>
  )
}

export function RequestsPage() {
  const { t } = useTranslation()
  const toast = useToast()
  const searchId = useId()
  const noteId = useId()

  const [input, setInput] = useState('')
  const [query, setQuery] = useState('')
  const [note, setNote] = useState('')
  /* On by default. This household's library is entirely vertical drama, and the mode exists
     because the general catalogues answer those queries with ordinary television. */

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(input.trim()), DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [input])

  const search = useRequestSearch(query, true)
  const mine = useMyRequests()
  const create = useCreateRequest()
  const cancel = useCancelRequest()

  const active = query.length >= MIN_QUERY_LENGTH
  const results = active ? (search.data?.results ?? []) : []

  /** True once the query has been answered and there is genuinely nothing to show. */
  const emptyAnswer = active && !search.isFetching && !search.isError && results.length === 0

  const ask = (body: Parameters<typeof create.mutate>[0], label: string) => {
    create.mutate(body, {
      onSuccess: (row) => {
        toast.show(
          row.alreadyExisted
            ? t('requests.alreadyAsked', { title: label })
            : t('requests.asked', { title: label }),
          row.alreadyExisted ? 'info' : 'success',
        )
      },
      onError: (error) => {
        // The one refusal that is good news, and it deserves its own sentence rather than
        // the generic error banner: the thing they want is already on the shelf.
        if (error instanceof ApiRequestError && error.code === 'REQUEST_ALREADY_IN_LIBRARY') {
          toast.show(t('requests.alreadyInLibrary', { title: label }), 'info')
          return
        }
        const code = error instanceof ApiRequestError ? error.code : ''
        toast.show(
          code ? t(`errors.${code}`, { defaultValue: t('errors.generic') }) : t('errors.generic'),
          'error',
        )
      },
    })
  }

  const askByName = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const title = input.trim()
    if (!title) return
    ask({ title, note: note.trim() || null }, title)
    setNote('')
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>
          <RequestIcon aria-hidden="true" />
          {t('requests.title')}
        </h1>
        {/* Says plainly what happens next. Nobody should have to guess whether this downloads. */}
        <p className={styles.intro}>{t('requests.intro')}</p>
      </header>

      <form className={styles.searchForm} role="search" onSubmit={(e) => e.preventDefault()}>
        <Field label={t('requests.searchLabel')} htmlFor={searchId} hint={t('requests.searchHint')}>
          <span className={styles.searchControl}>
            <SearchIcon className={styles.searchIcon} aria-hidden="true" />
            <Input
              id={searchId}
              type="search"
              autoComplete="off"
              value={input}
              placeholder={t('requests.searchPlaceholder')}
              onChange={(event) => setInput(event.target.value)}
            />
          </span>
        </Field>
      </form>

      {search.data && !search.data.configured && active ? (
        <p className={styles.notice} role="status">
          {t('requests.noProviders')}
        </p>
      ) : null}

      <section className={styles.results} aria-busy={search.isFetching} aria-live="polite">
        {search.isError ? (
          <ErrorState error={search.error} onRetry={() => void search.refetch()} />
        ) : null}

        {active && search.isLoading ? (
          <div className={styles.centered}>
            <Spinner size="lg" label={t('common.loading')} />
          </div>
        ) : null}

        {results.length > 0 ? (
          <ul className={styles.grid}>
            {results.map((candidate) => (
              <li key={candidate.key}>
                <CandidateCard
                  candidate={candidate}
                  busy={create.isPending}
                  onAsk={(posterUrl) =>
                    ask(
                      { ...requestFromCandidate(candidate), posterUrl },
                      candidate.title,
                    )
                  }
                />
              </li>
            ))}
          </ul>
        ) : null}

        {emptyAnswer ? (
          <EmptyState
            title={t('requests.noResults', { query })}
            description={t('requests.noResultsHint')}
          />
        ) : null}
      </section>

      {/* The fallback, and the only path that works with no provider configured at all. It is
          always visible once something is typed — a member who does not recognize any card is
          exactly the member who needs it. */}
      {input.trim().length >= MIN_QUERY_LENGTH ? (
        <form className={styles.byName} onSubmit={askByName}>
          <h2 className={styles.byNameTitle}>{t('requests.byNameTitle')}</h2>
          <p className={styles.byNameHint}>{t('requests.byNameHint', { query: input.trim() })}</p>
          <Field label={t('requests.noteLabel')} htmlFor={noteId} hint={t('requests.noteHint')}>
            <Input
              id={noteId}
              value={note}
              maxLength={500}
              placeholder={t('requests.notePlaceholder')}
              onChange={(event) => setNote(event.target.value)}
            />
          </Field>
          <Button type="submit" variant="secondary" loading={create.isPending}>
            <PlusIcon aria-hidden="true" />
            {t('requests.askByName')}
          </Button>
        </form>
      ) : null}

      <section className={styles.mine} aria-labelledby="my-requests">
        <h2 className={styles.sectionTitle} id="my-requests">
          {t('requests.mineTitle')}
        </h2>

        {mine.isError ? <ErrorState error={mine.error} onRetry={() => void mine.refetch()} /> : null}

        {mine.isLoading ? <Spinner label={t('common.loading')} /> : null}

        {mine.data && mine.data.length === 0 ? (
          <EmptyState title={t('requests.mineEmpty')} description={t('requests.mineEmptyHint')} />
        ) : null}

        {mine.data && mine.data.length > 0 ? (
          <ul className={styles.mineList}>
            {mine.data.map((row) => (
              <MyRequestRow
                key={row.id}
                row={row}
                busy={cancel.isPending}
                onCancel={() =>
                  cancel.mutate(row.id, {
                    onSuccess: () =>
                      toast.show(t('requests.cancelled', { title: row.title }), 'info'),
                  })
                }
              />
            ))}
          </ul>
        ) : null}
      </section>
    </div>
  )
}

// --- Cards ------------------------------------------------------------------------------------

interface CandidateCardProps {
  candidate: RequestCandidate
  busy: boolean
  /** Handed the poster the card actually resolved, which the search response may not have. */
  onAsk: (posterUrl: string | null) => void
}

function CandidateCard({ candidate, busy, onAsk }: CandidateCardProps) {
  const { t } = useTranslation()
  const year = candidate.year ? String(candidate.year) : null

  /* Every hit carries its own poster address now, because every hit comes from the indexer and
     the indexer keys artwork by title id. There used to be a second request per illustrationless
     card, back when results were web pages rather than records; nothing left can answer it.
     A title the indexer has no art for still renders — `Poster` has its own placeholder. */
  const poster = candidate.posterUrl

  return (
    <article className={styles.card}>
      <div className={styles.cardPoster}>
        <Poster src={poster} alt={candidate.title} fluid />
      </div>

      <div className={styles.cardBody}>
        <h3 className={styles.cardTitle}>{candidate.title}</h3>
        <p className={styles.cardMeta}>
          {year ? <span>{year}</span> : null}
          {/* Always shown: which source this card came from is how two similar hits are told
              apart, and it is the only provenance a member gets. */}
          {candidate.provider ? (
            <span className={styles.provider}>{candidate.provider}</span>
          ) : null}
        </p>

        {candidate.snippet ? <p className={styles.snippet}>{candidate.snippet}</p> : null}

        <div className={styles.cardActions}>
          {candidate.inLibrary && candidate.inLibrarySlug ? (
            <Link
              href={`/series/${encodeURIComponent(candidate.inLibrarySlug)}`}
              className={styles.inLibrary}
            >
              <CheckCircleIcon size="1em" aria-hidden="true" />
              {t('requests.inLibrary')}
            </Link>
          ) : candidate.requested && candidate.requestStatus ? (
            <StatusChip status={candidate.requestStatus} />
          ) : (
            <Button variant="primary" size="sm" disabled={busy} onClick={() => onAsk(poster)}>
              <PlusIcon aria-hidden="true" />
              {t('requests.ask')}
            </Button>
          )}

          {candidate.sourceUrl ? (
            <a
              className={styles.sourceLink}
              href={candidate.sourceUrl}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={t('requests.openSource', { provider: candidate.provider })}
              title={t('requests.openSource', { provider: candidate.provider })}
            >
              <ExternalLinkIcon size="1em" aria-hidden="true" />
            </a>
          ) : null}
        </div>
      </div>
    </article>
  )
}

interface MyRequestRowProps {
  row: SeriesRequest
  busy: boolean
  onCancel: () => void
}

function MyRequestRow({ row, busy, onCancel }: MyRequestRowProps) {
  const { t } = useTranslation()
  const links = useMemo(() => externalLinks(row.ids), [row.ids])

  return (
    <li className={styles.mineRow}>
      <div className={styles.mineThumb}>
        <Poster src={row.posterUrl} alt={row.title} size="sm" />
      </div>

      <div className={styles.mineBody}>
        <p className={styles.mineTitle}>
          {row.title}
          {row.year ? <span className={styles.mineYear}>{row.year}</span> : null}
        </p>

        <div className={styles.mineMeta}>
          <StatusChip status={row.status} />
          {row.provider ? <span className={styles.provider}>{row.provider}</span> : null}
        </div>

        {row.note ? <p className={styles.mineNote}>{row.note}</p> : null}

        {/* The admin's reply, when there is one. A decline without its reason is the thing
            this screen most needs to avoid. */}
        {row.adminNote ? (
          <p className={styles.adminNote}>
            <span className={styles.adminNoteLabel}>{t('requests.adminReply')}</span>
            {row.adminNote}
          </p>
        ) : null}

        {row.series ? (
          <Link href={`/series/${encodeURIComponent(row.series.slug)}`} className={styles.inLibrary}>
            <CheckCircleIcon size="1em" aria-hidden="true" />
            {t('requests.watchNow', { title: row.series.title })}
          </Link>
        ) : null}

        {links.length > 0 ? (
          <p className={styles.links}>
            {links.map((link) => (
              <a key={link.href} href={link.href} target="_blank" rel="noopener noreferrer">
                {link.label}
              </a>
            ))}
          </p>
        ) : null}
      </div>

      <div className={styles.mineActions}>
        <IconButton
          label={t('requests.cancel')}
          disabled={busy}
          onClick={onCancel}
          variant="ghost"
        >
          <TrashIcon />
        </IconButton>
      </div>
    </li>
  )
}

export default RequestsPage
