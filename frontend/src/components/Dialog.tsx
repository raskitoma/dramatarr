import { useCallback, useEffect, useId, useRef, type MouseEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'

import { IconButton } from './IconButton'
import { cx } from './cx'
import styles from './Dialog.module.css'

export type DialogSize = 'sm' | 'md' | 'lg'

export interface DialogProps {
  open: boolean
  onClose: () => void
  title: string
  description?: string
  children: ReactNode
  footer?: ReactNode
  size?: DialogSize
  className?: string
}

/* Nested dialogs are rare but a stack of one-shot locks would still leak the scrollbar, so
 * count the locks rather than toggling a boolean. */
let scrollLocks = 0

function lockBodyScroll(): void {
  scrollLocks += 1
  if (scrollLocks === 1) {
    document.body.style.overflow = 'hidden'
  }
}

function unlockBodyScroll(): void {
  scrollLocks = Math.max(0, scrollLocks - 1)
  if (scrollLocks === 0) {
    document.body.style.overflow = ''
  }
}

/**
 * Modal dialog on the native `<dialog>` element.
 *
 * `showModal()` buys the real thing: a top-layer stacking context above every z-index in the
 * app, a browser-managed focus trap, inertness for the rest of the page, and Escape handling.
 * On top of that we add backdrop-click dismissal, a body scroll lock and focus restoration.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  className,
}: DialogProps) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const descriptionId = useId()

  // The close event fires for our own programmatic close too; that one must not bubble back
  // out as a state change.
  const suppressClose = useRef(false)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  // Where focus was before the dialog opened. Browsers restore it themselves on close, but
  // only when the element is still in the document — React may well have replaced it.
  const restoreFocusTo = useRef<HTMLElement | null>(null)

  useEffect(() => {
    const node = ref.current
    if (!node || !open) return

    restoreFocusTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null

    if (!node.open) node.showModal()
    lockBodyScroll()

    return () => {
      unlockBodyScroll()
      if (node.open) {
        suppressClose.current = true
        node.close()
        suppressClose.current = false
      }
      const previous = restoreFocusTo.current
      if (previous && previous.isConnected) previous.focus()
    }
  }, [open])

  useEffect(() => {
    const node = ref.current
    if (!node) return

    // Escape closes natively; we only have to report it upward so the caller's state agrees
    // with what the DOM already did.
    const handleClose = (): void => {
      if (!suppressClose.current) onCloseRef.current()
    }

    node.addEventListener('close', handleClose)
    return () => node.removeEventListener('close', handleClose)
  }, [])

  // A click only counts as "on the backdrop" when it both started and ended there — otherwise
  // selecting text inside the panel and releasing outside it would dismiss the dialog.
  const pressedBackdrop = useRef(false)

  const handleMouseDown = useCallback((event: MouseEvent<HTMLDialogElement>): void => {
    pressedBackdrop.current = event.target === event.currentTarget
  }, [])

  const handleClick = useCallback((event: MouseEvent<HTMLDialogElement>): void => {
    if (pressedBackdrop.current && event.target === event.currentTarget) {
      onCloseRef.current()
    }
    pressedBackdrop.current = false
  }, [])

  /* Mounted on the body, not where it was written.
   *
   * `showModal()` is supposed to make where it lives irrelevant — the top layer has the
   * viewport as its containing block whatever is above it. When that promotion does not
   * happen, or happens late, the UA falls back to `position: absolute` and the dialog inherits
   * the nearest positioned ancestor instead: here, a 2.5rem icon button inside a hero card
   * with `overflow: hidden`. `inline-size: min(32rem, 100% - …)` then resolves against forty
   * pixels and the whole dialog collapses to a glyph, which is exactly what the owner kept
   * seeing and what I could not reproduce.
   *
   * A portal removes the question rather than answering it, and it is the pattern this
   * codebase already uses for `Toast` and `PersonPeek`. */
  return createPortal(
    <dialog
      ref={ref}
      className={cx(styles.dialog, styles[size], className)}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onMouseDown={handleMouseDown}
      onClick={handleClick}
    >
      {open ? (
        <div className={styles.panel}>
          <div className={styles.header}>
            <div className={styles.heading}>
              <h2 id={titleId} className={styles.title}>
                {title}
              </h2>
              {description ? (
                <p id={descriptionId} className={styles.description}>
                  {description}
                </p>
              ) : null}
            </div>
            <IconButton label={t('common.close')} size="sm" onClick={onClose}>
              <svg
                viewBox="0 0 24 24"
                width="16"
                height="16"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                aria-hidden="true"
                focusable="false"
              >
                <path d="M6 6l12 12M18 6L6 18" />
              </svg>
            </IconButton>
          </div>
          <div className={styles.body}>{children}</div>
          {footer ? <div className={styles.footer}>{footer}</div> : null}
        </div>
      ) : null}
    </dialog>,
    document.body,
  )
}

export default Dialog
