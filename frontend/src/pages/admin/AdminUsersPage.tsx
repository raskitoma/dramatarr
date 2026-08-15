/** Household members and the invitations that create them.
 *
 * Three details matter more than the table itself:
 *  - the last-admin guard is reported on the row that triggered it, because a toast that says
 *    "at least one admin must remain" while three rows are on screen tells you nothing;
 *  - the founding account's controls are rendered *disabled with a reason* rather than hidden.
 *    A missing control raises a question ("why can't I see the role selector on this row?");
 *    a disabled one with a sentence next to it answers it and teaches the rule at the same
 *    time. The server refuses these edits regardless — this is the explanation, not the guard;
 *  - a created invitation always shows its enrollment URL with a copy button, and says whether
 *    the mail actually went out. That is what makes dramatarr usable before SMTP is configured.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import {
  adminKeys,
  apiErrorKeys,
  createInvite,
  deleteUser,
  listInvites,
  listUsers,
  patchUser,
  resendInvite,
  revokeInvite,
} from '@/api/admin'
import { ApiRequestError } from '@/api/client'
import {
  avatarUrlOf,
  isProtectedUser,
  type Invite,
  type InviteCreated,
  type Role,
  type User,
} from '@/api/types'
import Avatar from '@/components/Avatar'
import Badge from '@/components/Badge'
import Button from '@/components/Button'
import Dialog from '@/components/Dialog'
import EmptyState from '@/components/EmptyState'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import IconButton from '@/components/IconButton'
import Input from '@/components/Input'
import Select from '@/components/Select'
import Spinner from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { cx } from '@/components/cx'
import { CheckCircleIcon, ShieldIcon, TrashIcon, UnwatchIcon } from '@/components/icons'
import { TocLayout } from '@/components/admin/TocLayout'
import { formatDate, formatRelativeTime } from '@/lib/format'

import styles from './AdminUsersPage.module.css'

const ROLES: Role[] = ['viewer', 'admin']
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function roleLabelKey(role: Role): string {
  return role === 'admin' ? 'account.roleAdmin' : 'account.roleViewer'
}

/**
 * A data cell that names its own column.
 *
 * Below 720px the table stops being a table and each row becomes a card, at which point the
 * header row is gone and a bare "viewer" or "never signed in" has lost the thing that told you
 * what it was. The label is real text rather than generated content precisely so a screen
 * reader reads it once the layout has stacked; above the breakpoint CSS takes it back out of
 * the document, and the column header is doing that job again.
 */
function Cell({
  label,
  className,
  children,
}: {
  label: string
  className?: string
  children: ReactNode
}) {
  return (
    <td className={cx(styles.cell, className)}>
      <span className={styles.cellLabel}>{label}</span>
      <div className={styles.cellValue}>{children}</div>
    </td>
  )
}

/** The page's two halves. Both always render, so the list is constant. */
const SECTIONS = [
  { id: 'users-members', labelKey: 'admin.users.title' },
  { id: 'users-invites', labelKey: 'admin.users.pendingInvites' },
] as const

export function AdminUsersPage() {
  const { t } = useTranslation()

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>{t('admin.users.title')}</h1>
      </header>
      <TocLayout sections={SECTIONS} labelKey="admin.users.tocLabel">
        <UsersSection />
        <InvitesSection />
      </TocLayout>
    </div>
  )
}

// --- Users ----------------------------------------------------------------------------------

