/** Admin API surface.
 *
 * One module per API area (§9): everything an admin screen talks to lives here — the queue,
 * users, invites, settings, and the admin-only views over `/library` (unmatched files, TMDB
 * matching, metadata editing). Wire types come from `./types`; the few admin-only shapes that
 * are not in the shared wire contract are declared here.
 *
 * Some collection endpoints are specified as plain arrays and some as pages; `toPage` accepts
 * either so a screen can always render a `Page<T>` without guessing.
 */

import { ApiRequestError, api, request } from '@/api/client'
import type {
  AdminStats,
  EpisodeDetail,
  Invite,
  InviteCreated,
  Job,
  JobDetail,
  JobStatus,
  JobType,
  Page,
  QueueState,
  Role,
  SeriesDetail,
  SeriesStatus,
  SeriesSummary,
  SeriesTitleOut,
  SystemSettings,
  UnmatchedFile,
  User,
} from '@/api/types'

// --- Admin-only shapes ----------------------------------------------------------------------

/** Minimal handle on an enqueued job so a screen can follow it over SSE. */
export interface JobRef {
  id: string
  type: JobType
  status: JobStatus
}

export interface TestResult {
  ok: boolean
  detail: string | null
}

export interface DeletedResult {
  deleted: number
}

/**
 * A row of the admin series table. `tmdbId` and `metadataLocked` are not part of the shared
 * `SeriesSummary`, so they are optional: when the server omits them the table renders the
 * column as unknown rather than lying about the state.
 */
export interface AdminSeriesRow extends SeriesSummary {
  tmdbId?: number | null
  metadataLocked?: boolean
}

export interface AdminMediaFile {
  id: string
  path: string
  sizeBytes: number
  container: string | null
  videoCodec: string | null
  audioCodec: string | null
  width: number | null
  height: number | null
  durationSeconds: number | null
  bitrate?: number | null
  status?: string
}

/** Episode detail plus the read-only file record the metadata editor shows. */
export interface AdminEpisodeDetail extends EpisodeDetail {
  mediaFile?: AdminMediaFile | null
}

export interface TmdbSearchResult {
  tmdbId: number
  name: string
  originalName: string | null
  overview: string | null
  firstAirDate: string | null
  year: number | null
  posterUrl: string | null
  /**
   * How many people voted on TMDB. Optional because older builds of the proxy omit it.
   *
   * It is on the card because it is the one number that separates the real show from the
   * fan-made entry with the same title: three votes and 4 000 votes look identical in a list
   * of names, and picking wrong binds the whole series to the wrong metadata.
   */
  voteCount?: number | null
  voteAverage?: number | null
  /** BCP-47 of the original production, so a cross-language hit can explain itself. */
  originalLanguage?: string | null
}

export interface SeriesPatch {
  title?: string
  originalTitle?: string | null
  overview?: string | null
  year?: number | null
  status?: SeriesStatus
  genres?: string[]
  network?: string | null
  contentRating?: string | null
  /** Sent on its own to hand a series back to the metadata jobs. */
  metadataLocked?: boolean
}

export interface EpisodePatch {
  title?: string | null
  overview?: string | null
  airDate?: string | null
  metadataLocked?: boolean
}

export interface SeriesTitleInput {
  title: string
  language: string
  kind: SeriesTitleOut['kind']
}

export interface AdminSeriesQuery {
  q?: string
  status?: SeriesStatus | ''
  sort?: 'title' | 'added' | 'recent' | 'progress'
  availableOnly?: boolean
  page?: number
  pageSize?: number
}

/**
 * What removing a series from the catalogue would cost — and whether it would even stick.
 *
 * Deletion is a catalogue operation: the library is mounted read-only and no video file is
 * ever touched. `filesStillOnDisk` is therefore the field that decides whether the deletion
 * means anything at all. The server measures it against the mount rather than against
 * `media_files.status`, and while it is true the next scan simply indexes the folder again —
 * so the admin has to move the files out of the library first. The UI says that before the
 * confirm, not after the series reappears.
 */
export interface SeriesDeletionPreview {
  seriesTitle: string
  episodeCount: number
  /** `media_files` rows, whatever their status. */
  mediaFileCount: number
  /** Rows the scanner last saw on the mount. */
  presentFileCount: number
  /** Rows the scanner could not find — the stale entries that need a way out. */
  missingFileCount: number
  /** Distinct household members whose watch progress the delete would throw away. */
  usersWithProgress: number
  filesStillOnDisk: boolean
}

/** Outcome of a delete. Every count is of catalogue rows; no media file was touched. */
export interface SeriesDeleted {
  ok: boolean
  deletedEpisodes: number
  deletedMediaFiles: number
  /** Re-measured at the moment of deletion, so the confirmation stays as honest as the preview. */
  filesStillOnDisk: boolean
}

/**
 * The wire's `CleanupResult` — identical in shape whether it was a rehearsal or the real
 * thing, so the dialog renders the dry run and the outcome with one renderer.
 */
export interface LibraryCleanupResult {
  /** Titles, not ids: this is what the admin reads back. Only series left with no episodes. */
  removedSeries: string[]
  removedEpisodes: number
  removedMediaFiles: number
  dryRun: boolean
}

/** Nothing to do — worth saying out loud rather than running a no-op. */
export function isEmptyCleanup(result: LibraryCleanupResult): boolean {
  return (
    result.removedEpisodes === 0 &&
    result.removedMediaFiles === 0 &&
    result.removedSeries.length === 0
  )
}

export interface UserPatch {
  role?: Role
  isActive?: boolean
  displayName?: string
}

export interface InviteInput {
  email: string
  role: Role
}

function toPage<T>(data: Page<T> | T[], page = 1, pageSize = 0): Page<T> {
  if (Array.isArray(data)) {
    return { items: data, total: data.length, page, pageSize: pageSize || data.length }
  }
  return data
}

// --- Jobs and queue -------------------------------------------------------------------------

export interface JobQuery {
  status?: JobStatus | ''
  type?: JobType | ''
  page?: number
  pageSize?: number
}

export async function listJobs(params: JobQuery = {}): Promise<Page<Job>> {
  const data = await api.get<Page<Job> | Job[]>('/admin/jobs', {
    status: params.status,
    type: params.type,
    page: params.page,
    pageSize: params.pageSize,
  })
  return toPage(data, params.page ?? 1, params.pageSize ?? 0)
}

export function getJob(id: string): Promise<JobDetail> {
  return api.get<JobDetail>(`/admin/jobs/${id}`)
}

export function createJob(body: {
  type: JobType
  payload?: Record<string, unknown>
  priority?: number
}): Promise<Job> {
  return api.post<Job>('/admin/jobs', {
    type: body.type,
    payload: body.payload ?? {},
    priority: body.priority ?? 100,
  })
}

export function cancelJob(id: string): Promise<Job> {
  return api.post<Job>(`/admin/jobs/${id}/cancel`)
}

export function retryJob(id: string): Promise<Job> {
  return api.post<Job>(`/admin/jobs/${id}/retry`)
}

export function reorderJob(jobId: string, priority: number): Promise<Job> {
  return api.post<Job>('/admin/jobs/reorder', { jobId, priority })
}

