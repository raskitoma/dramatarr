import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { api, ApiRequestError } from '@/api/client'
import type { Job } from '@/api/types'
import { useAuth } from '@/hooks/useAuth'
import { Button } from './Button'
import { Dialog } from './Dialog'
import styles from './Reporter.module.css'

type Category = 'error' | 'feature' | 'suggestion'
type ReportError = { id: string; message: string; section: string; timestamp: string }
type Action = 'send' | 'ignore' | 'cancel'
type ErrorEventDetail = Omit<ReportError, 'id'> & { id: string; remember?: boolean }

function originalMessage(error: unknown): string {
  if (error instanceof ApiRequestError && error.status === 401) return ''
  if (error instanceof DOMException && error.name === 'AbortError') return ''
  return error instanceof ApiRequestError
    ? `${error.code}: ${error.message}`
    : error instanceof Error ? error.message : typeof error === 'string' ? error : ''
}

export function captureReportError(error: unknown) {
  const message = originalMessage(error)
  if (!message) return
  window.dispatchEvent(new CustomEvent('dramatarr:report-error', {
    detail: { id: `${window.location.pathname}:${message}`, message, section: window.location.pathname, timestamp: new Date().toISOString() },
  }))
}

function ReportDialog({ errors, close, dismiss }: {
  errors: ReportError[]
  close: () => void
  dismiss: (ids: string[]) => void
}) {
  const { t } = useTranslation()
  const [selected, setSelected] = useState(() => errors.map(error => error.id))
  const [message, setMessage] = useState('')
  const [category, setCategory] = useState<Category | ''>(errors.length ? 'error' : '')
  const [action, setAction] = useState<Action>()
  const [sending, setSending] = useState(false)
  const [failure, setFailure] = useState('')
  const hasErrors = errors.length > 0
  const canSend = hasErrors ? selected.length > 0 : Boolean(category && message.trim())

  async function confirm() {
    if (action === 'cancel') {
      close()
      return
    }
    if (action === 'ignore') {
      dismiss(selected)
      close()
      return
    }
    if (!category || !canSend) return
    setSending(true)
    setFailure('')
    try {
      await api.post('/reports', {
        category,
        section: window.location.pathname.slice(0, 200),
        message: message.trim(),
        errors: errors.filter(error => selected.includes(error.id)).map(error => ({
          message: error.message.replace(/https?:\/\/[^\s<>"']+/g, raw => {
            try {
              const url = new URL(raw)
              return url.origin + url.pathname
            } catch {
              return raw
            }
          }).slice(0, 2000),
          section: error.section.slice(0, 200),
          timestamp: error.timestamp,
        })),
      })
      dismiss(selected)
      close()
    } catch (error) {
      setFailure(error instanceof ApiRequestError && error.status === 403
        ? t('reports.blocked') : t('reports.sendFailed'))
      setAction(undefined)
    } finally {
      setSending(false)
    }
  }

  return (
    <Dialog open onClose={() => { if (!sending) setAction('cancel') }} title={t('reports.title')}
      footer={action ? (
        <>
          <Button disabled={sending} onClick={() => setAction(undefined)}>{t('reports.back')}</Button>
          <Button loading={sending} variant="primary" onClick={() => void confirm()}>
            {t('reports.confirm')}
          </Button>
        </>
      ) : (
        <>
          <Button onClick={() => setAction('cancel')}>{t('reports.cancel')}</Button>
          {hasErrors && <Button disabled={!selected.length} onClick={() => setAction('ignore')}>{t('reports.ignore')}</Button>}
          <Button variant="primary" disabled={!canSend} onClick={() => setAction('send')}>{t('reports.send')}</Button>
        </>
      )}>
      {action ? <p>{t(`reports.confirm_${action}`)}</p> : (
        <div className={styles.form}>
          {!hasErrors && (
            <label>{t('reports.category')}
              <select required value={category} onChange={event => setCategory(event.target.value as Category | '')}>
                <option value="">{t('reports.choose')}</option>
                {(['error', 'feature', 'suggestion'] as const).map(value => (
                  <option key={value} value={value}>{t(`reports.${value}`)}</option>
                ))}
              </select>
            </label>
          )}
          {hasErrors && (
            <div className={styles.errorList}>
              {errors.map(error => (
                <div key={error.id} className={styles.errorCard} data-selected={selected.includes(error.id)}>
                  {errors.length > 1 && (
                    <label className={styles.errorSelection}>
                      <input type="checkbox" checked={selected.includes(error.id)} onChange={event => {
                        setSelected(event.target.checked ? [...selected, error.id] : selected.filter(id => id !== error.id))
                      }} /> {t('reports.include')}
                    </label>
                  )}
                  <small>{error.section} · {new Date(error.timestamp).toLocaleString()}</small>
                  <pre>{error.message}</pre>
                </div>
              ))}
            </div>
          )}
          <label>{t(!hasErrors ? 'reports.message' : selected.length > 1 ? 'reports.sharedComment' : 'reports.comment')}
            <textarea required={!hasErrors} maxLength={2000} value={message} onChange={event => setMessage(event.target.value)} />
          </label>
          {failure && <p role="alert">{failure}</p>}
        </div>
      )}
    </Dialog>
  )
}

