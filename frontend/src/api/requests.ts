/** The wishlist — `/api/requests` for members, `/api/admin/requests` for the admin.
 *
 * One module for both halves even though the API splits them, because they are two views of
 * one table and the cache has to know that: an admin answering a request must invalidate the
 * member-facing list too, or the person who asked keeps seeing "pending" until they reload.
 * Splitting this file would put those two facts in two places.
 *
 * Nothing here downloads anything, and the UI should not suggest otherwise. A request is a
 * note for the admin to act on by hand — every string in `@/i18n` that describes this feature
 * was written to say "ask", never "get".
 */

import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { Page } from '@/api/types'

// --- Wire shapes ------------------------------------------------------------------------------

export const REQUEST_STATUSES = ['pending', 'approved', 'fulfilled', 'declined'] as const
export type RequestStatus = (typeof REQUEST_STATUSES)[number]

/** The statuses that still want the admin's attention. Drives the default filter. */
export const OPEN_REQUEST_STATUSES: readonly RequestStatus[] = ['pending', 'approved']

export function isRequestStatus(value: string | null | undefined): value is RequestStatus {
  return value != null && (REQUEST_STATUSES as readonly string[]).includes(value)
}

/**
 * Every identifier a candidate's URL carried.
 *
 * All optional, and a card with none of them is still a card — it is a link to a page the
 * admin will read. The two that matter downstream are `tmdbId`, which makes the admin's later
 * match one click, and `dramaboxdbId`/`mydramalistId`, which are frequently the only address a
 * short-form drama has anywhere.
 */
export interface RequestIds {
  tmdbId?: number | null
  tmdbKind?: 'tv' | 'movie' | null
  imdbId?: string | null
  mydramalistId?: string | null
  dramaboxdbId?: string | null
  dramaboxdbSlug?: string | null
  wikipediaLang?: string | null
  wikipediaTitle?: string | null
  wikidataId?: string | null
}

export interface RequestCandidate {
  /** Stable identity, matching the row the request will become. Used as the React key. */
  key: string
  title: string
  year: number | null
  /** Always TMDB's CDN or null — the server allowlists the host. */
  posterUrl: string | null
  sourceUrl: string | null
  /** The host the card came from, always shown: it is how a member judges between two hits. */
  provider: string
  /** The engine's extract. Display only; never stored on the request. */
  snippet: string
  /** Set when the card is here because of somebody in the cast rather than the title. Empty
   *  for a title match, which is what lets the card say why it is on screen. */
  matchedPerson: string
  ids: RequestIds
  inLibrary: boolean
  inLibrarySlug: string | null
  requested: boolean
  requestStatus: RequestStatus | null
}

export interface RequestSearchResults {
  query: string
  /** Which sources answered. `dramadex` or nothing — it is the only one left. */
  providers: string[]
  /**
   * False when neither provider is configured. Not an error and not a dead end — asking by
   * name works with no provider at all, so the page offers that instead of an empty screen.
   */
  configured: boolean
  results: RequestCandidate[]
}

export interface RequestSeriesRef {
  id: string
  slug: string
  title: string
}

export interface SeriesRequest {
  id: string
  title: string
  year: number | null
  posterUrl: string | null
  sourceUrl: string | null
  provider: string
  ids: RequestIds
  note: string | null
  status: RequestStatus
  adminNote: string | null
  series: RequestSeriesRef | null
  createdAt: string
  updatedAt: string
  resolvedAt: string | null
  /** True when the POST reached a row that already existed rather than writing a new one. */
  alreadyExisted: boolean
}

export interface RequestRequester {
  id: string
  displayName: string
  email: string
  avatarUrl: string | null
}

export interface AdminSeriesRequest extends SeriesRequest {
  requester: RequestRequester
  resolvedByName: string | null
  /** How many *other* members asked for the same thing — the queue's priority signal. */
  alsoRequestedBy: number
}

export interface RequestCounts {
  pending: number
  approved: number
  fulfilled: number
  declined: number
  total: number
}

export interface RequestInput {
  title: string
  year?: number | null
  posterUrl?: string | null
  sourceUrl?: string | null
  ids?: RequestIds
  note?: string | null
}

export interface RequestPatch {
  status?: RequestStatus
  adminNote?: string | null
  /** `''` clears the link; an unknown id is refused with `SERIES_NOT_FOUND`. */
  seriesId?: string | null
}

// --- Cache keys -------------------------------------------------------------------------------

export const requestKeys = {
  all: ['requests'] as const,
  search: (q: string, shortDrama = false) =>
    [...requestKeys.all, 'search', q, shortDrama] as const,
  mine: () => [...requestKeys.all, 'mine'] as const,
  admin: () => [...requestKeys.all, 'admin'] as const,
  adminList: (status: RequestStatus | '', page: number) =>
    [...requestKeys.admin(), 'list', status, page] as const,
  counts: () => [...requestKeys.admin(), 'counts'] as const,
}

/**
 * Everything a write to this table can invalidate.
 *
 * Deliberately broad, and cheap: these are four small queries in a household of six people.
 * Being precise here would buy nothing and would eventually leave one of the four stale — the
 * admin's count badge is the one that would go unnoticed longest.
 */
async function invalidateRequests(client: QueryClient): Promise<void> {
  await client.invalidateQueries({ queryKey: requestKeys.all })
  // The dashboard tile reads the pending count out of `/admin/stats`, which lives under the
  // admin key space rather than this one.
  await client.invalidateQueries({ queryKey: ['admin', 'stats'] })
}

