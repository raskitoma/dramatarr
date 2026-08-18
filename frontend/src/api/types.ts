/** Wire types. These mirror the backend Pydantic schemas exactly — camelCase, ISO-8601 dates. */

export type Role = 'admin' | 'viewer'
export type ThemePreference = 'auto' | 'light' | 'dark'
export type SeriesStatus = 'ongoing' | 'completed' | 'unknown'
export type MediaStatus = 'present' | 'missing' | 'error'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled'
export type JobType =
  | 'library_scan'
  | 'metadata_refresh'
  | 'image_cache'
  | 'thumbnail_sprite'
  | 'remote_import'

export interface ApiError {
  code: string
  message: string
  details: Record<string, unknown>
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  pageSize: number
}

// --- Identity ------------------------------------------------------------------------------

export interface User {
  id: string
  email: string
  displayName: string
  role: Role
  isActive: boolean
  locale: string
  theme: ThemePreference
  preferredAudioLang: string | null
  preferredSubtitleLang: string | null
  autoplayNext: boolean
  avatarColor: string
  /**
   * Cached profile picture under `/data`, or null when the member has never uploaded one.
   *
   * Null is the normal state, not a degraded one: `avatarColor` plus initials is a complete
   * treatment on its own, so every avatar site renders that fallback rather than a broken
   * image or an empty circle. Optional because a server that predates profile pictures simply
   * omits the field — read it through `avatarUrlOf` rather than dereferencing it directly.
   */
  avatarUrl?: string | null
  /**
   * The founding account, which the API refuses to demote, deactivate or delete.
   *
   * It exists so a household can never lock itself out of its own server: the first user is
   * created from the console (`app/cli.py`) and only the console can change it back. The admin
   * screens render its controls disabled with that reason attached — see `isProtectedUser`.
   */
  isProtected?: boolean
  createdAt: string
  lastLoginAt: string | null
}

/**
 * Whether this account is the protected founding one.
 *
 * A server that does not send the flag has no protected account to report, which is exactly
 * what `false` means here — the UI then behaves as it always did.
 */
export function isProtectedUser(user: Pick<User, 'isProtected'>): boolean {
  return user.isProtected === true
}

/** The profile picture, normalized to `string | null` whether the field was sent or not. */
export function avatarUrlOf(user: Pick<User, 'avatarUrl'>): string | null {
  return user.avatarUrl ?? null
}

export interface Passkey {
  id: string
  nickname: string
  createdAt: string
  lastUsedAt: string | null
  backedUp: boolean
  transports: string[]
}

export interface AuthSessionInfo {
  id: string
  userAgent: string | null
  ip: string | null
  createdAt: string
  lastSeenAt: string
  expiresAt: string
  current: boolean
}

export interface Invite {
  id: string
  email: string
  role: Role
  expiresAt: string
  acceptedAt: string | null
  revokedAt: string | null
  createdAt: string
  createdByName: string | null
  status: 'pending' | 'accepted' | 'revoked' | 'expired'
}

export interface InviteCreated extends Invite {
  enrollmentUrl: string
  emailSent: boolean
}

/** Public bootstrap payload the login page reads before any authentication. */
export interface PublicConfig {
  siteName: string
  defaultLocale: string
  magicLinkEnabled: boolean
  /** Whether the household has configured Google Sign-In. Only then is the button shown. */
  googleEnabled: boolean
  hasUsers: boolean
}

/**
 * Which ways the signed-in user can get back into their own account (`GET /auth/methods`).
 *
 * The account screen needs all three at once to answer "could I still get in?", which is why
 * this is one payload rather than three queries. `googleEmail` is the address Google returned
 * when the link was made — shown so the user can tell which of their Google accounts it is.
 */
export interface SignInMethods {
  passkeys: number
  googleLinked: boolean
  googleEmail: string | null
  magicLinkEnabled: boolean
}

// --- Library -------------------------------------------------------------------------------

export interface SeriesSummary {
  id: string
  slug: string
  title: string
  year: number | null
  status: SeriesStatus
  posterUrl: string | null
  genres: string[]
  /** Episodes with a playable file present. */
  availableEpisodeCount: number
  /** Episodes the metadata says exist, which may exceed what is on disk. */
  declaredEpisodeCount: number | null
  watchedEpisodeCount: number
  progressPercent: number
  /**
   * The DramaDex title this series is bound to, or null when it is bound to none.
   *
   * On the summary because the admin table has a column for it. It used to be declared only on
   * `AdminSeriesRow`, as optional, while the list payload never carried it — so the column read
   * `undefined` for every row and rendered "Desconocido" across a library where all eighteen
   * series are bound. A field the screen asks about and the payload does not answer is not a
   * missing feature; it is the screen asserting something false.
   */
  dramadexId: number | null
}

