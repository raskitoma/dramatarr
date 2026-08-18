/** The header's search: a magnifier that opens into a field with its results underneath.
 *
 * Collapsed it is one icon button sitting just before the theme controls. Activated, the field
 * grows out of it and what you type is answered in place — the same records `/search` renders,
 * capped to what fits, and always ending in a row that carries the query to the full page.
 * That last row is the point of the whole control: a dropdown that can only offer what it
 * happens to have is a dead end, and this one always has somewhere to go.
 *
 * It is a real `combobox`/`listbox`, not the `menu` pattern the avatar uses beside it, and the
 * difference is not academic. A menu moves DOM focus onto each item as you arrow through it,
 * which would take focus out of the field and stop you typing. Here focus never leaves the
 * input and `aria-activedescendant` points at the highlighted row instead.
 *
 * Below the header's 720px breakpoint the field cannot grow in place — there is no room beside
 * the brand and the avatar — so the expanded state lays itself over the header bar instead.
 * Same component, same state, one CSS rule; the alternative was a second implementation for
 * phones, which is how two behaviours drift apart.
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from 'react'
import { useTranslation } from 'react-i18next'
import { useLocation } from 'wouter'

import { SEARCH_DEBOUNCE_MS, SEARCH_MIN_QUERY_LENGTH, useSearch } from '@/api/library'
import type { EpisodeHit, SeriesHit } from '@/api/types'
import { IconButton } from '@/components/IconButton'
import { Poster } from '@/components/Poster'
import { cx } from '@/components/cx'
import { episodeHref, matchNote, seriesHref } from '@/components/searchHits'
import { CloseIcon, SearchIcon } from '@/components/icons'
import { useDismissOnOutside } from '@/hooks/useDismissOnOutside'

import styles from './HeaderSearch.module.css'

/** How many of each kind the dropdown shows. The rest are what the full page is for. */
const MAX_SERIES = 5
const MAX_EPISODES = 3

interface HeaderSearchProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** One thing the highlight can land on. The fall-through row is an option like any other. */
type Option =
  | { kind: 'series'; href: string; hit: SeriesHit }
  | { kind: 'episode'; href: string; hit: EpisodeHit }
  | { kind: 'all'; href: string }

