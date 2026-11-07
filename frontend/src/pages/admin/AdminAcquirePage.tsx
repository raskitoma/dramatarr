import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'
import { api } from '@/api/client'
import { Button } from '@/components/Button'
import { Input } from '@/components/Input'
import { Field } from '@/components/Field'
import type { Job } from '@/api/types'
import styles from './AdminAcquirePage.module.css'

type Title = { id: number; title: string; year: number | null; synopsis: string | null; episodeCount: number | null }
type Results = { items: Title[]; nextCursor: string | null }

export function AdminAcquirePage() {
  const { t } = useTranslation()
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [cursor, setCursor] = useState<string | null>(null)
  const [selected, setSelected] = useState<Title | null>(null)
  const [url, setUrl] = useState('')
  const status = useQuery({ queryKey: ['integration-status'], queryFn: () => api.get<Record<string, boolean>>('/admin/integration/status') })
  const catalogue = useQuery({
    queryKey: ['catalogue-acquire', search, cursor],
    queryFn: () => api.get<Results>('/admin/integration/catalogue', { q: search, cursor }),
    enabled: search.length > 0,
  })
  const acquire = useMutation({ mutationFn: () => api.post<Job>('/admin/integration/acquire', { url, titleId: selected?.id }) })
  return <section className={styles.page}>
    <h1>{t('acquisition.title')}</h1>
    <a href="/api/admin/integration/console/admin" target="_blank" rel="noopener">{t('acquisition.console')}</a>
    <p>{t('acquisition.description')}</p>
    {status.data && <p>{t('acquisition.catalogue')}: {t(status.data.catalogue ? 'acquisition.ready' : 'acquisition.unavailable')} · {t('acquisition.engine')}: {t(status.data.downloader ? 'acquisition.ready' : 'acquisition.unavailable')}</p>}
    <form className={styles.form} onSubmit={e => { e.preventDefault(); setSearch(query.trim()); setCursor(null) }}>
      <Field htmlFor="acquire-search" label={t('acquisition.search')}><Input id="acquire-search" value={query} onChange={e => setQuery(e.target.value)} /></Field>
      <Button type="submit" loading={catalogue.isFetching}>{t('acquisition.search')}</Button>
    </form>
    {catalogue.isError && <p role="alert">{t('acquisition.searchError')}</p>}
    {catalogue.data && <div className={styles.results}>
      {catalogue.data.items.length === 0 && <p>{t('acquisition.empty')}</p>}
      {catalogue.data.items.map(title => <article className={styles.card} key={title.id}>
        <h2>{title.title}{title.year ? ` (${title.year})` : ''}</h2>
        <p>{title.synopsis}</p>
        <Button onClick={() => setSelected(title)} aria-pressed={selected?.id === title.id}>{t(selected?.id === title.id ? 'acquisition.selected' : 'acquisition.select')}</Button>
      </article>)}
      {catalogue.data.nextCursor && <Button onClick={() => setCursor(catalogue.data!.nextCursor)}>{t('acquisition.next')}</Button>}
    </div>}
    <form className={styles.form} onSubmit={e => { e.preventDefault(); acquire.mutate() }}>
      {selected && <p>{t('acquisition.binding', { title: selected.title })} <Button onClick={() => setSelected(null)}>{t('acquisition.clear')}</Button></p>}
      <Field htmlFor="acquire-url" label={t('acquisition.url')}><Input id="acquire-url" type="url" required value={url} onChange={e => setUrl(e.target.value)} placeholder="https://dramafren.org/…" /></Field>
      <p>{t('acquisition.preservation')}</p>
      <Button type="submit" variant="primary" loading={acquire.isPending} disabled={!url.trim() || acquire.isSuccess}>{t('acquisition.start')}</Button>
      {acquire.isError && <p role="alert">{t('acquisition.error')}</p>}
      {acquire.isSuccess && <p role="status">{t('acquisition.queued')} <Link href="/admin/queue">{t('nav.queue')}</Link> <Button onClick={() => { acquire.reset(); setUrl(''); setSelected(null) }}>{t('acquisition.another')}</Button></p>}
    </form>
  </section>
}