export interface SeriesTitleOut {
  id: string
  language: string
  title: string
  kind: 'official' | 'native' | 'romanization' | 'alternate' | 'acronym'
  source: 'tmdb' | 'manual' | 'filename'
}

/**
 * Where a reused overview was taken from.
 *
 * Reusing an encyclopedia's prose is only acceptable while the reader can see whose prose it
 * is, so the attribution is stored next to the text rather than reconstructed at render time:
 * the page as it was titled when it was copied, its address, and the language edition it came
 * from — `es.wikipedia.org` and `ko.wikipedia.org` are different articles about the same show.
 */
export interface OverviewSource {
  /** `wikipedia` today; the field exists so a second encyclopedia would not need a new shape. */
  provider: string
  title: string
  url: string
  /** The language edition, as its subdomain names it: `es`, `ko`, `zh`. */
  lang: string
}

/**
 * The provenance columns as the server actually serves them: three flat, independently
 * nullable fields, mirroring the three columns on the row. `overviewSource` alone is
 * meaningful — `"tmdb"` carries no link worth rendering — so the pair is not collapsed into a
 * single nullable object on the wire.
 */
export interface OverviewProvenance {
  /** `"tmdb" | "wikipedia" | "manual"`, or null on rows written before provenance existed. */
  overviewSource?: string | null
  /** The attribution link. Present for Wikipedia, where the licence requires it. */
  overviewSourceUrl?: string | null
  /** The language of the *text*, which need not be the household's. */
  overviewSourceLang?: string | null
}

/** One credited performer, cached from DramaDex when the series was bound to it. */
export interface CastMember {
  /** The indexer's person id — what makes the name a link to everything else they are in. */
  dramadexPersonId: number
  name: string
  role: string | null
  billingOrder: number | null
}

/** "Watch on ReelShort". The URL is built from the platform's id, so it survives a retitle. */
export interface SeriesLink {
  studioKey: string
  studioName: string
  url: string
}

export interface SeriesDetail extends SeriesSummary, OverviewProvenance {
  cast?: CastMember[]
  links?: SeriesLink[]
  overview: string | null
  originalTitle: string | null
  originalLanguage: string | null
  backdropUrl: string | null
  network: string | null
  contentRating: string | null
  tmdbId: number | null
  imdbId: string | null
  metadataLocked: boolean
  libraryPath: string
  addedAt: string
  titles: SeriesTitleOut[]
  seasons: SeasonGroup[]
  /** The episode a Play/Resume button should start with. Null when nothing is playable. */
  nextEpisode: EpisodeSummary | null
  /**
   * The titles either side of this one in the catalogue's A–Z order — Explorar's default.
   *
   * Sent with the detail rather than fetched, the same way `previousEpisodeId`/`nextEpisodeId`
   * ride along on an episode. That is what makes the arrows correct on a cold load of a pasted
   * link, where there is no list to walk, and it is why the series page needs no second query.
   *
   * The order is the catalogue's own, never the list the viewer arrived through: a series is
   * reachable from home, browse, search and the wishlist and the URL is identical in all four.
   * Null at each end — the control disables rather than disappears.
   */
  /** Set when an admin picked the artwork by hand. Refreshes leave that picture alone. */
  artworkLocked: boolean
  previousSeriesSlug: string | null
  previousSeriesTitle: string | null
  nextSeriesSlug: string | null
  nextSeriesTitle: string | null
}

export interface SeasonGroup {
  seasonNumber: number
  episodes: EpisodeSummary[]
}

export interface EpisodeSummary {
  id: string
  seasonNumber: number
  episodeNumber: number
  absoluteNumber: number | null
  title: string | null
  stillUrl: string | null
  airDate: string | null
  runtimeSeconds: number | null
  available: boolean
  directPlayOk: boolean
  watched: boolean
  inProgress: boolean
  percent: number
  /** Null when the UI should not offer Resume (under 15s in, or already finished). */
  resumePositionSeconds: number | null
  playCount: number
}

export interface AudioTrackOut {
  id: string
  streamIndex: number
  language: string | null
  title: string | null
  codec: string | null
  channels: number | null
  isDefault: boolean
}

export interface SubtitleTrackOut {
  id: string
  language: string | null
  title: string | null
  codec: string | null
  isDefault: boolean
  isForced: boolean
  /** URL to the WebVTT rendition; extraction happens on first request. */
  url: string
}