export function clearCompletedJobs(): Promise<DeletedResult> {
  return api.delete<DeletedResult>('/admin/jobs/completed')
}

export function getQueue(): Promise<QueueState> {
  return api.get<QueueState>('/admin/queue')
}

export function pauseQueue(): Promise<QueueState> {
  return api.post<QueueState>('/admin/queue/pause')
}

export function resumeQueue(): Promise<QueueState> {
  return api.post<QueueState>('/admin/queue/resume')
}

// --- Vocabulary -----------------------------------------------------------------------------
//
// Fetched rather than declared here. These lists are what the server canonicalizes *against*
// (`app/services/vocab.py`), so a copy in this file would be a second source of truth: add a
// provider on one side only and the select offers a spelling the server rewrites to something
// else, silently.

export interface ContentRatingGroup {
  /** `film` | `tv` — an i18n key suffix, not a label. */
  group: string
  ratings: string[]
}

export interface Vocabulary {
  contentRatings: ContentRatingGroup[]
  providers: string[]
}

export function getVocabulary(): Promise<Vocabulary> {
  return api.get<Vocabulary>('/admin/vocabulary')
}

export function getStats(): Promise<AdminStats> {
  return api.get<AdminStats>('/admin/stats')
}

// --- Users ----------------------------------------------------------------------------------

/**
 * The founding account is flagged on the wire; some builds name it `protected` rather than
 * `isProtected`. Both are accepted here so the users table has exactly one field to read, and
 * a rename on the server can never quietly re-enable the controls that must stay disabled.
 */
type WireUser = User & { protected?: boolean }

function normalizeUser(user: WireUser): User {
  const { protected: legacyProtected, ...rest } = user
  return { ...rest, isProtected: user.isProtected === true || legacyProtected === true }
}

export async function listUsers(): Promise<User[]> {
  const data = await api.get<Page<WireUser> | WireUser[]>('/admin/users')
  return toPage(data).items.map(normalizeUser)
}

export async function patchUser(id: string, patch: UserPatch): Promise<User> {
  return normalizeUser(await api.patch<WireUser>(`/admin/users/${id}`, patch))
}

export function deleteUser(id: string): Promise<void> {
  return api.delete<void>(`/admin/users/${id}`)
}

// --- Invites --------------------------------------------------------------------------------

export async function listInvites(): Promise<Invite[]> {
  const data = await api.get<Page<Invite> | Invite[]>('/admin/invites')
  return toPage(data).items
}

/**
 * The enrollment URL carries the only copy of the raw token, so it is returned whether or not
 * the mail went out. Some builds nest it as `{invite, url}`; flatten to the wire type either way
 * so callers never have to branch on the shape.
 */
type InviteCreatedWire = InviteCreated | { invite: Invite; url: string; emailSent: boolean }

function normalizeInviteCreated(data: InviteCreatedWire): InviteCreated {
  if ('invite' in data) {
    return { ...data.invite, enrollmentUrl: data.url, emailSent: data.emailSent }
  }
  return data
}

export async function createInvite(body: InviteInput): Promise<InviteCreated> {
  return normalizeInviteCreated(await api.post<InviteCreatedWire>('/admin/invites', body))
}

export function revokeInvite(id: string): Promise<void> {
  return api.delete<void>(`/admin/invites/${id}`)
}

export async function resendInvite(id: string): Promise<InviteCreated> {
  return normalizeInviteCreated(await api.post<InviteCreatedWire>(`/admin/invites/${id}/resend`))
}

// --- Settings -------------------------------------------------------------------------------

/**
 * Secrets read back as the `__SET__` sentinel, and the server may omit keys it has never
 * stored, so the result is deliberately partial.
 */
export function getSettings(): Promise<Partial<SystemSettings>> {
  return api.get<Partial<SystemSettings>>('/admin/settings')
}

/** Partial update: omitted keys are untouched, a secret sent as `__SET__` keeps its value. */
export function updateSettings(patch: Partial<SystemSettings>): Promise<Partial<SystemSettings>> {
  return api.put<Partial<SystemSettings>>('/admin/settings', patch)
}

export function testSmtp(): Promise<TestResult> {
  return api.post<TestResult>('/admin/settings/test-smtp')
}

export function testTmdb(apiKey?: string): Promise<TestResult> {
  return api.post<TestResult>('/admin/settings/test-tmdb', apiKey ? { apiKey } : {})
}

// --- Web image search -------------------------------------------------------------------------
//
// Configuration for the optional extra shelf of candidates in the artwork picker. Two providers
// and no default: `searxng` points at an instance the household already runs, which is why it is
// the documented choice — nothing to sign up for, no quota to exhaust, and the query stays on
// their network. `google_cse` stays because it costs nothing to keep and someone has it set up.

/**
 * Provider ids this build knows how to configure, in the order the select offers them.
 *
 * `''` is "off", and it is the shipped state: TMDB already supplies posters, so a web search
 * only adds a second shelf of candidates. A *stored* provider missing from this list is still
 * offered by the settings screen — an older UI must never quietly rewrite a newer server's
 * configuration to "off".
 */
export const IMAGE_SEARCH_PROVIDERS: readonly string[] = ['', 'searxng', 'google_cse']

/** The i18n key naming a provider, or `''` for one this build has no name for. */
export function imageSearchProviderKey(provider: string): string {
  if (provider === '') return 'admin.settings.imageSearchOff'
  if (provider === 'searxng') return 'admin.settings.imageSearchSearxng'
  if (provider === 'google_cse') return 'admin.settings.imageSearchGoogleCse'
  return ''
}

/**
 * SearXNG's `safesearch` levels, low to high.
 *
 * Stored by name rather than as the instance's 0/1/2, because a settings row that reads
 * `moderate` says what it does; `readSafeSearch` still accepts the numbers, since that is what
 * an admin who edited the row by hand would have written.
 */
export const SAFE_SEARCH_LEVELS: readonly string[] = ['off', 'moderate', 'strict']

/** What `POST /admin/settings/test-search` answers when the instance is reachable. */
export interface SearchTestResult {
  ok: boolean
  detail: string | null
  /** Which implementation answered, so the result cannot be read against the wrong provider. */
  provider: string
  /** The instance's version, or null when it does not report one. */
  version: string | null
  /**
   * How many of the instance's engines serve images — the number that decides whether this
   * search will ever return a poster. Null for a provider with no such notion.
   */
  imageEngineCount: number | null
}

interface WireSearchTest {
  ok?: boolean
  detail?: string | null
  provider?: string | null
  version?: string | null
  imageEngineCount?: number | null
  /** Some builds answer with the engines themselves rather than a count. */
  imageEngines?: number | unknown[] | null
}

function countEngines(payload: WireSearchTest): number | null {
  if (typeof payload.imageEngineCount === 'number') return payload.imageEngineCount
  if (typeof payload.imageEngines === 'number') return payload.imageEngines
  if (Array.isArray(payload.imageEngines)) return payload.imageEngines.length
  return null
}

/**
 * Probes the configured provider.
 *
 * Deliberately body-less: the server tests what it has **stored**, so the settings screen makes
 * the admin save first rather than reporting a success for a URL that is only on screen — the
 * same rule the SMTP test follows, and for the same reason.
 */
