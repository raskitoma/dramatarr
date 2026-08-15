/** The user's own account: profile, appearance, playback defaults, passkeys and sessions.
 *
 * Two conventions run through this screen.
 *
 * A *preference* (theme, language, audio/subtitle language, autoplay) commits the moment it
 * changes — the control itself is the confirmation, and a Save button next to a switch reads
 * as a bug. A *value the user types* (display name, passkey nickname) commits on submit,
 * because saving every keystroke would be both noisy and wrong. `patchUser` updates the cached
 * session immediately in either case, so the header re-renders without a refetch.
 *
 * Passkeys are the only way most people get back in, so removing one is treated as dangerous:
 * it asks first, and the server's `LAST_CREDENTIAL` refusal is surfaced on the row that
 * triggered it rather than as a floating toast that has lost its subject.
 *
 * The sign-in methods block above them applies the same idea one level up: all three ways in
 * are shown together, because the only question worth answering there is "if I remove this,
 * can I still get back in?". `LAST_SIGN_IN_METHOD` therefore lands beside the disconnect
 * control that caused it, for the same reason `LAST_CREDENTIAL` lands on its row.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type ReactNode,
} from 'react'
import { useTranslation } from 'react-i18next'

import {
  accountKeys,
  deleteAvatar,
  deletePasskey,
  listPasskeys,
  listSessions,
  renamePasskey,
  revokeSession,
  updateProfile,
  uploadAvatar,
  type ProfilePatch,
} from '@/api/account'
import { authKeys, disconnectGoogle, getSignInMethods, startGoogleLink } from '@/api/auth'
import { avatarUrlOf, type AuthSessionInfo, type Passkey, type User } from '@/api/types'
import Avatar from '@/components/Avatar'
import Badge from '@/components/Badge'
import Button from '@/components/Button'
import Dialog from '@/components/Dialog'
import EmptyState from '@/components/EmptyState'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import GoogleMark from '@/components/GoogleMark'
import IconButton from '@/components/IconButton'
import Input from '@/components/Input'
import { LocaleSelect } from '@/components/LocaleSelect'
import Select from '@/components/Select'
import Spinner from '@/components/Spinner'
import Switch from '@/components/Switch'
import { ThemeToggle } from '@/components/ThemeToggle'
import { useToast } from '@/components/Toast'
import { EditIcon, TrashIcon } from '@/components/icons'
import { TocLayout } from '@/components/admin/TocLayout'
import { useAuth } from '@/hooks/useAuth'
import { isPasskeyCancellation, isPasskeySupported, startPasskeyRegistration } from '@/lib/webauthn'
import { formatBytes, formatDate, formatRelativeTime, languageName } from '@/lib/format'
import { IMAGE_ACCEPT, IMAGE_MAX_BYTES, isAcceptedImageType } from '@/lib/images'

import styles from './AccountPage.module.css'

/**
 * The languages worth offering as a track preference. Labels come from `languageName`, which
 * reads `Intl.DisplayNames`: a language name is data the platform already has in every locale,
 * and hand-translating twelve names twice is how they drift.
 */
const TRACK_LANGUAGES = [
  'es',
  'en',
  'pt',
  'fr',
  'de',
  'it',
  'ja',
  'ko',
  'zh',
  'ru',
  'ar',
  'hi',
] as const

/** Six blocks, all of which always render — so the index is a constant. */
const ACCOUNT_SECTIONS = [
  { id: 'account-profile', labelKey: 'account.profile' },
  { id: 'account-appearance', labelKey: 'account.appearance' },
  { id: 'account-playback', labelKey: 'account.playback' },
  { id: 'account-signin', labelKey: 'account.signInMethods' },
  { id: 'account-passkeys', labelKey: 'account.passkeys' },
  { id: 'account-sessions', labelKey: 'account.sessions' },
] as const

