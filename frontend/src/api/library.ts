/** Library, watch and me endpoints.
 *
 * One module per API area (§9). Everything the viewer pages need to read the catalogue and
 * write watch state lives here, so the pages themselves never touch `api` directly and every
 * cache key is defined in exactly one place.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query'
import { useCallback } from 'react'

import { api } from '@/api/client'
import type {
  ContinueWatchingItem,
  EpisodeDetail,
  EpisodeSummary,
  GenreFacet,
  Job,
  Page,
  SearchResults,
  SeriesDetail,
  SeriesStatus,
  SeriesSummary,
} from '@/api/types'

// --- Query vocabulary ----------------------------------------------------------------------

export const SERIES_SORTS = ['title', 'added', 'recent', 'progress'] as const
export type SeriesSort = (typeof SERIES_SORTS)[number]

export const SERIES_STATUSES = ['ongoing', 'completed', 'unknown'] as const

export function isSeriesSort(value: string | null | undefined): value is SeriesSort {
  return value != null && (SERIES_SORTS as readonly string[]).includes(value)
}

export function isSeriesStatus(value: string | null | undefined): value is SeriesStatus {
  return value != null && (SERIES_STATUSES as readonly string[]).includes(value)
}

export const DEFAULT_PAGE_SIZE = 36

export interface SeriesListParams {
  q?: string
  genre?: string
  status?: SeriesStatus
  language?: string
  sort?: SeriesSort
  availableOnly?: boolean
  page?: number
  pageSize?: number
}

/** Normalized so two equivalent filter states share one cache entry. */
function listQuery(params: SeriesListParams): Record<string, string | number | boolean | undefined> {
  return {
    q: params.q || undefined,
    genre: params.genre || undefined,
    status: params.status || undefined,
    language: params.language || undefined,
    sort: params.sort ?? 'title',
    // Only sent when on: a `false` in the URL is noise and defeats cache sharing.
    availableOnly: params.availableOnly ? true : undefined,
    page: params.page ?? 1,
    pageSize: params.pageSize ?? DEFAULT_PAGE_SIZE,
  }
}

// --- Cache keys ----------------------------------------------------------------------------

export const libraryKeys = {
  all: ['library'] as const,
  series: () => [...libraryKeys.all, 'series'] as const,
  seriesList: (params: SeriesListParams) => [...libraryKeys.series(), 'list', listQuery(params)] as const,
  seriesDetail: (idOrSlug: string) => [...libraryKeys.series(), 'detail', idOrSlug] as const,
  seriesEpisodes: (id: string) => [...libraryKeys.series(), 'episodes', id] as const,
  episode: (id: string) => [...libraryKeys.all, 'episode', id] as const,
  search: (q: string, limit: number) => [...libraryKeys.all, 'search', q, limit] as const,
  genres: () => [...libraryKeys.all, 'genres'] as const,
} as const

export const meKeys = {
  all: ['me'] as const,
  continueWatching: () => [...meKeys.all, 'continue-watching'] as const,
} as const

/** Watch state is denormalized into series, episode and continue-watching payloads alike, so
 *  any write invalidates all three rather than trying to patch caches by hand. */
async function invalidateWatchState(client: QueryClient): Promise<void> {
  await Promise.all([
    client.invalidateQueries({ queryKey: libraryKeys.all }),
    client.invalidateQueries({ queryKey: meKeys.all }),
  ])
}

/**
 * The same refresh, for callers that change watch state without going through a mutation
 * here — the player advancing to the next episode is the case that matters.
 *
 * It exists so those callers do not hand-write key arrays: bare roots like `['series']` or
 * `['continue-watching']` look plausible and silently match nothing, leaving Home and the
 * series page showing yesterday's progress.
 */
export function useInvalidateWatchState(): () => void {
  const client = useQueryClient()
  return useCallback(() => {
    void invalidateWatchState(client)
  }, [client])
}

// --- Reads ---------------------------------------------------------------------------------

export function useSeriesList(params: SeriesListParams) {
  return useQuery({
    queryKey: libraryKeys.seriesList(params),
    queryFn: () => api.get<Page<SeriesSummary>>('/library/series', listQuery(params)),
    // Paging and filtering keep the previous page on screen instead of flashing a spinner.
    placeholderData: keepPreviousData,
  })
}

