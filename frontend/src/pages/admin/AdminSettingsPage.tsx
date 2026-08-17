/** Runtime-mutable settings (§3): the things an admin changes without touching env vars.
 *
 * Two rules shape this screen:
 *  - a secret read back as the `__SET__` sentinel is shown as "Configured", never echoed into a
 *    field, and only transmitted when the admin actually types a new one;
 *  - each section saves on its own, sending exactly the keys it changed, because a partial PUT
 *    is the contract and a whole-form save would resend values nobody touched.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { adminKeys, getSettings, testSmtp, updateSettings } from '@/api/admin'
import { SECRET_SENTINEL, type SystemSettings } from '@/api/types'
import Badge from '@/components/Badge'
import Button from '@/components/Button'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import Input from '@/components/Input'
import Select from '@/components/Select'
import Spinner from '@/components/Spinner'
import Switch from '@/components/Switch'
import { useAuth } from '@/hooks/useAuth'
import { LOCALE_LABELS, SUPPORTED_LOCALES } from '@/i18n'

import DramadexSection from '@/components/admin/DramadexSection'
import SettingsToc, { type TocSection } from '@/components/admin/SettingsToc'

import styles from './AdminSettingsPage.module.css'

/** The index, in the order the sections appear. Ids match the `id` on each section form. */
const SECTIONS: readonly TocSection[] = [
  { id: 'settings-site', labelKey: 'admin.settings.site' },
  { id: 'settings-library', labelKey: 'admin.settings.librarySection' },
  { id: 'settings-dramadex', labelKey: 'admin.settings.dramadexSection' },
  { id: 'settings-email', labelKey: 'admin.settings.email' },
  { id: 'settings-security', labelKey: 'admin.settings.security' },
  { id: 'settings-signin', labelKey: 'admin.settings.signIn' },
]

type SectionId =
  | 'site'
  | 'library'
  | 'dramadex'
  | 'email'
  | 'security'
  | 'signin'

interface FormState {
  siteName: string
  defaultLocale: string
  scanCron: string
  sourceBaseUrl: string
  dramadexUrl: string
  dramadexKey: string
  smtpHost: string
  smtpPort: string
  smtpUsername: string
  smtpPassword: string
  smtpFromEmail: string
  smtpFromName: string
  smtpUseTls: boolean
  smtpUseSsl: boolean
  magicLinkEnabled: boolean
  inviteTtlHours: string
  googleEnabled: boolean
  googleClientId: string
  googleClientSecret: string
  googleAllowInvites: boolean
}

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

// --- Cron ------------------------------------------------------------------------------------

const CRON_BOUNDS: ReadonlyArray<readonly [number, number]> = [
  [0, 59],
  [0, 23],
  [1, 31],
  [1, 12],
  [0, 7],
]

function isValidCronField(field: string, min: number, max: number): boolean {
  if (field === '*') return true
  for (const part of field.split(',')) {
    if (part === '') return false
    const slash = part.split('/')
    if (slash.length > 2) return false
    const base = slash[0] ?? ''
    if (slash.length === 2) {
      const step = Number(slash[1])
      if (!Number.isInteger(step) || step <= 0) return false
    }
    if (base === '*') continue
    const bounds = base.split('-')
    if (bounds.length > 2) return false
    const numbers: number[] = []
    for (const bound of bounds) {
      if (bound === '' || !/^\d+$/.test(bound)) return false
      const value = Number(bound)
      if (value < min || value > max) return false
      numbers.push(value)
    }
    const [first, second] = numbers
    if (numbers.length === 2 && first !== undefined && second !== undefined && first > second) {
      return false
    }
  }
  return true
}

/** Empty is valid — §3 says an empty `library.scan_cron` disables the scheduled scan. */
export function isValidCron(expression: string): boolean {
  const trimmed = expression.trim()
  if (trimmed === '') return true
  const parts = trimmed.split(/\s+/)
  if (parts.length !== 5) return false
  return parts.every((part, index) => {
    const bounds = CRON_BOUNDS[index]
    if (!bounds) return false
    return isValidCronField(part, bounds[0], bounds[1])
  })
}

