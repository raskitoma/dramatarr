/** Custom controls over a native `<video>`.
 *
 * No player library: the file is direct-played from `/api/stream/:id` with range requests, so
 * the browser's own media stack already does everything a library would wrap. What is left is
 * the chrome — and that has to be ours to be localized, tokenized and keyboard-complete.
 *
 * The shape of this component is dictated by what the library actually holds: a serial of
 * 58 five-minute portrait episodes, watched back to back on a phone. Two consequences run
 * through everything below.
 *
 * **The element is never remounted.** Advancing to the next episode swaps `src` on the same
 * `<video>` (see `usePlayer.loadSource`) and the *page* only rewrites its URL. A route change
 * would build a new element, and a new element has no user-activation history, so `play()`
 * would be refused and the serial would stop dead every five minutes. `episode` therefore
 * changes underneath a component that stays put, and every piece of per-episode state below
 * is reset explicitly rather than by a fresh mount.
 *
 * **The scrubber is a pointer-driven slider, not `<input type="range">`.** A range input binds
 * its value to React state; the video writes that state back four times a second, so a drag
 * fought the finger and every `pointermove` committed a real seek — one HTTP range request per
 * sample. Here a drag is local until release: the readout follows the finger, the video moves
 * once.
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { useTranslation } from 'react-i18next'

import { ApiRequestError } from '@/api/client'
import type { EpisodeDetail, SubtitleTrackOut } from '@/api/types'
import Button from '@/components/Button'
import ErrorState from '@/components/ErrorState'
import IconButton from '@/components/IconButton'
import Spinner from '@/components/Spinner'
import { cx } from '@/components/cx'
import {
  CheckIcon,
  ExitFullscreenIcon,
  FullscreenIcon,
  MuteIcon,
  NextIcon,
  PauseIcon,
  PlayIcon,
  PreviousIcon,
  ReplayIcon,
  SeekBackwardIcon,
  SeekForwardIcon,
  SubtitlesIcon,
  VolumeIcon,
} from '@/components/icons'
import { useAuth } from '@/hooks/useAuth'
import { usePlayer, type BufferedRange } from '@/hooks/usePlayer'
import { useProgressReporter } from '@/hooks/useProgressReporter'
import { formatPercent, formatTimecode } from '@/lib/format'

import styles from './VideoPlayer.module.css'

const SEEK_STEP_SECONDS = 10
/** One keyboard page is six nudges — a minute — which is a scene, not a line. */
const PAGE_STEP_MULTIPLIER = 6
const VOLUME_STEP = 0.05
const CONTROLS_HIDE_MS = 3000
const FLASH_MS = 900
const NEXT_CARD_THRESHOLD = 0.95
const AUTOPLAY_COUNTDOWN_SECONDS = 8

/** ffprobe reports ISO-639-2; user preferences are stored as ISO-639-1. */
const LANGUAGE_ALIASES: Record<string, string> = {
  spa: 'es',
  cas: 'es',
  eng: 'en',
  por: 'pt',
  fra: 'fr',
  fre: 'fr',
  deu: 'de',
  ger: 'de',
  ita: 'it',
  jpn: 'ja',
  kor: 'ko',
  zho: 'zh',
  chi: 'zh',
  cmn: 'zh',
  rus: 'ru',
  cat: 'ca',
}

export interface VideoPlayerProps {
  episode: EpisodeDetail
  /** Episodes in this season, for the "Episode 12 of 58" line. Null while unknown. */
  seasonEpisodeCount: number | null
  /** Title of `episode.nextEpisodeId`, when the page has it. Shown on the up-next card. */
  nextEpisodeTitle: string | null
  /**
   * Switch the mounted player to another episode. The page updates its URL with
   * `history.replaceState` and re-renders with new data — it must not navigate, or the
   * `<video>` is rebuilt and autoplay dies with it.
   */
  onSelectEpisode: (episodeId: string) => void
  /** Fired once per episode, when it is finished or skipped past. */
  onEpisodeComplete: (episodeId: string) => void
  /** Leave the player: the last episode's way out. */
  onLeave: () => void
  autoplayNext: boolean
  initialPositionSeconds: number | null
}

type FlashIcon = 'play' | 'pause' | 'forward' | 'back' | 'volume' | 'mute' | 'subtitles'

interface Flash {
  id: number
  icon: FlashIcon
  text: string
}