export interface EpisodeDetail extends EpisodeSummary {
  overview: string | null
  seriesId: string
  seriesTitle: string
  seriesSlug: string
  metadataLocked: boolean
  durationSeconds: number | null
  width: number | null
  height: number | null
  videoCodec: string | null
  audioCodec: string | null
  container: string | null
  streamUrl: string
  thumbnailsUrl: string | null
  audioTracks: AudioTrackOut[]
  subtitleTracks: SubtitleTrackOut[]
  nextEpisodeId: string | null
  previousEpisodeId: string | null
}

export interface ContinueWatchingItem {
  series: SeriesSummary
  episode: EpisodeSummary
  lastPlayedAt: string
}

export interface GenreFacet {
  genre: string
  count: number
}

// --- Search --------------------------------------------------------------------------------

export interface SeriesHit {
  series: SeriesSummary
  score: number
  /** The stored title variant that matched — lets the UI explain a cross-language hit. */
  matchedTitle: string
  matchedLanguage: string
  matchedField: 'title' | 'alternate' | 'romanization' | 'overview' | 'episode'
}

export interface EpisodeHit {
  episode: EpisodeSummary
  seriesId: string
  /** The series' slug, which is what `/series/:slug` actually takes. On the wire all along —
   *  `schemas/search.py` declares `series_slug` non-optional — and simply missing here, so
   *  every caller reached for `seriesId` and built a link that worked by accident. */
  seriesSlug: string
  seriesTitle: string
  score: number
  matchedTitle: string
}

export interface SearchResults {
  query: string
  series: SeriesHit[]
  episodes: EpisodeHit[]
  tookMs: number
}

// --- Jobs / admin --------------------------------------------------------------------------

export interface Job {
  id: string
  type: JobType
  status: JobStatus
  priority: number
  payload: Record<string, unknown>
  progressPercent: number
  progressMessage: string
  attempts: number
  maxAttempts: number
  error: string | null
  cancelRequested: boolean
  seriesId: string | null
  seriesTitle: string | null
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
}

export interface JobLogEntry {
  id: string
  level: 'debug' | 'info' | 'warning' | 'error'
  message: string
  at: string
}

export interface JobDetail extends Job {
  logs: JobLogEntry[]
}

export interface QueueState {
  paused: boolean
  running: boolean
  queuedCount: number
  current: Job | null
}

export interface UnmatchedFile {
  id: string
  path: string
  sizeBytes: number
  container: string | null
  durationSeconds: number | null
  scanError: string | null
  firstSeenAt: string
}

export interface AdminStats {
  seriesCount: number
  episodeCount: number
  availableEpisodeCount: number
  unmatchedCount: number
  userCount: number
  jobsByStatus: Record<JobStatus, number>
  dataDirBytes: number
  libraryBytes: number
}

/** Secrets read back as the sentinel `__SET__`; sending it unchanged leaves them untouched. */
export const SECRET_SENTINEL = '__SET__'

export interface SystemSettings {
  /** DramaDex — the household's indexer, and the only metadata source. */
  'dramadex.base_url': string
  'dramadex.api_key': string
  'smtp.host': string
  'smtp.port': number
  'smtp.username': string
  'smtp.password': string
  'smtp.from_email': string
  'smtp.from_name': string
  'smtp.use_tls': boolean
  'smtp.use_ssl': boolean
  'site.name': string
  'site.default_locale': string
  'library.scan_cron': string
  'library.source_base_url': string
  'queue.paused': boolean
  'auth.magic_link_enabled': boolean
  'auth.invite_ttl_hours': number
  'oauth.google.enabled': boolean
  'oauth.google.client_id': string
  /** Secret: reads back as `SECRET_SENTINEL` once stored, never as the value itself. */
  'oauth.google.client_secret': string
  'oauth.google.allow_invites': boolean
  /**
   * Read-only, server-derived: what this deployment will actually send Google, built from
   * `DRAMATARR_PUBLIC_URL`. These are the strings that must be pasted into Google Cloud
   * Console, so they cannot be guessed from `window.location.origin` — behind a reverse proxy
   * or on a LAN address the browser's origin is not the one Google will see. Sending them
   * back on a save is a no-op; the server ignores keys it does not own.
   */
  'derived.google.redirect_uri': string
  'derived.google.js_origin': string
}

// --- SSE -----------------------------------------------------------------------------------

export type QueueEventName =
  | 'job.created'
  | 'job.started'
  | 'job.progress'
  | 'job.finished'
  | 'queue.state'

export interface QueueEvent {
  event: QueueEventName
  data: Job | QueueState
}