export function AccountPage() {
  const { t } = useTranslation()
  const { user } = useAuth()

  // ProtectedRoute guarantees a session, so this is belt-and-braces for a race on sign-out.
  if (!user) {
    return (
      <div className={styles.center}>
        <Spinner label={t('common.loading')} />
      </div>
    )
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>{t('account.title')}</h1>
      </header>

      <TocLayout sections={ACCOUNT_SECTIONS} labelKey="account.tocLabel">
        <ProfileSection user={user} />
        <AppearanceSection />
        <PlaybackSection user={user} />
        <SignInMethodsSection />
        <PasskeysSection />
        <SessionsSection />
      </TocLayout>
    </div>
  )
}

/** Shared shell so every block on the page has the same card, heading and hint rhythm. */
function Section({
  id,
  title,
  hint,
  action,
  children,
}: {
  /** Anchor for the page index. Every section on this page has one. */
  id?: string
  title: string
  hint?: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section className={styles.card} id={id}>
      <div className={styles.sectionHead}>
        <h2 className={styles.sectionTitle}>{title}</h2>
        {action}
      </div>
      {hint ? <p className={styles.sectionHint}>{hint}</p> : null}
      {children}
    </section>
  )
}

// --- Profile --------------------------------------------------------------------------------

function ProfileSection({ user }: { user: User }) {
  const { t, i18n } = useTranslation()
  const { patchUser } = useAuth()
  const { show } = useToast()
  const [displayName, setDisplayName] = useState(user.displayName)

  // Re-seed when the stored name changes under us — after a save here, or after a refetch
  // picked up an edit made on another device.
  useEffect(() => {
    setDisplayName(user.displayName)
  }, [user.displayName])

  const save = useMutation({
    mutationFn: (name: string) => updateProfile({ displayName: name }),
    onSuccess: (updated) => {
      patchUser(updated)
      show(t('common.saved'), 'success')
    },
  })

  const trimmed = displayName.trim()
  const dirty = trimmed !== user.displayName && trimmed !== ''

  function handleSubmit(event: FormEvent): void {
    event.preventDefault()
    if (!dirty) return
    save.mutate(trimmed)
  }

  return (
    <Section id="account-profile" title={t('account.profile')}>
      <div className={styles.identity}>
        <Avatar
          name={user.displayName}
          email={user.email}
          src={avatarUrlOf(user)}
          color={user.avatarColor}
          size="lg"
        />
        <div className={styles.identityText}>
          <p className={styles.identityName}>{user.displayName}</p>
          <p className={styles.identityEmail}>{user.email}</p>
        </div>
        <Badge tone={user.role === 'admin' ? 'accent' : 'neutral'}>
          {t(user.role === 'admin' ? 'account.roleAdmin' : 'account.roleViewer')}
        </Badge>
      </div>

      <AvatarPicker user={user} />

      <form className={styles.form} onSubmit={handleSubmit}>
        <Field label={t('account.displayName')} htmlFor="account-name" required>
          <Input
            id="account-name"
            value={displayName}
            maxLength={80}
            autoComplete="nickname"
            onChange={(event) => {
              setDisplayName(event.target.value)
            }}
          />
        </Field>
        <Field
          label={t('account.email')}
          htmlFor="account-email"
          hint={t('account.emailImmutable')}
        >
          <Input id="account-email" value={user.email} readOnly disabled />
        </Field>
        <div className={styles.formFooter}>
          <p className={styles.meta}>
            {t('account.createdOn', { date: formatDate(user.createdAt, i18n.language) })}
          </p>
          <Button type="submit" disabled={!dirty} loading={save.isPending}>
            {t('common.save')}
          </Button>
        </div>
        {save.isError ? <ErrorState error={save.error} /> : null}
      </form>
    </Section>
  )
}

// --- Profile picture ------------------------------------------------------------------------

/**
 * Choose, preview, upload and remove the account's picture.
 *
 * Three things are deliberate here.
 *
 * The picture is *previewed locally before anything is sent*: a phone photo is the wrong crop
 * as often as it is the right one, and finding that out after the upload means a second upload
 * to undo it. The preview is an object URL, revoked when the choice changes so a member who
 * tries six photos does not leak six blobs.
 *
 * The privacy line is shown *before* the file input, not under it. A photo taken with a phone
 * usually carries GPS coordinates; the server strips them when it re-encodes, and people are
 * entitled to know that before they upload, not as reassurance afterwards.
 *
 * The type and size checks run here as well as on the server. Not because the client is
 * trusted — it never is — but because refusing a 40 MB HEIC instantly is a better answer than
 * a progress bar that ends in an error.
 */