/** Lets the player hand its measured aspect ratio to CSS without a raw value in the sheet. */
interface PlayerStyle extends CSSProperties {
  '--player-aspect'?: string
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function normalizeLanguage(code: string | null): string | null {
  if (!code) return null
  const head = code.trim().toLowerCase().split(/[-_]/)[0]
  if (!head) return null
  return LANGUAGE_ALIASES[head] ?? head
}

/** ErrorState localizes by error code, so a media failure is expressed as one. */
function playbackError(message: string): ApiRequestError {
  return new ApiRequestError(0, { code: 'PLAYBACK_FAILED', message, details: {} })
}

export function VideoPlayer({
  episode,
  seasonEpisodeCount,
  nextEpisodeTitle,
  onSelectEpisode,
  onEpisodeComplete,
  onLeave,
  autoplayNext,
  initialPositionSeconds,
}: VideoPlayerProps) {
  const { t, i18n } = useTranslation()
  const { user } = useAuth()

  const onSelectEpisodeRef = useRef(onSelectEpisode)
  onSelectEpisodeRef.current = onSelectEpisode
  const onEpisodeCompleteRef = useRef(onEpisodeComplete)
  onEpisodeCompleteRef.current = onEpisodeComplete

  const { attachVideo, attachContainer, video, state, actions } = usePlayer()

  const containerNodeRef = useRef<HTMLDivElement | null>(null)
  const setContainer = useCallback(
    (node: HTMLDivElement | null) => {
      containerNodeRef.current = node
      attachContainer(node)
    },
    [attachContainer],
  )

  const [resumePending, setResumePending] = useState(initialPositionSeconds !== null)
  /**
   * Which episode the `<video>` is actually pointed at.
   *
   * Not the same as `episode.id`: for exactly one render after the page hands over the next
   * episode, the element is still playing — and still holding the `ended` flag of — the
   * previous one. Reading `state.ended` against `episode.id` in that window made the new
   * episode look finished the instant it arrived, and the serial skipped an episode every
   * time it advanced.
   */
  const [loadedEpisodeId, setLoadedEpisodeId] = useState<string | null>(null)

  const [controlsAwake, setControlsAwake] = useState(true)
  const [focusInControls, setFocusInControls] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const hideTimerRef = useRef<number | null>(null)

  const [flash, setFlash] = useState<Flash | null>(null)
  const [liveText, setLiveText] = useState('')
  const flashSeqRef = useRef(0)

  const [autoplayCancelled, setAutoplayCancelled] = useState(false)
  const [countdown, setCountdown] = useState<number | null>(null)
  const [playbackBlocked, setPlaybackBlocked] = useState(false)
  /** True from the moment the next episode is asked for until its data arrives. */
  const [advancing, setAdvancing] = useState(false)
  const advancedRef = useRef(false)

  /** Seconds under the finger, or null when nothing is being dragged. */
  const [scrubSeconds, setScrubSeconds] = useState<number | null>(null)
  const scrubbing = scrubSeconds !== null

  const menuTriggerId = useId()

  // The media file is authoritative once metadata lands; declared runtimes are only a stand-in.
  const duration =
    state.duration > 0 ? state.duration : (episode.durationSeconds ?? episode.runtimeSeconds ?? 0)

  const displayTime = scrubSeconds ?? state.currentTime

  const announce = useCallback((text: string) => setLiveText(text), [])

  // --- source ------------------------------------------------------------------------------

  /**
   * Point the element at the current episode.
   *
   * Keyed on the URL, not on the object, so a background refetch of the same episode (which
   * `useMarkEpisodeWatched` triggers) does not restart playback. The first load honours the
   * resume prompt by not autoplaying; every later load is an advance the viewer already asked
   * for, so it plays — and because this element has played before, the browser allows it.
   */
  const loadedUrlRef = useRef<string | null>(null)
  useEffect(() => {
    if (!video) return
    if (loadedUrlRef.current === episode.streamUrl) return
    const isFirstLoad = loadedUrlRef.current === null
    loadedUrlRef.current = episode.streamUrl

    const autoplay = !isFirstLoad || initialPositionSeconds === null
    setPlaybackBlocked(false)
    // Batched with `loadSource`'s own reset below, so the first render that reports this
    // episode as loaded is also the first render with its cleared playback state.
    setLoadedEpisodeId(episode.id)
    void actions.loadSource(episode.streamUrl, { autoplay }).then((started) => {
      if (!autoplay) return
      // Not a silent failure: something has to appear on screen or the serial just stops.
      setPlaybackBlocked(!started)
    })
  }, [video, episode.id, episode.streamUrl, initialPositionSeconds, actions])

  // Per-episode state that a remount used to reset for us.
  useEffect(() => {
    advancedRef.current = false
    setAutoplayCancelled(false)
    setCountdown(null)
    setScrubSeconds(null)
    setAdvancing(false)
  }, [episode.id])

  // --- subtitles ---------------------------------------------------------------------------

  const languageName = useCallback(
    (code: string | null): string | null => {
      if (!code) return null
      try {
        const display = new Intl.DisplayNames([i18n.language], { type: 'language' })
        return display.of(code) ?? code
      } catch {
        return code
      }
    },
    [i18n.language],
  )

  const subtitleLabel = useCallback(
    (track: SubtitleTrackOut): string =>
      track.title ?? languageName(track.language) ?? t('common.unknown'),
    [languageName, t],
  )

  const preferredSubtitleLang = user?.preferredSubtitleLang ?? null
  const pickPreferredSubtitle = useCallback(
    (tracks: SubtitleTrackOut[]): string | null => {
      const preferred = normalizeLanguage(preferredSubtitleLang)
      if (!preferred) return null
      const matches = tracks.filter((track) => normalizeLanguage(track.language) === preferred)
      const chosen = matches.find((track) => !track.isForced) ?? matches[0]
      return chosen?.id ?? null
    },
    [preferredSubtitleLang],
  )

  const [activeSubtitleId, setActiveSubtitleId] = useState<string | null>(() =>
    pickPreferredSubtitle(episode.subtitleTracks),
  )

  // Track ids are per-episode, so a swap has to re-pick rather than keep a now-dangling id.
  const subtitleTracks = episode.subtitleTracks
  useEffect(() => {
    setActiveSubtitleId(pickPreferredSubtitle(subtitleTracks))
  }, [subtitleTracks, pickPreferredSubtitle])

  // `<track default>` cannot express "switch now", so the mode is driven imperatively.
  useEffect(() => {
    if (!video) return
    const desired = subtitleTracks.findIndex((track) => track.id === activeSubtitleId)
    const { textTracks } = video
    for (let index = 0; index < textTracks.length; index += 1) {
      const textTrack = textTracks[index]
      if (!textTrack) continue
      textTrack.mode = index === desired ? 'showing' : 'disabled'
    }
  }, [video, activeSubtitleId, subtitleTracks, state.ready])

  const cycleSubtitles = useCallback((): SubtitleTrackOut | undefined => {
    if (subtitleTracks.length === 0) return undefined
    const current = subtitleTracks.findIndex((track) => track.id === activeSubtitleId)
    const next = current + 1
    const nextTrack = next < subtitleTracks.length ? subtitleTracks[next] : undefined
    setActiveSubtitleId(nextTrack?.id ?? null)
    return nextTrack
  }, [subtitleTracks, activeSubtitleId])

  const audioLabel = useMemo(() => {
    const preferred = normalizeLanguage(user?.preferredAudioLang ?? null)
    const tracks = episode.audioTracks
    const chosen =
      tracks.find((track) => normalizeLanguage(track.language) === preferred) ??
      tracks.find((track) => track.isDefault) ??
      tracks[0]
    if (!chosen) return episode.audioCodec
    return chosen.title ?? languageName(chosen.language) ?? chosen.codec
  }, [episode.audioTracks, episode.audioCodec, languageName, user?.preferredAudioLang])

  // --- progress reporting ------------------------------------------------------------------

  const stateRef = useRef(state)
  stateRef.current = state
  const durationRef = useRef(duration)
  durationRef.current = duration

  const getSnapshot = useCallback(() => {
    const currentDuration = durationRef.current
    if (currentDuration <= 0) return null
    return { positionSeconds: stateRef.current.currentTime, durationSeconds: currentDuration }
  }, [])

  useProgressReporter({
    episodeId: episode.id,
    playing: state.playing,
    // A drag is as much "in flux" as an element seek: reporting mid-drag stores a position
    // the viewer is still moving away from.
    seeking: state.seeking || scrubbing,
    enabled: !resumePending && !state.failed,
    getSnapshot,
  })

  // --- resume ---------------------------------------------------------------------------------

  const chooseStart = useCallback(
    (seconds: number) => {
      setResumePending(false)
      // Safe before metadata: the hook holds the target and applies it at `loadedmetadata`.
      if (seconds > 0) actions.seekTo(seconds)
      void actions.play().then((started) => setPlaybackBlocked(!started))
      containerNodeRef.current?.focus({ preventScroll: true })
    },
    [actions],
  )

  // A dedicated page for one video: keyboard shortcuts should work without hunting for focus.
  useEffect(() => {
    containerNodeRef.current?.focus({ preventScroll: true })
  }, [])

  // --- controls visibility ---------------------------------------------------------------------

  const wake = useCallback(() => {
    setControlsAwake(true)
    if (hideTimerRef.current !== null) window.clearTimeout(hideTimerRef.current)
    hideTimerRef.current = window.setTimeout(() => setControlsAwake(false), CONTROLS_HIDE_MS)
  }, [])

  useEffect(() => {
    wake()
    return () => {
      if (hideTimerRef.current !== null) window.clearTimeout(hideTimerRef.current)
    }
  }, [wake])

  /** No hover on a phone, so a tap on the picture reveals the controls before it does anything
   *  else. Where hover exists the controls are already up whenever the mouse has moved, so a
   *  click keeps meaning play/pause. */
  const coarsePointerRef = useRef(false)
  useEffect(() => {
    coarsePointerRef.current = window.matchMedia?.('(pointer: coarse)').matches ?? false
  }, [])

  // Every end-of-episode signal is gated on the element actually holding this episode, so
  // nothing the previous file left behind can be read as this one finishing.
  const sourceReady = loadedEpisodeId === episode.id
  const hasEnded = sourceReady && state.ended
  const nearEnd = sourceReady && duration > 0 && state.currentTime / duration >= NEXT_CARD_THRESHOLD
  const atEdge = (nearEnd || hasEnded) && !state.failed && !resumePending
  const hasNext = episode.nextEpisodeId !== null
  const showNextCard = atEdge && hasNext
  const showFinaleCard = atEdge && !hasNext

  const controlsVisible =
    controlsAwake ||
    !state.playing ||
    menuOpen ||
    focusInControls ||
    scrubbing ||
    state.waiting ||
    showNextCard ||
    showFinaleCard

  // --- completion and advancing ---------------------------------------------------------------

  const completedRef = useRef<string | null>(null)
  useEffect(() => {
    if (!hasEnded || completedRef.current === episode.id) return
    completedRef.current = episode.id
    onEpisodeCompleteRef.current(episode.id)
  }, [hasEnded, episode.id])

  const advance = useCallback(() => {
    const nextId = episode.nextEpisodeId
    if (!nextId || advancedRef.current) return
    advancedRef.current = true
    setCountdown(null)
    setAdvancing(true)
    // Skipping the tail still counts as watched — the alternative is a serial that leaves a
    // trail of 96%-finished episodes behind it.
    if (completedRef.current !== episode.id) {
      completedRef.current = episode.id
      onEpisodeCompleteRef.current(episode.id)
    }
    onSelectEpisodeRef.current(nextId)
  }, [episode.id, episode.nextEpisodeId])

  useEffect(() => {
    if (!showNextCard || !autoplayNext || autoplayCancelled) {
      setCountdown(null)
      return undefined
    }
    setCountdown(AUTOPLAY_COUNTDOWN_SECONDS)
    const id = window.setInterval(() => {
      setCountdown((current) => (current === null ? null : Math.max(0, current - 1)))
    }, 1000)
    return () => window.clearInterval(id)
  }, [showNextCard, autoplayNext, autoplayCancelled])

  useEffect(() => {
    if (countdown !== 0) return
    advance()
  }, [countdown, advance])

  // With autoplay off the episode simply stops at the end; the card stays as an offer.
  useEffect(() => {
    if (!hasEnded || !autoplayNext || autoplayCancelled || !hasNext) return
    advance()
  }, [hasEnded, autoplayNext, autoplayCancelled, hasNext, advance])

  const nextCardTitle = nextEpisodeTitle ?? t('player.nextEpisode')
  useEffect(() => {
    if (!showNextCard) return
    announce(t('player.upNextNamed', { title: nextCardTitle }))
  }, [showNextCard, nextCardTitle, announce, t])

  // --- flash --------------------------------------------------------------------------------------

  const showFlash = useCallback(
    (icon: FlashIcon, text: string) => {
      flashSeqRef.current += 1
      setFlash({ id: flashSeqRef.current, icon, text })
      announce(text)
    },
    [announce],
  )

  useEffect(() => {
    if (!flash) return undefined
    const id = window.setTimeout(() => setFlash(null), FLASH_MS)
    return () => window.clearTimeout(id)
  }, [flash])

  // --- commands, shared by the buttons and the keyboard --------------------------------------------

  const commandTogglePlay = useCallback(() => {
    const willPlay = !state.playing
    if (willPlay) setPlaybackBlocked(false)
    actions.toggle()
    showFlash(willPlay ? 'play' : 'pause', willPlay ? t('player.play') : t('player.pause'))
  }, [actions, showFlash, state.playing, t])

  const commandSeekBy = useCallback(
    (delta: number) => {
      actions.seekBy(delta)
      showFlash(
        delta > 0 ? 'forward' : 'back',
        delta > 0
          ? t('player.seekForward', { seconds: Math.abs(delta) })
          : t('player.seekBackward', { seconds: Math.abs(delta) }),
      )
    },
    [actions, showFlash, t],
  )

  const commandAdjustVolume = useCallback(
    (delta: number) => {
      // Based on the stored level, not the muted-to-zero display: nudging the volume while
      // muted should unmute at the level the user last chose, not creep up from silence.
      const next = clamp(state.volume + delta, 0, 1)
      actions.setVolume(next)
      showFlash('volume', formatPercent(next * 100))
    },
    [actions, showFlash, state.volume],
  )

  const commandToggleMute = useCallback(() => {
    const willMute = !state.muted
    actions.toggleMute()
    showFlash(willMute ? 'mute' : 'volume', willMute ? t('player.mute') : t('player.unmute'))
  }, [actions, showFlash, state.muted, t])

  const commandToggleFullscreen = useCallback(() => {
    actions.toggleFullscreen()
  }, [actions])

  const commandCycleSubtitles = useCallback(() => {
    const nextTrack = cycleSubtitles()
    showFlash('subtitles', nextTrack ? subtitleLabel(nextTrack) : t('player.subtitlesOff'))
  }, [cycleSubtitles, showFlash, subtitleLabel, t])

  const selectSubtitle = useCallback(
    (id: string | null) => {
      setActiveSubtitleId(id)
      const track = subtitleTracks.find((item) => item.id === id)
      showFlash('subtitles', track ? subtitleLabel(track) : t('player.subtitlesOff'))
    },
    [subtitleTracks, showFlash, subtitleLabel, t],
  )

  const goToPrevious = useCallback(() => {
    if (episode.previousEpisodeId) onSelectEpisodeRef.current(episode.previousEpisodeId)
  }, [episode.previousEpisodeId])

  const goToNext = useCallback(() => {
    if (episode.nextEpisodeId) onSelectEpisodeRef.current(episode.nextEpisodeId)
  }, [episode.nextEpisodeId])

  const tapToContinue = useCallback(() => {
    setPlaybackBlocked(false)
    void actions.play().then((started) => setPlaybackBlocked(!started))
  }, [actions])

  const replay = useCallback(() => {
    actions.seekTo(0)
    void actions.play().then((started) => setPlaybackBlocked(!started))
  }, [actions])

  const onSurfaceClick = useCallback(() => {
    if (resumePending) return
    // On touch the first tap is "show me the controls" and nothing more.
    if (coarsePointerRef.current && !controlsVisible) {
      wake()
      return
    }
    commandTogglePlay()
  }, [resumePending, controlsVisible, wake, commandTogglePlay])

  // --- scrubbing ------------------------------------------------------------------------------

  const previewScrub = useCallback(
    (seconds: number) => {
      wake()
      setScrubSeconds(seconds)
    },
    [wake],
  )

  const commitScrub = useCallback(
    (seconds: number) => {
      // Also the keyboard path, which never previews, so the controls have to be woken here
      // rather than only on pointer input.
      wake()
      setScrubSeconds(null)
      actions.seekTo(seconds)
    },
    [actions, wake],
  )

  const cancelScrub = useCallback(() => setScrubSeconds(null), [])

  // --- keyboard --------------------------------------------------------------------------------------

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      wake()
      if (resumePending || state.failed) return
      if (event.altKey || event.ctrlKey || event.metaKey) return

      const target = event.target instanceof HTMLElement ? event.target : null
      const tag = target?.tagName
      // Form controls own their keys outright — the volume slider handles the arrows itself.
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target?.isContentEditable) {
        return
      }
      const activatable = tag === 'BUTTON' || tag === 'A'
      const key = event.key
      const isSpace = key === ' ' || key === 'Spacebar'

