import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'

import { IconButton } from './IconButton'
import { cx } from './cx'
import styles from './Toast.module.css'

export type ToastTone = 'success' | 'error' | 'info'

export interface ToastApi {
  show: (message: string, tone?: ToastTone) => void
}

interface ToastItem {
  id: number
  message: string
  tone: ToastTone
}

const MAX_TOASTS = 3
const DISMISS_AFTER_MS = 5000

let nextToastId = 0

const ToastContext = createContext<ToastApi | null>(null)

export function useToast(): ToastApi {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast must be used inside ToastProvider')
  return context
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const timers = useRef(new Map<number, number>())
  const paused = useRef(false)

  const dismiss = useCallback((id: number): void => {
    const handle = timers.current.get(id)
    if (handle !== undefined) {
      window.clearTimeout(handle)
      timers.current.delete(id)
    }
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const arm = useCallback(
    (id: number): void => {
      if (paused.current) return
      timers.current.set(
        id,
        window.setTimeout(() => dismiss(id), DISMISS_AFTER_MS),
      )
    },
    [dismiss],
  )

  const show = useCallback(
    (message: string, tone: ToastTone = 'info'): void => {
      const id = nextToastId++
      setToasts((current) => {
        const next = [...current, { id, message, tone }]
        // Oldest falls off the top; its pending timer resolves to a no-op.
        return next.length > MAX_TOASTS ? next.slice(next.length - MAX_TOASTS) : next
      })
      arm(id)
    },
    [arm],
  )

  // Hovering or tabbing into the stack means the user is reading it — stop the clock until
  // they leave, so a toast can never vanish mid-sentence or while its button has focus.
  const pause = useCallback((): void => {
    paused.current = true
    for (const handle of timers.current.values()) window.clearTimeout(handle)
    timers.current.clear()
  }, [])

  const resume = useCallback((): void => {
    paused.current = false
    for (const toast of toasts) arm(toast.id)
  }, [arm, toasts])

  useEffect(() => {
    const pending = timers.current
    return () => {
      for (const handle of pending.values()) window.clearTimeout(handle)
      pending.clear()
    }
  }, [])

  const api = useMemo<ToastApi>(() => ({ show }), [show])

  return (
    <ToastContext.Provider value={api}>
      {children}
      {createPortal(
        <div
          className={styles.viewport}
          role="status"
          aria-live="polite"
          aria-relevant="additions"
          onPointerEnter={pause}
          onPointerLeave={resume}
          onFocusCapture={pause}
          onBlurCapture={resume}
        >
          {toasts.map((toast) => (
            <div key={toast.id} className={cx(styles.toast, styles[toast.tone])}>
              <ToastIcon tone={toast.tone} />
              <p className={styles.message}>{toast.message}</p>
              <IconButton
                label={t('common.close')}
                size="sm"
                className={styles.dismiss}
                onClick={() => dismiss(toast.id)}
              >
                <svg
                  viewBox="0 0 24 24"
                  width="14"
                  height="14"
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
          ))}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  )
}

function ToastIcon({ tone }: { tone: ToastTone }) {
  return (
    <svg
      className={styles.icon}
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="12" cy="12" r="9" />
      {tone === 'success' ? <path d="M8 12.5l2.5 2.5L16 9.5" /> : null}
      {tone === 'error' ? <path d="M12 7.5v5.5M12 16.5v.01" /> : null}
      {tone === 'info' ? <path d="M12 11v5.5M12 7.5v.01" /> : null}
    </svg>
  )
}

export default ToastProvider
