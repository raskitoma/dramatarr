/** Bulk import: a folder of nameless files becomes a numbered season, in one approval.
 *
 * The scanner keeps every file it cannot parse (§8), and the Unmatched tab lets an admin place
 * them one at a time. That is the wrong tool for what real libraries actually produce: a folder
 * of 58 files called `701151231.720p.narrowv3.mp4` … `701151288.720p.narrowv3.mp4`, where no
 * single filename says anything but the *set* says everything. This page plans such a folder as
 * a group and asks for one decision instead of fifty-eight.
 *
 * Two things shape the screen.
 *
 * First, the explanation. Approving 58 episodes on someone else's say-so is only reasonable if
 * you can see the reasoning, so the sentence explaining *why* the order is what it is gets the
 * most prominent block on the card — above the controls, above the table — and every warning
 * the plan raised is a visible alert rather than fine print.
 *
 * Second, what "import" means. `LIBRARY_ROOT` is mounted read-only: importing creates episode
 * rows that point at the files under their existing names, and never renames, moves or writes
 * anything. Every card says so once, in words, immediately above the button — an admin who
 * expects tidy filenames afterwards and does not find them will conclude the import failed.
 * The rename script is the answer for that admin, and it is a download they run themselves.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import { useCallback, useEffect, useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'wouter'

import {
  adminKeys,
  applyImport,
  importConfidenceTone,
  listUnmatchedGroups,
  planImport,
  renameScriptUrl,
  type ImportApplyRequest,
  type ImportMessage,
  type ImportPlan,
  type ImportPlanRequest,
  type ImportResult,
  type ImportStrategy,
  type UnmatchedGroup,
} from '@/api/admin'
import { libraryKeys } from '@/api/library'
import ImportPlanTable from '@/components/admin/ImportPlanTable'
import Badge from '@/components/Badge'
import Button from '@/components/Button'
import EmptyState from '@/components/EmptyState'
import ErrorState from '@/components/ErrorState'
import Field from '@/components/Field'
import Input from '@/components/Input'
import Select from '@/components/Select'
import Spinner from '@/components/Spinner'
import { useToast } from '@/components/Toast'
import { formatBytes, formatPercent } from '@/lib/format'

import styles from './AdminImportPage.module.css'

/** Long enough that typing "12" is one re-plan, short enough to feel immediate. */
const REPLAN_DELAY_MS = 350

const MAX_SEASON = 999
const MAX_EPISODE = 9999
const MIN_YEAR = 1800
const MAX_YEAR = 2999

// --- Page -----------------------------------------------------------------------------------

export function AdminImportPage() {
  const { t } = useTranslation()

  const groupsQuery = useQuery({
    queryKey: adminKeys.unmatchedGroups(),
    queryFn: listUnmatchedGroups,
  })

  const groups = groupsQuery.data ?? []

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>{t('admin.import.title')}</h1>
        <p className={styles.subtitle}>{t('admin.import.subtitle')}</p>
      </header>

      {groupsQuery.isPending ? <Spinner label={t('common.loading')} /> : null}

      {groupsQuery.isError ? (
        <ErrorState error={groupsQuery.error} onRetry={() => void groupsQuery.refetch()} />
      ) : null}

      {!groupsQuery.isPending && !groupsQuery.isError && groups.length === 0 ? (
        <EmptyState
          title={t('admin.import.empty')}
          description={t('admin.import.emptyHint')}
          action={
            <Link href="/admin/queue" className={styles.emptyLink}>
              {t('admin.import.emptyAction')}
            </Link>
          }
        />
      ) : null}

      {groups.map((group) => (
        <GroupCard key={group.id} group={group} />
      ))}
    </div>
  )
}

// --- One folder -----------------------------------------------------------------------------

interface GroupCardProps {
  group: UnmatchedGroup
}