function AvatarPicker({ user }: { user: User }) {
  const { t } = useTranslation()
  const { patchUser } = useAuth()
  const { show } = useToast()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [rejection, setRejection] = useState<string | null>(null)

  const current = avatarUrlOf(user)

  // One object URL per chosen file, revoked when the choice changes or the section unmounts.
  useEffect(() => {
    if (file === null) {
      setPreview(null)
      return undefined
    }
    const url = URL.createObjectURL(file)
    setPreview(url)
    return () => {
      URL.revokeObjectURL(url)
    }
  }, [file])

  function reset(): void {
    setFile(null)
    setRejection(null)
    if (inputRef.current) inputRef.current.value = ''
  }

  const upload = useMutation({
    mutationFn: (chosen: File) => uploadAvatar(chosen),
    onSuccess: (updated) => {
      // The header reads the same session object, so it repaints with the new picture without
      // a refetch — the same trick the display-name form uses.
      patchUser(updated)
      reset()
      show(t('account.avatarUpdated'), 'success')
    },
  })

  const remove = useMutation({
    mutationFn: () => deleteAvatar(),
    onSuccess: (updated) => {
      patchUser({ ...updated, avatarUrl: null })
      reset()
      show(t('account.avatarRemoved'), 'success')
    },
  })

  function handleChoose(event: ChangeEvent<HTMLInputElement>): void {
    upload.reset()
    const chosen = event.target.files?.[0] ?? null
    if (chosen === null) {
      reset()
      return
    }
    if (!isAcceptedImageType(chosen)) {
      setFile(null)
      setRejection(t('account.avatarBadType'))
      return
    }
    if (chosen.size > IMAGE_MAX_BYTES) {
      setFile(null)
      setRejection(t('account.avatarTooLarge', { size: formatBytes(IMAGE_MAX_BYTES) }))
      return
    }
    setRejection(null)
    setFile(chosen)
  }

  const busy = upload.isPending || remove.isPending

  return (
    <div className={styles.avatarBlock}>
      <div className={styles.avatarPreview}>
        <Avatar
          name={user.displayName}
          email={user.email}
          // While a file is chosen this shows the file, so "what will I look like" is answered
          // before the upload rather than after it.
          src={preview ?? current}
          color={user.avatarColor}
          size="xl"
        />
        <p className={styles.avatarState} aria-live="polite">
          {upload.isPending
            ? t('account.avatarUploading')
            : file !== null
              ? t('account.avatarPreviewNote')
              : current === null
                ? t('account.avatarNone')
                : t('account.avatarCurrent')}
        </p>
      </div>

      <div className={styles.avatarControls}>
        <p className={styles.hint}>{t('account.avatarPrivacy')}</p>

        <Field
          label={t('account.avatarChoose')}
          htmlFor="account-avatar-file"
          hint={t('account.avatarFormats', { size: formatBytes(IMAGE_MAX_BYTES) })}
          error={rejection ?? undefined}
        >
          <input
            id="account-avatar-file"
            ref={inputRef}
            className={styles.fileInput}
            type="file"
            accept={IMAGE_ACCEPT}
            disabled={busy}
            onChange={handleChoose}
          />
        </Field>

        <div className={styles.avatarActions}>
          <Button
            variant="primary"
            loading={upload.isPending}
            disabled={file === null || busy}
            onClick={() => {
              if (file) upload.mutate(file)
            }}
          >
            {t('account.avatarUpload')}
          </Button>
          {file !== null ? (
            <Button variant="ghost" disabled={busy} onClick={reset}>
              {t('common.cancel')}
            </Button>
          ) : null}
          {current !== null && file === null ? (
            <Button
              variant="danger"
              loading={remove.isPending}
              disabled={busy}
              onClick={() => remove.mutate()}
            >
              {t('account.avatarRemove')}
            </Button>
          ) : null}
        </div>

        {upload.isError ? <ErrorState error={upload.error} /> : null}
        {remove.isError ? <ErrorState error={remove.error} /> : null}
      </div>
    </div>
  )
}

