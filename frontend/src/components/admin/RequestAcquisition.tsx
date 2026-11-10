import { JobErrorButton, ReportErrorButton } from '@/components/Reporter'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'
import { api } from '@/api/client'
import type { Job } from '@/api/types'
import { Badge } from '@/components/Badge'
import styles from './Acquisition.module.css'
import { Button } from '@/components/Button'
import { Input } from '@/components/Input'
import { AcquisitionProgress } from './AcquisitionProgress'

type Candidate = { title: string; url: string; provider: string; language: string; episodes: number | null; idMatch: boolean }
export function RequestAcquisition({ id, approved, review, languages, onReview, onLanguages }: { id: string; approved: boolean; review: boolean; languages: string; onReview: (value: boolean) => void; onLanguages: (value: string) => void }) {
  const [expandedSearch, setExpandedSearch] = useState<string>()
  const { t, i18n } = useTranslation()
  const cache = useQueryClient()
  const key = ['request-acquisition', id]
  const job = useQuery({ queryKey: key, queryFn: () => api.get<Job | null>(`/admin/integration/requests/${id}`), enabled: approved, refetchInterval: approved ? 3000 : false })
  const latest = job.data
  const active = latest?.status === 'running' || latest?.status === 'queued'
  const completed = latest?.type === 'download_series' && latest.status === 'succeeded'
  const currentSearch = latest?.type === 'find_source' && latest.status === 'succeeded' && Array.isArray(latest.payload.candidates) && latest.payload.candidates.length > 0
    ? latest : null
  const saved = useQuery({
    queryKey: ['request-search-results', id, latest?.id, latest?.status],
    queryFn: () => api.get<Job | null>(`/admin/integration/requests/${id}/results`),
    enabled: approved && Boolean(latest) && !currentSearch && !completed,
  })
  const results = currentSearch ?? saved.data
  const selectedLanguages = languages.split(',').map(value => value.trim()).filter(Boolean)
  const search = useMutation({
    mutationFn: () => api.post<Job>(`/admin/integration/requests/${id}/search`, {
      languages: selectedLanguages,
      reviewBeforeDownload: review,
      ...(results?.payload.candidates ? { refresh: true } : {}),
    }),
    onSuccess: data => cache.setQueryData(key, data),
  })
  const retry = useMutation({
    mutationFn: (jobId: string) => api.post<Job>(`/admin/jobs/${jobId}/retry`),
    onSuccess: data => cache.setQueryData(key, data),
  })
  const retryable = latest && (latest.status === 'failed' || latest.status === 'canceled') ? latest : null
  const failedSearch = retryable?.type === 'find_source'
  const changedSettings = failedSearch && (
    JSON.stringify(selectedLanguages) !== JSON.stringify(retryable.payload.languages) ||
    review !== retryable.payload.reviewBeforeDownload
  )
  const confirm = useMutation({
    mutationFn: (url: string) => api.post<Job>(`/admin/integration/requests/${id}/download`, {
      searchJobId: results?.id, url,
    }),
    onSuccess: data => cache.setQueryData(key, data),
  })
  const candidates = (results?.payload.candidates || []) as Candidate[]
  const available = (results?.payload.availableLanguages || {}) as Record<string, { code: string; label: string }[]>
  const mutating = search.isPending || retry.isPending || confirm.isPending
  const expanded = expandedSearch === results?.id
  const visible = expanded ? candidates : candidates.slice(0, 4)
  const languageNames = new Intl.DisplayNames([i18n.resolvedLanguage || 'es'], { type: 'language' })
  function languageName(code: string) {
    try {
      return languageNames.of(code) || code
    } catch {
      return Object.values(available).flat().find(language => language.code === code)?.label || code
    }
  }

  return (
    <section className={styles.section} aria-label={t('acquisitionFlow.title')}>
      <h3>{t('acquisitionFlow.title')}</h3>
      <details className={styles.settings} open={approved ? undefined : true}>
        <summary>{t('acquisitionFlow.searchOptions')}</summary>
        <div className={styles.settingsBody}>
          <label>
            <input type="checkbox" checked={review} onChange={e => onReview(e.target.checked)} />
            {' '}{t('acquisitionFlow.review')}
          </label>
          <p className={styles.muted}>{t('acquisitionFlow.autoHint')}</p>
          <label htmlFor={`languages-${id}`}>{t('acquisitionFlow.languages')}</label>
          <Input id={`languages-${id}`} value={languages} onChange={e => onLanguages(e.target.value)} placeholder="es,en,pt" />
          <p className={styles.muted}>{t('acquisitionFlow.languagesHint')}</p>
          {Object.entries(available).map(([provider, langs]) => (
            <p className={styles.muted} key={provider}>{provider} · {t('acquisitionFlow.availableLanguages')} · {langs.map(l => l.code).join(', ')}</p>
          ))}
        </div>
      </details>
      {approved && (
        <>
          <div className={styles.actions}>
            {retryable && (!failedSearch || candidates.length === 0) && (
              <Button size="sm" loading={retry.isPending} disabled={mutating} onClick={() => retry.mutate(retryable.id)}>
                {t(failedSearch ? 'reports.retrySearch' : 'acquisitionFlow.retryDownload')}
              </Button>
            )}
            {(!failedSearch || changedSettings || candidates.length > 0) && (
              <Button size="sm" variant={candidates.length ? 'secondary' : undefined} loading={search.isPending} disabled={mutating || active || completed} onClick={() => search.mutate()}>
                {t(candidates.length ? 'acquisitionFlow.refreshResults' : 'acquisitionFlow.search')}
              </Button>
            )}
            {job.data && <Link href="/admin/queue">{t('acquisitionFlow.queue')}</Link>}
          </div>
          {job.data && <AcquisitionProgress job={job.data} />}
          {!completed && candidates.length > 0 && (
            <div className={styles.results}>
              {!currentSearch && <p className={styles.muted}>{t('acquisitionFlow.savedResults')}</p>}
              <div className={styles.grid}>
                {visible.map(candidate => (
                  <article className={styles.card} key={candidate.url} data-match={candidate.idMatch}>
                    <div className={styles.meta}>
                      <Badge tone="neutral">{languageName(candidate.language)}</Badge>
                      <span>{candidate.provider}</span>
                      {candidate.episodes != null && <span>{candidate.episodes} {t('acquisitionFlow.episodeLabel')}</span>}
                    </div>
                    <h4>{candidate.title}</h4>
                    {candidate.idMatch && <span className={styles.muted}>{t('acquisitionFlow.idMatch')}</span>}
                    <div className={styles.actions}>
                      <a href={candidate.url} target="_blank" rel="noopener noreferrer">{t('acquisitionFlow.inspect')}</a>
                      <Button size="sm" variant="primary" loading={confirm.isPending && confirm.variables === candidate.url} disabled={mutating || active || retryable?.payload.url === candidate.url} onClick={() => confirm.mutate(candidate.url)}>
                        {t('acquisitionFlow.downloadEdition')}
                      </Button>
                    </div>
                  </article>
                ))}
              </div>
              {candidates.length > 4 && (
                <div>
                  <Button size="sm" aria-expanded={expanded} onClick={() => setExpandedSearch(expanded ? undefined : results?.id)}>
                    {expanded ? t('acquisitionFlow.showFewer') : t('acquisitionFlow.showAll', { count: candidates.length })}
                  </Button>
                </div>
              )}
            </div>
          )}
          {job.data?.error && <JobErrorButton job={job.data} />}
          {[job.error, saved.error, search.error, retry.error, confirm.error].map((error, index) => error && <ReportErrorButton key={index} error={error} />)}
        </>
      )}
    </section>
  )
}
