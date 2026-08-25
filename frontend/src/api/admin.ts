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
  EpisodeMediaFile,
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

/** The file record behind an episode. One wire object (`schemas/library.py:49`), so one type:
 *  this used to be declared here a second time, and the two drifted the moment the viewer side
 *  needed `sizeBytes`. */
export type AdminMediaFile = EpisodeMediaFile

/** Episode detail as the metadata editor sees it. The file record is already on the base. */
export type AdminEpisodeDetail = EpisodeDetail

export interface SeriesPatch {
  title?: string
  originalTitle?: string | null
  overview?: string | null
  year?: number | null
  status?: SeriesStatus
  genres?: string[]
  network?: string | null
  contentRating?: string | null
  /**
   * Adults only. Sent on its own from the toggle; deliberately not one of the metadata fields,
   * so classifying a series does not also freeze it against the nightly refresh.
   */
  restricted?: boolean
  /** Sent on its own to hand a series back to the metadata jobs. */
  metadataLocked?: boolean
  /** Sent on its own to let refreshes change the artwork again. */
  artworkLocked?: boolean
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

/** What a bulk edit actually did. `missing` names ids that were gone by the time it ran. */
export interface SeriesBulkResult {
  updated: number
  missing: string[]
}

/**
 * Apply one patch to many series.
 *
 * A single request rather than a loop of `patchSeries`, and the difference is not speed: forty
 * separate PATCHes can half-succeed, leaving the admin to work out which twenty landed. This
 * either applies to the whole selection or fails, and names the ids that no longer exist
 * instead of failing over them.
 *
 * Only the keys present in `patch` are written, which is the same contract as `patchSeries` and
 * matters far more here — an accidental `overview: null` would blank forty synopses.
 */
export function bulkPatchSeries(ids: string[], patch: SeriesPatch): Promise<SeriesBulkResult> {
  return api.post<SeriesBulkResult>('/library/series/bulk', { ids, patch })
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

// --- Artwork --------------------------------------------------------------------------------

/** Which slot an image fills. */
export type ArtworkKind = 'poster' | 'backdrop'

/** What was stored, and the address to render immediately. */
export interface ArtworkApplied {
  seriesId: string
  kind: string
  /** `DATA_DIR`-relative, as stored on the series row. */
  storedPath: string
  url: string | null
  /**
   * Always true, and reported rather than assumed: a chosen picture survives the next refresh.
   *
   * It says nothing about the metadata, and that separation is the point. The two used to be
   * one flag, so uploading a poster opted the series out of every future update — a decision
   * nobody made by picking an image.
   */
  artworkLocked: boolean
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
  /* Effectively static — it changes only when the server does — so the editor caches it hard
   * rather than refetching a constant on every visit to a series. */
  vocabulary: () => [...adminKeys.all, 'vocabulary'] as const,
  /* Keyed by the record being previewed as well as the series: previewing a *different*
     DramaDex title is how rebinding works, and each answer is only about the pair. */
  metadataPreview: (seriesId: string, dramadexId: number | null) =>
    [...adminKeys.series(), 'metadata-preview', seriesId, dramadexId] as const,
  dramadex: (q: string) => [...adminKeys.all, 'dramadex', q] as const,
  dramadexPerson: (id: number) => [...adminKeys.all, 'dramadex-person', id] as const,
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

// --- DramaDex -------------------------------------------------------------------------------
//
// The household's own indexer of DramaBox and ReelShort. It replaces the web-search panel and
// the TMDB match dialog for this library's genre, because unlike either of them it actually
// catalogues it — and it carries cast and platform links, which nothing else here ever had.

export interface DramadexHit {
  id: number
  title: string
  year: number | null
  episodeCount: number | null
  posterUrl: string | null
  synopsis: string | null
  studio: string | null
  watchUrl: string | null
  genres: string[]
  cast: string[]
  /**
   * True only when the normalised titles are equal.
   *
   * The distinction the whole panel rests on: a query the index cannot satisfy still returns
   * its nearest neighbours, so ranking alone would offer "Call to Arms, Call to Love" for
   * "The Call Boy I Met in Paris" with nothing to say it is a different show. The UI leads
   * with the row that *is* this series and presents the rest as neighbours.
   */
  matches: boolean
  inLibrary: boolean
  /**
   * Where it is, when `inLibrary`. Null otherwise.
   *
   * `inLibrary` alone was a claim with no address behind it, so the actor page rendered "Ya la
   * tenemos" over a card that led nowhere — which is worse than not saying it. The slug, not
   * the id, because the route is `/series/:slug`.
   */
  librarySlug: string | null
}

export interface DramadexHits {
  configured: boolean
  query: string
  items: DramadexHit[]
}

export interface DramadexStatus {
  configured: boolean
  reachable: boolean
  titles: number
  episodes: number
  people: number
  detail: string
}

export interface PersonCredits {
  id: number
  name: string
  bio: string | null
  /** This app's proxy path to the indexer's photograph, or null when it has none — which is
   *  about one credited performer in four. Never DramaDex's own path: its routes need an API
   *  key the browser must not hold. */
  photoUrl: string | null
  credits: DramadexHit[]
}

export function searchDramadex(q: string, limit = 20): Promise<DramadexHits> {
  return api.get<DramadexHits>('/library/dramadex/search', { q, limit })
}

/**
 * One field a DramaDex record would change, as the dialog shows it.
 *
 * Computed by the same server function that performs the write, which is the property the whole
 * dialog rests on: a preview that decided separately from the writer would disagree with it
 * eventually, and the disagreement is invisible — four boxes ticked, three edits made.
 */
export interface MetadataDiffRow {
  field: string
  /** `text` | `long_text` | `number` | `list` | `image`. `image` holds URLs. */
  kind: string
  /** Rendered values. `[]` means empty — never `['']`, which would draw a blank chip. */
  current: string[]
  incoming: string[]
  changed: boolean
  /** Ticked on open: changed, minus what the application will not presume on. */
  defaultChecked: boolean
  /** i18n key stem for a refusal or caveat, or null. */
  note: string | null
  noteArgs: Record<string, string>
}

/**
 * What "Actualizar metadata" would do, before it does any of it.
 *
 * Every not-ready state is a flag on a 200, never an error: not connected, not bound, and the
 * record having disappeared upstream are three ordinary situations with three different
 * repairs, and one red box for all of them makes the admin guess which.
 */
export interface MetadataPreview {
  configured: boolean
  bound: boolean
  found: boolean
  dramadexId: number | null
  dramadexTitle: string | null
  dramadexYear: number | null
  dramadexEpisodeCount: number | null
  watchUrl: string | null
  watchStudio: string | null
  rows: MetadataDiffRow[]
  changedCount: number
}

/** Read-only. Pass `dramadexId` to preview a different record than the one bound. */
export function metadataPreview(
  seriesId: string,
  dramadexId?: number | null,
): Promise<MetadataPreview> {
  return api.get<MetadataPreview>(
    `/library/series/${encodeURIComponent(seriesId)}/metadata/preview`,
    { dramadexId: dramadexId ?? undefined },
  )
}

/**
 * Write the rows the admin left ticked.
 *
 * `fields` comes straight from the preview they were looking at. Omitting it applies everything
 * that changed, which is what a plain refresh means.
 */
export function applyDramadex(
  seriesId: string,
  body: { dramadexId?: number | null; fields?: string[] } = {},
): Promise<SeriesDetail> {
  return api.post<SeriesDetail>(
    `/library/series/${encodeURIComponent(seriesId)}/metadata/dramadex`,
    { dramadexId: body.dramadexId ?? undefined, fields: body.fields },
  )
}

/**
 * Take a DramaDex title's key art.
 *
 * Sends the *title id*, not a URL: the indexer's poster route wants an API key in a header, so
 * the fetch happens on the server and that key never reaches a page.
 */
export function chooseDramadexArtwork(
  seriesId: string,
  titleId: number,
  kind: ArtworkKind,
): Promise<ArtworkApplied> {
  return api.post<ArtworkApplied>(
    `/library/series/${encodeURIComponent(seriesId)}/artwork/dramadex`,
    { titleId, kind },
  )
}

/** An actor and everything the indexer has them in. Any signed-in member may read this. */
export function dramadexPerson(personId: number): Promise<PersonCredits> {
  return api.get<PersonCredits>(`/library/dramadex/people/${personId}`)
}

/** Ask DramaDex who it is. Never throws for a bad key — that is a state of the form. */
export function testDramadex(): Promise<DramadexStatus> {
  return api.post<DramadexStatus>('/admin/dramadex/test')
}