      if (activatable && (isSpace || key === 'Enter' || key.startsWith('Arrow'))) return

      if (isSpace || key === 'k') {
        event.preventDefault()
        commandTogglePlay()
        return
      }
      if (key === 'ArrowLeft') {
        event.preventDefault()
        commandSeekBy(-SEEK_STEP_SECONDS)
        return
      }
      if (key === 'ArrowRight') {
        event.preventDefault()
        commandSeekBy(SEEK_STEP_SECONDS)
        return
      }
      if (key === 'ArrowUp') {
        event.preventDefault()
        commandAdjustVolume(VOLUME_STEP)
        return
      }
      if (key === 'ArrowDown') {
        event.preventDefault()
        commandAdjustVolume(-VOLUME_STEP)
        return
      }
      if (key === 'Home') {
        event.preventDefault()
        actions.seekTo(0)
        return
      }
      if (key === 'End' && duration > 0) {
        event.preventDefault()
        actions.seekTo(duration)
        return
      }
      if (key === 'f') {
        event.preventDefault()
        commandToggleFullscreen()
        return
      }
      if (key === 'm') {
        event.preventDefault()
        commandToggleMute()
        return
      }
      if (key === 'c') {
        event.preventDefault()
        commandCycleSubtitles()
        return
      }
      if (key === 'n' && episode.nextEpisodeId) {
        event.preventDefault()
        goToNext()
        return
      }
      if (key === 'p' && episode.previousEpisodeId) {
        event.preventDefault()
        goToPrevious()
        return
      }
      if (key >= '0' && key <= '9' && duration > 0) {
        event.preventDefault()
        const fraction = Number.parseInt(key, 10) / 10
        actions.seekToFraction(fraction)
        showFlash('forward', formatTimecode(duration * fraction))
      }
    },
    [
      wake,
      resumePending,
      state.failed,
      commandTogglePlay,
      commandSeekBy,
      commandAdjustVolume,
      commandToggleFullscreen,
      commandToggleMute,
      commandCycleSubtitles,
      goToNext,
      goToPrevious,
      episode.nextEpisodeId,
      episode.previousEpisodeId,
      duration,
      actions,
      showFlash,
    ],
  )

  // --- render ------------------------------------------------------------------------------------------

  const heading =
    episode.title ??
    t('player.episodeLabel', { season: episode.seasonNumber, episode: episode.episodeNumber })

  const continuityLabel =
    seasonEpisodeCount && seasonEpisodeCount > 0
      ? t('player.episodeOfCount', {
          episode: episode.episodeNumber,
          total: seasonEpisodeCount,
        })
      : t('player.episodeLabel', {
          season: episode.seasonNumber,
          episode: episode.episodeNumber,
        })

  const displayVolume = state.muted ? 0 : state.volume

  const aspectStyle = useMemo<PlayerStyle | undefined>(() => {
    const width = state.videoWidth > 0 ? state.videoWidth : (episode.width ?? 0)
    const height = state.videoHeight > 0 ? state.videoHeight : (episode.height ?? 0)
    if (width <= 0 || height <= 0) return undefined
    return { '--player-aspect': `${width} / ${height}` }
  }, [state.videoWidth, state.videoHeight, episode.width, episode.height])

  const mediaError = useMemo(
    () => (state.failed ? playbackError(t('player.error')) : null),
    [state.failed, t],
  )

  const showOverlayCard = Boolean(mediaError) || resumePending || playbackBlocked

  return (
    <div
      ref={setContainer}
      className={cx(styles.player, !controlsVisible && styles.idle)}
      style={aspectStyle}
      role="region"
      aria-label={heading}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onPointerMove={wake}
      onPointerDown={wake}
    >
      {/* No `src` attribute: the source is driven imperatively so the same element can be
       * pointed at the next episode without React rebuilding it. */}
      <video
        ref={attachVideo}
        className={styles.video}
        preload="auto"
        playsInline
        onClick={onSurfaceClick}
        onDoubleClick={resumePending ? undefined : commandToggleFullscreen}
      >
        {subtitleTracks.map((track) => (
          <track
            key={track.id}
            kind="subtitles"
            src={track.url}
            srcLang={track.language ?? undefined}
            label={subtitleLabel(track)}
          />
        ))}
      </video>

      {state.waiting && !state.failed && !showOverlayCard ? (
        <div className={styles.loading}>
          <Spinner size="lg" label={t('common.loading')} />
        </div>
      ) : null}

      <div className={cx(styles.header, styles.fade)}>
        <p className={styles.continuity}>{continuityLabel}</p>
      </div>

      {/* Mounted for the life of the player: a live region only announces changes that happen
       * after it is already in the accessibility tree. */}
      <div className="sr-only" aria-live="polite">
        {liveText}
      </div>

      {flash ? (
        <div key={flash.id} className={styles.flash} aria-hidden="true">
          <span className={styles.flashIcon}>
            <FlashGlyph icon={flash.icon} />
          </span>
          <span className={styles.flashText}>{flash.text}</span>
        </div>
      ) : null}

      {mediaError ? (
        <div className={styles.overlay}>
          <div className={styles.overlayCard}>
            <ErrorState error={mediaError} onRetry={actions.reload} />
          </div>
        </div>
      ) : null}

      {resumePending && initialPositionSeconds !== null ? (
        <div className={styles.overlay}>
          <div
            className={styles.overlayCard}
            role="group"
            aria-label={t('player.resumePrompt', { time: formatTimecode(initialPositionSeconds) })}
          >
            <p className={styles.overlayTitle}>{heading}</p>
            <div className={styles.overlayActions}>
              <Button
                variant="primary"
                autoFocus
                onClick={() => chooseStart(initialPositionSeconds)}
              >
                {t('series.resumeFrom', { time: formatTimecode(initialPositionSeconds) })}
              </Button>
              <Button variant="secondary" onClick={() => chooseStart(0)}>
                {t('player.startOverAction')}
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {/* A refused `play()` is the one failure that must never be quiet: without this the
       * serial stops between episodes and looks like a dead app. */}
      {playbackBlocked && !mediaError && !resumePending ? (
        <div className={styles.overlay}>
          <button type="button" className={styles.continueTap} onClick={tapToContinue}>
            <span className={styles.continueGlyph} aria-hidden="true">
              <PlayIcon size="2.5rem" />
            </span>
            <span className={styles.continueText}>{t('player.tapToContinue')}</span>
            <span className={styles.continueHint}>{t('player.autoplayBlocked')}</span>
          </button>
        </div>
      ) : null}

      {showNextCard ? (
        <section className={styles.nextCard} aria-label={t('player.upNext')}>
          <p className={styles.nextKicker}>{t('player.upNext')}</p>
          <p className={styles.nextTitle}>{nextCardTitle}</p>
          <div className={styles.nextActions}>
            <Button variant="primary" loading={advancing} onClick={advance}>
              <NextIcon />
              {t('player.playNow')}
            </Button>
            {countdown === null || advancing ? null : (
              <Button variant="ghost" onClick={() => setAutoplayCancelled(true)}>
                {t('player.cancelAutoplay')}
              </Button>
            )}
          </div>
          {countdown === null || advancing ? null : (
            <p className={styles.countdown} aria-hidden="true">
              {t('player.playingNext', { seconds: countdown })}
            </p>
          )}
        </section>
      ) : null}

      {showFinaleCard ? (
        <section className={styles.nextCard} aria-label={t('player.lastEpisode')}>
          <p className={styles.nextKicker}>{t('player.lastEpisode')}</p>
          <p className={styles.nextTitle}>{episode.seriesTitle}</p>
          <div className={styles.nextActions}>
            <Button variant="primary" onClick={onLeave}>
              {t('player.back')}
            </Button>
            <Button variant="ghost" onClick={replay}>
              <ReplayIcon />
              {t('player.replay')}
            </Button>
          </div>
        </section>
      ) : null}

      {resumePending ? null : (
        <div
          className={cx(styles.controls, styles.fade)}
          onFocus={() => setFocusInControls(true)}
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setFocusInControls(false)
          }}
        >
          <div className={styles.timeRow}>
            <span className={styles.time}>{formatTimecode(displayTime)}</span>
            <span className={styles.time}>{formatTimecode(duration)}</span>
          </div>

          <Scrubber
            label={t('player.seekBar')}
            valueText={t('player.seekPosition', {
              position: formatTimecode(displayTime),
              duration: formatTimecode(duration),
            })}
            duration={duration}
            position={displayTime}
            buffered={state.buffered}
            scrubbing={scrubbing}
            disabled={duration <= 0}
            stepSeconds={SEEK_STEP_SECONDS}
            onPreview={previewScrub}
            onCommit={commitScrub}
            onCancel={cancelScrub}
          />

          <div className={styles.transport}>
            <IconButton
              label={t('player.previousEpisode')}
              variant="ghost"
              disabled={episode.previousEpisodeId === null}
              onClick={goToPrevious}
            >
              <PreviousIcon />
            </IconButton>
            <IconButton
              label={t('player.seekBackward', { seconds: SEEK_STEP_SECONDS })}
              variant="ghost"
              onClick={() => commandSeekBy(-SEEK_STEP_SECONDS)}
            >
              <SeekBackwardIcon />
            </IconButton>
            <IconButton
              label={state.playing ? t('player.pause') : t('player.play')}
              variant="ghost"
              className={styles.primaryButton}
              onClick={commandTogglePlay}
            >
              {state.playing ? <PauseIcon size="1.9rem" /> : <PlayIcon size="1.9rem" />}
            </IconButton>
            <IconButton
              label={t('player.seekForward', { seconds: SEEK_STEP_SECONDS })}
              variant="ghost"
              onClick={() => commandSeekBy(SEEK_STEP_SECONDS)}
            >
              <SeekForwardIcon />
            </IconButton>
            <IconButton
              label={t('player.nextEpisode')}
              variant="ghost"
              disabled={episode.nextEpisodeId === null}
              onClick={goToNext}
            >
              <NextIcon />
            </IconButton>
          </div>

          <div className={styles.bar}>
            <div className={styles.volumeGroup}>
              <IconButton
                label={state.muted ? t('player.unmute') : t('player.mute')}
                variant="ghost"
                onClick={commandToggleMute}
              >
                {state.muted || state.volume === 0 ? <MuteIcon /> : <VolumeIcon />}
              </IconButton>
              <input
                className={styles.volumeInput}
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={displayVolume}
                aria-label={t('player.volume')}
                aria-valuetext={formatPercent(displayVolume * 100)}
                onChange={(event) => actions.setVolume(Number(event.target.value))}
              />
            </div>

            <div className={styles.group}>
              {audioLabel ? (
                <p className={styles.audio}>
                  <span className="sr-only">{t('player.audioTrack')}</span>
                  <span className={styles.audioName}>{audioLabel}</span>
                </p>
              ) : null}

              {subtitleTracks.length > 0 ? (
                <SubtitleMenu
                  id={menuTriggerId}
                  tracks={subtitleTracks}
                  activeId={activeSubtitleId}
                  labelFor={subtitleLabel}
                  open={menuOpen}
                  onOpenChange={setMenuOpen}
                  onSelect={selectSubtitle}
                />
              ) : null}

              <IconButton
                label={state.fullscreen ? t('player.exitFullscreen') : t('player.fullscreen')}
                variant="ghost"
                onClick={commandToggleFullscreen}
              >
                {state.fullscreen ? <ExitFullscreenIcon /> : <FullscreenIcon />}
              </IconButton>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// --- scrubber ------------------------------------------------------------------------------------

interface ScrubberProps {
  label: string
  valueText: string
  duration: number
  /** Where to draw the head — the finger's position while dragging, the video's otherwise. */
  position: number
  buffered: BufferedRange[]
  scrubbing: boolean
  disabled: boolean
  stepSeconds: number
  onPreview: (seconds: number) => void
  onCommit: (seconds: number) => void
  onCancel: () => void
}

/**
 * The seek bar.
 *
 * Pointer events rather than mouse events, because a finger produces no mouse events until it
 * lifts — which is why dragging the old range input did nothing on a phone. `setPointerCapture`
 * keeps the drag alive after the finger wanders off a bar four pixels tall, and
 * `touch-action: none` (in the stylesheet) stops the browser handing the same gesture to the
 * page scroller and cancelling it halfway.
 *
 * Dragging never touches the video. Each `pointermove` would otherwise assign `currentTime`,
 * and every assignment on a file streamed from the NAS is a fresh byte-range request; the
 * element stalls, `timeupdate` keeps reporting the old position, and the head snaps back under
 * the finger. The seek happens once, on release.
 */
function Scrubber({
  label,
  valueText,
  duration,
  position,
  buffered,
  scrubbing,
  disabled,
  stepSeconds,
  onPreview,
  onCommit,
  onCancel,
}: ScrubberProps) {
  const trackRef = useRef<HTMLDivElement | null>(null)
  const pointerRef = useRef<number | null>(null)
  const latestRef = useRef(position)

  const secondsAt = useCallback(
    (clientX: number): number => {
      const node = trackRef.current
      if (!node || duration <= 0) return 0
      const rect = node.getBoundingClientRect()
      if (rect.width <= 0) return 0
      return clamp((clientX - rect.left) / rect.width, 0, 1) * duration
    },
    [duration],
  )

  const begin = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (disabled || duration <= 0) return
    if (event.pointerType === 'mouse' && event.button !== 0) return
    pointerRef.current = event.pointerId
    const seconds = secondsAt(event.clientX)
    latestRef.current = seconds
    onPreview(seconds)
    // A tap anywhere on the bar is already a seek to that point; the press must not also
    // start a text selection or a native image drag.
    event.preventDefault()
    event.currentTarget.focus({ preventScroll: true })
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      // No capture available for this pointer. The drag still works over the bar itself;
      // losing the seek entirely because of a throw here would be far worse.
    }
  }

  const move = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (pointerRef.current !== event.pointerId) return
    const seconds = secondsAt(event.clientX)
    latestRef.current = seconds
    onPreview(seconds)
  }

  const finish = (event: ReactPointerEvent<HTMLDivElement>, commit: boolean) => {
    if (pointerRef.current !== event.pointerId) return
    pointerRef.current = null
    try {
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId)
      }
    } catch {
      // Already released by the browser (a cancel usually does it for us).
    }
    // `pointercancel` means the gesture was taken away mid-drag (a system edge swipe). Landing
    // on a position the viewer never released on would be worse than not moving at all.
    if (commit) onCommit(latestRef.current)
    else onCancel()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (disabled || duration <= 0) return
    const page = stepSeconds * PAGE_STEP_MULTIPLIER
    let target: number
    switch (event.key) {
      case 'ArrowLeft':
        target = position - stepSeconds
        break
      case 'ArrowRight':
        target = position + stepSeconds
        break
      case 'PageDown':
        target = position - page
        break
      case 'PageUp':
        target = position + page
        break
      case 'Home':
        target = 0
        break
      case 'End':
        target = duration
        break
      default:
        return
    }
    // The player region binds the arrows too; without this a keyboard seek moves twice.
    event.preventDefault()
    event.stopPropagation()
    onCommit(clamp(target, 0, duration))
  }

  const percent = duration > 0 ? clamp((position / duration) * 100, 0, 100) : 0

  return (
    <div
      className={cx(styles.scrubber, scrubbing && styles.scrubbing)}
      role="slider"
      tabIndex={disabled ? -1 : 0}
      aria-label={label}
      aria-orientation="horizontal"
      aria-valuemin={0}
      aria-valuemax={Math.round(duration)}
      aria-valuenow={Math.round(position)}
      aria-valuetext={valueText}
      aria-disabled={disabled || undefined}
      onPointerDown={begin}
      onPointerMove={move}
      onPointerUp={(event) => finish(event, true)}
      onPointerCancel={(event) => finish(event, false)}
      onKeyDown={onKeyDown}
    >
      <div className={styles.track} ref={trackRef}>
        {buffered.map((range) => (
          <span
            key={`${range.start}-${range.end}`}
            className={styles.buffered}
            style={{
              insetInlineStart: `${duration > 0 ? (range.start / duration) * 100 : 0}%`,
              inlineSize: `${duration > 0 ? ((range.end - range.start) / duration) * 100 : 0}%`,
            }}
          />
        ))}
        <span className={styles.played} style={{ inlineSize: `${percent}%` }} />
        <span className={styles.head} style={{ insetInlineStart: `${percent}%` }} />
      </div>
    </div>
  )
}