export async function testSearch(): Promise<SearchTestResult> {
  const data = await api.post<WireSearchTest>('/admin/settings/test-search')
  return {
    ok: data.ok !== false,
    detail: data.detail ?? null,
    provider: data.provider ?? '',
    version: data.version ?? null,
    imageEngineCount: countEngines(data),
  }
}

/**
 * The one failure with a specific, likely fix behind it.
 *
 * A stock SearXNG answers in HTML only, so the very first test against a fresh instance fails
 * here — not because anything is misconfigured on this side, but because `json` is missing from
 * `search.formats` in its `settings.yml`. The settings screen singles the code out and shows
 * that fix rather than filing it under "the search failed".
 */
export function isJsonFormatDisabled(error: unknown): boolean {
  return error instanceof ApiRequestError && error.code === 'SEARXNG_JSON_DISABLED'
}

/** One engine the instance has, as it names it. `bing images`, `duckduckgo images`, … */
export interface SearchEngine {
  name: string
  /** The instance's own categories. `images` is the one that matters here. */
  categories: string[]
  enabled: boolean
  /** The instance's `!bang` shortcut, when it has one. */
  shortcut: string | null
}

interface WireEngine {
  name?: string
  categories?: unknown
  enabled?: boolean
  shortcut?: string | null
}

function toEngine(engine: WireEngine): SearchEngine {
  const categories = Array.isArray(engine.categories)
    ? engine.categories.filter((value): value is string => typeof value === 'string')
    : []
  return {
    name: String(engine.name ?? ''),
    categories,
    // Absent means present-and-usable: the instance would not have listed it otherwise.
    enabled: engine.enabled !== false,
    shortcut: engine.shortcut ?? null,
  }
}

/** The instance's engines, so the admin picks from names it actually has. */
export async function listSearchEngines(): Promise<SearchEngine[]> {
  const data = await api.get<WireEngine[] | { engines: WireEngine[] }>('/admin/search/engines')
  const list = Array.isArray(data) ? data : (data.engines ?? [])
  return list.map(toEngine).filter((engine) => engine.name !== '')
}

/**
 * The image engines among them.
 *
 * A server that already filtered its answer sends engines with no categories at all, and
 * dropping every one of them would leave the admin staring at an empty checkbox list next to a
 * test that just succeeded — so an empty match over a non-empty list is read as "already
 * filtered" rather than "no image engines".
 */
export function imageEngines(engines: SearchEngine[]): SearchEngine[] {
  const matching = engines.filter((engine) => engine.categories.includes('images'))
  return matching.length === 0 ? engines : matching
}

/** Engine names from either stored shape, trimmed and de-duplicated, order preserved. */
export function parseEngineList(value: unknown): string[] {
  const raw = Array.isArray(value) ? value : String(value ?? '').split(',')
  const names: string[] = []
  for (const entry of raw) {
    const name = typeof entry === 'string' ? entry.trim() : ''
    if (name !== '' && !names.includes(name)) names.push(name)
  }
  return names
}

/** The comma-separated form, which is what both the free-text field and the wire use. */
export function formatEngineList(names: readonly string[]): string {
  return names.join(', ')
}

// --- Library curation -----------------------------------------------------------------------

export function listAdminSeries(params: AdminSeriesQuery = {}): Promise<Page<AdminSeriesRow>> {
  return api.get<Page<AdminSeriesRow>>('/library/series', {
    q: params.q,
    status: params.status,
    sort: params.sort,
    availableOnly: params.availableOnly,
    page: params.page,
    pageSize: params.pageSize,
  })
}

export function getAdminSeries(idOrSlug: string): Promise<SeriesDetail> {
  return api.get<SeriesDetail>(`/library/series/${idOrSlug}`)
}

export function patchSeries(id: string, patch: SeriesPatch): Promise<SeriesDetail> {
  return api.patch<SeriesDetail>(`/library/series/${id}`, patch)
}

export function getAdminEpisode(id: string): Promise<AdminEpisodeDetail> {
  return api.get<AdminEpisodeDetail>(`/library/episodes/${id}`)
}

export function patchEpisode(id: string, patch: EpisodePatch): Promise<AdminEpisodeDetail> {
  return api.patch<AdminEpisodeDetail>(`/library/episodes/${id}`, patch)
}

export function addSeriesTitle(seriesId: string, body: SeriesTitleInput): Promise<SeriesTitleOut> {
  return api.post<SeriesTitleOut>(`/library/series/${seriesId}/titles`, body)
}

export function deleteSeriesTitle(seriesId: string, titleId: string): Promise<void> {
  return api.delete<void>(`/library/series/${seriesId}/titles/${titleId}`)
}

/** Read-only: nothing is deleted until `deleteSeries` is called. */
export function getDeletionPreview(id: string): Promise<SeriesDeletionPreview> {
  return api.get<SeriesDeletionPreview>(`/library/series/${id}/deletion-preview`)
}

/** Removes the catalogue rows for a series — never a file on the NAS. */
export function deleteSeries(id: string): Promise<SeriesDeleted> {
  return api.delete<SeriesDeleted>(`/library/series/${id}`)
}

/**
 * Drops catalogue rows whose media is entirely gone.
 *
 * `dryRun` is what makes this safe to offer at all: the scanner never deletes a row itself,
 * precisely because an unplugged disk must not wipe the library, so the admin gets to see the
 * exact damage before authorizing it. The server plans both runs identically, which is what
 * makes the rehearsal worth trusting.
 */
export function cleanupLibrary(dryRun: boolean): Promise<LibraryCleanupResult> {
  return api.post<LibraryCleanupResult>('/library/cleanup', { dryRun })
}

export function refreshSeries(id: string): Promise<JobRef> {
  return api.post<JobRef>(`/library/series/${id}/refresh`)
}

/**
 * Fill a series' fields from IMDb, by way of OMDb.
 *
 * The fallback for the id TMDB does not carry — which for vertical short drama is most of
 * them. Measured on this household's four series: every TMDB lookup a miss, every one of them
 * present on IMDb. Blanks only unless `overwrite`, so a synopsis somebody wrote survives a
 * later trip for the year.
 */
export function applyImdbMetadata(
  seriesId: string,
  imdbId: string,
  overwrite = false,
): Promise<SeriesDetail> {
  return api.post<SeriesDetail>(
    `/library/series/${encodeURIComponent(seriesId)}/metadata/imdb`,
    { imdbId, overwrite },
  )
}

export function matchSeries(id: string, tmdbId: number): Promise<JobRef> {
  return api.post<JobRef>(`/library/series/${id}/match`, { tmdbId })
}

/** What forgetting a wrong match actually did. */
export interface SeriesUnlinked {
  seriesId: string
  /** The id that was cleared, so the dialog can say what it had been bound to. */
  previousTmdbId: number | null
  /** Always true: an unlink also locks the row, or the next refresh would re-match it. */
  metadataLocked: boolean
}