export function useRecentlyAdded(limit = 20) {
  const params: SeriesListParams = { sort: 'added', page: 1, pageSize: limit }
  return useQuery({
    queryKey: libraryKeys.seriesList(params),
    queryFn: () => api.get<Page<SeriesSummary>>('/library/series', listQuery(params)),
  })
}

export function useSeriesDetail(idOrSlug: string | undefined) {
  return useQuery({
    queryKey: libraryKeys.seriesDetail(idOrSlug ?? ''),
    queryFn: () => api.get<SeriesDetail>(`/library/series/${encodeURIComponent(idOrSlug ?? '')}`),
    enabled: Boolean(idOrSlug),
  })
}

export function useSeriesEpisodes(seriesId: string | undefined) {
  return useQuery({
    queryKey: libraryKeys.seriesEpisodes(seriesId ?? ''),
    queryFn: () => api.get<EpisodeSummary[]>(`/library/series/${encodeURIComponent(seriesId ?? '')}/episodes`),
    enabled: Boolean(seriesId),
  })
}

/**
 * Episode detail, including `streamUrl` and the resume position.
 *
 * Never served stale: this payload is what the player points `<video>` at, and a cached
 * `streamUrl` or `resumePositionSeconds` from a previous visit would start the wrong file at
 * the wrong second.
 */
export function useEpisodeDetail(episodeId: string | undefined) {
  return useQuery({
    queryKey: libraryKeys.episode(episodeId ?? ''),
    queryFn: () => api.get<EpisodeDetail>(`/library/episodes/${encodeURIComponent(episodeId ?? '')}`),
    enabled: Boolean(episodeId),
    staleTime: 0,
  })
}

/** Multilingual search (§7). Callers debounce; a query under 2 characters is not sent. */
export function useSearch(query: string, limit = 30) {
  const trimmed = query.trim()
  return useQuery({
    queryKey: libraryKeys.search(trimmed, limit),
    queryFn: () => api.get<SearchResults>('/library/search', { q: trimmed, limit }),
    enabled: trimmed.length >= 2,
    placeholderData: keepPreviousData,
  })
}

export function useGenres() {
  return useQuery({
    queryKey: libraryKeys.genres(),
    queryFn: () => api.get<GenreFacet[]>('/library/genres'),
    staleTime: 5 * 60_000,
  })
}

export function useContinueWatching() {
  return useQuery({
    queryKey: meKeys.continueWatching(),
    queryFn: () => api.get<ContinueWatchingItem[]>('/me/continue-watching'),
  })
}

// --- Writes --------------------------------------------------------------------------------

export interface ProgressInput {
  episodeId: string
  positionSeconds: number
  durationSeconds: number
}

/** Called every 10s by the player, so it deliberately invalidates nothing — refetching the
 *  whole library on a heartbeat would be absurd. Lists pick the change up on their next fetch. */
export function useSetProgress() {
  return useMutation({
    mutationFn: ({ episodeId, positionSeconds, durationSeconds }: ProgressInput) =>
      api.post<void>(`/watch/${encodeURIComponent(episodeId)}/progress`, {
        positionSeconds,
        durationSeconds,
      }),
  })
}

export function useMarkEpisodeWatched() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (episodeId: string) => api.post<void>(`/watch/${encodeURIComponent(episodeId)}/complete`),
    onSuccess: () => invalidateWatchState(client),
  })
}

/** Clearing progress is also how an episode is marked unwatched — there is one endpoint. */
export function useClearEpisodeProgress() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (episodeId: string) => api.delete<void>(`/watch/${encodeURIComponent(episodeId)}/progress`),
    onSuccess: () => invalidateWatchState(client),
  })
}

export function useSetSeriesWatched() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ seriesId, watched }: { seriesId: string; watched: boolean }) =>
      api.post<void>(`/watch/series/${encodeURIComponent(seriesId)}/watched`, { watched }),
    onSuccess: () => invalidateWatchState(client),
  })
}

export function useRefreshSeriesMetadata() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (seriesId: string) =>
      api.post<Job>(`/library/series/${encodeURIComponent(seriesId)}/refresh`),
    onSuccess: () => client.invalidateQueries({ queryKey: libraryKeys.series() }),
  })
}

/** Admin affordance surfaced on an empty home page: index the NAS for the first time. */
export function useEnqueueScan() {
  return useMutation({
    mutationFn: (full: boolean) =>
      api.post<Job>('/admin/jobs', { type: 'library_scan', payload: { full }, priority: 100 }),
  })
}