// --- Appearance -----------------------------------------------------------------------------

/**
 * Theme and language reuse the header's own controls. They already own the localStorage key
 * and the fire-and-forget `PATCH /me`, so re-implementing them here would mean two components
 * racing to write the same preference.
 */
function AppearanceSection() {
  const { t } = useTranslation()

  return (
    <Section id="account-appearance" title={t('account.appearance')} hint={t('account.appearanceHint')}>
      {/* Both controls already carry their own accessible name (`aria-label` on the
          radiogroup and on the select), so these headings are deliberately plain text —
          associating them too would announce the label twice. */}
      <div className={styles.grid}>
        <div className={styles.stack}>
          <span className={styles.label}>{t('theme.label')}</span>
          <ThemeToggle />
          <p className={styles.hint}>{t('theme.autoHint')}</p>
        </div>
        <div className={styles.stack}>
          <span className={styles.label}>{t('locale.label')}</span>
          <LocaleSelect />
        </div>
      </div>
    </Section>
  )
}

// --- Playback -------------------------------------------------------------------------------

function PlaybackSection({ user }: { user: User }) {
  const { t, i18n } = useTranslation()
  const { patchUser } = useAuth()

  const save = useMutation({
    mutationFn: (input: { patch: ProfilePatch; rollback: ProfilePatch }) =>
      updateProfile(input.patch),
    onSuccess: (updated) => {
      patchUser(updated)
    },
    onError: (_error, input) => {
      // Put the controls back where they were. Leaving a switch showing a value the server
      // refused is worse than the error message alone: the next visit would contradict it.
      patchUser(input.rollback)
    },
  })

  /**
   * Optimistic: the control moves now and a failed write is reported below the section.
   * The rollback restores all three fields rather than just the changed one — only one can
   * differ per call, so it costs nothing and cannot miss the field that actually moved.
   */
  function commit(patch: ProfilePatch): void {
    const rollback: ProfilePatch = {
      preferredAudioLang: user.preferredAudioLang,
      preferredSubtitleLang: user.preferredSubtitleLang,
      autoplayNext: user.autoplayNext,
    }
    patchUser(patch)
    save.mutate({ patch, rollback })
  }

  const languageOptions = TRACK_LANGUAGES.map((code) => ({
    code,
    label: languageName(code, i18n.language),
  })).sort((a, b) => a.label.localeCompare(b.label, i18n.language))

  return (
    <Section id="account-playback" title={t('account.playback')} hint={t('account.playbackHint')}>
      <div className={styles.grid}>
        <Field label={t('account.preferredAudio')} htmlFor="account-audio">
          <Select
            id="account-audio"
            value={user.preferredAudioLang ?? ''}
            onChange={(event) => {
              commit({ preferredAudioLang: event.target.value || null })
            }}
          >
            <option value="">{t('account.noPreference')}</option>
            {languageOptions.map((option) => (
              <option key={option.code} value={option.code}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t('account.preferredSubtitles')} htmlFor="account-subtitles">
          <Select
            id="account-subtitles"
            value={user.preferredSubtitleLang ?? ''}
            onChange={(event) => {
              commit({ preferredSubtitleLang: event.target.value || null })
            }}
          >
            <option value="">{t('account.noPreference')}</option>
            {languageOptions.map((option) => (
              <option key={option.code} value={option.code}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <Switch
        className={styles.switch}
        checked={user.autoplayNext}
        label={t('account.autoplayNext')}
        hint={t('account.autoplayNextHint')}
        onChange={(value) => {
          commit({ autoplayNext: value })
        }}
      />

      {save.isError ? <ErrorState error={save.error} /> : null}
    </Section>
  )
}

// --- Sign-in methods ------------------------------------------------------------------------

/** One tile of the sign-in methods grid: what it is, whether it works, and what to do about it. */
function MethodTile({
  icon,
  title,
  available,
  state,
  children,
}: {
  icon?: ReactNode
  title: string
  available: boolean
  state: string
  children?: ReactNode
}) {
  const { t } = useTranslation()

  return (
    <div className={styles.method}>
      <div className={styles.methodHead}>
        {icon ? (
          <span className={styles.methodIcon} aria-hidden="true">
            {icon}
          </span>
        ) : null}
        <span className={styles.methodName}>{title}</span>
        <Badge tone={available ? 'success' : 'neutral'}>
          {t(available ? 'account.methodActive' : 'account.methodInactive')}
        </Badge>
      </div>
      <p className={styles.methodState}>{state}</p>
      {children}
    </div>
  )
}

/**
 * The three ways into this account, side by side.
 *
 * Google is the only one with controls here — passkeys have their own section below and the
 * email link is a household-wide setting nobody but an admin can move — but showing all three
 * is the point: connecting or disconnecting one is only safe knowledge in the context of the
 * other two.
 */
function SignInMethodsSection() {
  const { t } = useTranslation()
  const { config } = useAuth()
  const queryClient = useQueryClient()
  const { show } = useToast()
  const [disconnectOpen, setDisconnectOpen] = useState(false)
  const [disconnectError, setDisconnectError] = useState<unknown>(null)

  const methodsQuery = useQuery({
    queryKey: authKeys.methods(),
    queryFn: getSignInMethods,
  })

  // Linking is a full navigation to Google and back, so the return trip has to land here.
  const connect = useMutation({
    mutationFn: () => startGoogleLink('/account'),
  })

  const disconnect = useMutation({
    mutationFn: disconnectGoogle,
    onSuccess: async () => {
      setDisconnectOpen(false)
      setDisconnectError(null)
      show(t('account.googleDisconnected'), 'success')
      await queryClient.invalidateQueries({ queryKey: authKeys.methods() })
    },
    onError: (error) => {
      // `LAST_SIGN_IN_METHOD` is an answer about *this* control, so it belongs under it and
      // not in a toast that outlives the dialog it came from.
      setDisconnectOpen(false)
      setDisconnectError(error)
    },
  })

  const methods = methodsQuery.data ?? null
  const googleEnabled = config?.googleEnabled ?? false

  return (
    <Section id="account-signin" title={t('account.signInMethods')} hint={t('account.signInMethodsHint')}>
      {methodsQuery.isPending ? <Spinner label={t('common.loading')} /> : null}

      {methodsQuery.isError ? (
        <ErrorState
          error={methodsQuery.error}
          onRetry={() => {
            void methodsQuery.refetch()
          }}
        />
      ) : null}

      {methods ? (
        <div className={styles.methods}>
          <MethodTile
            title={t('account.passkeys')}
            available={methods.passkeys > 0}
            state={
              methods.passkeys > 0
                ? t('account.passkeyCount', { count: methods.passkeys })
                : t('account.noPasskeysShort')
            }
          >
            <p className={styles.methodHint}>{t('account.passkeysManagedBelow')}</p>
          </MethodTile>

          <MethodTile
            icon={<GoogleMark size={16} />}
            title={t('account.methodGoogle')}
            available={methods.googleLinked}
            state={
              methods.googleLinked
                ? methods.googleEmail
                  ? t('account.googleLinkedAs', { email: methods.googleEmail })
                  : t('account.googleLinkedNoEmail')
                : t('account.googleNotLinked')
            }
          >
            {methods.googleLinked ? (
              <>
                <div className={styles.methodActions}>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => {
                      disconnect.reset()
                      setDisconnectError(null)
                      setDisconnectOpen(true)
                    }}
                  >
                    {t('account.disconnectGoogle')}
                  </Button>
                </div>
                {disconnectError ? <ErrorState error={disconnectError} /> : null}
              </>
            ) : googleEnabled ? (
              <>
                <div className={styles.methodActions}>
                  <Button
                    size="sm"
                    loading={connect.isPending}
                    onClick={() => connect.mutate()}
                  >
                    {t('account.connectGoogle')}
                  </Button>
                </div>
                {connect.error ? <ErrorState error={connect.error} /> : null}
              </>
            ) : (
              <p className={styles.methodHint}>{t('account.googleUnavailable')}</p>
            )}
          </MethodTile>

          <MethodTile
            title={t('account.methodMagicLink')}
            available={methods.magicLinkEnabled}
            state={
              methods.magicLinkEnabled
                ? t('account.magicLinkAvailable')
                : t('account.magicLinkUnavailable')
            }
          />
        </div>
      ) : null}

      <Dialog
        open={disconnectOpen}
        onClose={() => {
          setDisconnectOpen(false)
        }}
        title={t('account.disconnectGoogle')}
        description={
          methods?.googleEmail
            ? t('account.disconnectGoogleConfirm', { email: methods.googleEmail })
            : t('account.disconnectGoogleConfirmPlain')
        }
        size="sm"
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                setDisconnectOpen(false)
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="danger"
              loading={disconnect.isPending}
              onClick={() => disconnect.mutate()}
            >
              {t('account.disconnectGoogle')}
            </Button>
          </>
        }
      >
        <p className={styles.hint}>{t('account.disconnectGoogleWarning')}</p>
      </Dialog>
    </Section>
  )
}

// --- Passkeys -------------------------------------------------------------------------------

function PasskeysSection() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { show } = useToast()
  const [addOpen, setAddOpen] = useState(false)
  const [nickname, setNickname] = useState('')
  const [renameTarget, setRenameTarget] = useState<Passkey | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<Passkey | null>(null)
  const [rowErrors, setRowErrors] = useState<Record<string, unknown>>({})

  const supported = isPasskeySupported()

  const passkeysQuery = useQuery({
    queryKey: accountKeys.passkeys(),
    queryFn: listPasskeys,
  })

  async function refresh(): Promise<void> {
    await queryClient.invalidateQueries({ queryKey: accountKeys.passkeys() })
  }

  const add = useMutation({
    mutationFn: (name: string) => startPasskeyRegistration(name),
    onSuccess: async () => {
      setAddOpen(false)
      setNickname('')
      show(t('account.passkeyAdded'), 'success')
      await refresh()
    },
    onError: (error) => {
      // Dismissing the system prompt is a normal thing to do, not a failure to report.
      if (isPasskeyCancellation(error)) {
        setAddOpen(false)
        show(t('auth.passkeyCancelled'), 'info')
      }
    },
  })

  const rename = useMutation({
    mutationFn: (input: { id: string; nickname: string }) =>
      renamePasskey(input.id, input.nickname),
    onSuccess: async () => {
      setRenameTarget(null)
      show(t('account.passkeyRenamed'), 'success')
      await refresh()
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => deletePasskey(id),
    onSuccess: async (_result, id) => {
      setDeleteTarget(null)
      setRowErrors((previous) => {
        const next = { ...previous }
        delete next[id]
        return next
      })
      show(t('account.passkeyRemoved'), 'success')
      await refresh()
    },
    onError: (error, id) => {
      // `LAST_CREDENTIAL` is about this specific row; it belongs next to it.
      setDeleteTarget(null)
      setRowErrors((previous) => ({ ...previous, [id]: error }))
    },
  })

  const passkeys = passkeysQuery.data ?? []

  return (
    <Section
      id="account-passkeys"
      title={t('account.passkeys')}
      hint={t('account.passkeysHint')}
      action={
        <Button
          size="sm"
          disabled={!supported}
          onClick={() => {
            setNickname('')
            add.reset()
            setAddOpen(true)
          }}
        >
          {t('account.addPasskey')}
        </Button>
      }
    >
      {!supported ? <p className={styles.notice}>{t('auth.passkeyUnsupported')}</p> : null}

      {passkeysQuery.isPending ? <Spinner label={t('common.loading')} /> : null}

      {passkeysQuery.isError ? (
        <ErrorState
          error={passkeysQuery.error}
          onRetry={() => {
            void passkeysQuery.refetch()
          }}
        />
      ) : null}

      {passkeysQuery.isSuccess && passkeys.length === 0 ? (
        <EmptyState title={t('account.noPasskeys')} />
      ) : null}

      {passkeys.length > 0 ? (
        <ul className={styles.list}>
          {passkeys.map((passkey) => (
            <PasskeyRow
              key={passkey.id}
              passkey={passkey}
              error={rowErrors[passkey.id]}
              onRename={() => {
                rename.reset()
                setRenameValue(passkey.nickname)
                setRenameTarget(passkey)
              }}
              onDelete={() => {
                remove.reset()
                setDeleteTarget(passkey)
              }}
            />
          ))}
        </ul>
      ) : null}

      <Dialog
        open={addOpen}
        onClose={() => {
          setAddOpen(false)
        }}
        title={t('account.addPasskey')}
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                setAddOpen(false)
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              type="submit"
              form="add-passkey-form"
              loading={add.isPending}
            >
              {t('account.addPasskey')}
            </Button>
          </>
        }
      >
        <form
          id="add-passkey-form"
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault()
            add.mutate(nickname)
          }}
        >
          <Field
            label={t('account.passkeyNickname')}
            htmlFor="passkey-nickname"
            hint={t('account.passkeyNicknameHint')}
          >
            <Input
              id="passkey-nickname"
              autoFocus
              value={nickname}
              maxLength={60}
              onChange={(event) => {
                setNickname(event.target.value)
              }}
            />
          </Field>
          {add.isError && !isPasskeyCancellation(add.error) ? (
            <ErrorState error={add.error} />
          ) : null}
        </form>
      </Dialog>

      <Dialog
        open={renameTarget !== null}
        onClose={() => {
          setRenameTarget(null)
        }}
        title={t('account.renamePasskey')}
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                setRenameTarget(null)
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button type="submit" form="rename-passkey-form" loading={rename.isPending}>
              {t('common.save')}
            </Button>
          </>
        }
      >
        <form
          id="rename-passkey-form"
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault()
            const target = renameTarget
            const value = renameValue.trim()
            if (!target || value === '') return
            rename.mutate({ id: target.id, nickname: value })
          }}
        >
          <Field label={t('account.passkeyNickname')} htmlFor="passkey-rename" required>
            <Input
              id="passkey-rename"
              autoFocus
              value={renameValue}
              maxLength={60}
              onChange={(event) => {
                setRenameValue(event.target.value)
              }}
            />
          </Field>
          {rename.isError ? <ErrorState error={rename.error} /> : null}
        </form>
      </Dialog>

      <Dialog
        open={deleteTarget !== null}
        onClose={() => {
          setDeleteTarget(null)
        }}
        title={t('account.deletePasskey')}
        description={
          deleteTarget
            ? t('account.deletePasskeyConfirm', { name: deleteTarget.nickname })
            : undefined
        }
        size="sm"
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                setDeleteTarget(null)
              }}
            >
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
        <p className={styles.hint}>{t('account.deletePasskeyWarning')}</p>
      </Dialog>
    </Section>
  )
}

