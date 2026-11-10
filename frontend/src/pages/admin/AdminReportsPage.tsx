import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { api } from '@/api/client'
import { Button } from '@/components/Button'
import styles from '@/components/Reporter.module.css'

type Priority = 'normal' | 'high' | 'urgent' | 'critical'
type Report = {
  id: string
  user_id: string | null
  reporter: string
  category: string
  section: string
  message: string
  errors: { message: string; section: string; timestamp: string; comment: string }[]
  priority: Priority
  closed: boolean
  notes: { author: string; at: string; message: string }[]
  created_at: string
}
type Block = { id: string; email: string }

function ReportCard({ report, blocked, update, block }: {
  report: Report
  blocked: boolean
  update: (body: Partial<Pick<Report, 'priority' | 'closed'>> & { note?: string }) => Promise<void>
  block: (id: string) => Promise<void>
}) {
  const { t } = useTranslation()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  async function run(action: () => Promise<void>, success = '') {
    setBusy(true)
    setStatus('')
    try {
      await action()
      setStatus(success)
    } catch {
      setStatus(t('reports.sendFailed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className={styles.reportCard}>
      <h2>{t(`reports.${report.category}`)} · {t(report.closed ? 'reports.closed' : 'reports.open')}</h2>
      <p>{report.reporter} · {report.section} · {new Date(report.created_at).toLocaleString()}</p>
      {report.message && <p>{report.message}</p>}
      {report.errors.map((error, index) => (
        <details key={index}>
          <summary>{error.section} · {new Date(error.timestamp).toLocaleString()}</summary>
          <pre>{error.message}</pre>
          {error.comment && <p>{error.comment}</p>}
        </details>
      ))}
      <div className={styles.reportActions}>
        <label>{t('reports.priority')}{' '}
          <select disabled={busy} value={report.priority} onChange={event => {
            const priority = event.target.value as Priority
            void run(() => update({ priority }))
          }}>
            {(['normal', 'high', 'urgent', 'critical'] as const).map(priority => (
              <option key={priority} value={priority}>{t(`reports.${priority}`)}</option>
            ))}
          </select>
        </label>
        <Button size="sm" disabled={busy} onClick={() => void run(
          () => navigator.clipboard.writeText(JSON.stringify(report, null, 2)), t('reports.copied'),
        )}>{t('reports.copy')}</Button>
        <Button size="sm" disabled={busy} onClick={() => void run(() => update({ closed: !report.closed }))}>
          {t(report.closed ? 'reports.reopen' : 'reports.close')}
        </Button>
        {report.user_id && !blocked && <Button size="sm" disabled={busy} onClick={() => {
          if (report.user_id && window.confirm(t('reports.confirmBlock'))) {
            const id = report.user_id
            void run(() => block(id))
          }
        }}>{t('reports.block')}</Button>}
      </div>
      {report.notes.map((item, index) => (
        <p key={index}><strong>{item.author}</strong> · {new Date(item.at).toLocaleString()}<br />{item.message}</p>
      ))}
      <form className={styles.form} onSubmit={event => {
        event.preventDefault()
        if (note.trim()) void run(async () => {
          await update({ note: note.trim() })
          setNote('')
        })
      }}>
        <label>{t('reports.note')}<textarea maxLength={2000} value={note} onChange={event => setNote(event.target.value)} /></label>
        <Button size="sm" type="submit" disabled={busy || !note.trim()}>{t('reports.addNote')}</Button>
      </form>
      {status && <p role="status">{status}</p>}
    </article>
  )
}

export function AdminReportsPage() {
  const { t } = useTranslation()
  const [before, setBefore] = useState<string>()
  const [blockError, setBlockError] = useState(false)
  const reports = useQuery({ queryKey: ['reports', before], queryFn: () => api.get<Report[]>('/admin/reports', { before }) })
  const blocks = useQuery({ queryKey: ['report-blocks'], queryFn: () => api.get<Block[]>('/admin/reports/blocks') })

  async function unblock(id: string) {
    if (!window.confirm(t('reports.confirmUnblock'))) return
    setBlockError(false)
    try {
      await api.delete(`/admin/reports/blocks/${id}`)
      await blocks.refetch()
    } catch {
      setBlockError(true)
    }
  }

  return (
    <section className={styles.form}>
      <h1>{t('reports.explorer')}</h1>
      <details>
        <summary>{t('reports.blockedUsers')}</summary>
        {blocks.isError || blockError ? <p role="alert">{t('reports.sendFailed')}</p> : null}
        {blocks.data?.length === 0 && <p>{t('reports.empty')}</p>}
        {blocks.data?.map(block => (
          <p key={block.id}>{block.email}{' '}
            <Button size="sm" onClick={() => void unblock(block.id)}>{t('reports.unblock')}</Button>
          </p>
        ))}
      </details>
      {reports.isLoading && <p>{t('common.loading')}</p>}
      {reports.isError && <p role="alert">{t('reports.sendFailed')}</p>}
      {reports.data?.length === 0 && <p>{t('reports.empty')}</p>}
      {reports.data?.map(report => (
        <ReportCard key={report.id} report={report}
          blocked={Boolean(blocks.data?.some(block => block.id === report.user_id))}
          update={async body => { await api.patch(`/admin/reports/${report.id}`, body); await reports.refetch() }}
          block={async id => { await api.put(`/admin/reports/blocks/${id}`); await blocks.refetch() }} />
      ))}
      <div className={styles.reportActions}>
        {before && <Button onClick={() => setBefore(undefined)}>{t('reports.newest')}</Button>}
        {reports.data?.length === 50 && <Button onClick={() => setBefore(reports.data?.at(-1)?.id)}>{t('reports.older')}</Button>}
      </div>
    </section>
  )
}