function formatClock(hour: number, minute: number, locale: string): string {
  return new Intl.DateTimeFormat(locale, { hour: 'numeric', minute: '2-digit' }).format(
    new Date(2000, 0, 1, hour, minute),
  )
}

function weekdayName(day: number, locale: string): string {
  // 2024-01-07 was a Sunday, so day 0 (and cron's 7) land on the right name.
  const index = day === 7 ? 0 : day
  return new Intl.DateTimeFormat(locale, { weekday: 'long', timeZone: 'UTC' }).format(
    new Date(Date.UTC(2024, 0, 7 + index)),
  )
}

interface CronDescription {
  key: string
  params: Record<string, string | number>
}

/**
 * Plain language for the common shapes; anything exotic is honestly labelled "custom".
 * Returns a key plus interpolation values rather than a rendered string, so the caller owns
 * translation and this stays a pure function.
 */
export function describeCron(expression: string, locale: string): CronDescription {
  const none: Record<string, string | number> = {}
  const trimmed = expression.trim()
  if (trimmed === '') return { key: 'admin.settings.scanCronDisabled', params: none }
  if (!isValidCron(trimmed)) return { key: 'admin.settings.scanCronInvalid', params: none }

  const parts = trimmed.split(/\s+/)
  const minute = parts[0]
  const hour = parts[1]
  const dayOfMonth = parts[2]
  const month = parts[3]
  const dayOfWeek = parts[4]
  if (!minute || !hour || !dayOfMonth || !month || !dayOfWeek) {
    return { key: 'admin.settings.cronCustom', params: none }
  }

  const exactMinute = /^\d+$/.test(minute)
  const exactHour = /^\d+$/.test(hour)
  const everyDate = dayOfMonth === '*' && month === '*'
  const time = exactHour && exactMinute ? formatClock(Number(hour), Number(minute), locale) : ''

  if (everyDate && dayOfWeek === '*' && exactMinute && exactHour) {
    return { key: 'admin.settings.cronDaily', params: { time } }
  }
  if (everyDate && dayOfWeek === '*' && exactMinute && hour === '*') {
    return { key: 'admin.settings.cronHourly', params: { minute: Number(minute) } }
  }
  const step = /^\*\/(\d+)$/.exec(hour)
  const stepValue = step ? Number(step[1] ?? '') : Number.NaN
  if (everyDate && dayOfWeek === '*' && exactMinute && !Number.isNaN(stepValue)) {
    return { key: 'admin.settings.cronEveryHours', params: { count: stepValue } }
  }
  if (everyDate && /^[0-7]$/.test(dayOfWeek) && exactMinute && exactHour) {
    return {
      key: 'admin.settings.cronWeekly',
      params: { weekday: weekdayName(Number(dayOfWeek), locale), time },
    }
  }
  if (/^\d+$/.test(dayOfMonth) && month === '*' && dayOfWeek === '*' && exactMinute && exactHour) {
    return { key: 'admin.settings.cronMonthly', params: { day: Number(dayOfMonth), time } }
  }
  return { key: 'admin.settings.cronCustom', params: none }
}

// --- Page -------------------------------------------------------------------------------------

function readString(settings: Partial<SystemSettings>, key: keyof SystemSettings): string {
  const value = settings[key]
  return value === undefined || value === null ? '' : String(value)
}

function readBool(
  settings: Partial<SystemSettings>,
  key: keyof SystemSettings,
  fallback: boolean,
): boolean {
  const value = settings[key]
  return typeof value === 'boolean' ? value : fallback
}