// --- Member --------------------------------------------------------------------------------

const MIN_QUERY_LENGTH = 2

export function useRequestSearch(query: string, shortDrama = false) {
  const trimmed = query.trim()
  return useQuery({
    queryKey: requestKeys.search(trimmed, shortDrama),
    queryFn: () =>
      api.get<RequestSearchResults>('/requests/search', {
        q: trimmed,
        // Omitted rather than sent false, so the default mode keeps its existing cache key
        // and its existing URL.
        shortDrama: shortDrama || undefined,
      }),
    enabled: trimmed.length >= MIN_QUERY_LENGTH,
    // The indexer answers this over the network; re-running it because a component remounted
    // would cost a member a second wait for an answer that has not changed.
    staleTime: 60_000,
  })
}

export function useMyRequests() {
  return useQuery({
    queryKey: requestKeys.mine(),
    queryFn: () => api.get<SeriesRequest[]>('/requests'),
  })
}

export function useCreateRequest() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: RequestInput) => api.post<SeriesRequest>('/requests', body),
    onSuccess: () => invalidateRequests(client),
  })
}

export function useCancelRequest() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/requests/${encodeURIComponent(id)}`),
    onSuccess: () => invalidateRequests(client),
  })
}

/** Turns a chosen card into the body the create endpoint takes. */
export function requestFromCandidate(
  candidate: RequestCandidate,
  note?: string | null,
): RequestInput {
  return {
    title: candidate.title,
    year: candidate.year,
    posterUrl: candidate.posterUrl,
    sourceUrl: candidate.sourceUrl,
    ids: candidate.ids,
    note: note?.trim() || null,
  }
}

// --- Admin ---------------------------------------------------------------------------------

export interface AdminRequestQuery {
  status?: RequestStatus | ''
  page?: number
  pageSize?: number
}

export function useAdminRequests(params: AdminRequestQuery = {}) {
  const status = params.status ?? ''
  const page = params.page ?? 1
  return useQuery({
    queryKey: requestKeys.adminList(status, page),
    queryFn: () =>
      api.get<Page<AdminSeriesRequest>>('/admin/requests', {
        status: status || undefined,
        page,
        pageSize: params.pageSize ?? 25,
      }),
  })
}

export function useRequestCounts() {
  return useQuery({
    queryKey: requestKeys.counts(),
    queryFn: () => api.get<RequestCounts>('/admin/requests/counts'),
  })
}

export function usePatchRequest() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: RequestPatch }) =>
      api.patch<AdminSeriesRequest>(`/admin/requests/${encodeURIComponent(id)}`, patch),
    onSuccess: () => invalidateRequests(client),
  })
}

export interface RequestFolder {
  /** Library-relative, which is what you navigate to on the share. */
  path: string
  /** False when the directory was already there — not an error. */
  created: boolean
}

/**
 * Create the destination directory for a request, named the way the scanner reads it.
 *
 * The only wishlist call that touches the library mount, and all it does is `mkdir`. Its value
 * is the *name*: a folder called `Fruit Dimension Orange Light Redemption (2025)` scans into a
 * series with that title and that year, so there is nothing to fix in the editor afterwards
 * and nothing for the renamer to correct. A folder typed by hand almost never does that.
 */
export function usePrepareFolder() {
  return useMutation({
    mutationFn: (id: string) =>
      api.post<RequestFolder>(`/admin/requests/${encodeURIComponent(id)}/folder`),
  })
}

export function useDeleteRequest() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/admin/requests/${encodeURIComponent(id)}`),
    onSuccess: () => invalidateRequests(client),
  })
}

// --- Display helpers -------------------------------------------------------------------------

/** i18n key for a status chip. One place, so the member's and the admin's screens agree. */
export function requestStatusKey(status: RequestStatus): string {
  return `requests.status.${status}`
}

/**
 * The external links a request or candidate carries, as label + href pairs.
 *
 * These are leads for the admin to open, never anything this app fetches. `mydramalist` and
 * `dramaboxdb` are included precisely because dramatarr cannot resolve them: for a lot of this
 * library they are the only page that describes the show at all.
 */
export function externalLinks(ids: RequestIds): { label: string; href: string }[] {
  const out: { label: string; href: string }[] = []
  if (ids.tmdbId) {
    const kind = ids.tmdbKind === 'movie' ? 'movie' : 'tv'
    out.push({ label: 'TMDB', href: `https://www.themoviedb.org/${kind}/${ids.tmdbId}` })
  }
  if (ids.imdbId) {
    out.push({ label: 'IMDb', href: `https://www.imdb.com/title/${ids.imdbId}/` })
  }
  if (ids.mydramalistId) {
    out.push({ label: 'MyDramaList', href: `https://mydramalist.com/${ids.mydramalistId}` })
  }
  if (ids.dramaboxdbId) {
    const slug = ids.dramaboxdbSlug ? `/${ids.dramaboxdbSlug}` : ''
    out.push({ label: 'DramaBoxDB', href: `https://www.dramaboxdb.com/movie/${ids.dramaboxdbId}${slug}` })
  }
  if (ids.wikipediaTitle) {
    const lang = ids.wikipediaLang || 'en'
    const page = encodeURIComponent(ids.wikipediaTitle.replace(/ /g, '_'))
    out.push({ label: 'Wikipedia', href: `https://${lang}.wikipedia.org/wiki/${page}` })
  }
  return out
}
