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

import {
  IMAGE_SEARCH_PROVIDERS,
  SAFE_SEARCH_LEVELS,
  adminKeys,
  apiErrorKeys,
  formatEngineList,
  getSettings,
  imageEngines,
  imageSearchProviderKey,
  isJsonFormatDisabled,
  listSearchEngines,
  parseEngineList,
  testSearch,
  testSmtp,
  testTmdb,
  updateSettings,
} from '@/api/admin'
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

import ProvidersSection from '@/components/admin/ProvidersSection'
import SettingsToc, { type TocSection } from '@/components/admin/SettingsToc'

import styles from './AdminSettingsPage.module.css'

/** The index, in the order the sections appear. Ids match the `id` on each section form. */
const SECTIONS: readonly TocSection[] = [
  { id: 'settings-site', labelKey: 'admin.settings.site' },
  { id: 'settings-library', labelKey: 'admin.settings.librarySection' },
  { id: 'settings-providers', labelKey: 'admin.settings.providersSection' },
  { id: 'settings-metadata', labelKey: 'admin.settings.metadata' },
  { id: 'settings-email', labelKey: 'admin.settings.email' },
  { id: 'settings-security', labelKey: 'admin.settings.security' },
  { id: 'settings-signin', labelKey: 'admin.settings.signIn' },
]

type SectionId = 'site' | 'library' | 'metadata' | 'email' | 'security' | 'signin'

interface FormState {
  siteName: string
  defaultLocale: string
  scanCron: string
  sourceBaseUrl: string
  tmdbKey: string
  omdbKey: string
  tmdbLanguage: string
  imageSearchProvider: string
  imageSearchKey: string
  imageSearchCseId: string
  searxngBaseUrl: string
  searxngSafeSearch: string
  searxngLanguage: string
  searxngTimeout: string
  /** The chosen engine names, canonical. Empty means the instance's own defaults. */
  searchEngines: string[]
  /** The same list as text, for the field shown when the instance could not be asked. */
  searchEnginesText: string
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

/** Neither too permissive to be worth checking nor stricter than what the server accepts. */
const MAX_SEARCH_TIMEOUT_SECONDS = 120

/**
 * What a SearXNG instance needs adding to its `settings.yml`, shown verbatim.
 *
 * Not translated and not paraphrased: it is YAML the admin pastes into a file, and the failure
 * mode of describing it in prose is an admin who indents it wrong.
 */
const SEARXNG_JSON_SNIPPET = `search:
  formats:
    - html
    - json`

/** The i18n key for a safe-search level. A level this build has no name for shows its own. */
function safeSearchLabelKey(level: string): string {
  if (level === 'off') return 'admin.settings.searxngSafeOff'
  if (level === 'moderate') return 'admin.settings.searxngSafeModerate'
  if (level === 'strict') return 'admin.settings.searxngSafeStrict'
  return level
}

/** `http(s)` only, which is all the server will talk to either. */
function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value.trim())
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

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

/** Moderate rather than off: this searches the open web for a picture of a television show. */
const DEFAULT_SAFE_SEARCH = 'moderate'

/**
 * The stored safe-search level, by name.
 *
 * SearXNG itself speaks 0/1/2 and a settings row edited by hand may well say `1`, so both are
 * accepted; anything else falls back to the default rather than silently searching wider than
 * the admin asked for. Reading and defaulting live in one place so `toForm` and the patch
 * comparison can never disagree about what "unset" means and mark the section dirty on load.
 */
function readSafeSearch(settings: Partial<SystemSettings>): string {
  const raw: unknown = settings['images.search.safe_search']
  if (typeof raw === 'number') return SAFE_SEARCH_LEVELS[raw] ?? DEFAULT_SAFE_SEARCH
  const text = String(raw ?? '')
    .trim()
    .toLowerCase()
  if (text === '') return DEFAULT_SAFE_SEARCH
  if (SAFE_SEARCH_LEVELS.includes(text)) return text
  const level = Number(text)
  if (Number.isInteger(level)) return SAFE_SEARCH_LEVELS[level] ?? DEFAULT_SAFE_SEARCH
  return DEFAULT_SAFE_SEARCH
}

/** The chosen engines, from whichever shape the server stores them in. */
function readEngines(settings: Partial<SystemSettings>): string[] {
  return parseEngineList(settings['images.search.engines'])
}