/**
 * Breaks the TMDB link, leaving the metadata that was already fetched in place.
 *
 * Unlinking is not a delete: the titles, overview and images the series already has are what
 * the household sees tonight, and throwing them away to punish a bad match would be worse than
 * the bad match. What it does stop is the next automatic refresh writing that wrong entry back
 * — which is why the server locks the row at the same time, and why the dialog says so.
 */
export function unlinkSeries(id: string): Promise<SeriesUnlinked> {
  return api.post<SeriesUnlinked>(`/library/series/${id}/unlink`)
}

// --- Artwork --------------------------------------------------------------------------------

/** Which slot an image fills. The web search vouches for neither, so its results carry `''`. */
export type ArtworkKind = 'poster' | 'backdrop'

/**
 * One image on offer, whichever source produced it.
 *
 * `url` is exactly what `POST .../artwork` takes back, so a card never has to reconstruct an
 * address. Dimensions and rating are on every card because they are the whole basis for
 * choosing: a 300×450 upscale and a 2000×3000 scan look identical at thumbnail size.
 */
export interface ArtworkCandidate {
  /** `tmdb`, or the image-search provider's name (`searxng`, `google_cse`). */
  source: string
  /**
   * Which of the provider's engines found this one — `bing images`, `duckduckgo images`, …
   *
   * Optional because only a metasearch provider has engines to name, and because an older
   * server does not send it. Read it through `candidateEngine`, which falls back to the
   * provider: the point on screen is that a result can be traced to where it came from, and a
   * poster from a wallpaper site is worth a different look than one from a poster database.
   */
  engine?: string | null
  kind: ArtworkKind | ''
  url: string
  thumbnailUrl: string | null
  width: number | null
  height: number | null
  /** ISO-639-1, or null for textless artwork — usually the most useful kind. */
  language: string | null
  voteAverage: number | null
  voteCount: number | null
  /** Web results only: the page the image was found on. */
  title: string
  contextUrl: string | null
}

/**
 * Why a source contributed nothing.
 *
 * An unavailable source is not an error: a fresh install has no web search configured and a
 * series may not be linked to TMDB yet. Both are ordinary states with different answers, so
 * the code travels and the picker turns it into the right sentence.
 */
export type ArtworkSourceReason = 'NOT_LINKED' | 'NOT_CONFIGURED' | 'PROVIDER_ERROR' | ''

export interface ArtworkSource {
  /** The channel: `tmdb` or `web`. */
  name: string
  /** Which implementation answered — `searxng`, `google_cse`, or `''` for none. */
  provider: string
  available: boolean
  reason: ArtworkSourceReason
  count: number
}

/** Where a candidate actually came from: the engine when there is one, else the provider. */
export function candidateEngine(candidate: ArtworkCandidate): string {
  const engine = (candidate.engine ?? '').trim()
  return engine === '' ? candidate.source : engine
}

export interface ArtworkOptions {
  seriesId: string
  tmdbId: number | null
  posters: ArtworkCandidate[]
  backdrops: ArtworkCandidate[]
  /** Web-search hits, kept apart because they carry no declared kind. */
  web: ArtworkCandidate[]
  sources: ArtworkSource[]
}

/** The source entry for a channel, or null when the server said nothing about it. */
export function artworkSource(
  options: ArtworkOptions | undefined,
  name: 'tmdb' | 'web',
): ArtworkSource | null {
  return options?.sources.find((source) => source.name === name) ?? null
}

/** Read-only: listing candidates neither downloads nor commits anything. */
export function listArtwork(seriesId: string): Promise<ArtworkOptions> {
  return api.get<ArtworkOptions>(`/library/series/${seriesId}/artwork`)
}

/** What was stored, and the address to render immediately. */
export interface ArtworkApplied {
  seriesId: string
  kind: string
  /** `DATA_DIR`-relative, as stored on the series row. */
  storedPath: string
  url: string | null
  /**
   * Always true, and reported rather than assumed: choosing artwork also stops metadata
   * refreshes overwriting the series' other fields, which is a consequence worth showing.
   */
  metadataLocked: boolean
}

/**
 * Commits an image by URL — a candidate from the grid, or one the admin pasted.
 *
 * The server fetches it (checking scheme, host and every redirect hop, since this box sits on
 * the household network) and stores it under `DATA_DIR`. `LIBRARY_ROOT` stays read-only, so no
 * artwork is ever written next to the video files.
 */
export function selectArtwork(
  seriesId: string,
  kind: ArtworkKind,
  url: string,
): Promise<ArtworkApplied> {
  return api.post<ArtworkApplied>(`/library/series/${seriesId}/artwork`, { kind, url })
}

/** Same pipeline for a file the admin has locally. `kind` rides in the query string. */
export function uploadArtwork(
  seriesId: string,
  kind: ArtworkKind,
  file: File,
): Promise<ArtworkApplied> {
  const form = new FormData()
  form.append('file', file, file.name)
  return request<ArtworkApplied>(`/library/series/${seriesId}/artwork/upload`, {
    method: 'POST',
    body: form,
    query: { kind },
  })
}

export async function listUnmatched(page = 1, pageSize = 50): Promise<Page<UnmatchedFile>> {
  const data = await api.get<Page<UnmatchedFile> | UnmatchedFile[]>('/library/unmatched', {
    page,
    pageSize,
  })
  return toPage(data, page, pageSize)
}

export function assignUnmatched(
  fileId: string,
  body: { seriesId: string; seasonNumber: number; episodeNumber: number },
): Promise<void> {
  return api.post<void>(`/library/unmatched/${fileId}/assign`, body)
}

/**
 * Proxy TMDB's search.
 *
 * `language` is the search language, not the display one: a Chinese drama is findable by its
 * Chinese title only when TMDB is asked in `zh`, and the household's UI language has nothing to
 * do with which alphabet the admin is typing. Omitted, the server uses its configured default.
 */
export async function searchTmdb(
  q: string,
  year?: number | null,
  language?: string | null,
): Promise<TmdbSearchResult[]> {
  const data = await api.get<TmdbSearchResult[] | { results: TmdbSearchResult[] }>(
    '/library/tmdb/search',
    { q, year: year ?? undefined, language: language || undefined },
  )
  return Array.isArray(data) ? data : data.results
}

/** Exactly one of the two ids. IMDb ids keep their `tt` prefix. */
export interface TmdbLookupParams {
  tmdbId?: number | null
  imdbId?: string | null
}

/**
 * Resolves a series from an id typed straight in, skipping the search entirely.
 *
 * This is the escape hatch for everything search cannot do: a show whose title is ambiguous in
 * every language, a remake that outranks the original, a title TMDB stores under a
 * transliteration nobody would guess. The admin already has the page open — pasting the id is
 * both faster and exact.
 *
 * The server answers with a list (of one, for a TMDB id) so the dialog renders it with the same
 * card it renders search results with. An id that is well-formed but unknown comes back as an
 * empty list, or as a TMDB 404 — both mean "nothing with that id", which is a sentence rather
 * than a red box, so the 404 is folded into the empty list here.
 */
