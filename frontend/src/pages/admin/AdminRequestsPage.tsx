/** Attending the wishlist.
 *
 * The screen exists to answer one question per row — *am I going to get this, and did I?* —
 * so every row carries the four things that decide it: who asked, what they asked for, how
 * many other people asked for the same thing, and the leads that let the admin go and look.
 *
 * The leads are the part that earns its space. dramatarr cannot resolve a MyDramaList or
 * DramaBoxDB id, and that is exactly why they are printed: for a large part of this library
 * those pages are the only description of the show that exists anywhere, and the admin's next
 * step is to read one. They open in a new tab, from the admin's own browser. Nothing on this
 * page fetches them, and nothing on this page starts any kind of transfer — marking a row
 * fulfilled records that the files are already on the mount.
 *
 * Declining keeps the row and asks for a reason, which is not politeness: the requester's
 * screen shows that reason, and a request that silently disappears gets made again next week.
 */

import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  OPEN_REQUEST_STATUSES,
  REQUEST_STATUSES,
  externalLinks,
  requestStatusKey,
  useAdminRequests,
  useDeleteRequest,
  usePatchRequest,
  useRequestCounts,
  usePrepareFolder,
  type AdminSeriesRequest,
  type RequestStatus,
} from '@/api/requests'
import { Avatar } from '@/components/Avatar'
import { Badge, type BadgeTone } from '@/components/Badge'
import { Button } from '@/components/Button'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { IconButton } from '@/components/IconButton'
import { Pagination } from '@/components/Pagination'
import { Poster } from '@/components/Poster'
import { Spinner } from '@/components/Spinner'
import { Textarea } from '@/components/Textarea'
import { useToast } from '@/components/Toast'
import { TocLayout } from '@/components/admin/TocLayout'
import {
  BanIcon,
  CheckCircleIcon,
  CheckIcon,
  ClockIcon,
  ExternalLinkIcon,
  FolderIcon,
  RequestIcon,
  TrashIcon,
} from '@/components/icons'

import styles from './AdminRequestsPage.module.css'

const PAGE_SIZE = 25

/** Where the owner actually goes to find a requested show. Change it here; it is a bookmark,
 *  not a service this application calls, so it needs no setting and no key. */
const DRAMADOWN_URL = 'https://dramadown.raskitoma.io'

const STATUS_TONE: Record<RequestStatus, BadgeTone> = {
  pending: 'warning',
  approved: 'info',
  fulfilled: 'success',
  declined: 'neutral',
}

const SECTIONS = [
  { id: 'requests-queue', labelKey: 'admin.requests.tocQueue' },
  { id: 'requests-history', labelKey: 'admin.requests.tocHistory' },
] as const

/** `''` is "everything"; the rest map to the API's status filter. */
type Filter = RequestStatus | ''

function StatusIcon({ status }: { status: RequestStatus }) {
  if (status === 'fulfilled') return <CheckCircleIcon size="1em" />
  if (status === 'approved') return <CheckIcon size="1em" />
  if (status === 'declined') return <BanIcon size="1em" />
  return <ClockIcon size="1em" />
}