function UsersSection() {
  const { t, i18n } = useTranslation()
  const queryClient = useQueryClient()
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const [deleteTarget, setDeleteTarget] = useState<User | null>(null)

  const usersQuery = useQuery({
    queryKey: adminKeys.users(),
    queryFn: listUsers,
  })

  /**
   * Row-scoped error text.
   *
   * `LAST_ADMIN` gets a sentence written for this table; everything else — `PROTECTED_ACCOUNT`
   * included — resolves through `apiErrorKeys`, which finds `errors.<CODE>` when we have it and
   * degrades to the generic message when we do not. Either way the answer lands on the row that
   * asked the question, never in a toast that has lost its subject.
   */
  const setRowError = (id: string, error: unknown) => {
    const message =
      error instanceof ApiRequestError && error.code === 'LAST_ADMIN'
        ? t('admin.users.lastAdminError')
        : t(apiErrorKeys(error))
    setRowErrors((previous) => ({ ...previous, [id]: message }))
  }

  const clearRowError = (id: string) => {
    setRowErrors((previous) => {
      if (!(id in previous)) return previous
      const next = { ...previous }
      delete next[id]
      return next
    })
  }

  const update = useMutation({
    mutationFn: (input: { id: string; role?: Role; isActive?: boolean }) =>
      patchUser(input.id, { role: input.role, isActive: input.isActive }),
    onMutate: (input) => clearRowError(input.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: adminKeys.users() })
    },
    onError: (error, input) => setRowError(input.id, error),
  })

  const remove = useMutation({
    mutationFn: (id: string) => deleteUser(id),
    onMutate: (id) => clearRowError(id),
    onSuccess: async () => {
      setDeleteTarget(null)
      await queryClient.invalidateQueries({ queryKey: adminKeys.users() })
    },
    onError: (error, id) => {
      setDeleteTarget(null)
      setRowError(id, error)
    },
  })

  const users = usersQuery.data ?? []

  return (
    <section className={styles.card} id="users-members">
      <h2 className={styles.sectionTitle}>{t('admin.users.title')}</h2>

      {usersQuery.isPending ? <Spinner label={t('common.loading')} /> : null}

      {usersQuery.isError ? (
        <ErrorState error={usersQuery.error} onRetry={() => void usersQuery.refetch()} />
      ) : null}

      {!usersQuery.isPending && !usersQuery.isError && users.length === 0 ? (
        <EmptyState title={t('common.empty')} />
      ) : null}

      {users.length > 0 ? (
        <div className={styles.scroller}>
          <table className={styles.table}>
            <caption className="sr-only">{t('admin.users.title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('account.displayName')}</th>
                <th scope="col">{t('account.email')}</th>
                <th scope="col">{t('account.role')}</th>
                <th scope="col">{t('admin.users.lastAccess')}</th>
                <th scope="col" className={styles.actionsCell}>
                  {t('common.actions')}
                </th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => {
                const rowError = rowErrors[user.id]
                const busy = update.isPending && update.variables?.id === user.id
                // The founding account: the server refuses to demote, deactivate or delete it,
                // so every control that would attempt one of those is disabled and says why.
                const locked = isProtectedUser(user)
                const lockedNoteId = `protected-${user.id}`
                return (
                  <tr key={user.id} className={user.isActive ? undefined : styles.inactiveRow}>
                    {/* The flex row is an element inside the cell, not the cell itself: a
                        `th` set to `display: flex` stops stretching to the row's height, and
                        its bottom rule then floats halfway up a tall row. */}
                    <th scope="row" className={styles.userCell}>
                      <div className={styles.userLine}>
                        <Avatar
                          name={user.displayName}
                          email={user.email}
                          src={avatarUrlOf(user)}
                          color={user.avatarColor}
                          size="sm"
                        />
                        <span className={styles.userText}>
                          <span className={styles.userNameRow}>
                            <span className={styles.userName}>{user.displayName}</span>
                            {locked ? (
                              <Badge
                                tone="info"
                                className={styles.protectedBadge}
                                title={t('admin.users.protectedHint')}
                              >
                                <ShieldIcon size="1em" />
                                {t('admin.users.protected')}
                              </Badge>
                            ) : null}
                          </span>
                          {user.isActive ? null : (
                            <Badge tone="warning">{t('admin.users.inactive')}</Badge>
                          )}
                        </span>
                      </div>
                    </th>
                    <Cell label={t('account.email')} className={styles.email}>
                      {user.email}
                    </Cell>
                    <Cell label={t('account.role')}>
                      <Select
                        id={`role-${user.id}`}
                        aria-label={t('admin.users.changeRole')}
                        aria-describedby={locked ? lockedNoteId : undefined}
                        value={user.role}
                        disabled={busy || locked}
                        onChange={(event) =>
                          update.mutate({ id: user.id, role: event.target.value as Role })
                        }
                      >
                        {ROLES.map((role) => (
                          <option key={role} value={role}>
                            {t(roleLabelKey(role))}
                          </option>
                        ))}
                      </Select>
                    </Cell>
                    <Cell label={t('admin.users.lastAccess')} className={styles.muted}>
                      {user.lastLoginAt === null
                        ? t('admin.users.neverLoggedIn')
                        : formatRelativeTime(user.lastLoginAt, t)}
                    </Cell>
                    <Cell label={t('common.actions')} className={styles.actionsCell}>
                      <div className={styles.rowActions}>
                        {/* Glyphs, but never bare ones: with several identical rows on screen
                            each name says which member it acts on, so a screen reader hears
                            "Deactivate Beto" rather than the fourth "Deactivate" in a row. */}
                        <IconButton
                          variant="secondary"
                          label={
                            user.isActive
                              ? t('admin.users.deactivateFor', { name: user.displayName })
                              : t('admin.users.activateFor', { name: user.displayName })
                          }
                          loading={busy}
                          disabled={update.isPending || locked}
                          aria-describedby={locked ? lockedNoteId : undefined}
                          onClick={() =>
                            update.mutate({ id: user.id, isActive: !user.isActive })
                          }
                        >
                          {user.isActive ? <UnwatchIcon /> : <CheckCircleIcon />}
                        </IconButton>
                        <IconButton
                          variant="danger"
                          label={t('admin.users.deleteUserFor', { name: user.displayName })}
                          disabled={locked}
                          aria-describedby={locked ? lockedNoteId : undefined}
                          onClick={() => setDeleteTarget(user)}
                        >
                          <TrashIcon />
                        </IconButton>
                      </div>
                      {locked ? (
                        <p id={lockedNoteId} className={styles.protectedNote}>
                          {t('admin.users.protectedHint')}
                        </p>
                      ) : null}
                      {rowError ? (
                        <p className={styles.rowError} role="alert">
                          {rowError}
                        </p>
                      ) : null}
                    </Cell>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      <Dialog
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title={t('admin.users.deleteUser')}
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="danger"
              loading={remove.isPending}
              onClick={() => {
                if (deleteTarget) remove.mutate(deleteTarget.id)
              }}
            >
              {t('common.delete')}
            </Button>
          </>
        }
      >
        <p>
          {t('admin.users.deleteUserConfirm', {
            name: deleteTarget?.displayName ?? deleteTarget?.email ?? '',
          })}
        </p>
        <p className={styles.dialogMeta}>
          {deleteTarget
            ? t('account.createdOn', { date: formatDate(deleteTarget.createdAt, i18n.language) })
            : ''}
        </p>
      </Dialog>
    </section>
  )
}

// --- Invites --------------------------------------------------------------------------------

function InvitesSection() {
  const { t, i18n } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const [inviteOpen, setInviteOpen] = useState(false)
  const [created, setCreated] = useState<InviteCreated | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<Invite | null>(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<Role>('viewer')
  const [touched, setTouched] = useState(false)

  const invitesQuery = useQuery({
    queryKey: adminKeys.invites(),
    queryFn: listInvites,
  })

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: adminKeys.invites() })
  }

  const create = useMutation({
    mutationFn: () => createInvite({ email: email.trim().toLowerCase(), role }),
    onSuccess: async (result) => {
      setInviteOpen(false)
      setEmail('')
      setRole('viewer')
      setTouched(false)
      setCreated(result)
      await invalidate()
    },
  })

  const resend = useMutation({
    mutationFn: (id: string) => resendInvite(id),
    onSuccess: async (result) => {
      setCreated(result)
      await invalidate()
    },
  })

  const revoke = useMutation({
    mutationFn: (id: string) => revokeInvite(id),
    onSuccess: async () => {
      setRevokeTarget(null)
      await invalidate()
    },
  })

  const emailError =
    touched && !EMAIL_PATTERN.test(email.trim()) ? t('admin.settings.invalidEmail') : undefined

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setTouched(true)
    if (!EMAIL_PATTERN.test(email.trim())) return
    create.mutate()
  }

  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      show(t('common.copied'), 'success')
    } catch {
      show(t('errors.generic'), 'error')
    }
  }

  const pending = (invitesQuery.data ?? []).filter((invite) => invite.status === 'pending')

  return (
    <section className={styles.card} id="users-invites">
      <div className={styles.sectionHeader}>
        <h2 className={styles.sectionTitle}>{t('admin.users.pendingInvites')}</h2>
        <Button variant="primary" onClick={() => setInviteOpen(true)}>
          {t('admin.users.invite')}
        </Button>
      </div>

      {invitesQuery.isPending ? <Spinner label={t('common.loading')} /> : null}

      {invitesQuery.isError ? (
        <ErrorState error={invitesQuery.error} onRetry={() => void invitesQuery.refetch()} />
      ) : null}

      {!invitesQuery.isPending && !invitesQuery.isError && pending.length === 0 ? (
        <EmptyState
          title={t('admin.users.noPendingInvites')}
          description={t('admin.users.inviteHint')}
        />
      ) : null}

      {pending.length > 0 ? (
        <ul className={styles.inviteList}>
          {pending.map((invite) => (
            <li key={invite.id} className={styles.inviteRow}>
              <div className={styles.inviteText}>
                <span className={styles.inviteEmail}>{invite.email}</span>
                <span className={styles.inviteMeta}>
                  {t('admin.users.inviteExpires', {
                    when: formatDate(invite.expiresAt, i18n.language),
                  })}
                </span>
              </div>
              <Badge tone={invite.role === 'admin' ? 'accent' : 'neutral'}>
                {t(roleLabelKey(invite.role))}
              </Badge>
              <Badge tone="info">{t(`admin.users.inviteStatus.${invite.status}`)}</Badge>
              <div className={styles.rowActions}>
                <Button
                  size="sm"
                  loading={resend.isPending && resend.variables === invite.id}
                  onClick={() => resend.mutate(invite.id)}
                >
                  {t('admin.users.resendInvite')}
                </Button>
                <Button size="sm" variant="danger" onClick={() => setRevokeTarget(invite)}>
                  {t('admin.users.revokeInvite')}
                </Button>
              </div>
            </li>
          ))}
        </ul>
      ) : null}

      {resend.isError ? <ErrorState error={resend.error} /> : null}
      {revoke.isError ? <ErrorState error={revoke.error} /> : null}

      {/* Create ------------------------------------------------------------------------- */}
      <Dialog
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        title={t('admin.users.inviteTitle')}
        description={t('admin.users.inviteHint')}
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setInviteOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" form="invite-form" variant="primary" loading={create.isPending}>
              {t('admin.users.inviteSend')}
            </Button>
          </>
        }
      >
        <form id="invite-form" className={styles.inviteForm} onSubmit={onSubmit}>
          <Field
            label={t('admin.users.inviteEmail')}
            htmlFor="invite-email"
            required
            error={emailError}
          >
            <Input
              id="invite-email"
              type="email"
              autoComplete="email"
              value={email}
              invalid={Boolean(emailError)}
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>
          <Field label={t('admin.users.inviteRole')} htmlFor="invite-role">
            <Select
              id="invite-role"
              value={role}
              onChange={(event) => setRole(event.target.value as Role)}
            >
              {ROLES.map((option) => (
                <option key={option} value={option}>
                  {t(roleLabelKey(option))}
                </option>
              ))}
            </Select>
          </Field>
          {create.isError ? <ErrorState error={create.error} /> : null}
        </form>
      </Dialog>

      {/* Enrollment URL --------------------------------------------------------------- */}
      <Dialog
        open={created !== null}
        onClose={() => setCreated(null)}
        title={t('admin.users.inviteLink')}
        size="md"
        footer={
          <Button variant="primary" onClick={() => setCreated(null)}>
            {t('common.close')}
          </Button>
        }
      >
        {created ? (
          <div className={styles.created}>
            <p className={created.emailSent ? styles.sent : styles.notSent} role="status">
              {created.emailSent
                ? t('admin.users.inviteSent', { email: created.email })
                : t('admin.users.inviteNotSent')}
            </p>
            <Field label={t('admin.users.inviteLink')} htmlFor="invite-url">
              <div className={styles.copyRow}>
                <Input
                  id="invite-url"
                  readOnly
                  value={created.enrollmentUrl}
                  onFocus={(event) => event.target.select()}
                />
                <Button onClick={() => void copy(created.enrollmentUrl)}>{t('common.copy')}</Button>
              </div>
            </Field>
            <p className={styles.inviteMeta}>
              {t('admin.users.inviteExpires', {
                when: formatDate(created.expiresAt, i18n.language),
              })}
            </p>
          </div>
        ) : null}
      </Dialog>

      {/* Revoke ------------------------------------------------------------------------- */}
      <Dialog
        open={revokeTarget !== null}
        onClose={() => setRevokeTarget(null)}
        title={t('admin.users.revokeInvite')}
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRevokeTarget(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="danger"
              loading={revoke.isPending}
              onClick={() => {
                if (revokeTarget) revoke.mutate(revokeTarget.id)
              }}
            >
              {t('admin.users.revokeInvite')}
            </Button>
          </>
        }
      >
        <p>{t('admin.users.revokeInviteConfirm', { email: revokeTarget?.email ?? '' })}</p>
      </Dialog>
    </section>
  )
}

export default AdminUsersPage