export async function lookupTmdb(params: TmdbLookupParams): Promise<TmdbSearchResult[]> {
  try {
    return await api.get<TmdbSearchResult[]>('/library/tmdb/lookup', {
      tmdbId: params.tmdbId ?? undefined,
      imdbId: params.imdbId || undefined,
    })
  } catch (error) {
    const missing =
      error instanceof ApiRequestError &&
      (error.code === 'TMDB_NOT_FOUND' || (error.status === 404 && error.code === 'NOT_FOUND'))
    if (missing) return []
    throw error
  }
}

/**
 * The languages the match dialog offers, as BCP-47 codes TMDB accepts.
 *
 * Labels come from `Intl.DisplayNames` at the call site rather than from the locale files —
 * a language name is data the platform already has in every locale (§ AccountPage).
 */
export const TMDB_SEARCH_LANGUAGES: readonly string[] = [
  'es',
  'en',
  'ko',
  'ja',
  'zh',
  'pt',
  'fr',
  'de',
  'it',
  'tr',
  'th',
  'hi',
  'ru',
  'ar',
]

// --- Web discovery --------------------------------------------------------------------------
//
// TMDB's title search cannot find a short-form Asian drama often enough to be relied on, and the
// admin is then left holding a folder name. A plain web search does find it — not as prose, but
// as a *pointer*: the first page of hits is usually the show's TMDB, IMDb, Wikipedia or
// MyDramaList entry. So this is a way to LOCATE the authoritative record, and nothing else.
//
// A search result is `title / url / content / engine`, where `content` is a truncated extract of
// someone else's page, frequently cut mid-sentence and sometimes carrying navigation furniture.
// That is exactly good enough for a human choosing between six links and nowhere near good
// enough to store: an overview comes from TMDB or from Wikipedia, both of which publish
// structured text meant to be consumed this way. `snippet` is therefore display-only and no code
// path sends it back.

/** One hit, already reduced to the four things a choice is made on. */
export interface DiscoveryResult {
  title: string
  /** Guaranteed `http(s)` — see `toDiscoveryResult`. */
  url: string
  /** The host without `www.`, which is what actually tells the admin whose page this is. */
  domain: string
  /**
   * The engine's extract of the page. **Never stored.** Shown quoted and muted so it cannot be
   * mistaken for the synopsis the app keeps.
   */
  snippet: string
  /** Which engine of the metasearch found it, when the provider names one. */
  engine: string | null
  /**
   * The ids the server read out of the result's own URL, and the whole point of this list: a row
   * carrying one resolves through the existing TMDB lookup and binds the series in one action.
   * Null on a row that is only a lead.
   */
  tmdbId: number | null
  imdbId: string | null
}

export interface Discovery {
  /** What was actually searched for, which may not be what was typed. */
  query: string
  /** Which implementation answered — `searxng`, `google_cse`, or `''` for none. */
  provider: string
  /**
   * False when no search provider is set up. Not an error: a fresh install ships this way, and
   * the panel answers with the one sentence that fixes it instead of an empty list.
   */
  configured: boolean
  /**
   * True when the query was restricted to the configured platform domains.
   *
   * It is the difference between "your platforms have nothing about this" and "the web has
   * nothing about this", which are two different things to tell an admin. The search no longer
   * widens itself when the narrow one is empty, so this is how the screen explains an empty
   * list instead of leaving it looking broken.
   */
  narrowed: boolean
  /**
   * Engines the instance reported as failing for this query.
   *
   * Load-bearing for how an empty list is read. This household ran for days on an instance
   * where only two of fourteen general engines answered — Google was returning a JavaScript
   * interstitial that parses as zero results and raises no error — and read the emptiness as
   * "this content is not on the web". It was not. The instance knew; the app was discarding
   * the answer.
   */
  unresponsiveEngines: string[]
  results: DiscoveryResult[]
}

interface WireDiscoveryResult {
  title?: string | null
  url?: string | null
  /** The server calls this `sourceDomain`; `domain` is accepted as the shorter spelling. */
  sourceDomain?: string | null
  domain?: string | null
  /** SearXNG's own name for the extract; `snippet` is what a normalized server would send. */
  content?: string | null
  snippet?: string | null
  engine?: string | null
  /**
   * The server groups every identifier a result URL carries under `ids`, because one URL can
   * hold several and they are read out together. Flat `tmdbId`/`imdbId` are still accepted so
   * this parser does not depend on which of the two shapes answered.
   */
  ids?: {
    tmdbId?: number | string | null
    imdbId?: string | null
  } | null
  tmdbId?: number | string | null
  imdbId?: string | null
}

interface WireDiscovery {
  query?: string | null
  provider?: string | null
  configured?: boolean
  narrowed?: boolean
  unresponsiveEngines?: string[]
  results?: WireDiscoveryResult[]
}

/** The host a URL points at, minus `www.`, or `''` when it is not a URL at all. */
function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./i, '').toLowerCase()
  } catch {
    return ''
  }
}

/**
 * The other half of the trust split the artwork path already draws (§3).
 *
 * The configured SearXNG instance is trusted; what its results *point at* is not, and here the
 * exposure is an href. A `javascript:` or `data:` URL in a result title would run in the admin's
 * session, so a row whose URL is not plain http(s) is dropped rather than rendered inert — it
 * was never a usable lead anyway.
 */
function isBrowsableUrl(url: string): boolean {
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}

/** `tt` + digits, lowercased; anything else is not an IMDb id and must not reach the lookup. */
function toImdbId(value: unknown): string | null {
  const raw = typeof value === 'string' ? value.trim().toLowerCase() : ''
  return /^tt\d{5,12}$/.test(raw) ? raw : null
}

/** A positive integer id from either a number or its decimal string. Null for anything else. */
function toPositiveInt(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null
}

function toDiscoveryResult(row: WireDiscoveryResult): DiscoveryResult | null {
  const url = String(row.url ?? '').trim()
  if (!isBrowsableUrl(url)) return null
  const domain = (row.sourceDomain ?? row.domain ?? '').trim() || hostOf(url)
  return {
    // A hit with no title still has a domain and a link, so it stays usable.
    title: String(row.title ?? '').trim() || domain,
    url,
    domain,
    snippet: String(row.snippet ?? row.content ?? '').trim(),
    engine: (row.engine ?? '').trim() || null,
    // The ids are what make a row one click from a match, so both shapes are read. Missing
    // them silently would turn every result into an unlinkable lead — the one failure this
    // panel cannot afford, because it looks exactly like "the web found nothing useful".
    tmdbId: toPositiveInt(row.tmdbId ?? row.ids?.tmdbId),
    imdbId: toImdbId(row.imdbId ?? row.ids?.imdbId),
  }
}

function isSearchUnconfigured(error: unknown): boolean {
  return error instanceof ApiRequestError && error.code === 'SEARCH_NOT_CONFIGURED'
}

/**
 * Searches the web for the record behind a title the metadata provider cannot find.
 *
 * An unconfigured provider comes back as `configured: false` rather than as a throw, the same
 * way `lookupTmdb` folds a 404 into an empty list: a feature nobody has switched on yet is an
 * ordinary state with a specific repair, not a failure to report in red. The server may say so
 * either in the payload or with `SEARCH_NOT_CONFIGURED`; both land in the same place.
 *
 * Read-only. Nothing here writes to the series — matching is a separate, explicit act.
 */