export function HeaderSearch({ open, onOpenChange }: HeaderSearchProps) {
  const { t } = useTranslation()
  const [, navigate] = useLocation()

  const wrapRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const listId = useId()

  const [input, setInput] = useState('')
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(-1)

  const trimmed = input.trim()
  const longEnough = trimmed.length >= SEARCH_MIN_QUERY_LENGTH

  // Debounced only for the *request*. The listbox opens on the keystroke, because a field that
  // is visibly open with nothing under it for a quarter of a second reads as broken, and
  // `aria-expanded` flapping false→true→false is worse for a screen reader than for an eye.
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(trimmed), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [trimmed])

  // No facets: the header is a jump-to box, and a filter set on /search must not silently
  // narrow what it offers from another page.
  const search = useSearch(query, {}, MAX_SERIES + MAX_EPISODES + 10)

  const close = useCallback(() => {
    onOpenChange(false)
  }, [onOpenChange])

  useDismissOnOutside(open, close, wrapRef)

  // Each visit starts empty. A query left over from last time is not a question being asked now.
  const [wasOpen, setWasOpen] = useState(open)
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) {
      setInput('')
      setQuery('')
      setActiveIndex(-1)
    }
  }

  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  /**
   * Results, only when they answer the query on screen.
   *
   * `useSearch` keeps the previous page's data while the next one loads, which is right for a
   * results page and wrong here: deleting back to one character would leave yesterday's hits
   * hanging under an emptied box. Gating on the *current* trimmed text makes that impossible.
   */
  const answered = longEnough && search.data?.query.trim() === query && query === trimmed
  const seriesHits = answered ? (search.data?.series ?? []).slice(0, MAX_SERIES) : []
  const episodeHits = answered ? (search.data?.episodes ?? []).slice(0, MAX_EPISODES) : []

  const options = useMemo<Option[]>(() => {
    const list: Option[] = [
      ...seriesHits.map((hit) => ({
        kind: 'series' as const,
        href: seriesHref(hit.series),
        hit,
      })),
      ...episodeHits.map((hit) => ({ kind: 'episode' as const, href: episodeHref(hit), hit })),
    ]
    // Always last, and always present while the box is open: it is the only row guaranteed to
    // lead somewhere, so the list is never a dead end.
    list.push({ kind: 'all' as const, href: `/search?q=${encodeURIComponent(trimmed)}` })
    return list
  }, [seriesHits, episodeHits, trimmed])

  const listOpen = open && longEnough
  const optionId = (index: number) => `${listId}-option-${index}`

  const go = useCallback(
    (href: string) => {
      close()
      navigate(href)
    },
    [close, navigate],
  )

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    if (!longEnough) return
    const chosen = activeIndex >= 0 ? options[activeIndex] : undefined
    go(chosen ? chosen.href : `/search?q=${encodeURIComponent(trimmed)}`)
  }

  function move(delta: number): void {
    if (!listOpen || options.length === 0) return
    setActiveIndex((previous) => {
      const next = previous + delta
      if (next < 0) return options.length - 1
      if (next >= options.length) return 0
      return next
    })
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      event.preventDefault()
      // Stopped here on purpose: without it the same Escape closes the mobile disclosure panel
      // underneath, and the user sees two things vanish for one keypress.
      event.stopPropagation()
      close()
      triggerRef.current?.focus()
      return
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      move(1)
      return
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      move(-1)
    }
  }

  const liveMessage = !listOpen
    ? ''
    : search.isError
      ? t('search.suggestionsError')
      : answered
        ? t('common.results', { count: seriesHits.length + episodeHits.length })
        : t('common.loading')

  return (
    <div
      ref={wrapRef}
      className={cx(styles.wrap, open && styles.open)}
      data-open={open ? 'true' : 'false'}
      onKeyDown={handleKeyDown}
    >
      <form role="search" className={styles.field} onSubmit={handleSubmit}>
        <IconButton
          ref={triggerRef}
          type={open ? 'submit' : 'button'}
          className={styles.trigger}
          label={t('common.search')}
          onClick={open ? undefined : () => onOpenChange(true)}
        >
          <SearchIcon />
        </IconButton>

        {open ? (
          <>
            <input
              ref={inputRef}
              className={styles.input}
              // `role="combobox"` rather than `type="search"`: the browser's own clear button
              // sits where the close control goes and answers Escape by emptying the field
              // instead of closing the control.
              role="combobox"
              type="text"
              aria-label={t('common.search')}
              aria-expanded={listOpen}
              aria-controls={listOpen ? listId : undefined}
              aria-activedescendant={activeIndex >= 0 ? optionId(activeIndex) : undefined}
              aria-autocomplete="list"
              autoComplete="off"
              spellCheck={false}
              placeholder={t('search.placeholder')}
              value={input}
              onChange={(event) => {
                setInput(event.target.value)
                setActiveIndex(-1)
              }}
            />
            <IconButton
              size="sm"
              className={styles.close}
              label={t('search.close')}
              onClick={() => {
                close()
                triggerRef.current?.focus()
              }}
            >
              <CloseIcon />
            </IconButton>
          </>
        ) : null}
      </form>

      {listOpen ? (
        <div id={listId} role="listbox" aria-label={t('search.suggestions')} className={styles.list}>
          {seriesHits.length > 0 ? (
            <div role="group" aria-label={t('search.seriesSection')}>
              <p className={styles.groupLabel}>{t('search.seriesSection')}</p>
              {seriesHits.map((hit, index) => {
                const note = matchNote(hit)
                return (
                  <Row
                    key={hit.series.id}
                    id={optionId(index)}
                    active={activeIndex === index}
                    posterUrl={hit.series.posterUrl}
                    title={hit.series.title}
                    meta={[
                      hit.series.year ? String(hit.series.year) : '',
                      note ? t(note.key, { title: note.title }) : '',
                    ]}
                    onChoose={() => go(seriesHref(hit.series))}
                  />
                )
              })}
            </div>
          ) : null}

          {episodeHits.length > 0 ? (
            <div role="group" aria-label={t('search.episodesSection')}>
              <p className={styles.groupLabel}>{t('search.episodesSection')}</p>
              {episodeHits.map((hit, offset) => {
                const index = seriesHits.length + offset
                return (
                  <Row
                    key={hit.episode.id}
                    id={optionId(index)}
                    active={activeIndex === index}
                    posterUrl={null}
                    title={hit.episode.title || hit.seriesTitle}
                    meta={[
                      hit.seriesTitle,
                      t('series.episodeLabel', {
                        season: hit.episode.seasonNumber,
                        episode: hit.episode.episodeNumber,
                      }),
                    ]}
                    onChoose={() => go(episodeHref(hit))}
                  />
                )
              })}
            </div>
          ) : null}

          {answered && seriesHits.length === 0 && episodeHits.length === 0 ? (
            <p className={styles.empty}>{t('search.noResults', { query: trimmed })}</p>
          ) : null}

          {search.isError ? <p className={styles.empty}>{t('search.suggestionsError')}</p> : null}

          {/* The row that is always here. Rendered as an option so arrowing reaches it. */}
          <div
            id={optionId(options.length - 1)}
            role="option"
            aria-selected={activeIndex === options.length - 1}
            data-active={activeIndex === options.length - 1 ? 'true' : undefined}
            className={styles.searchAll}
            // `onPointerDown` with the default prevented, not `onClick`: a click would first
            // blur the input, `useDismissOnOutside` would close the control, and the row would
            // be gone before its own handler ran.
            onPointerDown={(event) => {
              event.preventDefault()
              go(`/search?q=${encodeURIComponent(trimmed)}`)
            }}
          >
            <SearchIcon size="1em" />
            <span>{t('search.searchAll', { query: trimmed })}</span>
          </div>
        </div>
      ) : null}

      {open ? (
        <p className="sr-only" role="status" aria-live="polite">
          {liveMessage}
        </p>
      ) : null}
    </div>
  )
}

interface RowProps {
  id: string
  active: boolean
  posterUrl: string | null
  title: string
  meta: string[]
  onChoose: () => void
}

function Row({ id, active, posterUrl, title, meta, onChoose }: RowProps) {
  const parts = meta.filter((part) => part !== '')
  return (
    <div
      id={id}
      role="option"
      aria-selected={active}
      data-active={active ? 'true' : undefined}
      className={styles.option}
      onPointerDown={(event) => {
        event.preventDefault()
        onChoose()
      }}
    >
      <span className={styles.thumb}>
        <Poster src={posterUrl} alt="" size="sm" fluid />
      </span>
      <span className={styles.optionText}>
        <span className={styles.optionTitle}>{title}</span>
        {parts.length > 0 ? <span className={styles.optionMeta}>{parts.join(' · ')}</span> : null}
      </span>
    </div>
  )
}

export default HeaderSearch
