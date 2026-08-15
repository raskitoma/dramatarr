/** Imperative state for a native `<video>`.
 *
 * The element is the source of truth — this hook only mirrors it into React state and exposes
 * the handful of commands the controls need. Volume and mute survive reloads because the
 * element resets them on every navigation and nothing else remembers them for us.
 *
 * Two rules here exist because of how this library is actually watched — a 58-episode serial
 * of five-minute portrait episodes, on a phone:
 *
 *  1. `loadSource` swaps the file on the *same* element rather than mounting a new one. A
 *     media element that has played once under a real user gesture keeps that permission
 *     across a source change, so the next episode can start on its own; a freshly created
 *     element has no such history and `play()` is refused. Advancing must therefore never go
 *     through a route change that remounts the `<video>`.
 *  2. `play()` reports whether it was allowed. The old version swallowed the rejection, which
 *     is precisely how "it doesn't advance" becomes a bug with no symptom to chase.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

const VOLUME_STORAGE_KEY = 'dramatarr.player.volume'
const MUTED_STORAGE_KEY = 'dramatarr.player.muted'

/** `HTMLMediaElement.HAVE_METADATA` — duration and seeking are only usable from here up. */
const HAVE_METADATA = 1

/** Seeking exactly to `duration` fires `ended`; stop just short so End means "the end". */
const END_GUARD_SECONDS = 0.25

/** Safari on iOS refuses element fullscreen and only offers this on the video itself. */
interface LegacyFullscreenVideo {
  webkitEnterFullscreen?: () => void
}

export interface BufferedRange {
  start: number
  end: number
}

export interface PlayerState {
  /** Metadata has arrived, so duration and seeking are usable. */
  ready: boolean
  playing: boolean
  waiting: boolean
  seeking: boolean
  ended: boolean
  currentTime: number
  duration: number
  buffered: BufferedRange[]
  volume: number
  muted: boolean
  fullscreen: boolean
  failed: boolean
  /** Intrinsic frame size, 0 until metadata lands. Drives the portrait/landscape fit. */
  videoWidth: number
  videoHeight: number
}

export interface LoadSourceOptions {
  /** Position to open at. Applied at `loadedmetadata`, since the element ignores it before. */
  startAt?: number
  /** Defaults to true — the point of a source swap is that playback continues. */
  autoplay?: boolean
}

export interface PlayerActions {
  /** Resolves `false` when the browser refused; never rejects. */
  play: () => Promise<boolean>
  pause: () => void
  toggle: () => void
  seekTo: (seconds: number) => void
  seekBy: (deltaSeconds: number) => void
  seekToFraction: (fraction: number) => void
  setVolume: (value: number) => void
  adjustVolume: (delta: number) => void
  toggleMute: () => void
  toggleFullscreen: () => void
  reload: () => void
  /** Point the same element at a different file. Resolves `false` if playback was refused. */
  loadSource: (url: string, options?: LoadSourceOptions) => Promise<boolean>
}