function toForm(settings: Partial<SystemSettings>): FormState {
  return {
    siteName: readString(settings, 'site.name'),
    defaultLocale: readString(settings, 'site.default_locale') || 'es',
    scanCron: readString(settings, 'library.scan_cron'),
    sourceBaseUrl: readString(settings, 'library.source_base_url'),
    dramadexUrl: readString(settings, 'dramadex.base_url'),
    dramadexKey: '',
    smtpHost: readString(settings, 'smtp.host'),
    smtpPort: readString(settings, 'smtp.port'),
    smtpUsername: readString(settings, 'smtp.username'),
    smtpPassword: '',
    smtpFromEmail: readString(settings, 'smtp.from_email'),
    smtpFromName: readString(settings, 'smtp.from_name'),
    smtpUseTls: readBool(settings, 'smtp.use_tls', true),
    smtpUseSsl: readBool(settings, 'smtp.use_ssl', false),
    magicLinkEnabled: readBool(settings, 'auth.magic_link_enabled', true),
    inviteTtlHours: readString(settings, 'auth.invite_ttl_hours') || '168',
    googleEnabled: readBool(settings, 'oauth.google.enabled', false),
    googleClientId: readString(settings, 'oauth.google.client_id'),
    googleClientSecret: '',
    googleAllowInvites: readBool(settings, 'oauth.google.allow_invites', true),
  }
}

/**
 * The two strings Google Cloud Console demands, shown literally with a copy button.
 *
 * Typing them by hand is the single most common way a household's setup fails, and the error
 * Google returns for a mismatch names neither value — so they are rendered read-only, exactly
 * as they must be pasted, derived from the origin the admin is browsing right now.
 */
function CopyableValue({ id, value, label }: { id: string; value: string; label: string }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard access can be refused; the field is selectable, so there is still a way.
      setCopied(false)
    }
  }

  return (
    <Field label={label} htmlFor={id}>
      <div className={styles.secretRow}>
        <Input id={id} value={value} readOnly spellCheck={false} className={styles.uri} />
        <Button type="button" size="sm" onClick={() => void copy()}>
          {copied ? t('common.copied') : t('common.copy')}
        </Button>
      </div>
    </Field>
  )
}