function GroupCard({ group }: GroupCardProps) {
  const { t } = useTranslation()
  const { show } = useToast()
  const queryClient = useQueryClient()
  const resolveMessage = useImportMessage()

  const headingId = useId()
  const whyId = useId()
  const warningsId = useId()
  const strategyId = useId()
  const seasonId = useId()
  const startId = useId()
  const titleId = useId()
  const yearId = useId()

  // Season and first-episode default to 1/1 rather than to whatever the server would infer:
  // the boxes then never fight the value the admin typed into them, and for a folder with no
  // episode signal at all, 1 is the answer often enough that anything cleverer is a surprise.
  const [seasonText, setSeasonText] = useState('1')
  const [startText, setStartText] = useState('1')
  const [strategy, setStrategy] = useState<ImportStrategy | null>(null)
  const [target, setTarget] = useState<'existing' | 'new'>(
    group.existingSeriesId !== null ? 'existing' : 'new',
  )
  const [titleText, setTitleText] = useState<string | null>(null)
  const [yearText, setYearText] = useState<string | null>(null)
  const [result, setResult] = useState<ImportResult | null>(null)

  const seasonNumber = parseIntInRange(seasonText, 0, MAX_SEASON)
  const startNumber = parseIntInRange(startText, 1, MAX_EPISODE)

  const plannedSeason = useSettledNumber(seasonNumber, 1, REPLAN_DELAY_MS)
  const plannedStart = useSettledNumber(startNumber, 1, REPLAN_DELAY_MS)

  const request: ImportPlanRequest = {
    groupId: group.id,
    seasonNumber: plannedSeason,
    startEpisodeNumber: plannedStart,
    ...(strategy !== null ? { strategy } : {}),
  }

  const planQuery = useQuery({
    queryKey: adminKeys.importPlan(request),
    queryFn: () => planImport(request),
    // Keep the previous mapping on screen while a changed control re-plans: the table
    // blanking out on every keystroke would make the page feel like it lost the folder.
    placeholderData: (previous) => previous,
    retry: false,
  })

  const plan = planQuery.data ?? null
  const replanning = planQuery.isFetching && !planQuery.isPending

  const existingSeriesId = plan?.existingSeriesId ?? group.existingSeriesId
  const existingSeriesTitle = plan?.existingSeriesTitle ?? group.existingSeriesTitle
  // A target the card cannot offer must never be the one it submits.
  const effectiveTarget = existingSeriesId === null ? 'new' : target

  const inferredTitle = plan?.inferredTitle ?? group.inferredTitle
  const inferredYear = plan?.inferredYear ?? group.inferredYear
  const titleValue = titleText ?? inferredTitle
  const yearValue = yearText ?? (inferredYear === null ? '' : String(inferredYear))
  const yearNumber = yearValue.trim() === '' ? null : parseIntInRange(yearValue, MIN_YEAR, MAX_YEAR)

  const seasonError = seasonNumber === null ? t('admin.settings.invalidNumber') : undefined
  const startError = startNumber === null ? t('admin.settings.invalidNumber') : undefined
  const titleError =
    effectiveTarget === 'new' && titleValue.trim() === '' ? t('common.required') : undefined
  const yearError =
    effectiveTarget === 'new' && yearValue.trim() !== '' && yearNumber === null
      ? t('admin.import.invalidYear')
      : undefined

  const apply = useMutation({
    mutationFn: (body: ImportApplyRequest) => applyImport(body),
    onSuccess: async (imported) => {
      setResult(imported)
      show(t('admin.import.imported', { count: imported.createdEpisodes }), 'success')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: adminKeys.unmatchedGroups() }),
        queryClient.invalidateQueries({ queryKey: [...adminKeys.all, 'unmatched'] }),
        queryClient.invalidateQueries({ queryKey: adminKeys.series() }),
        queryClient.invalidateQueries({ queryKey: adminKeys.stats() }),
        queryClient.invalidateQueries({ queryKey: libraryKeys.all }),
      ])
    },
  })

  const canImport =
    plan !== null &&
    plan.items.length > 0 &&
    !replanning &&
    seasonError === undefined &&
    startError === undefined &&
    titleError === undefined &&
    yearError === undefined

  const onImport = (): void => {
    if (plan === null || !canImport) return
    apply.mutate({
      groupId: group.id,
      strategy: plan.strategy,
      seriesId: effectiveTarget === 'existing' ? existingSeriesId : null,
      newSeries:
        effectiveTarget === 'new' ? { title: titleValue.trim(), year: yearNumber } : null,
      items: plan.items.map((item) => ({
        mediaFileId: item.mediaFileId,
        seasonNumber: item.seasonNumber,
        episodeNumber: item.episodeNumber,
      })),
    })
  }

  if (result !== null) {
    return <ImportedCard group={group} result={result} headingId={headingId} />
  }

  return (
    <article className={styles.card} aria-labelledby={headingId}>
      <header className={styles.cardHeader}>
        <div className={styles.cardHeading}>
          <h2 id={headingId} className={styles.folder}>
            {group.folderName}
          </h2>
          <p className={styles.path}>{group.relativePath}</p>
          {/* Separators are drawn in CSS so a wrapped line never begins or ends on a stray
              dot, and so they disappear entirely when the row becomes a column on a phone. */}
          <p className={styles.meta}>
            <span>{t('admin.import.fileCount', { count: group.fileCount })}</span>
            <span>{formatBytes(plan?.totalSizeBytes ?? group.totalSizeBytes)}</span>
            <span>{t('admin.import.detectedTitle', { title: inferredTitle })}</span>
          </p>
        </div>
        {plan !== null ? (
          <Badge tone={importConfidenceTone(plan.confidence)}>
            {t('admin.import.confidenceValue', { percent: formatPercent(plan.confidence * 100) })}
          </Badge>
        ) : null}
      </header>

      {planQuery.isPending ? <Spinner label={t('admin.import.planning')} /> : null}

      {planQuery.isError ? (
        <ErrorState error={planQuery.error} onRetry={() => void planQuery.refetch()} />
      ) : null}

      {plan !== null ? (
        <>
          {/* The single most important element on the card: the reasoning, in a sentence,
              before any control that could change it. */}
          <section className={styles.why} aria-labelledby={whyId}>
            <h3 id={whyId} className={styles.whyTitle}>
              {t('admin.import.whyTitle')}
            </h3>
            <p className={styles.whyText}>
              {resolveMessage('admin.import.why', plan.explanation, derivedParams(plan, t))}
            </p>
          </section>

          {plan.warnings.length > 0 ? (
            <section className={styles.warnings} aria-labelledby={warningsId}>
              <h3 id={warningsId} className={styles.warningsTitle}>
                {t('admin.import.warningsTitle')}
              </h3>
              <ul className={styles.warningList}>
                {plan.warnings.map((warning, index) => (
                  <li key={`${warning.code}-${index}`} className={styles.warningItem}>
                    <WarningIcon />
                    <span>{resolveMessage('admin.import.warnings', warning, {})}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <div className={styles.controls}>
            <Field
              label={t('admin.import.strategyLabel')}
              htmlFor={strategyId}
              hint={t('admin.import.strategyHint')}
            >
              <Select
                id={strategyId}
                value={strategy ?? plan.strategy}
                onChange={(event) => setStrategy(event.target.value as ImportStrategy)}
              >
                {plan.availableStrategies.map((option) => (
                  <option key={option} value={option}>
                    {strategyLabel(option, t)}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label={t('admin.import.seasonLabel')} htmlFor={seasonId} error={seasonError}>
              <Input
                id={seasonId}
                inputMode="numeric"
                value={seasonText}
                invalid={Boolean(seasonError)}
                onChange={(event) => setSeasonText(event.target.value)}
              />
            </Field>

            <Field label={t('admin.import.startLabel')} htmlFor={startId} error={startError}>
              <Input
                id={startId}
                inputMode="numeric"
                value={startText}
                invalid={Boolean(startError)}
                onChange={(event) => setStartText(event.target.value)}
              />
            </Field>
          </div>

          <fieldset className={styles.target}>
            <legend className={styles.targetLegend}>{t('admin.import.targetTitle')}</legend>

            {existingSeriesId !== null ? (
              <div className={styles.choices}>
                <label className={styles.choice}>
                  <input
                    type="radio"
                    name={`import-target-${group.id}`}
                    value="existing"
                    checked={effectiveTarget === 'existing'}
                    onChange={() => setTarget('existing')}
                  />
                  <span>
                    {t('admin.import.targetExisting', {
                      title: existingSeriesTitle ?? inferredTitle,
                    })}
                  </span>
                </label>
                <label className={styles.choice}>
                  <input
                    type="radio"
                    name={`import-target-${group.id}`}
                    value="new"
                    checked={effectiveTarget === 'new'}
                    onChange={() => setTarget('new')}
                  />
                  <span>{t('admin.import.targetNew')}</span>
                </label>
              </div>
            ) : (
              <p className={styles.targetHint}>{t('admin.import.targetNewOnly')}</p>
            )}

            {effectiveTarget === 'new' ? (
              <div className={styles.newSeries}>
                <Field
                  label={t('admin.import.newTitleLabel')}
                  htmlFor={titleId}
                  required
                  error={titleError}
                >
                  <Input
                    id={titleId}
                    value={titleValue}
                    invalid={Boolean(titleError)}
                    onChange={(event) => setTitleText(event.target.value)}
                  />
                </Field>
                <Field
                  label={t('admin.import.newYearLabel')}
                  htmlFor={yearId}
                  hint={t('common.optional')}
                  error={yearError}
                >
                  <Input
                    id={yearId}
                    inputMode="numeric"
                    value={yearValue}
                    invalid={Boolean(yearError)}
                    onChange={(event) => setYearText(event.target.value)}
                  />
                </Field>
              </div>
            ) : null}
          </fieldset>

          <div className={styles.tableBlock}>
            <p className={styles.tableStatus} aria-live="polite">
              {replanning ? t('admin.import.replanning') : t('admin.import.planReady')}
            </p>
            <ImportPlanTable items={plan.items} caption={group.folderName} />
          </div>

          {/* Said once, right where the decision is made. */}
          <p className={styles.noRename}>{t('admin.import.noRename')}</p>

          {apply.isError ? <ErrorState error={apply.error} /> : null}

          <div className={styles.actions}>
            <Button
              variant="primary"
              loading={apply.isPending}
              disabled={!canImport}
              onClick={onImport}
            >
              {t('admin.import.import')}
            </Button>
            {/* A plain link, not a fetch: the admin sees where the script comes from, the
                browser saves it, and dramatarr never runs it. */}
            <a
              className={styles.scriptLink}
              href={renameScriptUrl({
                groupId: group.id,
                strategy: plan.strategy,
                seasonNumber: plan.seasonNumber,
                startEpisodeNumber: plan.startEpisodeNumber,
                title: titleValue.trim() || inferredTitle,
              })}
              download
            >
              {t('admin.import.renameScript')}
            </a>
          </div>
          <p className={styles.actionsHint}>{t('admin.import.renameScriptHint')}</p>
        </>
      ) : null}
    </article>
  )
}

// --- After the import -----------------------------------------------------------------------

interface ImportedCardProps {
  group: UnmatchedGroup
  result: ImportResult
  headingId: string
}

function ImportedCard({ group, result, headingId }: ImportedCardProps) {
  const { t } = useTranslation()

  return (
    <article className={styles.cardDone} aria-labelledby={headingId}>
      <header className={styles.cardHeader}>
        <div className={styles.cardHeading}>
          <h2 id={headingId} className={styles.folder}>
            {group.folderName}
          </h2>
          <p className={styles.meta}>{t('admin.import.successTitle')}</p>
        </div>
        <Badge tone="success">{t('admin.import.successBadge')}</Badge>
      </header>

      <p className={styles.successBody}>
        {t('admin.import.successBody', {
          title: result.seriesTitle,
          count: result.createdEpisodes,
        })}
      </p>
      {/* The moment the "why are the filenames still ugly?" question gets asked. */}
      <p className={styles.noRename}>{t('admin.import.successFilesKept')}</p>

      <div className={styles.actions}>
        <Link href={`/series/${result.seriesSlug}`} className={styles.primaryLink}>
          {t('admin.import.viewSeries')}
        </Link>
        <Link href={`/admin/library/${result.seriesId}`} className={styles.scriptLink}>
          {t('admin.import.editSeries')}
        </Link>
      </div>
    </article>
  )
}

// --- Messages -------------------------------------------------------------------------------

/**
 * Turns a server `ImportMessage` into a sentence in the reader's language.
 *
 * The code is looked up first and the server's own `text` is only a fallback, so a backend
 * that learns a new reason degrades to its English sentence instead of rendering
 * `admin.import.why.SOME_CODE` at a Spanish-speaking admin — the same contract `ErrorState`
 * applies to error codes.
 *
 * `extra` is merged *under* the server's params: everything derivable from the plan itself
 * (how many files, the first and last sort keys, the shared prefix) is supplied locally, so a
 * sentence still reads correctly when the server did not send that particular value.
 */
function useImportMessage(): (
  prefix: string,
  message: ImportMessage,
  extra: Record<string, string | number>,
) => string {
  const { t, i18n } = useTranslation()

  return useCallback(
    (prefix: string, message: ImportMessage, extra: Record<string, string | number>) => {
      const key = `${prefix}.${message.code}`
      if (i18n.exists(key)) return t(key, { ...extra, ...message.params })
      if (message.text !== null && message.text !== '') return message.text
      return t(`${prefix}.generic`, { ...extra, ...message.params })
    },
    [t, i18n],
  )
}

function strategyLabel(strategy: ImportStrategy, t: TFunction): string {
  const key = `admin.import.strategies.${strategy}`
  const label = t(key)
  // A strategy we have no label for is still selectable; showing its id beats showing a path.
  return label === key ? strategy : label
}

/**
 * The facts about a plan that can be read straight off it.
 *
 * Supplying these locally is what makes the explanation robust: the sentence for a numeric
 * sequence wants the shared prefix and the first and last numbers, and those are all visible
 * in `items` whether or not the server bothered to repeat them in `params`.
 */
function derivedParams(plan: ImportPlan, t: TFunction): Record<string, string | number> {
  const names = plan.items.map((item) => item.filename)
  const first = plan.items[0]
  const last = plan.items[plan.items.length - 1]

  return {
    count: plan.items.length,
    folder: plan.folderName,
    season: plan.seasonNumber,
    start: plan.startEpisodeNumber,
    strategy: strategyLabel(plan.strategy, t),
    prefix: sharedPrefix(names),
    suffix: sharedSuffix(names),
    first: first ? (first.orderKey ?? first.filename) : '',
    last: last ? (last.orderKey ?? last.filename) : '',
  }
}

/** Longest string every name starts with. Empty for a single name — nothing is "shared". */
function sharedPrefix(names: string[]): string {
  const [head, ...rest] = names
  if (head === undefined || rest.length === 0) return ''
  let length = head.length
  for (const name of rest) {
    let index = 0
    while (index < length && index < name.length && name[index] === head[index]) index += 1
    length = index
    if (length === 0) return ''
  }
  return head.slice(0, length)
}

function sharedSuffix(names: string[]): string {
  const reversed = names.map((name) => [...name].reverse().join(''))
  return [...sharedPrefix(reversed)].reverse().join('')
}

// --- Small helpers --------------------------------------------------------------------------

/** `null` for anything that is not a whole number inside the range — including a half-typed box. */
function parseIntInRange(text: string, min: number, max: number): number | null {
  const trimmed = text.trim()
  if (!/^\d+$/u.test(trimmed)) return null
  const value = Number.parseInt(trimmed, 10)
  if (!Number.isFinite(value) || value < min || value > max) return null
  return value
}

/**
 * Debounced value for the query key, where an invalid box never becomes one.
 *
 * Clearing the season field to retype it must not fire a plan for "no season" and flash a
 * different table; the last good number stands until a new good number settles.
 */
function useSettledNumber(value: number | null, initial: number, delayMs: number): number {
  const [settled, setSettled] = useState(initial)

  useEffect(() => {
    if (value === null) return
    const handle = window.setTimeout(() => setSettled(value), delayMs)
    return () => window.clearTimeout(handle)
  }, [value, delayMs])

  return settled
}

function WarningIcon() {
  return (
    <svg
      className={styles.warningIcon}
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
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4M12 17h.01" />
    </svg>
  )
}

export default AdminImportPage
