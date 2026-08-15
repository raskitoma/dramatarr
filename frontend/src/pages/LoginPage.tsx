/** `/login` — the first screen anyone sees.
 *
 * Three ways in, ordered by how the household will actually use them: a passkey (one button,
 * no email needed, because the credential is discoverable), Google when the admin has enabled
 * it, and an emailed sign-in link last.
 *
 * Google is only ever a way *into an account that already exists*, or into an invitation
 * addressed to that same verified address — the app is invite-only and this screen never
 * creates anything. Everything that decides that lives on the server; all this page does is
 * hand the browser over to Google and, when the round trip fails, explain the code that came
 * back on `?error=`.
 *
 * The magic-link form always reports the same neutral outcome, whether or not the address has
 * an account — the login screen is unauthenticated, so anything else would turn it into an
 * account-enumeration oracle.
 *
 * The emailed link points at `/login?token=…`, so a token in the query means this page is
 * really the magic-link consumer; it hands over to MagicLinkPage in that case.
 */

import { useMutation } from '@tanstack/react-query'
import { useEffect, useId, useMemo, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useLocation, useSearch } from 'wouter'

import { startGoogleSignIn } from '@/api/auth'
import { api } from '@/api/client'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { Field } from '@/components/Field'
import { GoogleMark } from '@/components/GoogleMark'
import { Input } from '@/components/Input'
import { useAuth } from '@/hooks/useAuth'
import { isPasskeyCancellation, isPasskeySupported, startPasskeyLogin } from '@/lib/webauthn'

import styles from './LoginPage.module.css'
import { MagicLinkPage } from './MagicLinkPage'

function queryParam(search: string, key: string): string | null {
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
  return params.get(key)
}

/** `?next=` is attacker-controllable, so only same-origin absolute paths are honoured. */
function safeInternalPath(raw: string | null): string {
  if (!raw || !raw.startsWith('/')) return '/'
  if (raw.startsWith('//') || raw.startsWith('/\\')) return '/'
  return raw
}

export function LoginPage() {
  const { t, i18n } = useTranslation()
  const { user, config, refresh } = useAuth()
  const [, navigate] = useLocation()
  const search = useSearch()
  const emailId = useId()

  const [email, setEmail] = useState('')
  const [linkSent, setLinkSent] = useState(false)
  const [errorDismissed, setErrorDismissed] = useState(false)

  const magicToken = queryParam(search, 'token')
  const next = useMemo(() => safeInternalPath(queryParam(search, 'next')), [search])
  const passkeySupported = useMemo(() => isPasskeySupported(), [])

  // The Google callback cannot render anything itself — it is an API redirect — so it reports
  // its outcome as `/login?error=CODE` and this screen turns the code into a sentence.
  const errorCode = queryParam(search, 'error')

  const login = useMutation({
    mutationFn: startPasskeyLogin,
    onSuccess: async () => {
      await refresh()
      navigate(next, { replace: true })
    },
  })

  const google = useMutation({
    mutationFn: () => startGoogleSignIn({ next }),
  })

  const magicLink = useMutation({
    mutationFn: (address: string) => api.post<void>('/auth/magic-link/request', { email: address }),
    onSuccess: () => setLinkSent(true),
  })

  // Someone who is already signed in has no business on this screen — send them where they
  // were headed. This also covers the moment right after a successful ceremony.
  useEffect(() => {
    if (user) navigate(next, { replace: true })
  }, [user, next, navigate])

  // A fresh code deserves to be seen even if the previous one was dismissed by an attempt.
  useEffect(() => {
    setErrorDismissed(false)
  }, [errorCode])

  function onSubmitMagicLink(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    setLinkSent(false)
    setErrorDismissed(true)
    magicLink.mutate(email.trim())
  }

  if (magicToken) return <MagicLinkPage token={magicToken} next={next} />

  const siteName = config?.siteName || t('app.name')
  const magicLinkEnabled = config?.magicLinkEnabled ?? false
  const googleEnabled = config?.googleEnabled ?? false
  const loginCancelled = isPasskeyCancellation(login.error)

  // Same rule ErrorState follows: a code we have no sentence for degrades to the generic one
  // rather than leaking a raw server string into a Spanish UI.
  const errorKey = errorCode ? `errors.${errorCode}` : null
  const errorMessage =
    errorCode && !errorDismissed
      ? i18n.exists(errorKey ?? '')
        ? t(errorKey ?? '')
        : t('errors.generic')
      : null

  return (
    <main className={styles.page}>
      <div className={styles.card}>
        <header className={styles.header}>
          <h1 className={styles.title}>{siteName}</h1>
          <p className={styles.tagline}>{t('app.tagline')}</p>
        </header>

        {errorMessage ? (
          <p className={styles.alert} role="alert">
            {errorMessage}
          </p>
        ) : null}

        {passkeySupported ? (
          <section className={styles.section}>
            <Button
              variant="primary"
              full
              loading={login.isPending}
              onClick={() => {
                setErrorDismissed(true)
                login.mutate()
              }}
            >
              {t('auth.signInWithPasskey')}
            </Button>
            <p className={styles.hint}>{t('auth.signInHint')}</p>
          </section>
        ) : (
          <p className={styles.note}>{t('auth.passkeyUnsupported')}</p>
        )}

        <div className={styles.live} role="status" aria-live="polite">
          {loginCancelled ? <p className={styles.note}>{t('auth.passkeyCancelled')}</p> : null}
          {linkSent ? <p className={styles.note}>{t('auth.magicLinkSent')}</p> : null}
        </div>

        {login.error && !loginCancelled ? (
          <ErrorState error={login.error} onRetry={() => login.mutate()} />
        ) : null}

        {googleEnabled ? (
          <>
            {passkeySupported ? (
              <p className={styles.divider}>
                <span>{t('auth.orDivider')}</span>
              </p>
            ) : null}

            <section className={styles.section}>
              <Button
                full
                loading={google.isPending}
                onClick={() => {
                  setErrorDismissed(true)
                  google.mutate()
                }}
              >
                <GoogleMark />
                {t('auth.continueWithGoogle')}
              </Button>
            </section>

            {google.error ? <ErrorState error={google.error} /> : null}
          </>
        ) : null}

        {magicLinkEnabled ? (
          <>
            {passkeySupported || googleEnabled ? (
              <p className={styles.divider}>
                <span>{t('auth.orDivider')}</span>
              </p>
            ) : null}

            <form className={styles.section} onSubmit={onSubmitMagicLink}>
              <p className={styles.sectionTitle}>{t('auth.noPasskeyYet')}</p>
              <Field label={t('auth.emailLabel')} htmlFor={emailId} required>
                <Input
                  id={emailId}
                  type="email"
                  name="email"
                  autoComplete="email"
                  inputMode="email"
                  required
                  value={email}
                  onChange={(event) => setEmail(event.currentTarget.value)}
                />
              </Field>
              <Button type="submit" full loading={magicLink.isPending}>
                {t('auth.sendMagicLink')}
              </Button>
            </form>

            {magicLink.error ? <ErrorState error={magicLink.error} /> : null}
          </>
        ) : null}
      </div>
    </main>
  )
}