// --- subtitle menu -----------------------------------------------------------------------------------

interface SubtitleMenuProps {
  id: string
  tracks: SubtitleTrackOut[]
  activeId: string | null
  labelFor: (track: SubtitleTrackOut) => string
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelect: (id: string | null) => void
}

function SubtitleMenu({
  id,
  tracks,
  activeId,
  labelFor,
  open,
  onOpenChange,
  onSelect,
}: SubtitleMenuProps) {
  const { t } = useTranslation()
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const itemsRef = useRef<Array<HTMLButtonElement | null>>([])

  const options = useMemo<Array<{ id: string | null; label: string }>>(
    () => [
      { id: null, label: t('player.subtitlesOff') },
      ...tracks.map((track) => ({ id: track.id, label: labelFor(track) })),
    ],
    [tracks, labelFor, t],
  )

  useEffect(() => {
    if (!open) return undefined
    const onPointerDown = (event: PointerEvent) => {
      const node = event.target instanceof Node ? event.target : null
      if (node && wrapRef.current?.contains(node)) return
      onOpenChange(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open, onOpenChange])

  useEffect(() => {
    if (!open) return
    const index = options.findIndex((option) => option.id === activeId)
    itemsRef.current[index >= 0 ? index : 0]?.focus()
  }, [open, options, activeId])

  const close = useCallback(
    (returnFocus: boolean) => {
      onOpenChange(false)
      if (returnFocus) wrapRef.current?.querySelector('button')?.focus()
    },
    [onOpenChange],
  )

  const onMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close(true)
      return
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    event.stopPropagation()
    const current = itemsRef.current.findIndex((node) => node === document.activeElement)
    const delta = event.key === 'ArrowDown' ? 1 : -1
    const count = options.length
    itemsRef.current[(current + delta + count) % count]?.focus()
  }

  return (
    <div className={styles.menuWrap} ref={wrapRef}>
      <IconButton
        id={id}
        label={t('player.subtitles')}
        variant="ghost"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => onOpenChange(!open)}
      >
        <SubtitlesIcon />
      </IconButton>
      {open ? (
        <div className={styles.menu} role="menu" aria-labelledby={id} onKeyDown={onMenuKeyDown}>
          {options.map((option, index) => (
            <button
              key={option.id ?? 'off'}
              ref={(node) => {
                itemsRef.current[index] = node
              }}
              type="button"
              role="menuitemradio"
              aria-checked={option.id === activeId}
              className={styles.menuItem}
              onClick={() => {
                onSelect(option.id)
                close(true)
              }}
            >
              <span className={styles.menuCheck} aria-hidden="true">
                {option.id === activeId ? <CheckIcon /> : null}
              </span>
              {option.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function FlashGlyph({ icon }: { icon: FlashIcon }) {
  switch (icon) {
    case 'play':
      return <PlayIcon size="1.75rem" />
    case 'pause':
      return <PauseIcon size="1.75rem" />
    case 'forward':
      return <SeekForwardIcon size="1.75rem" />
    case 'back':
      return <SeekBackwardIcon size="1.75rem" />
    case 'volume':
      return <VolumeIcon size="1.75rem" />
    case 'mute':
      return <MuteIcon size="1.75rem" />
    case 'subtitles':
      return <SubtitlesIcon size="1.75rem" />
  }
}

export default VideoPlayer