export function AdminRequestsPage() {
  const { t } = useTranslation()
  const [filter, setFilter] = useState<Filter>('pending')
  const [page, setPage] = useState(1)

  const counts = useRequestCounts()
  const listing = useAdminRequests({ status: filter, page, pageSize: PAGE_SIZE })

  const rows = listing.data?.items ?? []
  const total = listing.data?.total ?? 0

  const choose = (next: Filter) => {
    setFilter(next)
    // A filter change is a different list; keeping page 4 would land on an empty screen.
    setPage(1)
  }

  const tally = counts.data
  const countFor = (value: Filter): number | null => {
    if (!tally) return null
    return value === '' ? tally.total : tally[value]
  }

  return (
    <TocLayout sections={SECTIONS} labelKey="admin.requests.tocLabel">
      <div className={styles.page}>
        <header className={styles.header}>
          <h1 className={styles.title}>
            <RequestIcon aria-hidden="true" />
            {t('admin.requests.title')}
          </h1>
          <p className={styles.intro}>{t('admin.requests.intro')}</p>

          {/* A shortcut, and only a shortcut. Attending a request means going and finding the
              show by hand, and DramaDown is where the owner does that — so the useful thing
              this screen can offer is one click to the other tab, not an integration. Nothing
              here talks to it: no request is handed over, no transfer is started, and closing
              that tab leaves this page exactly as it was. */}
          <a
            className={styles.shortcut}
            href={DRAMADOWN_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            <ExternalLinkIcon size="1em" aria-hidden="true" />
            {t('admin.requests.openDramadown')}
          </a>
        </header>

        <section id="requests-queue" className={styles.section}>
          <h2 className={styles.sectionTitle}>{t('admin.requests.tocQueue')}</h2>

          {/* Tabs rather than a select: there are five, they carry counts, and on this screen
              the counts are half the reason to look. */}
          <div className={styles.filters} role="tablist" aria-label={t('admin.requests.filterLabel')}>
            {(['', ...REQUEST_STATUSES] as Filter[]).map((value) => {
              const count = countFor(value)
              return (
                <button
                  key={value || 'all'}
                  type="button"
                  role="tab"
                  aria-selected={filter === value}
                  className={styles.filter}
                  onClick={() => choose(value)}
                >
                  {value === '' ? t('common.all') : t(requestStatusKey(value))}
                  {count === null ? null : <span className={styles.filterCount}>{count}</span>}
                </button>
              )
            })}
          </div>

          {listing.isError ? (
            <ErrorState error={listing.error} onRetry={() => void listing.refetch()} />
          ) : null}

          {listing.isLoading ? <Spinner label={t('common.loading')} /> : null}

          {listing.data && rows.length === 0 ? (
            <EmptyState
              title={t('admin.requests.empty')}
              description={
                filter === '' ? t('admin.requests.emptyHint') : t('admin.requests.emptyFiltered')
              }
            />
          ) : null}

          {rows.length > 0 ? (
            <ul className={styles.list}>
              {rows.map((row) => (
                <RequestRow key={row.id} row={row} />
              ))}
            </ul>
          ) : null}

          {total > PAGE_SIZE ? (
            <Pagination page={page} pageSize={PAGE_SIZE} total={total} onPage={setPage} />
          ) : null}
        </section>

        <section id="requests-history" className={styles.section}>
          <h2 className={styles.sectionTitle}>{t('admin.requests.tocHistory')}</h2>
          <p className={styles.helpText}>{t('admin.requests.historyHint')}</p>
          <div className={styles.summary}>
            {REQUEST_STATUSES.map((status) => (
              <div key={status} className={styles.summaryCell}>
                <span className={styles.summaryLabel}>{t(requestStatusKey(status))}</span>
                <span className={styles.summaryValue}>
                  {tally ? tally[status] : '—'}
                </span>
              </div>
            ))}
          </div>
          <p className={styles.helpText}>
            {t('admin.requests.openCount', {
              count: tally ? tally.pending + tally.approved : 0,
            })}
          </p>
        </section>
      </div>
    </TocLayout>
  )
}

// --- One request ------------------------------------------------------------------------------

function RequestRow({ row }: { row: AdminSeriesRequest }) {
  const { t } = useTranslation()
  const toast = useToast()
  const patch = usePatchRequest()
  const remove = useDeleteRequest()
  const folder = usePrepareFolder()

  const [replying, setReplying] = useState(false)
  const [reply, setReply] = useState(row.adminNote ?? '')

  const links = externalLinks(row.ids)
  const open = OPEN_REQUEST_STATUSES.includes(row.status)

  const setStatus = (status: RequestStatus, adminNote?: string) => {
    patch.mutate(
      { id: row.id, patch: { status, ...(adminNote === undefined ? {} : { adminNote }) } },
      {
        onSuccess: () => {
          setReplying(false)
          toast.show(t('admin.requests.updated', { title: row.title }), 'success')
        },
      },
    )
  }

  return (
    <li className={styles.row}>
      <div className={styles.rowPoster}>
        <Poster src={row.posterUrl} alt={row.title} size="sm" />
      </div>

      <div className={styles.rowBody}>
        <p className={styles.rowTitle}>
          {row.title}
          {row.year ? <span className={styles.rowYear}>{row.year}</span> : null}
        </p>

        <div className={styles.rowMeta}>
          <Badge tone={STATUS_TONE[row.status]} className={styles.statusChip}>
            <StatusIcon status={row.status} />
            {t(requestStatusKey(row.status))}
          </Badge>

          <span className={styles.requester}>
            <Avatar
              name={row.requester.displayName}
              email={row.requester.email}
              src={row.requester.avatarUrl}
              size="sm"
            />
            {row.requester.displayName}
          </span>

          {/* The queue's priority signal: three people waiting is a different job from one. */}
          {row.alsoRequestedBy > 0 ? (
            <Badge tone="accent">
              {t('admin.requests.alsoRequested', { count: row.alsoRequestedBy })}
            </Badge>
          ) : null}

          {row.provider ? <span className={styles.provider}>{row.provider}</span> : null}
        </div>

        {row.note ? (
          <p className={styles.note}>
            <span className={styles.noteLabel}>{t('admin.requests.requesterNote')}</span>
            {row.note}
          </p>
        ) : null}

        {/* Leads, not sources. Opened by hand, in this browser. */}
        {links.length > 0 || row.sourceUrl ? (
          <p className={styles.links}>
            {row.sourceUrl ? (
              <a href={row.sourceUrl} target="_blank" rel="noopener noreferrer">
                <ExternalLinkIcon size="1em" aria-hidden="true" />
                {row.provider || t('admin.requests.openSource')}
              </a>
            ) : null}
            {links.map((link) => (
              <a key={link.href} href={link.href} target="_blank" rel="noopener noreferrer">
                {link.label}
              </a>
            ))}
          </p>
        ) : null}

        {row.adminNote && !replying ? (
          <p className={styles.reply}>
            <span className={styles.noteLabel}>{t('admin.requests.yourReply')}</span>
            {row.adminNote}
          </p>
        ) : null}

        {row.resolvedByName ? (
          <p className={styles.resolvedBy}>
            {t('admin.requests.resolvedBy', { name: row.resolvedByName })}
          </p>
        ) : null}

        {replying ? (
          <div className={styles.replyBox}>
            <Textarea
              rows={2}
              value={reply}
              maxLength={1000}
              placeholder={t('admin.requests.replyPlaceholder')}
              aria-label={t('admin.requests.replyLabel')}
              onChange={(event) => setReply(event.target.value)}
            />
            <div className={styles.replyActions}>
              <Button
                variant="primary"
                size="sm"
                loading={patch.isPending}
                onClick={() => setStatus('declined', reply)}
              >
                {t('admin.requests.declineWithReply')}
              </Button>
              <Button
                size="sm"
                onClick={() => {
                  setReplying(false)
                  setReply(row.adminNote ?? '')
                }}
              >
                {t('common.cancel')}
              </Button>
            </div>
          </div>
        ) : null}
      </div>

      <div className={styles.rowActions}>
        {open ? (
          <>
            {row.status === 'pending' ? (
              <Button size="sm" disabled={patch.isPending} onClick={() => setStatus('approved')}>
                <CheckIcon size="1em" aria-hidden="true" />
                {t('admin.requests.approve')}
              </Button>
            ) : null}
            <Button
              variant="primary"
              size="sm"
              disabled={patch.isPending}
              onClick={() => setStatus('fulfilled')}
            >
              <CheckCircleIcon size="1em" aria-hidden="true" />
              {t('admin.requests.fulfil')}
            </Button>
            <Button size="sm" disabled={patch.isPending} onClick={() => setReplying(true)}>
              <BanIcon size="1em" aria-hidden="true" />
              {t('admin.requests.decline')}
            </Button>
          </>
        ) : (
          <Button size="sm" disabled={patch.isPending} onClick={() => setStatus('pending')}>
            <ClockIcon size="1em" aria-hidden="true" />
            {t('admin.requests.reopen')}
          </Button>
        )}

        {/* Where to put the files. Named canonically before anything lands in it, so the
            scan that picks them up already knows what they are. */}
        <Button
          size="sm"
          loading={folder.isPending}
          onClick={() =>
            folder.mutate(row.id, {
              onSuccess: (made) =>
                toast.show(
                  made.created
                    ? t('admin.requests.folderCreated', { path: made.path })
                    : t('admin.requests.folderExists', { path: made.path }),
                  'success',
                ),
            })
          }
        >
          <FolderIcon size="1em" aria-hidden="true" />
          {t('admin.requests.prepareFolder')}
        </Button>

        <IconButton
          label={t('admin.requests.deleteRow')}
          disabled={remove.isPending}
          onClick={() => remove.mutate(row.id)}
        >
          <TrashIcon />
        </IconButton>
      </div>
    </li>
  )
}

export default AdminRequestsPage