export function AdminSettingsPage() {
  const { t, i18n } = useTranslation()
  const { user } = useAuth()
  const queryClient = useQueryClient()

  const settingsQuery = useQuery({
    queryKey: adminKeys.settings(),
    queryFn: getSettings,
  })

  const [form, setForm] = useState<FormState>(() => toForm({}))
  const [editingDramadexKey, setEditingDramadexKey] = useState(false)
  const [seeded, setSeeded] = useState(false)
  const [editingSmtpPassword, setEditingSmtpPassword] = useState(false)
  const [editingGoogleSecret, setEditingGoogleSecret] = useState(false)
  const [savedSection, setSavedSection] = useState<SectionId | null>(null)

  const settings: Partial<SystemSettings> = settingsQuery.data ?? {}

  if (settingsQuery.data && !seeded) {
    setSeeded(true)
    setForm(toForm(settingsQuery.data))
  }

  const setField = (partial: Partial<FormState>) => {
    setForm((previous) => ({ ...previous, ...partial }))
    setSavedSection(null)
  }

  const save = useMutation({
    mutationFn: (input: { section: SectionId; patch: Partial<SystemSettings> }) =>
      updateSettings(input.patch),
    onSuccess: (result, input) => {
      setForm(toForm(result))
      setEditingSmtpPassword(false)
      setEditingGoogleSecret(false)
      setSavedSection(input.section)
      queryClient.setQueryData(adminKeys.settings(), result)
    },
  })

  const smtpTest = useMutation({
    mutationFn: () => testSmtp(),
  })

  const dramadexConfigured = settings['dramadex.api_key'] === SECRET_SENTINEL
  const dramadexKeyEditable = !dramadexConfigured || editingDramadexKey
  const smtpPasswordConfigured = settings['smtp.password'] === SECRET_SENTINEL
  const googleSecretConfigured = settings['oauth.google.client_secret'] === SECRET_SENTINEL

  // A secret that has never been set is being edited by definition: its input is on screen
  // from the first render, and the "Edit" button that would flip the flag only exists once
  // there is a stored value to replace. Gating on the flag alone made a first-time key
  // impossible to save *or* test — it was silently dropped from the patch and the test probed
  // the empty stored value instead.
  const smtpPasswordEditable = !smtpPasswordConfigured || editingSmtpPassword
  const googleSecretEditable = !googleSecretConfigured || editingGoogleSecret

  // --- Validation ---------------------------------------------------------------------------
  const cronValid = isValidCron(form.scanCron)
  const cronError = cronValid ? undefined : t('admin.settings.scanCronInvalid')
  const cronDescription = describeCron(form.scanCron, i18n.language)

  const portNumber = form.smtpPort === '' ? null : Number(form.smtpPort)
  const portValid =
    portNumber === null ||
    (Number.isInteger(portNumber) && portNumber > 0 && portNumber <= 65535)
  const portError = portValid ? undefined : t('admin.settings.invalidNumber')

  const fromEmailValid = form.smtpFromEmail === '' || EMAIL_PATTERN.test(form.smtpFromEmail.trim())
  const fromEmailError = fromEmailValid ? undefined : t('admin.settings.invalidEmail')

  const ttlNumber = form.inviteTtlHours === '' ? null : Number(form.inviteTtlHours)
  const ttlValid = ttlNumber !== null && Number.isInteger(ttlNumber) && ttlNumber > 0
  const ttlError = ttlValid ? undefined : t('admin.settings.invalidNumber')

  // These two have to be exactly what the *server* will send Google, which it builds from
  // DRAMATARR_PUBLIC_URL — so it reports them rather than the browser guessing. The origin the
  // admin happens to be looking at is only the same thing on a plain localhost install: reach
  // the box by LAN address or through a reverse proxy and a browser-derived value would tell
  // them to register a URI the server never uses, which Google refuses as
  // `redirect_uri_mismatch` long after the settings screen said everything was fine.
  // The fallback only covers the first paint, before the settings query resolves.
  const googleOrigin = readString(settings, 'derived.google.js_origin') || window.location.origin
  const googleRedirectUri =
    readString(settings, 'derived.google.redirect_uri') ||
    `${googleOrigin}/api/auth/google/callback`

  // --- Patches ------------------------------------------------------------------------------
  const buildPatch = (section: SectionId): Partial<SystemSettings> => {
    const patch: Partial<SystemSettings> = {}

    if (section === 'site') {
      if (form.siteName.trim() !== readString(settings, 'site.name')) {
        patch['site.name'] = form.siteName.trim()
      }
      if (form.defaultLocale !== readString(settings, 'site.default_locale')) {
        patch['site.default_locale'] = form.defaultLocale
      }
    }

    if (section === 'dramadex') {
      if (form.dramadexUrl.trim() !== readString(settings, 'dramadex.base_url')) {
        patch['dramadex.base_url'] = form.dramadexUrl.trim()
      }
      // Only a freshly typed key travels; the sentinel is never echoed back as if it were one.
      if (dramadexKeyEditable && form.dramadexKey.trim() !== '') {
        patch['dramadex.api_key'] = form.dramadexKey.trim()
      }
    }

    if (section === 'library') {
      if (form.scanCron.trim() !== readString(settings, 'library.scan_cron')) {
        patch['library.scan_cron'] = form.scanCron.trim()
      }
      if (form.sourceBaseUrl.trim() !== readString(settings, 'library.source_base_url')) {
        patch['library.source_base_url'] = form.sourceBaseUrl.trim()
      }
    }

    if (section === 'email') {
      if (form.smtpHost.trim() !== readString(settings, 'smtp.host')) {
        patch['smtp.host'] = form.smtpHost.trim()
      }
      if (form.smtpPort !== readString(settings, 'smtp.port') && portNumber !== null) {
        patch['smtp.port'] = portNumber
      }
      if (form.smtpUsername.trim() !== readString(settings, 'smtp.username')) {
        patch['smtp.username'] = form.smtpUsername.trim()
      }
      if (smtpPasswordEditable && form.smtpPassword !== '') {
        patch['smtp.password'] = form.smtpPassword
      }
      if (form.smtpFromEmail.trim() !== readString(settings, 'smtp.from_email')) {
        patch['smtp.from_email'] = form.smtpFromEmail.trim()
      }
      if (form.smtpFromName.trim() !== readString(settings, 'smtp.from_name')) {
        patch['smtp.from_name'] = form.smtpFromName.trim()
      }
      if (form.smtpUseTls !== readBool(settings, 'smtp.use_tls', true)) {
        patch['smtp.use_tls'] = form.smtpUseTls
      }
      if (form.smtpUseSsl !== readBool(settings, 'smtp.use_ssl', false)) {
        patch['smtp.use_ssl'] = form.smtpUseSsl
      }
    }

    if (section === 'security') {
      if (form.magicLinkEnabled !== readBool(settings, 'auth.magic_link_enabled', true)) {
        patch['auth.magic_link_enabled'] = form.magicLinkEnabled
      }
      const storedTtl = readString(settings, 'auth.invite_ttl_hours')
      if (form.inviteTtlHours !== storedTtl && ttlNumber !== null) {
        patch['auth.invite_ttl_hours'] = ttlNumber
      }
    }

    if (section === 'signin') {
      if (form.googleEnabled !== readBool(settings, 'oauth.google.enabled', false)) {
        patch['oauth.google.enabled'] = form.googleEnabled
      }
      if (form.googleClientId.trim() !== readString(settings, 'oauth.google.client_id')) {
        patch['oauth.google.client_id'] = form.googleClientId.trim()
      }
      // Same rule as the TMDB key: a never-set secret is editable from the first render, so
      // gating on the "Edit" flag alone would silently drop a first-time client secret.
      if (googleSecretEditable && form.googleClientSecret.trim() !== '') {
        patch['oauth.google.client_secret'] = form.googleClientSecret.trim()
      }
      if (form.googleAllowInvites !== readBool(settings, 'oauth.google.allow_invites', true)) {
        patch['oauth.google.allow_invites'] = form.googleAllowInvites
      }
    }

    return patch
  }

  const submit = (section: SectionId, valid: boolean) => (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!valid) return
    const patch = buildPatch(section)
    if (Object.keys(patch).length === 0) return
    save.mutate({ section, patch })
  }

  if (settingsQuery.isPending) {
    return (
      <div className={styles.center}>
        <Spinner size="lg" label={t('common.loading')} />
      </div>
    )
  }

  if (settingsQuery.isError) {
    return <ErrorState error={settingsQuery.error} onRetry={() => void settingsQuery.refetch()} />
  }

  const emailDirty = Object.keys(buildPatch('email')).length > 0

  const sectionFooter = (section: SectionId, valid: boolean): ReactNode => {
    const changed = Object.keys(buildPatch(section)).length > 0
    return (
      <div className={styles.sectionFooter}>
        <p className={styles.savedNote} role="status">
          {savedSection === section ? t('common.saved') : ''}
        </p>
        <Button
          type="submit"
          variant="primary"
          loading={save.isPending && save.variables?.section === section}
          disabled={!changed || !valid}
        >
          {save.isPending && save.variables?.section === section
            ? t('common.saving')
            : t('common.save')}
        </Button>
      </div>
    )
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>{t('admin.settings.title')}</h1>
      </header>

      <div className={styles.layout}>
        <SettingsToc sections={SECTIONS} />

        <div className={styles.content}>
          {save.isError ? <ErrorState error={save.error} /> : null}

      {/* Site --------------------------------------------------------------------------- */}
      <form className={styles.card} id="settings-site" onSubmit={submit('site', true)}>
        <h2 className={styles.sectionTitle}>{t('admin.settings.site')}</h2>
        <div className={styles.grid}>
          <Field label={t('admin.settings.siteName')} htmlFor="site-name">
            <Input
              id="site-name"
              value={form.siteName}
              onChange={(event) => setField({ siteName: event.target.value })}
            />
          </Field>
          <Field label={t('admin.settings.defaultLocale')} htmlFor="site-locale">
            <Select
              id="site-locale"
              value={form.defaultLocale}
              onChange={(event) => setField({ defaultLocale: event.target.value })}
            >
              {SUPPORTED_LOCALES.map((locale) => (
                <option key={locale} value={locale}>
                  {LOCALE_LABELS[locale]}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        {sectionFooter('site', true)}
      </form>


      {/* Library ------------------------------------------------------------------------ */}
      <form className={styles.card} id="settings-library" onSubmit={submit('library', cronValid)}>
        <h2 className={styles.sectionTitle}>{t('admin.settings.librarySection')}</h2>
        <div className={styles.grid}>
          <div className={styles.spanAll}>
            <Field
              label={t('admin.settings.scanCron')}
              htmlFor="scan-cron"
              hint={t('admin.settings.scanCronHint')}
              error={cronError}
            >
              <Input
                id="scan-cron"
                value={form.scanCron}
                invalid={!cronValid}
                spellCheck={false}
                autoCapitalize="none"
                autoCorrect="off"
                onChange={(event) => setField({ scanCron: event.target.value })}
              />
            </Field>
            <p className={styles.cronDescription} aria-live="polite">
              {t(cronDescription.key, cronDescription.params)}
            </p>
          </div>
          <div className={styles.spanAll}>
            <Field
              label={t('admin.settings.sourceBaseUrl')}
              htmlFor="source-base-url"
              hint={t('admin.settings.sourceBaseUrlHint')}
            >
              <Input
                id="source-base-url"
                type="url"
                value={form.sourceBaseUrl}
                onChange={(event) => setField({ sourceBaseUrl: event.target.value })}
              />
            </Field>
          </div>
        </div>
        {sectionFooter('library', cronValid)}
      </form>

      {/* DramaDex ----------------------------------------------------------------------- */}
      {/* First, because it is now the primary source. Everything in the card below it —
          TMDB, OMDb, the web image search — predates the indexer and exists because nothing
          catalogued this library's genre. */}
      <form
        className={styles.card}
        id="settings-dramadex"
        onSubmit={submit('dramadex', true)}
      >
        <h2 className={styles.sectionTitle}>{t('admin.settings.dramadexSection')}</h2>
        <DramadexSection
          baseUrl={form.dramadexUrl}
          apiKey={form.dramadexKey}
          keyConfigured={dramadexConfigured}
          editingKey={editingDramadexKey}
          onEditKey={() => setEditingDramadexKey(true)}
          onChange={(patch: { baseUrl?: string; apiKey?: string }) =>
            setField({
              ...(patch.baseUrl === undefined ? {} : { dramadexUrl: patch.baseUrl }),
              ...(patch.apiKey === undefined ? {} : { dramadexKey: patch.apiKey }),
            })
          }
        />
        {sectionFooter('dramadex', true)}
      </form>

      {/* Email -------------------------------------------------------------------------- */}
      <form
        className={styles.card} id="settings-email"
        onSubmit={submit('email', portValid && fromEmailValid)}
      >
        <h2 className={styles.sectionTitle}>{t('admin.settings.email')}</h2>
        <p className={styles.sectionHint}>{t('admin.settings.emailHint')}</p>
        <div className={styles.grid}>
          <Field label={t('admin.settings.smtpHost')} htmlFor="smtp-host">
            <Input
              id="smtp-host"
              value={form.smtpHost}
              spellCheck={false}
              onChange={(event) => setField({ smtpHost: event.target.value })}
            />
          </Field>

          <Field label={t('admin.settings.smtpPort')} htmlFor="smtp-port" error={portError}>
            <Input
              id="smtp-port"
              inputMode="numeric"
              value={form.smtpPort}
              invalid={!portValid}
              onChange={(event) => setField({ smtpPort: event.target.value })}
            />
          </Field>

          <Field label={t('admin.settings.smtpUser')} htmlFor="smtp-user">
            <Input
              id="smtp-user"
              autoComplete="off"
              value={form.smtpUsername}
              onChange={(event) => setField({ smtpUsername: event.target.value })}
            />
          </Field>

          <Field
            label={t('admin.settings.smtpPassword')}
            htmlFor="smtp-password"
            hint={
              smtpPasswordConfigured && !editingSmtpPassword
                ? t('admin.settings.secretKeep')
                : undefined
            }
          >
            {smtpPasswordConfigured && !editingSmtpPassword ? (
              <div className={styles.secretRow}>
                <Badge tone="success">{t('admin.settings.secretSet')}</Badge>
                <Button
                  id="smtp-password"
                  type="button"
                  size="sm"
                  onClick={() => setEditingSmtpPassword(true)}
                >
                  {t('common.edit')}
                </Button>
              </div>
            ) : (
              <div className={styles.secretRow}>
                <Input
                  id="smtp-password"
                  type="password"
                  autoComplete="new-password"
                  value={form.smtpPassword}
                  onChange={(event) => setField({ smtpPassword: event.target.value })}
                />
                {smtpPasswordConfigured ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setEditingSmtpPassword(false)
                      setField({ smtpPassword: '' })
                    }}
                  >
                    {t('common.cancel')}
                  </Button>
                ) : (
                  <Badge tone="neutral">{t('admin.settings.secretUnset')}</Badge>
                )}
              </div>
            )}
          </Field>

          <Field
            label={t('admin.settings.smtpFrom')}
            htmlFor="smtp-from"
            error={fromEmailError}
          >
            <Input
              id="smtp-from"
              type="email"
              value={form.smtpFromEmail}
              invalid={!fromEmailValid}
              onChange={(event) => setField({ smtpFromEmail: event.target.value })}
            />
          </Field>

          <Field label={t('admin.settings.smtpFromName')} htmlFor="smtp-from-name">
            <Input
              id="smtp-from-name"
              value={form.smtpFromName}
              onChange={(event) => setField({ smtpFromName: event.target.value })}
            />
          </Field>

          <div className={styles.spanAll}>
            <Switch
              checked={form.smtpUseTls}
              onChange={(value) => setField({ smtpUseTls: value })}
              label={t('admin.settings.smtpTls')}
            />
          </div>
          <div className={styles.spanAll}>
            <Switch
              checked={form.smtpUseSsl}
              onChange={(value) => setField({ smtpUseSsl: value })}
              label={t('admin.settings.smtpSsl')}
            />
          </div>

          <div className={styles.testCell}>
            {/* The endpoint sends through the *stored* configuration, not what is on screen.
                Testing with unsaved edits therefore exercises the old settings and fails in a
                way that reads as "SMTP is broken", so the button waits for a save. */}
            <Button
              type="button"
              loading={smtpTest.isPending}
              disabled={emailDirty}
              onClick={() => smtpTest.mutate()}
            >
              {t('admin.settings.smtpTest')}
            </Button>
            <p className={styles.testResult} role="status">
              {emailDirty ? t('admin.settings.smtpTestNeedsSave') : ''}
              {!emailDirty && smtpTest.isSuccess
                ? t('admin.settings.smtpTestSent', {
                    email: smtpTest.data?.detail ?? user?.email ?? '',
                  })
                : ''}
              {!emailDirty && smtpTest.isError ? t('admin.settings.smtpTestFailed') : ''}
            </p>
          </div>
        </div>
        {sectionFooter('email', portValid && fromEmailValid)}
      </form>

      {/* Security ----------------------------------------------------------------------- */}
      <form className={styles.card} id="settings-security" onSubmit={submit('security', ttlValid)}>
        <h2 className={styles.sectionTitle}>{t('admin.settings.security')}</h2>
        <div className={styles.grid}>
          <div className={styles.spanAll}>
            <Switch
              checked={form.magicLinkEnabled}
              onChange={(value) => setField({ magicLinkEnabled: value })}
              label={t('admin.settings.magicLinkEnabled')}
              hint={t('admin.settings.magicLinkHint')}
            />
          </div>
          <Field label={t('admin.settings.inviteTtl')} htmlFor="invite-ttl" error={ttlError}>
            <Input
              id="invite-ttl"
              inputMode="numeric"
              value={form.inviteTtlHours}
              invalid={!ttlValid}
              onChange={(event) => setField({ inviteTtlHours: event.target.value })}
            />
          </Field>
        </div>
        {sectionFooter('security', ttlValid)}
      </form>

      {/* Sign-in ------------------------------------------------------------------------ */}
      <form className={styles.card} id="settings-signin" onSubmit={submit('signin', true)}>
        <h2 className={styles.sectionTitle}>{t('admin.settings.signIn')}</h2>
        <p className={styles.sectionHint}>{t('admin.settings.signInHint')}</p>
        <div className={styles.grid}>
          <div className={styles.spanAll}>
            <Switch
              checked={form.googleEnabled}
              onChange={(value) => setField({ googleEnabled: value })}
              label={t('admin.settings.googleEnabled')}
              hint={t('admin.settings.googleEnabledHint')}
            />
          </div>

          <div className={styles.spanAll}>
            <Field
              label={t('admin.settings.googleClientId')}
              htmlFor="google-client-id"
              hint={t('admin.settings.googleClientIdHint')}
            >
              <Input
                id="google-client-id"
                value={form.googleClientId}
                spellCheck={false}
                autoCapitalize="none"
                autoCorrect="off"
                autoComplete="off"
                onChange={(event) => setField({ googleClientId: event.target.value })}
              />
            </Field>
          </div>

          <div className={styles.spanAll}>
            <Field
              label={t('admin.settings.googleClientSecret')}
              htmlFor="google-client-secret"
              hint={
                googleSecretConfigured && !editingGoogleSecret
                  ? t('admin.settings.secretKeep')
                  : t('admin.settings.googleClientSecretHint')
              }
            >
              {googleSecretConfigured && !editingGoogleSecret ? (
                <div className={styles.secretRow}>
                  <Badge tone="success">{t('admin.settings.secretSet')}</Badge>
                  <Button
                    id="google-client-secret"
                    type="button"
                    size="sm"
                    onClick={() => setEditingGoogleSecret(true)}
                  >
                    {t('common.edit')}
                  </Button>
                </div>
              ) : (
                <div className={styles.secretRow}>
                  <Input
                    id="google-client-secret"
                    type="password"
                    autoComplete="new-password"
                    value={form.googleClientSecret}
                    onChange={(event) => setField({ googleClientSecret: event.target.value })}
                  />
                  {googleSecretConfigured ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setEditingGoogleSecret(false)
                        setField({ googleClientSecret: '' })
                      }}
                    >
                      {t('common.cancel')}
                    </Button>
                  ) : (
                    <Badge tone="neutral">{t('admin.settings.secretUnset')}</Badge>
                  )}
                </div>
              )}
            </Field>
          </div>

          <div className={styles.spanAll}>
            <Switch
              checked={form.googleAllowInvites}
              onChange={(value) => setField({ googleAllowInvites: value })}
              label={t('admin.settings.googleAllowInvites')}
              hint={t('admin.settings.googleAllowInvitesHint')}
            />
          </div>

          <div className={styles.spanAll}>
            <CopyableValue
              id="google-redirect-uri"
              label={t('admin.settings.googleRedirectUri')}
              value={googleRedirectUri}
            />
            <p className={styles.uriHint}>{t('admin.settings.googleRedirectUriHint')}</p>
          </div>

          <div className={styles.spanAll}>
            <CopyableValue
              id="google-js-origin"
              label={t('admin.settings.googleJsOrigin')}
              value={googleOrigin}
            />
            <p className={styles.uriHint}>{t('admin.settings.googleJsOriginHint')}</p>
          </div>

          <div className={styles.spanAll}>
            <p className={styles.cronDescription}>{t('admin.settings.googleTestingNote')}</p>
          </div>
        </div>
        {sectionFooter('signin', true)}
      </form>
        </div>
      </div>
    </div>
  )
}

export default AdminSettingsPage
