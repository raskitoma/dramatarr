/** Connect dramatarr to the household's own indexer.
 *
 * Two fields and a button, and the button is the reason this is a component rather than two
 * more inputs on the settings form. A URL and a key that are merely *stored* leave an admin
 * with four indistinguishable failures — typo, wrong port, wrong key, container down — none of
 * which show up here. They show up hours later as an empty search on a different screen, which
 * is the worst possible place to learn that a setting is wrong.
 *
 * So the test says which of them it is, and when it works it reports the catalogue size. That
 * number is also the answer to "is it worth switching over yet": an index with 300 titles and
 * one with 7000 are different propositions for a library this app has to match against.
 */

import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { testDramadex, type DramadexStatus } from '@/api/admin'
import { Badge } from '@/components/Badge'
import { Button } from '@/components/Button'
import { Field } from '@/components/Field'
import { Input } from '@/components/Input'

import styles from './DramadexSection.module.css'

export interface DramadexSectionProps {
  baseUrl: string
  apiKey: string
  /** True when a key is already stored; the field then shows a badge instead of the secret. */
  keyConfigured: boolean
  editingKey: boolean
  onEditKey: () => void
  onChange: (patch: { baseUrl?: string; apiKey?: string }) => void
}

export function DramadexSection({
  baseUrl,
  apiKey,
  keyConfigured,
  editingKey,
  onEditKey,
  onChange,
}: DramadexSectionProps) {
  const { t } = useTranslation()
  const [status, setStatus] = useState<DramadexStatus | null>(null)

  const test = useMutation({
    mutationFn: testDramadex,
    onSuccess: setStatus,
  })

  const keyEditable = !keyConfigured || editingKey

  return (
    <div className={styles.section}>
      <p className={styles.intro}>{t('admin.settings.dramadexIntro')}</p>

      <div className={styles.grid}>
        <Field
          label={t('admin.settings.dramadexUrl')}
          htmlFor="dramadex-url"
          hint={t('admin.settings.dramadexUrlHint')}
        >
          <Input
            id="dramadex-url"
            type="url"
            inputMode="url"
            placeholder="http://10.0.50.240:8099"
            value={baseUrl}
            onChange={(event) => onChange({ baseUrl: event.target.value })}
          />
        </Field>

        <Field
          label={t('admin.settings.dramadexKey')}
          htmlFor="dramadex-key"
          hint={
            keyConfigured && !editingKey
              ? t('admin.settings.secretKeep')
              : t('admin.settings.dramadexKeyHint')
          }
        >
          {keyEditable ? (
            <Input
              id="dramadex-key"
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(event) => onChange({ apiKey: event.target.value })}
            />
          ) : (
            <div className={styles.secretRow}>
              <Badge tone="success">{t('admin.settings.secretSet')}</Badge>
              <Button id="dramadex-key" type="button" size="sm" onClick={onEditKey}>
                {t('admin.settings.secretReplace')}
              </Button>
            </div>
          )}
        </Field>
      </div>

      <div className={styles.testRow}>
        {/* Deliberately tests what is *saved*, not what is typed. Testing the form would tell
            an admin the connection works and then leave them wondering why nothing else does,
            because the value never reached the database. */}
        <Button type="button" size="sm" loading={test.isPending} onClick={() => test.mutate()}>
          {t('admin.settings.dramadexTest')}
        </Button>
        <span className={styles.hint}>{t('admin.settings.dramadexTestHint')}</span>
      </div>

      {status === null ? null : (
        <p className={styles.result} role="status">
          {!status.configured ? (
            <Badge tone="neutral">{t('admin.settings.dramadexUnset')}</Badge>
          ) : status.reachable ? (
            <>
              <Badge tone="success">{t('admin.settings.dramadexOk')}</Badge>{' '}
              {t('admin.settings.dramadexCounts', {
                titles: status.titles.toLocaleString(),
                episodes: status.episodes.toLocaleString(),
                people: status.people.toLocaleString(),
              })}
            </>
          ) : (
            <>
              <Badge tone="danger">{t('admin.settings.dramadexFailed')}</Badge>{' '}
              {/* The distinction that saves the debugging session: a refused key and an
                  unreachable host look identical from every other screen. */}
              {t(`errors.${status.detail}`, { defaultValue: status.detail })}
            </>
          )}
        </p>
      )}
    </div>
  )
}

export default DramadexSection