function PasskeyRow({
  passkey,
  error,
  onRename,
  onDelete,
}: {
  passkey: Passkey
  error: unknown
  onRename: () => void
  onDelete: () => void
}) {
  const { t, i18n } = useTranslation()

  return (
    <li className={styles.row}>
      <div className={styles.rowMain}>
        <div className={styles.rowTitle}>
          <span className={styles.rowName}>{passkey.nickname}</span>
          {passkey.backedUp ? <Badge tone="info">{t('account.passkeyBackedUp')}</Badge> : null}
        </div>
        <p className={styles.meta}>
          {t('account.createdOn', { date: formatDate(passkey.createdAt, i18n.language) })}
          {' · '}
          {passkey.lastUsedAt
            ? t('account.lastUsed', { when: formatRelativeTime(passkey.lastUsedAt, t) })
            : t('account.neverUsed')}
        </p>
        {error ? <ErrorState error={error} /> : null}
      </div>
      {/* Glyphs on the row, words in the dialog: several passkeys are listed at once, so each
          control names the device it belongs to rather than repeating "Rename" four times. */}
      <div className={styles.rowActions}>
        <IconButton
          variant="ghost"
          label={t('account.renamePasskeyFor', { name: passkey.nickname })}
          onClick={onRename}
        >
          <EditIcon />
        </IconButton>
        <IconButton
          variant="danger"
          label={t('account.deletePasskeyFor', { name: passkey.nickname })}
          onClick={onDelete}
        >
          <TrashIcon />
        </IconButton>
      </div>
    </li>
  )
}