export interface Player {
  /** Callback ref for the `<video>`; effects re-run when the node attaches. */
  attachVideo: (node: HTMLVideoElement | null) => void
  /** Callback ref for the element that should go fullscreen. */
  attachContainer: (node: HTMLElement | null) => void
  video: HTMLVideoElement | null
  state: PlayerState
  actions: PlayerActions
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function readStoredVolume(): number {
  try {
    const raw = localStorage.getItem(VOLUME_STORAGE_KEY)
    if (raw === null) return 1
    const parsed = Number.parseFloat(raw)
    return Number.isFinite(parsed) ? clamp(parsed, 0, 1) : 1
  } catch {
    return 1
  }
}

function readStoredMuted(): boolean {
  try {
    return localStorage.getItem(MUTED_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

function persist(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Preference simply will not survive a reload.
  }
}

function readDuration(video: HTMLVideoElement): number {
  return Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0
}

function readBuffered(video: HTMLVideoElement): BufferedRange[] {
  const ranges: BufferedRange[] = []
  const { buffered } = video
  for (let index = 0; index < buffered.length; index += 1) {
    ranges.push({ start: buffered.start(index), end: buffered.end(index) })
  }
  return ranges
}

/** Keeps a target inside the media, once there is a media to be inside of. */
function boundedTime(video: HTMLVideoElement, seconds: number): number {
  const duration = readDuration(video)
  if (duration <= 0) return Math.max(0, seconds)
  return clamp(seconds, 0, Math.max(0, duration - END_GUARD_SECONDS))
}

export function usePlayer(): Player {
  const [video, setVideo] = useState<HTMLVideoElement | null>(null)

  const [state, setState] = useState<PlayerState>(() => ({
    ready: false,
    playing: false,
    waiting: false,
    seeking: false,
    ended: false,
    currentTime: 0,
    duration: 0,
    buffered: [],
    volume: readStoredVolume(),
    muted: readStoredMuted(),
    fullscreen: false,
    failed: false,
    videoWidth: 0,
    videoHeight: 0,
  }))

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const containerRef = useRef<HTMLElement | null>(null)
  const stateRef = useRef(state)
  stateRef.current = state

  /**
   * A seek asked for before metadata existed.
   *
   * Assigning `currentTime` while `readyState` is 0 is silently dropped — duration is still
   * NaN and there is nothing to seek within — which is why "resume at 2:14" and "open the
   * next episode where I left it" used to land at zero. The target is held here and applied
   * the moment `loadedmetadata` fires.
   */
  const pendingSeekRef = useRef<number | null>(null)

  const patch = useCallback((next: Partial<PlayerState>) => {
    setState((previous) => ({ ...previous, ...next }))
  }, [])

  const attachVideo = useCallback((node: HTMLVideoElement | null) => {
    videoRef.current = node
    setVideo(node)
  }, [])

  const attachContainer = useCallback((node: HTMLElement | null) => {
    containerRef.current = node
  }, [])

  // --- element -> state -----------------------------------------------------------------

  useEffect(() => {
    if (!video) return undefined

    video.volume = stateRef.current.volume
    video.muted = stateRef.current.muted

    const onLoadedMetadata = () => {
      const pending = pendingSeekRef.current
      if (pending !== null) {
        pendingSeekRef.current = null
        video.currentTime = boundedTime(video, pending)
      }
      patch({
        ready: true,
        failed: false,
        duration: readDuration(video),
        currentTime: video.currentTime,
        videoWidth: video.videoWidth,
        videoHeight: video.videoHeight,
      })
    }
    const onDurationChange = () => patch({ duration: readDuration(video) })
    const onTimeUpdate = () => {
      patch({ currentTime: video.currentTime, buffered: readBuffered(video) })
    }
    const onProgress = () => patch({ buffered: readBuffered(video) })
    const onPlay = () => patch({ playing: true, ended: false, failed: false })
    const onPause = () => patch({ playing: false })
    const onPlaying = () => patch({ playing: true, waiting: false, ended: false })
    const onWaiting = () => patch({ waiting: true })
    const onCanPlay = () => patch({ waiting: false, ready: true })
    const onSeeking = () => patch({ seeking: true })
    const onSeeked = () => patch({ seeking: false, currentTime: video.currentTime })
    const onVolumeChange = () => patch({ volume: video.volume, muted: video.muted })
    const onEnded = () => patch({ playing: false, ended: true, currentTime: video.currentTime })
    const onError = () => patch({ failed: true, playing: false, waiting: false })
    const onLoadStart = () => patch({ failed: false, ready: false, ended: false })

    video.addEventListener('loadedmetadata', onLoadedMetadata)
    video.addEventListener('durationchange', onDurationChange)
    video.addEventListener('timeupdate', onTimeUpdate)
    video.addEventListener('progress', onProgress)
    video.addEventListener('play', onPlay)
    video.addEventListener('pause', onPause)
    video.addEventListener('playing', onPlaying)
    video.addEventListener('waiting', onWaiting)
    video.addEventListener('canplay', onCanPlay)
    video.addEventListener('seeking', onSeeking)
    video.addEventListener('seeked', onSeeked)
    video.addEventListener('volumechange', onVolumeChange)
    video.addEventListener('ended', onEnded)
    video.addEventListener('error', onError)
    video.addEventListener('loadstart', onLoadStart)

    // The element may already have metadata by the time we attach (cached response).
    if (video.readyState >= HAVE_METADATA) onLoadedMetadata()

    return () => {
      video.removeEventListener('loadedmetadata', onLoadedMetadata)
      video.removeEventListener('durationchange', onDurationChange)
      video.removeEventListener('timeupdate', onTimeUpdate)
      video.removeEventListener('progress', onProgress)
      video.removeEventListener('play', onPlay)
      video.removeEventListener('pause', onPause)
      video.removeEventListener('playing', onPlaying)
      video.removeEventListener('waiting', onWaiting)
      video.removeEventListener('canplay', onCanPlay)
      video.removeEventListener('seeking', onSeeking)
      video.removeEventListener('seeked', onSeeked)
      video.removeEventListener('volumechange', onVolumeChange)
      video.removeEventListener('ended', onEnded)
      video.removeEventListener('error', onError)
      video.removeEventListener('loadstart', onLoadStart)
    }
  }, [video, patch])

  useEffect(() => {
    const onFullscreenChange = () => {
      const element = containerRef.current
      patch({ fullscreen: element !== null && document.fullscreenElement === element })
    }
    document.addEventListener('fullscreenchange', onFullscreenChange)
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange)
  }, [patch])

  // --- state -> element -----------------------------------------------------------------

  const play = useCallback(async (): Promise<boolean> => {
    const element = videoRef.current
    if (!element) return false
    try {
      await element.play()
      return true
    } catch {
      // Refused (no user activation) or interrupted by a newer load. Either way the caller
      // has to be able to say so on screen, so the answer is returned rather than dropped.
      return false
    }
  }, [])

  const pause = useCallback(() => {
    videoRef.current?.pause()
  }, [])

  const toggle = useCallback(() => {
    const element = videoRef.current
    if (!element) return
    if (element.paused || element.ended) void play()
    else element.pause()
  }, [play])

  const seekTo = useCallback((seconds: number) => {
    const element = videoRef.current
    if (!element) return
    const target = Math.max(0, Number.isFinite(seconds) ? seconds : 0)
    if (element.readyState < HAVE_METADATA) {
      pendingSeekRef.current = target
      setState((previous) => ({ ...previous, currentTime: target }))
      return
    }
    const bounded = boundedTime(element, target)
    element.currentTime = bounded
    setState((previous) => ({ ...previous, currentTime: bounded }))
  }, [])

  const seekBy = useCallback(
    (deltaSeconds: number) => {
      const element = videoRef.current
      if (!element) return
      const base = pendingSeekRef.current ?? element.currentTime
      seekTo(base + deltaSeconds)
    },
    [seekTo],
  )

  const seekToFraction = useCallback(
    (fraction: number) => {
      const element = videoRef.current
      if (!element) return
      const duration = readDuration(element)
      if (duration <= 0) return
      seekTo(duration * clamp(fraction, 0, 1))
    },
    [seekTo],
  )

  const setVolume = useCallback((value: number) => {
    const next = clamp(value, 0, 1)
    const element = videoRef.current
    if (element) {
      element.volume = next
      // Nudging the slider up from silence is an unmute request, not a volume change.
      if (next > 0 && element.muted) {
        element.muted = false
        persist(MUTED_STORAGE_KEY, 'false')
      }
    }
    persist(VOLUME_STORAGE_KEY, String(next))
    setState((previous) => ({ ...previous, volume: next, muted: next > 0 ? false : previous.muted }))
  }, [])

  const adjustVolume = useCallback(
    (delta: number) => {
      setVolume((videoRef.current?.volume ?? stateRef.current.volume) + delta)
    },
    [setVolume],
  )

  const toggleMute = useCallback(() => {
    const element = videoRef.current
    const next = !(element ? element.muted : stateRef.current.muted)
    if (element) element.muted = next
    persist(MUTED_STORAGE_KEY, String(next))
    setState((previous) => ({ ...previous, muted: next }))
  }, [])

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(() => {
        // Nothing useful to do; the button simply had no effect.
      })
      return
    }
    const container = containerRef.current
    if (container?.requestFullscreen) {
      void container.requestFullscreen().catch(() => {
        // Some browsers refuse outside a user gesture chain.
      })
      return
    }
    const element = videoRef.current as (HTMLVideoElement & LegacyFullscreenVideo) | null
    element?.webkitEnterFullscreen?.()
  }, [])

  const reload = useCallback(() => {
    const element = videoRef.current
    if (!element) return
    pendingSeekRef.current = stateRef.current.currentTime > 0 ? stateRef.current.currentTime : null
    setState((previous) => ({ ...previous, failed: false, ready: false }))
    element.load()
    void play()
  }, [play])

  const loadSource = useCallback(
    async (url: string, loadOptions: LoadSourceOptions = {}): Promise<boolean> => {
      const element = videoRef.current
      if (!element) return false
      const startAt = Math.max(0, loadOptions.startAt ?? 0)
      pendingSeekRef.current = startAt > 0 ? startAt : null

      element.src = url
      element.load()
      setState((previous) => ({
        ...previous,
        ready: false,
        playing: false,
        waiting: true,
        seeking: false,
        ended: false,
        failed: false,
        currentTime: startAt,
        duration: 0,
        buffered: [],
        videoWidth: 0,
        videoHeight: 0,
      }))

      if (loadOptions.autoplay === false) return false
      // Permitted without a fresh gesture: this element has already played under one.
      return play()
    },
    [play],
  )

  const actions = useMemo<PlayerActions>(
    () => ({
      play,
      pause,
      toggle,
      seekTo,
      seekBy,
      seekToFraction,
      setVolume,
      adjustVolume,
      toggleMute,
      toggleFullscreen,
      reload,
      loadSource,
    }),
    [
      play,
      pause,
      toggle,
      seekTo,
      seekBy,
      seekToFraction,
      setVolume,
      adjustVolume,
      toggleMute,
      toggleFullscreen,
      reload,
      loadSource,
    ],
  )

  return { attachVideo, attachContainer, video, state, actions }
}
