/** A performer's card, on hover or on focus.
 *
 * The cast strip has room for a face and a name. The bio the indexer writes is 120–390
 * characters — measured across this library — so putting it under every disc would turn a
 * strip you scan into a wall you read. It belongs one gesture away, not zero.
 *
 * **Portaled to the body, positioned fixed.** The strip is `overflow-x: auto`, and a
 * horizontal `overflow` clips vertically too, so a card positioned inside it would be cut off
 * at the disc's edge. There is nothing to be gained by fighting that with z-index.
 *
 * **Only for a mouse.** `pointerType` is checked rather than a media query because it answers
 * the real question — a hybrid laptop with a touchscreen has both, and what matters is which
 * one is being used right now. A tap has no hover state to reveal anything with, and it
 * already goes somewhere better: the person's own page, which carries the same bio at a size
 * meant for reading.
 *
 * It follows WCAG 1.4.13, which is the whole reason the timers exist: dismissible (Escape),
 * hoverable (a grace period so the pointer can travel into the card without it fleeing), and
 * persistent (nothing closes it on a timer).
 */

import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'

import { adminKeys, dramadexPerson } from '@/api/admin'
import { PersonFace } from '@/components/PersonFace'

import styles from './PersonPeek.module.css'

/** Long enough that crossing a strip of eight faces does not fire eight requests, short enough
 *  that a deliberate pause feels answered. */
const OPEN_DELAY_MS = 260
/** The pointer has to cross a gap of real pixels to reach the card. This is that journey. */
const GRACE_MS = 140

export interface PersonPeekProps {
  personId: number
  name: string
  tint?: number
  className?: string
  children: ReactNode
}

export function PersonPeek({ personId, name, tint, className, children }: PersonPeekProps) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [at, setAt] = useState<{ top: number; left: number } | null>(null)
  const anchorRef = useRef<HTMLSpanElement | null>(null)
  const openTimer = useRef<number | undefined>(undefined)
  const closeTimer = useRef<number | undefined>(undefined)
  const cardId = useId()

  const query = useQuery({
    queryKey: adminKeys.dramadexPerson(personId),
    queryFn: () => dramadexPerson(personId),
    enabled: open,
    staleTime: 5 * 60_000,
  })

  const clearTimers = useCallback(() => {
    window.clearTimeout(openTimer.current)
    window.clearTimeout(closeTimer.current)
  }, [])

  const place = useCallback(() => {
    const rect = anchorRef.current?.getBoundingClientRect()
    if (!rect) return
    setAt({ top: rect.bottom, left: rect.left + rect.width / 2 })
    setOpen(true)
  }, [])

  const scheduleOpen = useCallback(() => {
    clearTimers()
    openTimer.current = window.setTimeout(place, OPEN_DELAY_MS)
  }, [clearTimers, place])

  const scheduleClose = useCallback(() => {
    clearTimers()
    closeTimer.current = window.setTimeout(() => setOpen(false), GRACE_MS)
  }, [clearTimers])

  useEffect(() => clearTimers, [clearTimers])

  /* A card pinned to a rect that has moved is worse than no card. Scrolling the strip under it
     is the common way that happens, so any scroll anywhere dismisses. */
  useEffect(() => {
    if (!open) return undefined
    const dismiss = () => setOpen(false)
    window.addEventListener('scroll', dismiss, true)
    window.addEventListener('resize', dismiss)
    return () => {
      window.removeEventListener('scroll', dismiss, true)
      window.removeEventListener('resize', dismiss)
    }
  }, [open])

  const person = query.data
  /* A person the indexer cannot resolve gets no card at all, rather than an empty one. */
  const showCard = open && at !== null && !query.isError

  return (
    <span
      ref={anchorRef}
      className={className}
      onPointerEnter={(event) => {
        if (event.pointerType !== 'mouse') return
        scheduleOpen()
      }}
      onPointerLeave={(event) => {
        if (event.pointerType !== 'mouse') return
        scheduleClose()
      }}
      onFocus={place}
      onBlur={scheduleClose}
      onKeyDown={(event) => {
        if (event.key !== 'Escape' || !open) return
        event.stopPropagation()
        clearTimers()
        setOpen(false)
      }}
    >
      <span aria-describedby={showCard && person?.bio ? cardId : undefined}>{children}</span>

      {showCard
        ? createPortal(
            <div
              id={cardId}
              className={styles.card}
              role="tooltip"
              style={{ top: `${at.top}px`, left: `${at.left}px` }}
              onPointerEnter={clearTimers}
              onPointerLeave={scheduleClose}
            >
              <div className={styles.head}>
                <PersonFace
                  className={styles.face}
                  name={name}
                  photoUrl={`/api/library/dramadex/people/${personId}/photo`}
                  tint={tint}
                />
                <div className={styles.headText}>
                  <p className={styles.name}>{person?.name ?? name}</p>
                  {person ? (
                    <p className={styles.count}>
                      {t('person.creditCount', { count: person.credits.length })}
                    </p>
                  ) : null}
                </div>
              </div>

              {query.isLoading ? (
                <p className={styles.skeleton} aria-hidden="true" />
              ) : person?.bio ? (
                <p className={styles.bio}>{person.bio}</p>
              ) : person ? (
                /* One performer in this library has a photo and no bio. Saying so is better
                   than a card that opens onto nothing. */
                <p className={styles.empty}>{t('person.noBio')}</p>
              ) : null}
            </div>,
            document.body,
          )
        : null}
    </span>
  )
}

export default PersonPeek