function SignedInReporter() {
  const { t } = useTranslation()
  const cache = useQueryClient()
  const [errors, setErrors] = useState<ReportError[]>([])
  const [snapshot, setSnapshot] = useState<ReportError[]>()
  const seenJobs = useRef(new Set<string>())
  useEffect(() => {
    const collect = (event: Event) => {
      const error = (event as CustomEvent<ErrorEventDetail>).detail
      if (error.remember) {
        if (seenJobs.current.has(error.id)) return
        seenJobs.current.add(error.id)
      }
      setErrors(current => {
        if (current.some(item => item.id === error.id)) return current
        return [...current, error].slice(-20)
      })
    }
    const runtimeError = (event: ErrorEvent) => captureReportError(event.error ?? event.message)
    const rejection = (event: PromiseRejectionEvent) => captureReportError(event.reason)
    const dismissJob = (event: Event) => {
      const id = (event as CustomEvent<string>).detail
      setErrors(current => current.filter(error => error.id !== id))
    }
    window.addEventListener('dramatarr:report-error', collect)
    window.addEventListener('dramatarr:dismiss-error', dismissJob)
    window.addEventListener('error', runtimeError)
    window.addEventListener('unhandledrejection', rejection)
    const queries = cache.getQueryCache().subscribe(event => {
      if (event.type === 'updated' && event.action.type === 'error') captureReportError(event.action.error)
    })
    const mutations = cache.getMutationCache().subscribe(event => {
      if (event.type === 'updated' && event.action.type === 'error') captureReportError(event.action.error)
    })
    return () => {
      queries()
      mutations()
      window.removeEventListener('dramatarr:report-error', collect)
      window.removeEventListener('dramatarr:dismiss-error', dismissJob)
      window.removeEventListener('error', runtimeError)
      window.removeEventListener('unhandledrejection', rejection)
    }
  }, [cache])
  return (
    <>
      <button type="button" className={styles.launcher} data-alert={errors.length > 0}
        aria-label={t('reports.title')} onClick={() => setSnapshot(errors)}>
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
          <path d="m8 3 2 3m6-3-2 3M3 9l4 2m14-2-4 2M3 16h4m10 0h4M5 22l3-4m11 4-3-4M12 10v10" />
          <rect x="7" y="6" width="10" height="14" rx="5" />
        </svg>
      </button>
      {snapshot && <ReportDialog errors={snapshot} close={() => setSnapshot(undefined)}
        dismiss={ids => setErrors(current => current.filter(error => !ids.includes(error.id)))} />}
    </>
  )
}

export function Reporter() {
  const { user } = useAuth()
  return user ? <SignedInReporter key={user.id} /> : null
}

function ErrorDetailsButton({ error }: { error: ReportError }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className={styles.errorButton} onClick={() => setOpen(true)}>{t('reports.error')}</button>
      {open && <ReportDialog errors={[error]} close={() => setOpen(false)} dismiss={ids => {
        for (const id of ids) window.dispatchEvent(new CustomEvent('dramatarr:dismiss-error', { detail: id }))
      }} />}
    </>
  )
}

export function ReportErrorButton({ error }: { error: unknown }) {
  const message = originalMessage(error)
  const [timestamp] = useState(() => new Date().toISOString())
  if (!message) return null
  const section = window.location.pathname
  return <ErrorDetailsButton error={{ id: `${section}:${message}`, section, timestamp, message }} />
}

export function JobErrorButton({ job }: { job: Job }) {
  const details = job.payload.searchErrors as { provider: string; language: string; error: string }[] | undefined
  const message = [job.error, ...(Array.isArray(details)
    ? details.map(item => `${item.provider} · ${item.language}: ${item.error}`) : [])].join('\n')
  const section = window.location.pathname
  const timestamp = job.finishedAt || job.startedAt || job.createdAt
  const id = `${job.id}:${timestamp}`
  // Current request failures belong in the global stack. Historical queue rows stay available
  // on demand so opening the queue does not register old failures as new alerts.
  useEffect(() => {
    if (job.error && section === '/admin/requests') {
      window.dispatchEvent(new CustomEvent('dramatarr:report-error', {
        detail: { id, section, timestamp, message, remember: true },
      }))
    }
  }, [id, job.error, message, section, timestamp])
  if (!job.error) return null
  return <ErrorDetailsButton error={{ id, section, timestamp, message }} />
}
