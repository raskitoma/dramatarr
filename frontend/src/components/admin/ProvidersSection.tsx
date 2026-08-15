/** The household's list of vertical-drama platforms.
 *
 * Its own component, and its own save, because it is the one setting that is a *list* rather
 * than a field. Threading an array editor through the settings page's form state would have
 * meant teaching that state machine about rows, and the rest of the page would carry the cost
 * of a shape only this section has.
 *
 * The domain is optional and the form says so, which matters more than it looks. A platform
 * with no domain is still a perfectly good network label — it simply cannot narrow a web
 * search. Filling one in with a guess is worse than leaving it blank: a `site:` filter with a
 * wrong domain does not error, it returns nothing, silently and forever. So the field is
 * marked optional, validated only for shape, and never invented.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { adminKeys, getSettings, updateSettings } from '@/api/admin'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { IconButton } from '@/components/IconButton'
import { Input } from '@/components/Input'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { CloseIcon, PlusIcon } from '@/components/icons'

import styles from './ProvidersSection.module.css'

export interface ProviderEntry {
  name: string
  domain: string
}

/** Mirrors `vocab.clean_domain`. The server re-cleans on write; this is for the live hint. */
function looksLikeDomain(value: string): boolean {
  const text = value.trim()
  if (text === '') return true // empty is valid — the field is optional
  const host = (text.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').split('/')[0] ?? '').replace(
    /^www\./i,
    '',
  )
  return /^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(host)
}

function readProviders(settings: Record<string, unknown> | undefined): ProviderEntry[] {
  const raw = settings?.['library.providers']
  if (!Array.isArray(raw)) return []
  return raw.flatMap((entry) => {
    if (typeof entry === 'string') return [{ name: entry, domain: '' }]
    if (entry && typeof entry === 'object') {
      const row = entry as { name?: unknown; domain?: unknown }
      const name = String(row.name ?? '').trim()
      return name ? [{ name, domain: String(row.domain ?? '') }] : []
    }
    return []
  })
}

export function ProvidersSection() {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()

  const settings = useQuery({ queryKey: adminKeys.settings(), queryFn: getSettings })
  const [rows, setRows] = useState<ProviderEntry[] | null>(null)
  /* What the server last gave us, so Save can be disabled until something actually changed —
     the same rule every other section of this page follows. Without it the button invites a
     write that would be a no-op, and on a settings screen that is indistinguishable from a
     button that does not work. */
  const [baseline, setBaseline] = useState('')

  // Seeded once from the server, then owned locally so typing is not fought by refetches.
  useEffect(() => {
    if (settings.data && rows === null) {
      const loaded = readProviders(settings.data)
      setRows(loaded)
      setBaseline(JSON.stringify(loaded))
    }
  }, [settings.data, rows])

  const save = useMutation({
    mutationFn: (next: ProviderEntry[]) =>
      updateSettings({
        // Blank rows are dropped here rather than refused: an admin who added a row and
        // changed their mind should be able to save, not hunt for the empty one.
        'library.providers': next
          .map((row) => ({ name: row.name.trim(), domain: row.domain.trim() }))
          .filter((row) => row.name !== ''),
      } as never),
    onSuccess: async (_data, saved) => {
      setBaseline(JSON.stringify(saved))
      show(t('common.saved'), 'success')
      await queryClient.invalidateQueries({ queryKey: adminKeys.settings() })
      await queryClient.invalidateQueries({ queryKey: adminKeys.vocabulary() })
    },
  })

  if (settings.isLoading || rows === null) {
    return (
      <section className={styles.card} id="settings-providers">
        <h2 className={styles.title}>{t('admin.settings.providersSection')}</h2>
        <Spinner label={t('common.loading')} />
      </section>
    )
  }

  const update = (index: number, patch: Partial<ProviderEntry>) =>
    setRows(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)))

  const withDomains = rows.filter((row) => row.domain.trim() !== '').length
  const dirty = JSON.stringify(rows) !== baseline

  return (
    <section className={styles.card} id="settings-providers">
      <h2 className={styles.title}>{t('admin.settings.providersSection')}</h2>
      <p className={styles.hint}>{t('admin.settings.providersHint')}</p>

      {settings.isError ? <ErrorState error={settings.error} /> : null}
      {save.isError ? <ErrorState error={save.error} /> : null}

      {/* A header row rather than a `Field` per cell. Two reasons, and the first is the bug
          this replaced: only the domain had a hint, so aligning the two boxes put their
          inputs on different baselines. The second is that twenty rows meant forty repeated
          labels. Each input keeps an `aria-label`, so the column heading is decoration and
          the control is still named for a screen reader. */}
      <div className={styles.head} aria-hidden="true">
        <span>{t('admin.settings.providerName')}</span>
        <span>{t('admin.settings.providerDomain')}</span>
      </div>

      <ul className={styles.list}>
        {rows.map((row, index) => {
          const valid = looksLikeDomain(row.domain)
          return (
            <li key={index} className={styles.row}>
              <Input
                value={row.name}
                aria-label={t('admin.settings.providerName')}
                onChange={(event) => update(index, { name: event.target.value })}
              />
              <Input
                value={row.domain}
                invalid={!valid}
                spellCheck={false}
                autoCapitalize="none"
                autoCorrect="off"
                placeholder="dramabox.com"
                aria-label={t('admin.settings.providerDomain')}
                aria-invalid={!valid}
                onChange={(event) => update(index, { domain: event.target.value })}
              />
              <IconButton
                size="sm"
                label={t('admin.settings.providerRemove', { name: row.name || '—' })}
                onClick={() => setRows(rows.filter((_, i) => i !== index))}
              >
                <CloseIcon />
              </IconButton>
              {valid ? null : (
                <p className={styles.rowError} role="alert">
                  {t('admin.settings.providerDomainInvalid')}
                </p>
              )}
            </li>
          )
        })}
      </ul>

      <p className={styles.count}>
        {t('admin.settings.providersCount', { total: rows.length, withDomains })}
      </p>

      <div className={styles.actions}>
        <Button onClick={() => setRows([...rows, { name: '', domain: '' }])}>
          <PlusIcon aria-hidden="true" />
          {t('admin.settings.providerAdd')}
        </Button>
        <Button
          variant="primary"
          loading={save.isPending}
          disabled={!dirty || rows.some((row) => !looksLikeDomain(row.domain))}
          onClick={() => save.mutate(rows)}
        >
          {save.isPending ? t('common.saving') : t('common.save')}
        </Button>
      </div>
    </section>
  )
}

export default ProvidersSection