export async function discoverSeries(
  seriesId: string,
  q: string,
  platforms = false,
): Promise<Discovery> {
  const query = q.trim()
  try {
    const data = await api.get<WireDiscovery | WireDiscoveryResult[]>(
      `/library/series/${seriesId}/discover`,
      // Omitted rather than sent false, so the default search keeps its existing URL.
      { q: query, platforms: platforms || undefined },
    )
    const payload: WireDiscovery = Array.isArray(data) ? { results: data } : data
    const rows = Array.isArray(payload.results) ? payload.results : []
    return {
      query: (payload.query ?? '').trim() || query,
      provider: (payload.provider ?? '').trim(),
      // Absent means configured: an empty list from a working instance is "no results", and
      // reading it as "not configured" would send the admin to fix something that is not broken.
      configured: payload.configured !== false,
      narrowed: payload.narrowed === true,
      unresponsiveEngines: Array.isArray(payload.unresponsiveEngines)
        ? payload.unresponsiveEngines.map(String)
        : [],
      results: rows
        .map(toDiscoveryResult)
        .filter((row): row is DiscoveryResult => row !== null),
    }
  } catch (error) {
    if (isSearchUnconfigured(error)) {
      return {
        query,
        provider: '',
        configured: false,
        narrowed: false,
        unresponsiveEngines: [],
        results: [],
      }
    }
    throw error
  }
}

/** Whichever id the row carries, in the shape `lookupTmdb` takes. Null when it carries neither. */
export function discoveryLookup(result: DiscoveryResult): TmdbLookupParams | null {
  if (result.tmdbId !== null) return { tmdbId: result.tmdbId, imdbId: null }
  if (result.imdbId !== null) return { tmdbId: null, imdbId: result.imdbId }
  return null
}

// --- Bulk import ----------------------------------------------------------------------------

/**
 * Whole folders of files that carry no episode signal, planned as a group.
 *
 * The one-at-a-time `assign` flow above is right for a stray file and hopeless for a folder of
 * 58 named `701151231.720p.narrowv3.mp4`. These endpoints look at such a folder as a unit, work
 * out the intended order, and hand back a complete mapping for the admin to approve in one act.
 *
 * Applying a plan **creates episode rows that point at the files under their existing names**.
 * `LIBRARY_ROOT` is mounted read-only and nothing here ever renames, moves or writes a media
 * file — the same reason `deleteSeries` and `cleanupLibrary` are catalogue-only operations.
 * `renameScriptUrl` exists for the admin who does want tidy names on disk: the server writes
 * the script, the admin runs it themselves, and dramatarr never executes it.
 *
 * The server calls these routes `/library/unmatched/*` — they are the group-level half of the
 * same admin view as `listUnmatched` above, not a separate feature — and it addresses a group
 * by its **folder path**, which is the only identifier a folder of files actually has. The
 * shapes below are the ones this page wants to render; `_wire.ts`-style translation lives in
 * the mappers at the end of this section so the components never see two vocabularies.
 */
const IMPORT_BASE = '/library/unmatched'

/** How a plan decided the order. The select only ever offers what the plan says it can do. */
export type ImportStrategy =
  | 'numeric_sequence'
  | 'filename_order'
  | 'embedded_number'
  | 'date_order'
  | 'modified_time'

/**
 * A sentence the server wants said, in the reader's language.
 *
 * The wire carries a stable code plus its interpolation values rather than English prose, so
 * the explanation and the warnings localize like everything else (§9: no literal ever reaches
 * the UI without passing through a key). `text` is the server's own rendering, used only when
 * we have no translation for the code yet — the same degradation `ErrorState` applies to error
 * codes, so a backend that learns a new reason can never render a raw English string into a
 * Spanish page.
 */
export interface ImportMessage {
  code: string
  params: Record<string, string | number>
  text: string | null
}

/** A folder the scanner could not place, and what we can guess about it before planning. */
export interface UnmatchedGroup {
  /** Addresses the folder in `planImport` / `applyImport`. */
  id: string
  /** Folder name as it reads on disk, e.g. `Tempest The Last Mecha`. */
  folderName: string
  /** Path relative to `LIBRARY_ROOT`. */
  relativePath: string
  fileCount: number
  totalSizeBytes: number
  /** Series title read out of the folder name. */
  inferredTitle: string
  inferredYear: number | null
  /** Set when the folder already lines up with a series in the catalogue. */
  existingSeriesId: string | null
  existingSeriesTitle: string | null
  /** A handful of names, so the folder is recognizable before a plan is asked for. */
  sampleFilenames: string[]
}

/** Every field but `groupId` is an override; omitted, the server picks and reports its choice. */
export interface ImportPlanRequest {
  groupId: string
  strategy?: ImportStrategy
  seasonNumber?: number
  startEpisodeNumber?: number
}

export interface ImportPlanItem {
  mediaFileId: string
  /** Basename only — the folder is the card's heading, repeating it in 58 rows is noise. */
  filename: string
  relativePath: string
  sizeBytes: number
  seasonNumber: number
  episodeNumber: number
  /** 0–1. */
  confidence: number
  /** What the strategy sorted on (the extracted integer, the parsed date, …). */
  orderKey: string | null
}

export interface ImportPlan {
  groupId: string
  folderName: string
  /** The strategy actually used, which may not be the one that was asked for. */
  strategy: ImportStrategy
  /** Everything this folder could be ordered by — the select offers exactly these. */
  availableStrategies: ImportStrategy[]
  /** Why the order is what it is. The most important thing on the screen. */
  explanation: ImportMessage
  warnings: ImportMessage[]
  /** 0–1, for the plan as a whole. */
  confidence: number
  seasonNumber: number
  startEpisodeNumber: number
  inferredTitle: string
  inferredYear: number | null
  existingSeriesId: string | null
  existingSeriesTitle: string | null
  totalSizeBytes: number
  items: ImportPlanItem[]
}

/** The approved mapping, row by row. */
export interface ImportApplyItem {
  mediaFileId: string
  seasonNumber: number
  episodeNumber: number
}

export interface NewSeriesInput {
  title: string
  year: number | null
}

/**
 * `items` is what the admin actually approved, so that is what is sent — not the planning
 * inputs for the server to re-derive. `strategy` rides along for the audit log only.
 * Exactly one of `seriesId` / `newSeries` is set.
 */
export interface ImportApplyRequest {
  groupId: string
  strategy: ImportStrategy
  seriesId: string | null
  newSeries: NewSeriesInput | null
  items: ImportApplyItem[]
}

/** Counts of catalogue rows. No media file was renamed, moved or written. */
export interface ImportResult {
  seriesId: string
  seriesSlug: string
  seriesTitle: string
  createdSeries: boolean
  createdEpisodes: number
  attachedFiles: number
}

export interface RenameScriptParams {
  groupId: string
  strategy: ImportStrategy
  seasonNumber: number
  startEpisodeNumber: number
  /** The name the script should build the new filenames from. */
  title: string
}

export async function listUnmatchedGroups(): Promise<UnmatchedGroup[]> {
  const data = await api.get<Page<WireGroup> | WireGroup[]>(`${IMPORT_BASE}/groups`)
  return toPage(data).items.map(toGroup)
}