// --- Sessions -------------------------------------------------------------------------------

function SessionsSection() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { show } = useToast()
  const [revokeTarget, setRevokeTarget] = useState<AuthSessionInfo | null>(null)

  const sessionsQuery = useQuery({
    queryKey: accountKeys.sessions(),
    queryFn: listSessions,
  })

  const revoke = useMutation({
    mutationFn: (id: string) => revokeSession(id),
    onSuccess: async () => {
      setRevokeTarget(null)
      show(t('account.sessionRevoked'), 'success')
      await queryClient.invalidateQueries({ queryKey: accountKeys.sessions() })
    },
  })

  const sessions = sessionsQuery.data ?? []

  return (
    <Section id="account-sessions" title={t('account.sessions')} hint={t('account.sessionsHint')}>
      {sessionsQuery.isPending ? <Spinner label={t('common.loading')} /> : null}

      {sessionsQuery.isError ? (
        <ErrorState
          error={sessionsQuery.error}
          onRetry={() => {
            void sessionsQuery.refetch()
          }}
        />
      ) : null}

      {sessions.length > 0 ? (
        <ul className={styles.list}>
          {sessions.map((session) => (
            <SessionRow
              key={session.id}
              session={session}
              onRevoke={() => {
                revoke.reset()
                setRevokeTarget(session)
              }}
            />
          ))}
        </ul>
      ) : null}

      {revoke.isError ? <ErrorState error={revoke.error} /> : null}

      <Dialog
        open={revokeTarget !== null}
        onClose={() => {
          setRevokeTarget(null)
        }}
        title={t('account.revokeSession')}
        description={t('account.revokeSessionConfirm')}
        size="sm"
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                setRevokeTarget(null)
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="danger"
              loading={revoke.isPending}
              onClick={() => {
                if (revokeTarget) revoke.mutate(revokeTarget.id)
              }}
            >
              {t('account.revokeSession')}
            </Button>
          </>
        }
      >
        <p className={styles.hint}>
          {revokeTarget?.userAgent ?? t('account.unknownDevice')}
        </p>
      </Dialog>
    </Section>
  )
}

function SessionRow({
  session,
  onRevoke,
}: {
  session: AuthSessionInfo
  onRevoke: () => void
}) {
  const { t, i18n } = useTranslation()

  return (
    <li className={styles.row}>
      <div className={styles.rowMain}>
        <div className={styles.rowTitle}>
          <span className={styles.rowName}>{session.userAgent ?? t('account.unknownDevice')}</span>
          {session.current ? <Badge tone="accent">{t('account.sessionCurrent')}</Badge> : null}
        </div>
        <p className={styles.meta}>
          {t('account.sessionLastSeen', { when: formatRelativeTime(session.lastSeenAt, t) })}
          {session.ip ? ` · ${session.ip}` : ''}
          {' · '}
          {t('account.sessionExpires', { date: formatDate(session.expiresAt, i18n.language) })}
        </p>
      </div>
      <div className={styles.rowActions}>
        {/* Revoking the session you are sitting in is just signing out, which lives in the
            header. Offering it here as a destructive action only invites a mis-click. */}
        {session.current ? null : (
          <Button size="sm" variant="danger" onClick={onRevoke}>
            {t('account.revokeSession')}
          </Button>
        )}
      </div>
    </li>
  )
}

export default AccountPage
