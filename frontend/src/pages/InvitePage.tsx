/** `/invite/:token` — enrollment, and the only way an account is ever created.
 *
 * The token is validated on load so a dead link says so immediately instead of after the
 * person has typed their name and poked at a system prompt. A valid one shows who was invited,
 * a name we guessed from the address, and one button that creates the passkey and the account
 * together — completing the ceremony signs them in, so there is nothing left to do afterwards.
 *
 * Most household members will never have made a passkey before, hence the one-line
 * explanation above the button.
 *
 * When Google Sign-In is on, accepting with Google is offered as an alternative: the token
 * travels to `/auth/google/start`, and the server accepts the invitation only if Google
 * returns that same address with `email_verified`. It is one click for someone who would
 * otherwise have to make their first passkey just to get in.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useId, useMemo, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useLocation, useParams } from 'wouter'

import { startGoogleSignIn } from '@/api/auth'
import { ApiRequestError, api } from '@/api/client'
import type { Role } from '@/api/types'
import { Button } from '@/components/Button'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { Field } from '@/components/Field'
import { GoogleMark } from '@/components/GoogleMark'
import { Input } from '@/components/Input'
import { Spinner } from '@/components/Spinner'
import { useAuth } from '@/hooks/useAuth'
import { isPasskeyCancellation, isPasskeySupported, startInviteRegistration } from '@/lib/webauthn'

import styles from './InvitePage.module.css'

/** `InviteInfoOut` — the public, pre-authentication view of an invite. */
interface InviteInfo {
  email: string
  role: Role
  siteName: string
  expiresAt: string
}

interface InvitePageProps {
  /** Supplied when the router passes params as props; otherwise read from the route. */
  token?: string
}

/** Mirrors the server's own guess so the prefilled name matches what it would have chosen. */
function displayNameFromEmail(email: string): string {
  const local = email.split('@')[0] ?? ''
  return local
    .replace(/[._-]+/g, ' ')
    .trim()
    .split(/\s+/)
    .filter((word) => word !== '')
    .map((word) => `${word.charAt(0).toUpperCase()}${word.slice(1)}`)
    .join(' ')
}

/** Every way an invite can be unusable maps to one of the three sentences we have. */
function invalidInviteKey(error: unknown): string {
  if (!(error instanceof ApiRequestError)) return 'auth.inviteInvalid'
  switch (error.code) {
    case 'INVITE_EXPIRED':
      return 'auth.inviteExpired'
    case 'INVITE_USED':
    case 'INVITE_ALREADY_USED':
      return 'auth.inviteUsed'
    default:
      return 'auth.inviteInvalid'
  }
}

export function InvitePage({ token: tokenProp }: InvitePageProps) {
  const { t, i18n } = useTranslation()
  const { config, refresh } = useAuth()
  const [, navigate] = useLocation()
  const params = useParams()
  const nameId = useId()

  const routeToken = params['token']
  const token = tokenProp ?? (typeof routeToken === 'string' ? routeToken : '')
  const passkeySupported = useMemo(() => isPasskeySupported(), [])
  // Null means "the field has not been touched", so the guess tracks the loaded invite.
  const [typedName, setTypedName] = useState<string | null>(null)

  const inviteQuery = useQuery({
    queryKey: ['invite', token],
    queryFn: () => api.get<InviteInfo>(`/auth/invite/${encodeURIComponent(token)}`),
    enabled: token !== '',
    retry: false,
    staleTime: 5 * 60_000,
  })

  const invite = inviteQuery.data ?? null
  const displayName = typedName ?? (invite ? displayNameFromEmail(invite.email) : '')

  const enroll = useMutation({
    mutationFn: (name: string) => startInviteRegistration(token, name, i18n.language),
    onSuccess: async () => {
      await refresh()
      navigate('/', { replace: true })
    },
  })

  const google = useMutation({
    mutationFn: () => startGoogleSignIn({ invite: token }),
  })

  function onSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    enroll.mutate(displayName)
  }

  const backToLogin = (
    <Button variant="primary" onClick={() => navigate('/login', { replace: true })}>
      {t('auth.signIn')}
    </Button>
  )

  if (token === '' || inviteQuery.isError) {
    return (
      <main className={styles.page}>
        <div className={styles.card}>
          <EmptyState
            title={t(token === '' ? 'auth.inviteInvalid' : invalidInviteKey(inviteQuery.error))}
            action={backToLogin}
          />
        </div>
      </main>
    )
  }

  if (inviteQuery.isPending || !invite) {
    return (
      <main className={styles.page}>
        <div className={styles.card}>
          <div className={styles.loading} role="status" aria-live="polite">
            <Spinner size="lg" label={t('common.loading')} />
          </div>
        </div>
      </main>
    )
  }

  const cancelled = isPasskeyCancellation(enroll.error)
  const googleEnabled = config?.googleEnabled ?? false

  return (
    <main className={styles.page}>
      <div className={styles.card}>
        <header className={styles.header}>
          <p className={styles.welcome}>{t('auth.welcome')}</p>
          <h1 className={styles.title}>{t('auth.inviteTitle', { siteName: invite.siteName })}</h1>
          <p className={styles.email}>{t('auth.inviteFor', { email: invite.email })}</p>
        </header>

        {passkeySupported ? (
          <form className={styles.form} onSubmit={onSubmit}>
            <Field label={t('auth.displayNameLabel')} htmlFor={nameId}>
              <Input
                id={nameId}
                name="displayName"
                autoComplete="name"
                maxLength={120}
                value={displayName}
                onChange={(event) => setTypedName(event.currentTarget.value)}
              />
            </Field>

            <p className={styles.intro}>{t('auth.inviteIntro')}</p>

            <Button type="submit" variant="primary" full loading={enroll.isPending}>
              {enroll.isPending ? t('auth.creatingPasskey') : t('auth.createPasskey')}
            </Button>
          </form>
        ) : (
          <p className={styles.note}>{t('auth.passkeyUnsupported')}</p>
        )}

        {googleEnabled ? (
          <>
            {passkeySupported ? (
              <p className={styles.divider}>
                <span>{t('auth.orDivider')}</span>
              </p>
            ) : null}

            <section className={styles.section}>
              <Button full loading={google.isPending} onClick={() => google.mutate()}>
                <GoogleMark />
                {t('auth.continueWithGoogle')}
              </Button>
              <p className={styles.intro}>{t('auth.inviteGoogleHint', { email: invite.email })}</p>
            </section>

            {google.error ? <ErrorState error={google.error} /> : null}
          </>
        ) : null}

        <div className={styles.live} role="status" aria-live="polite">
          {cancelled ? <p className={styles.note}>{t('auth.passkeyCancelled')}</p> : null}
        </div>

        {enroll.error && !cancelled ? (
          <ErrorState error={enroll.error} onRetry={() => enroll.mutate(displayName)} />
        ) : null}
      </div>
    </main>
  )
}