/** Read-only: planning touches neither the catalogue nor the mount. */
export async function planImport(body: ImportPlanRequest): Promise<ImportPlan> {
  const plan = await api.post<WirePlan>(`${IMPORT_BASE}/plan`, {
    folder: body.groupId,
    strategy: body.strategy === undefined ? undefined : toWireStrategy(body.strategy),
    seasonNumber: body.seasonNumber,
    startEpisode: body.startEpisodeNumber,
  })
  return toPlan(plan)
}

/** Creates/attaches episode rows pointing at the files under their existing names. */
export async function applyImport(body: ImportApplyRequest): Promise<ImportResult> {
  const applied = await api.post<WireApplied>(`${IMPORT_BASE}/apply`, {
    folder: body.groupId,
    seriesId: body.seriesId,
    newSeriesTitle: body.newSeries?.title,
    newSeriesYear: body.newSeries?.year ?? undefined,
    assignments: body.items,
  })
  return {
    seriesId: applied.seriesId,
    seriesSlug: applied.seriesSlug,
    seriesTitle: applied.seriesTitle,
    createdSeries: applied.created.series,
    createdEpisodes: applied.created.episodes,
    attachedFiles: applied.attached,
  }
}

/**
 * A plain URL, not a fetch: the script is offered as a link so the browser saves it with the
 * filename the server chose, and so the admin can see where it comes from before clicking.
 *
 * `title` is not sent: the server names the files after the series it has, or the one the
 * folder implies, and a title typed into the form is not yet either of those.
 */
export function renameScriptUrl(params: RenameScriptParams): string {
  const query = new URLSearchParams({
    folder: params.groupId,
    strategy: toWireStrategy(params.strategy),
    seasonNumber: String(params.seasonNumber),
    startEpisode: String(params.startEpisodeNumber),
  })
  return `/api${IMPORT_BASE}/rename-script?${query.toString()}`
}

// --- Wire translation -------------------------------------------------------------------------
//
// The server names its strategies after *what it read* (a shared filename pattern, an embedded
// episode marker) and this page names them after *what the admin sees* (a numeric sequence,
// alphabetical order). The map below is the whole difference and it is a bijection, so an
// unknown name from either side is a genuine mismatch rather than something to paper over —
// hence `null` and a filtered list rather than a silent default.

const STRATEGY_FROM_WIRE: Record<string, ImportStrategy> = {
  shared_pattern_number: 'numeric_sequence',
  embedded_episode: 'embedded_number',
  date: 'date_order',
  natural_name: 'filename_order',
  mtime: 'modified_time',
}

const STRATEGY_TO_WIRE: Record<ImportStrategy, string> = {
  numeric_sequence: 'shared_pattern_number',
  embedded_number: 'embedded_episode',
  date_order: 'date',
  filename_order: 'natural_name',
  modified_time: 'mtime',
}

function toWireStrategy(strategy: ImportStrategy): string {
  return STRATEGY_TO_WIRE[strategy]
}

function fromWireStrategy(name: string): ImportStrategy | null {
  return STRATEGY_FROM_WIRE[name] ?? null
}

/**
 * Marks a sentence the server wrote in English.
 *
 * `useImportMessage` looks the code up first and falls back to `text`, so a warning that
 * carries this code renders the server's own wording. That is the honest state of things:
 * plan *explanations* carry a real code and localize, warnings do not carry one yet.
 */
const SERVER_TEXT = 'SERVER_TEXT'

interface WireStrategy {
  name: string
  confidence: number
}

interface WireAssignment {
  mediaFileId: string
  path: string
  filename: string
  sizeBytes: number
  seasonNumber: number
  episodeNumber: number
  confidence: number
  reason: string
  orderKey: string | null
  proposedFilename: string
  existingEpisodeId: string | null
  conflict: boolean
}

interface WirePlan {
  folder: string
  strategy: string
  confidence: number
  explanation: string
  explanationCode: string
  seasonNumber: number
  startEpisode: number
  fileCount: number
  suggestedSeriesTitle: string
  suggestedYear: number | null
  existingSeriesId: string | null
  existingSeriesTitle: string | null
  strategies: WireStrategy[]
  assignments: WireAssignment[]
  warnings: string[]
  hasConflicts: boolean
}

interface WireGroup {
  folder: string
  fileCount: number
  totalBytes: number
  suggestedSeriesTitle: string
  suggestedYear: number | null
  existingSeriesId: string | null
  existingSeriesTitle: string | null
  strategies: WireStrategy[]
  plan: WirePlan
}

interface WireApplied {
  seriesId: string
  seriesSlug: string
  seriesTitle: string
  created: { series: boolean; episodes: number }
  attached: number
}

/** Last segment of a library-relative folder path — `Show (2024)/Season 02` → `Season 02`. */
function basename(folder: string): string {
  const parts = folder.split('/').filter(Boolean)
  return parts[parts.length - 1] ?? folder
}

function toGroup(group: WireGroup): UnmatchedGroup {
  return {
    id: group.folder,
    folderName: basename(group.folder),
    relativePath: group.folder,
    fileCount: group.fileCount,
    totalSizeBytes: group.totalBytes,
    inferredTitle: group.suggestedSeriesTitle,
    inferredYear: group.suggestedYear,
    existingSeriesId: group.existingSeriesId,
    existingSeriesTitle: group.existingSeriesTitle,
    // The server already planned the folder, so the sample is the real first few rows rather
    // than an arbitrary directory listing.
    sampleFilenames: group.plan.assignments.slice(0, 4).map((item) => item.filename),
  }
}

function toPlan(plan: WirePlan): ImportPlan {
  const strategy = fromWireStrategy(plan.strategy)
  return {
    groupId: plan.folder,
    folderName: basename(plan.folder),
    strategy: strategy ?? 'filename_order',
    availableStrategies: plan.strategies
      .map((item) => fromWireStrategy(item.name))
      .filter((name): name is ImportStrategy => name !== null),
    explanation: { code: plan.explanationCode, params: {}, text: plan.explanation },
    warnings: plan.warnings.map((text) => ({ code: SERVER_TEXT, params: {}, text })),
    confidence: plan.confidence,
    seasonNumber: plan.seasonNumber,
    startEpisodeNumber: plan.startEpisode,
    inferredTitle: plan.suggestedSeriesTitle,
    inferredYear: plan.suggestedYear,
    existingSeriesId: plan.existingSeriesId,
    existingSeriesTitle: plan.existingSeriesTitle,
    totalSizeBytes: plan.assignments.reduce((total, item) => total + item.sizeBytes, 0),
    items: plan.assignments.map((item) => ({
      mediaFileId: item.mediaFileId,
      filename: item.filename,
      relativePath: item.path,
      sizeBytes: item.sizeBytes,
      seasonNumber: item.seasonNumber,
      episodeNumber: item.episodeNumber,
      confidence: item.confidence,
      orderKey: item.orderKey,
    })),
  }
}

// --- Query keys -----------------------------------------------------------------------------