function toForm(settings: Partial<SystemSettings>): FormState {
  const engines = readEngines(settings)
  return {
    siteName: readString(settings, 'site.name'),
    defaultLocale: readString(settings, 'site.default_locale') || 'es',
    scanCron: readString(settings, 'library.scan_cron'),
    sourceBaseUrl: readString(settings, 'library.source_base_url'),
    tmdbKey: '',
    omdbKey: '',
    tmdbLanguage: readString(settings, 'tmdb.language'),
    imageSearchProvider: readString(settings, 'images.search.provider'),
    imageSearchKey: '',
    imageSearchCseId: readString(settings, 'images.search.cse_id'),
    searxngBaseUrl: readString(settings, 'images.search.base_url'),
    searxngSafeSearch: readSafeSearch(settings),
    searxngLanguage: readString(settings, 'images.search.language'),
    searxngTimeout: readString(settings, 'images.search.timeout'),
    searchEngines: engines,
    searchEnginesText: formatEngineList(engines),
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
  const [seeded, setSeeded] = useState(false)
  const [editingTmdbKey, setEditingTmdbKey] = useState(false)
  const [editingOmdbKey, setEditingOmdbKey] = useState(false)
  const [editingImageSearchKey, setEditingImageSearchKey] = useState(false)
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
      setEditingTmdbKey(false)
      setEditingImageSearchKey(false)
      setEditingSmtpPassword(false)
      setEditingGoogleSecret(false)
      setSavedSection(input.section)
      queryClient.setQueryData(adminKeys.settings(), result)
    },
  })

  const tmdbTest = useMutation({
    mutationFn: () => testTmdb(tmdbKeyEditable && form.tmdbKey ? form.tmdbKey : undefined),
  })

  const smtpTest = useMutation({
    mutationFn: () => testSmtp(),
  })

  const searchTest = useMutation({
    mutationFn: () => testSearch(),
  })

  const tmdbConfigured = settings['tmdb.api_key'] === SECRET_SENTINEL
  const omdbConfigured = settings['omdb.api_key'] === SECRET_SENTINEL
  const imageSearchKeyConfigured = settings['images.search.api_key'] === SECRET_SENTINEL
  const smtpPasswordConfigured = settings['smtp.password'] === SECRET_SENTINEL
  const googleSecretConfigured = settings['oauth.google.client_secret'] === SECRET_SENTINEL

  // A secret that has never been set is being edited by definition: its input is on screen
  // from the first render, and the "Edit" button that would flip the flag only exists once
  // there is a stored value to replace. Gating on the flag alone made a first-time key
  // impossible to save *or* test — it was silently dropped from the patch and the test probed
  // the empty stored value instead.
  const tmdbKeyEditable = !tmdbConfigured || editingTmdbKey
  const omdbKeyEditable = !omdbConfigured || editingOmdbKey
  const imageSearchKeyEditable = !imageSearchKeyConfigured || editingImageSearchKey
  const smtpPasswordEditable = !smtpPasswordConfigured || editingSmtpPassword
  const googleSecretEditable = !googleSecretConfigured || editingGoogleSecret

  // A stored provider this build does not know still gets an option of its own, so saving an
  // unrelated field cannot quietly turn the image search off.
  const providerOptions = IMAGE_SEARCH_PROVIDERS.includes(form.imageSearchProvider)
    ? IMAGE_SEARCH_PROVIDERS
    : [...IMAGE_SEARCH_PROVIDERS, form.imageSearchProvider]

  // Which provider is selected decides which fields exist at all: a Google engine id sitting
  // under a SearXNG URL is not a harmless extra field, it is a question the admin has to work
  // out the answer to ("do I need this one?") every time they open the screen.
  const searxngSelected = form.imageSearchProvider === 'searxng'
  const googleSelected = form.imageSearchProvider === 'google_cse'

  /**
   * The instance's engine list, asked for only once it has answered a test.
   *
   * Discovery is a convenience, never a gate: the free-text field below covers a failed call,
   * an instance that does not expose the route, and the admin who already knows the names.
   */
  const enginesQuery = useQuery({
    queryKey: adminKeys.searchEngines(),
    queryFn: listSearchEngines,
    enabled: searxngSelected && searchTest.isSuccess,
    staleTime: 5 * 60_000,
    retry: false,
  })

  const discoveredEngines = enginesQuery.data ? imageEngines(enginesQuery.data) : []
  // A configured engine the instance did not list keeps its checkbox — an engine disabled for
  // an afternoon must not disappear from the configuration on the next save.
  const engineChoices: string[] = enginesQuery.isSuccess
    ? [
        ...discoveredEngines.map((engine) => engine.name),
        ...form.searchEngines.filter(
          (name) => !discoveredEngines.some((engine) => engine.name === name),
        ),
      ]
    : []

  const toggleEngine = (name: string) => {
    const next = form.searchEngines.includes(name)
      ? form.searchEngines.filter((entry) => entry !== name)
      : [...form.searchEngines, name]
    setField({ searchEngines: next, searchEnginesText: formatEngineList(next) })
  }

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

  // An empty SearXNG URL is not an error: it is the half-configured state a provider sits in
  // between choosing it and pasting the address, and the server treats that as "off" exactly
  // like a Google setup missing its engine id. A *malformed* one is worth stopping.
  const searxngUrl = form.searxngBaseUrl.trim()
  const searxngUrlValid = !searxngSelected || searxngUrl === '' || isHttpUrl(searxngUrl)
  const searxngUrlError = searxngUrlValid ? undefined : t('admin.settings.searxngUrlInvalid')

  const timeoutRaw = form.searxngTimeout.trim()
  const timeoutNumber = timeoutRaw === '' ? null : Number(timeoutRaw)
  const timeoutValid =
    !searxngSelected ||
    timeoutNumber === null ||
    (Number.isFinite(timeoutNumber) &&
      timeoutNumber > 0 &&
      timeoutNumber <= MAX_SEARCH_TIMEOUT_SECONDS)
  const timeoutError = timeoutValid ? undefined : t('admin.settings.invalidNumber')

  const metadataValid = searxngUrlValid && timeoutValid

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

    if (section === 'library') {
      if (form.scanCron.trim() !== readString(settings, 'library.scan_cron')) {
        patch['library.scan_cron'] = form.scanCron.trim()
      }
      if (form.sourceBaseUrl.trim() !== readString(settings, 'library.source_base_url')) {
        patch['library.source_base_url'] = form.sourceBaseUrl.trim()
      }
    }

    if (section === 'metadata') {
      // Only a freshly typed key travels; the sentinel is never echoed back as if it were one.
      if (omdbKeyEditable && form.omdbKey.trim() !== '') {
        patch['omdb.api_key'] = form.omdbKey.trim()
      }
      if (tmdbKeyEditable && form.tmdbKey.trim() !== '') {
        patch['tmdb.api_key'] = form.tmdbKey.trim()
      }
      if (form.tmdbLanguage.trim() !== readString(settings, 'tmdb.language')) {
        patch['tmdb.language'] = form.tmdbLanguage.trim()
      }
      if (form.imageSearchProvider !== readString(settings, 'images.search.provider')) {
        patch['images.search.provider'] = form.imageSearchProvider
      }
      // Exactly the TMDB key's rule, and for exactly the same reason: a secret that has never
      // been set is being edited from the first render, so gating on the "Edit" flag alone
      // would make a first-time key typeable but unsavable. It is one slot for both providers
      // — Google's API key, or the token a locked-down SearXNG asks for.
      if (imageSearchKeyEditable && form.imageSearchKey.trim() !== '') {
        patch['images.search.api_key'] = form.imageSearchKey.trim()
      }
      // Each provider sends only its own fields. The other's stay stored rather than being
      // cleared, so switching back does not mean typing everything in again — and an edit made
      // before switching providers cannot ride along on a save it no longer belongs to.
      const storedCseId = readString(settings, 'images.search.cse_id')
      if (googleSelected && form.imageSearchCseId.trim() !== storedCseId) {
        patch['images.search.cse_id'] = form.imageSearchCseId.trim()
      }
      if (searxngSelected) {
        if (searxngUrl !== readString(settings, 'images.search.base_url')) {
          patch['images.search.base_url'] = searxngUrl
        }
        if (form.searxngSafeSearch !== readSafeSearch(settings)) {
          patch['images.search.safe_search'] = form.searxngSafeSearch
        }
        if (form.searxngLanguage.trim() !== readString(settings, 'images.search.language')) {
          patch['images.search.language'] = form.searxngLanguage.trim()
        }
        if (
          timeoutRaw !== readString(settings, 'images.search.timeout') &&
          timeoutNumber !== null &&
          timeoutValid
        ) {
          patch['images.search.timeout'] = timeoutNumber
        }
        const storedEngines = readEngines(settings)
        if (formatEngineList(form.searchEngines) !== formatEngineList(storedEngines)) {
          // Written back in the shape the server sent, so neither a list-valued setting nor a
          // comma-separated one changes type behind the server's back.
          const stored: unknown = settings['images.search.engines']
          patch['images.search.engines'] = Array.isArray(stored)
            ? [...form.searchEngines]
            : formatEngineList(form.searchEngines)
        }
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
  // The search test, like the SMTP one, exercises what is *stored*. An unsaved URL would be
  // tested against the old configuration and fail in a way that reads as "SearXNG is broken".
  const searchDirty = Object.keys(buildPatch('metadata')).some((key) =>
    key.startsWith('images.search.'),
  )

  const searchJsonDisabled = isJsonFormatDisabled(searchTest.error)

  // What a successful probe is worth saying: how many engines can actually return an image —
  // an instance answering happily with none of them is a working URL and a dead search — and
  // the version, when it reports one, since that is what a bug report needs.
  const searchResult = searchTest.data ?? null
  const searchTestSummary =
    searchResult === null
      ? ''
      : [
          searchResult.imageEngineCount === null
            ? t('admin.settings.searchTestOkNoCount')
            : t('admin.settings.searchTestOk', { count: searchResult.imageEngineCount }),
          searchResult.version
            ? t('admin.settings.searchTestVersion', { version: searchResult.version })
            : '',
        ]
          .filter((part) => part !== '')
          .join(' ')

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

      <ProvidersSection />

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

      {/* Metadata ----------------------------------------------------------------------- */}
      <form
        className={styles.card}
        id="settings-metadata"
        onSubmit={submit('metadata', metadataValid)}
      >
        <h2 className={styles.sectionTitle}>{t('admin.settings.metadata')}</h2>
        <div className={styles.grid}>
          {/* OMDb — the fallback for an IMDb id TMDB does not carry, which for vertical short
              drama is most of them. Optional: with no key the "use this metadata" button
              simply falls back to what it did before. */}
          <div className={styles.spanAll}>
            <Field
              label={t('admin.settings.omdbKey')}
              htmlFor="omdb-key"
              hint={
                omdbConfigured && !editingOmdbKey
                  ? t('admin.settings.secretKeep')
                  : t('admin.settings.omdbKeyHint')
              }
            >
              {omdbConfigured && !editingOmdbKey ? (
                <div className={styles.secretRow}>
                  <Badge tone="success">{t('admin.settings.secretSet')}</Badge>
                  <Button
                    id="omdb-key"
                    type="button"
                    size="sm"
                    onClick={() => setEditingOmdbKey(true)}
                  >
                    {t('common.edit')}
                  </Button>
                </div>
              ) : (
                <div className={styles.secretRow}>
                  <Input
                    id="omdb-key"
                    type="password"
                    autoComplete="off"
                    value={form.omdbKey}
                    onChange={(event) => setField({ omdbKey: event.target.value })}
                  />
                </div>
              )}
            </Field>
          </div>

          <div className={styles.spanAll}>
            <Field
              label={t('admin.settings.tmdbKey')}
              htmlFor="tmdb-key"
              hint={
                tmdbConfigured && !editingTmdbKey
                  ? t('admin.settings.secretKeep')
                  : t('admin.settings.tmdbKeyHint')
              }
            >
              {tmdbConfigured && !editingTmdbKey ? (
                <div className={styles.secretRow}>
                  <Badge tone="success">{t('admin.settings.secretSet')}</Badge>
                  <Button
                    id="tmdb-key"
                    type="button"
                    size="sm"
                    onClick={() => setEditingTmdbKey(true)}
                  >
                    {t('common.edit')}
                  </Button>
                </div>
              ) : (
                <div className={styles.secretRow}>
                  <Input
                    id="tmdb-key"
                    type="password"
                    autoComplete="off"
                    value={form.tmdbKey}
                    onChange={(event) => setField({ tmdbKey: event.target.value })}
                  />
                  {tmdbConfigured ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setEditingTmdbKey(false)
                        setField({ tmdbKey: '' })
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

          <Field label={t('admin.settings.tmdbLanguage')} htmlFor="tmdb-language">
            <Input
              id="tmdb-language"
              value={form.tmdbLanguage}
              spellCheck={false}
              onChange={(event) => setField({ tmdbLanguage: event.target.value })}
            />
          </Field>

          <div className={styles.testCell}>
            <Button
              type="button"
              loading={tmdbTest.isPending}
              onClick={() => tmdbTest.mutate()}
            >
              {t('admin.settings.tmdbTest')}
            </Button>
            <p className={styles.testResult} role="status">
              {tmdbTest.isSuccess ? t('admin.settings.tmdbValid') : ''}
              {tmdbTest.isError ? t('admin.settings.tmdbInvalid') : ''}
            </p>
          </div>

          {/* Image search — optional, and only the artwork picker uses it. */}
          <div className={styles.spanAll}>
            <h3 className={styles.subsectionTitle}>{t('admin.settings.imageSearch')}</h3>
            <p className={styles.subsectionHint}>{t('admin.settings.imageSearchHint')}</p>
          </div>

          <Field
            label={t('admin.settings.imageSearchProvider')}
            htmlFor="image-search-provider"
            hint={searxngSelected ? t('admin.settings.imageSearchSearxngHint') : undefined}
          >
            <Select
              id="image-search-provider"
              value={form.imageSearchProvider}
              onChange={(event) => {
                // A success belongs to the provider that produced it; carrying it across would
                // show a green SearXNG result next to a Google configuration nobody tested.
                searchTest.reset()
                setField({ imageSearchProvider: event.target.value })
              }}
            >
              {providerOptions.map((provider) => (
                <option key={provider || 'off'} value={provider}>
                  {imageSearchProviderKey(provider) === ''
                    ? provider
                    : t(imageSearchProviderKey(provider))}
                </option>
              ))}
            </Select>
          </Field>

          {/* --- SearXNG ------------------------------------------------------------------ */}
          {searxngSelected ? (
            <>
              <div className={styles.spanAll}>
                <Field
                  label={t('admin.settings.searxngUrl')}
                  htmlFor="searxng-base-url"
                  hint={t('admin.settings.searxngUrlHint')}
                  error={searxngUrlError}
                >
                  <Input
                    id="searxng-base-url"
                    // Not `type="url"`: native constraint validation would answer in the
                    // browser's own words, so the field would have two error languages.
                    inputMode="url"
                    value={form.searxngBaseUrl}
                    invalid={!searxngUrlValid}
                    spellCheck={false}
                    autoCapitalize="none"
                    autoCorrect="off"
                    autoComplete="off"
                    onChange={(event) => setField({ searxngBaseUrl: event.target.value })}
                  />
                </Field>
              </div>

              <Field
                label={t('admin.settings.searxngSafeSearch')}
                htmlFor="searxng-safe-search"
                hint={t('admin.settings.searxngSafeSearchHint')}
              >
                <Select
                  id="searxng-safe-search"
                  value={form.searxngSafeSearch}
                  onChange={(event) => setField({ searxngSafeSearch: event.target.value })}
                >
                  {SAFE_SEARCH_LEVELS.map((level) => (
                    <option key={level} value={level}>
                      {t(safeSearchLabelKey(level))}
                    </option>
                  ))}
                </Select>
              </Field>

              <Field
                label={t('admin.settings.searxngLanguage')}
                htmlFor="searxng-language"
                hint={t('admin.settings.searxngLanguageHint')}
              >
                <Input
                  id="searxng-language"
                  value={form.searxngLanguage}
                  spellCheck={false}
                  autoCapitalize="none"
                  autoCorrect="off"
                  autoComplete="off"
                  onChange={(event) => setField({ searxngLanguage: event.target.value })}
                />
              </Field>

              <Field
                label={t('admin.settings.searxngTimeout')}
                htmlFor="searxng-timeout"
                hint={t('admin.settings.searxngTimeoutHint')}
                error={timeoutError}
              >
                <Input
                  id="searxng-timeout"
                  inputMode="numeric"
                  value={form.searxngTimeout}
                  invalid={!timeoutValid}
                  onChange={(event) => setField({ searxngTimeout: event.target.value })}
                />
              </Field>
            </>
          ) : null}

          {/* --- Google Custom Search ----------------------------------------------------- */}
          {googleSelected ? (
            <Field
              label={t('admin.settings.imageSearchCseId')}
              htmlFor="image-search-cse"
              hint={t('admin.settings.imageSearchCseIdHint')}
            >
              <Input
                id="image-search-cse"
                value={form.imageSearchCseId}
                spellCheck={false}
                autoCapitalize="none"
                autoCorrect="off"
                autoComplete="off"
                onChange={(event) => setField({ imageSearchCseId: event.target.value })}
              />
            </Field>
          ) : null}

          {/* One secret slot, serving whichever provider is selected. Google demands its API
              key; a SearXNG instance usually asks for nothing, so it is offered as optional. */}
          {form.imageSearchProvider === '' ? null : (
            <div className={styles.spanAll}>
              <Field
                label={t(
                  searxngSelected
                    ? 'admin.settings.searxngToken'
                    : 'admin.settings.imageSearchKey',
                )}
                htmlFor="image-search-key"
                hint={
                  imageSearchKeyConfigured && !editingImageSearchKey
                    ? t('admin.settings.secretKeep')
                    : t(
                        searxngSelected
                          ? 'admin.settings.searxngTokenHint'
                          : 'admin.settings.imageSearchKeyHint',
                      )
                }
              >
                {imageSearchKeyConfigured && !editingImageSearchKey ? (
                  <div className={styles.secretRow}>
                    <Badge tone="success">{t('admin.settings.secretSet')}</Badge>
                    <Button
                      id="image-search-key"
                      type="button"
                      size="sm"
                      onClick={() => setEditingImageSearchKey(true)}
                    >
                      {t('common.edit')}
                    </Button>
                  </div>
                ) : (
                  <div className={styles.secretRow}>
                    <Input
                      id="image-search-key"
                      type="password"
                      autoComplete="off"
                      value={form.imageSearchKey}
                      onChange={(event) => setField({ imageSearchKey: event.target.value })}
                    />
                    {imageSearchKeyConfigured ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setEditingImageSearchKey(false)
                          setField({ imageSearchKey: '' })
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
          )}

          {/* --- Test connection ---------------------------------------------------------- */}
          {form.imageSearchProvider === '' ? null : (
            <div className={styles.spanAll}>
              <div className={styles.testRow}>
                <Button
                  id="search-test"
                  type="button"
                  loading={searchTest.isPending}
                  disabled={searchDirty || !metadataValid}
                  onClick={() => searchTest.mutate()}
                >
                  {t('admin.settings.searchTest')}
                </Button>
                <p className={styles.testResult} role="status">
                  {searchDirty ? t('admin.settings.searchTestNeedsSave') : ''}
                  {!searchDirty && searchTest.isSuccess ? searchTestSummary : ''}
                  {!searchDirty && searchTest.isError && !searchJsonDisabled
                    ? t(apiErrorKeys(searchTest.error))
                    : ''}
                </p>
              </div>

              {/* The single most likely thing to be wrong on a fresh SearXNG: it answers in
                  HTML only until `json` is listed in `search.formats`. Nothing about that is
                  discoverable from "the search failed", so the fix is on screen instead. */}
              {searchJsonDisabled ? (
                <div className={styles.fixNote} role="alert">
                  <p className={styles.fixTitle}>{t('admin.settings.searchJsonDisabled')}</p>
                  <p>{t('admin.settings.searchJsonDisabledFix')}</p>
                  <pre className={styles.fixCode}>{SEARXNG_JSON_SNIPPET}</pre>
                  <p>{t('admin.settings.searchJsonDisabledRestart')}</p>
                </div>
              ) : null}
            </div>
          )}

          {/* --- Engines ------------------------------------------------------------------ */}
          {searxngSelected ? (
            <div className={styles.spanAll}>
              {engineChoices.length > 0 ? (
                <fieldset className={styles.engines}>
                  <legend className={styles.engineLegend}>
                    {t('admin.settings.searchEngines')}
                  </legend>
                  <p className={styles.subsectionHint}>{t('admin.settings.searchEnginesHint')}</p>
                  <div className={styles.engineGrid}>
                    {engineChoices.map((name) => (
                      <label key={name} className={styles.engineOption}>
                        <input
                          type="checkbox"
                          checked={form.searchEngines.includes(name)}
                          onChange={() => toggleEngine(name)}
                        />
                        {/* The instance's own name for the engine, not ours to translate. */}
                        <span>{name}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
              ) : (
                <Field
                  label={t('admin.settings.searchEngines')}
                  htmlFor="search-engines"
                  hint={
                    enginesQuery.isError
                      ? t('admin.settings.searchEnginesFallback')
                      : t('admin.settings.searchEnginesManual')
                  }
                >
                  <Input
                    id="search-engines"
                    value={form.searchEnginesText}
                    spellCheck={false}
                    autoCapitalize="none"
                    autoCorrect="off"
                    autoComplete="off"
                    onChange={(event) =>
                      setField({
                        searchEnginesText: event.target.value,
                        searchEngines: parseEngineList(event.target.value),
                      })
                    }
                  />
                </Field>
              )}
              {form.searchEngines.length === 0 ? (
                <p className={styles.engineNote}>{t('admin.settings.searchEnginesDefault')}</p>
              ) : null}
            </div>
          ) : null}
        </div>
        {sectionFooter('metadata', metadataValid)}
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
