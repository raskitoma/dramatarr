/** Persists the watch position.
 *
 * `POST /api/watch/:id/progress` is idempotent, so the only real constraints are: report often
 * enough that a crash loses little, and never report a position the user did not actually
 * watch. Seeks are therefore excluded (the position is in flux) and so is a paused player at
 * zero, which is what an untouched page looks like and would wipe a stored resume point.
 *
 * The unload path uses `navigator.sendBeacon`: a normal fetch started during `pagehide` is
 * routinely cancelled, which is exactly the case — closing the tab mid-episode — where losing
 * the position is most noticeable.
 *
 * The player now advances between episodes without unmounting (it swaps the source on one
 * `<video>` so autoplay survives), so "the component went away" is no longer the signal that
 * an episode is over. `episodeId` changing is: the outgoing id is flushed before the counters
 * reset, and every send names its own episode rather than reading the current one.
 */

import { useMutation } from '@tanstack/react-query'
import { useCallback, useEffect, useRef } from 'react'

import { api } from '@/api/client'
import { recordLocal } from '@/offline/progress'

const API_BASE = '/api'
const REPORT_INTERVAL_MS = 10_000
/** Collapses bursts (pause/play drumming, a seek that lands as a pause) into one request. */
const MIN_GAP_MS = 1_500
/** Below this the position has not meaningfully moved and the write is pure noise. */
const MIN_DELTA_SECONDS = 1

export interface ProgressSnapshot {
  positionSeconds: number
  durationSeconds: number
}

interface ProgressWrite extends ProgressSnapshot {
  episodeId: string
}

export interface UseProgressReporterOptions {
  episodeId: string
  /** Only used to file the local copy under the right series when there is no server to tell. */
  seriesId: string
  playing: boolean
  /** True while the position is in flux — a real element seek *or* a drag in progress. */
  seeking: boolean
  /** Returns the live position, or null when there is nothing worth reporting yet. */
  getSnapshot: () => ProgressSnapshot | null
  enabled?: boolean
}

export interface ProgressReporter {
  /** Debounced report — safe to call on any interaction. */
  flush: () => void
}

export function useProgressReporter(options: UseProgressReporterOptions): ProgressReporter {
  const { episodeId, seriesId, playing, seeking, getSnapshot, enabled = true } = options

  const mutation = useMutation({
    mutationFn: ({ episodeId: target, ...payload }: ProgressWrite) =>
      api.post<void>(`/watch/${encodeURIComponent(target)}/progress`, payload),
    retry: false,
  })
  const { mutate } = mutation

  const snapshotRef = useRef(getSnapshot)
  snapshotRef.current = getSnapshot
  const seekingRef = useRef(seeking)
  seekingRef.current = seeking
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled
  const seriesIdRef = useRef(seriesId)
  seriesIdRef.current = seriesId
  const episodeIdRef = useRef(episodeId)
  episodeIdRef.current = episodeId

  const lastSentRef = useRef<{ position: number; at: number }>({ position: -1, at: 0 })
  const timerRef = useRef<number | null>(null)

  const collect = useCallback((): ProgressSnapshot | null => {
    if (!enabledRef.current) return null
    if (seekingRef.current) return null
    const snapshot = snapshotRef.current()
    if (!snapshot) return null
    const { positionSeconds, durationSeconds } = snapshot
    if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) return null
    if (!Number.isFinite(positionSeconds) || positionSeconds <= 0) return null
    if (Math.abs(positionSeconds - lastSentRef.current.position) < MIN_DELTA_SECONDS) return null
    return { positionSeconds, durationSeconds }
  }, [])

  const sendNow = useCallback(() => {
    const snapshot = collect()
    if (!snapshot) return
    lastSentRef.current = { position: snapshot.positionSeconds, at: Date.now() }
    // Local first, always. On a plane the call below simply fails, and it fails *silently* —
    // `retry: false` with no error handler, and `sendBeacon` reports nothing either. Writing
    // here first is what stops three weeks of viewing disappearing on landing.
    void recordLocal({
      episodeId: episodeIdRef.current,
      seriesId: seriesIdRef.current,
      positionSeconds: snapshot.positionSeconds,
      durationSeconds: snapshot.durationSeconds,
      completed: false,
    })
    mutate({ episodeId: episodeIdRef.current, ...snapshot })
  }, [collect, mutate])

  const flush = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    const elapsed = Date.now() - lastSentRef.current.at
    if (elapsed >= MIN_GAP_MS) {
      sendNow()
      return
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      sendNow()
    }, MIN_GAP_MS - elapsed)
  }, [sendNow])

  /**
   * Final write for `target`, taken synchronously.
   *
   * Named explicitly because the two callers — page unload and an episode change — both run at
   * a moment when `episodeId` is about to stop being the one the position belongs to.
   */
  const sendFinal = useCallback(
    (target: string) => {
      const snapshot = collect()
      if (!snapshot) return
      lastSentRef.current = { position: snapshot.positionSeconds, at: Date.now() }
      void recordLocal({
        episodeId: target,
        seriesId: seriesIdRef.current,
        positionSeconds: snapshot.positionSeconds,
        durationSeconds: snapshot.durationSeconds,
        completed: false,
      })
      const url = `${API_BASE}/watch/${encodeURIComponent(target)}/progress`
      if (typeof navigator.sendBeacon === 'function') {
        const body = new Blob([JSON.stringify(snapshot)], { type: 'application/json' })
        if (navigator.sendBeacon(url, body)) return
      }
      mutate({ episodeId: target, ...snapshot })
    },
    [collect, mutate],
  )

  const sendFinalRef = useRef(sendFinal)
  sendFinalRef.current = sendFinal

  // Heartbeat while playing.
  useEffect(() => {
    if (!playing || !enabled) return undefined
    const id = window.setInterval(sendNow, REPORT_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [playing, enabled, sendNow])

  // Every pause is a deliberate stop; capture it.
  const wasPlayingRef = useRef(playing)
  useEffect(() => {
    if (wasPlayingRef.current && !playing) flush()
    wasPlayingRef.current = playing
  }, [playing, flush])

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') sendFinalRef.current(episodeIdRef.current)
    }
    const onPageHide = () => sendFinalRef.current(episodeIdRef.current)
    document.addEventListener('visibilitychange', onVisibilityChange)
    window.addEventListener('pagehide', onPageHide)
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('pagehide', onPageHide)
    }
  }, [])

  /**
   * Close out an episode.
   *
   * The cleanup captures the id this effect ran for, so a source swap writes the last position
   * of the episode that just finished — not the one already playing. It also runs on unmount,
   * which covers a client-side route change (never fires `pagehide`). The dedupe watermark is
   * reset afterwards so the new episode's first seconds are not mistaken for "no movement".
   */
  useEffect(() => {
    const target = episodeId
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current)
        timerRef.current = null
      }
      sendFinalRef.current(target)
      lastSentRef.current = { position: -1, at: 0 }
    }
  }, [episodeId])

  return { flush }
}