/**
 * One factory for every admin cache entry, arranged as prefixes
 * (`admin > jobs > list | detail`).
 *
 * The nesting is what lets the SSE consumer broadcast a progress patch across every cached job
 * page with a single `setQueriesData` on `jobLists()`, without touching the `detail` entries
 * that hold log lines — and lets any screen invalidate a whole family in one call.
 */
export const adminKeys = {
  all: ['admin'] as const,
  stats: () => [...adminKeys.all, 'stats'] as const,
  queue: () => [...adminKeys.all, 'queue'] as const,
  jobs: () => [...adminKeys.all, 'jobs'] as const,
  jobLists: () => [...adminKeys.jobs(), 'list'] as const,
  jobList: (params: JobQuery) => [...adminKeys.jobLists(), params] as const,
  jobDetails: () => [...adminKeys.jobs(), 'detail'] as const,
  job: (id: string) => [...adminKeys.jobDetails(), id] as const,
  users: () => [...adminKeys.all, 'users'] as const,
  invites: () => [...adminKeys.all, 'invites'] as const,
  settings: () => [...adminKeys.all, 'settings'] as const,
  /* Discovery, not configuration: the engine list belongs to the instance, so it is cached
   * apart from the settings it ends up writing into. */
  searchEngines: () => [...adminKeys.all, 'search-engines'] as const,
  /* Effectively static — it changes only when the server does — so the editor caches it hard
   * rather than refetching a constant on every visit to a series. */
  vocabulary: () => [...adminKeys.all, 'vocabulary'] as const,
  series: () => [...adminKeys.all, 'series'] as const,
  seriesList: (params: AdminSeriesQuery) => [...adminKeys.series(), 'list', params] as const,
  seriesDetail: (idOrSlug: string) => [...adminKeys.series(), 'detail', idOrSlug] as const,
  deletionPreview: (id: string) => [...adminKeys.series(), 'deletion-preview', id] as const,
  /* The dry run is a rehearsal of a destructive act: it is cached under its own key so a stale
   * count from an earlier visit can never be what the admin confirms. */
  cleanupPreview: () => [...adminKeys.all, 'cleanup-preview'] as const,
  episode: (id: string) => [...adminKeys.all, 'episode', id] as const,
  unmatched: (page: number, pageSize: number) =>
    [...adminKeys.all, 'unmatched', page, pageSize] as const,
  unmatchedGroups: () => [...adminKeys.all, 'unmatched-groups'] as const,
  /* Keyed by the whole request: changing the strategy, the season or the first episode number
   * is a different plan, and each one stays cached so flipping back is instant. */
  importPlans: () => [...adminKeys.all, 'import-plan'] as const,
  importPlan: (request: ImportPlanRequest) => [...adminKeys.importPlans(), request] as const,
  tmdbSearch: (q: string, year: number | null, language: string) =>
    [...adminKeys.all, 'tmdb-search', q, year, language] as const,
  tmdbLookup: (tmdbId: number | null, imdbId: string) =>
    [...adminKeys.all, 'tmdb-lookup', tmdbId, imdbId] as const,
  artwork: (seriesId: string) => [...adminKeys.series(), 'artwork', seriesId] as const,
  /* Keyed by the series as well as the query: the endpoint is per-series, and a hit's ids are
   * only actionable against the series they were searched for. */
  discover: (seriesId: string, q: string, platforms = false) =>
    [...adminKeys.series(), 'discover', seriesId, q, platforms] as const,
  /* Read fresh every time it is opened: it is a statement about what is on disk right
   * now, and a cached one would invite approving a plan the filesystem has moved past. */
  renamePlan: (seriesId: string) => [...adminKeys.series(), 'rename-plan', seriesId] as const,
} as const

// --- Queue events ---------------------------------------------------------------------------

/** SSE endpoint. Same-origin, so the session cookie rides along without any token handling. */
export const ADMIN_EVENTS_URL = '/api/admin/events'

// --- Job vocabulary -------------------------------------------------------------------------

export const JOB_TYPES: readonly JobType[] = [
  'library_scan',
  'metadata_refresh',
  'image_cache',
  'thumbnail_sprite',
  'remote_import',
]

export const JOB_STATUSES: readonly JobStatus[] = [
  'queued',
  'running',
  'succeeded',
  'failed',
  'canceled',
]

/** The statuses a job can never leave — what the queue history lists. */
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = ['succeeded', 'failed', 'canceled']

export function isTerminalJob(status: JobStatus): boolean {
  return TERMINAL_JOB_STATUSES.includes(status)
}

/** Only a failed or canceled job can be re-queued; the backend enforces the same rule. */
export function isRetryableJob(status: JobStatus): boolean {
  return status === 'failed' || status === 'canceled'
}

export type Tone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'info'

export function jobStatusTone(status: JobStatus): Tone {
  switch (status) {
    case 'running':
      return 'accent'
    case 'succeeded':
      return 'success'
    case 'failed':
      return 'danger'
    case 'canceled':
      return 'warning'
    case 'queued':
      return 'neutral'
  }
}

/**
 * How much of the plan the admin has to check by hand.
 *
 * The thresholds are deliberately harsh: approving a folder of 58 files is a bulk act, so
 * anything under 0.9 is coloured to say "read the table before you press Import", and under
 * 0.7 it reads as a warning about the plan itself, not a decoration.
 */
export function importConfidenceTone(confidence: number): Tone {
  if (confidence >= 0.9) return 'success'
  if (confidence >= 0.7) return 'warning'
  return 'danger'
}

/**
 * i18n keys for an arbitrary thrown value, most specific first.
 *
 * `t()` takes an array and resolves the first key that exists, so a server error code we have
 * not localized yet degrades to the generic message instead of rendering `errors.SOME_CODE`
 * at the user.
 */
export function apiErrorKeys(error: unknown): string[] {
  if (error instanceof ApiRequestError) return [`errors.${error.code}`, 'errors.generic']
  return ['errors.network', 'errors.generic']
}

// --- Renaming on disk -------------------------------------------------------------------------
//
// The only part of this app that changes files on the NAS. The two calls mirror that: a plan is
// a pure read that shows what would happen — including the rows that will be skipped and why —
// and the run enqueues the job that does it. Nothing renames as a side effect of anything else.

export type RenameStatus = 'ok' | 'unchanged' | 'collision' | 'missing' | 'failed'

export interface RenameItem {
  mediaFileId: string
  currentPath: string
  /** Equal to `currentPath` when the file is already correctly named. */
  proposedPath: string
  status: RenameStatus
  /** Why this row will be left alone. Empty for the ones that will run. */
  reason: string
  seasonNumber: number | null
  episodeNumber: number | null
  sizeBytes: number
}

export interface RenamePlan {
  seriesId: string
  seriesTitle: string
  /** How many rows would actually move — the number the confirm button shows. */
  renamableCount: number
  items: RenameItem[]
}

export function getRenamePlan(seriesId: string): Promise<RenamePlan> {
  return api.get<RenamePlan>(`/library/series/${encodeURIComponent(seriesId)}/rename-plan`)
}

export function renameSeriesFiles(seriesId: string): Promise<JobRef> {
  return api.post<JobRef>(`/library/series/${encodeURIComponent(seriesId)}/rename`)
}
