/** Locale parity.
 *
 * `es` is the source of truth; `en` mirrors it. A missing key would silently render its own
 * dotted path in the UI, which is exactly the kind of bug nobody notices until a user in the
 * other language does — so it fails here instead, with a diff naming the offending keys.
 */

import { describe, expect, it } from 'vitest'

import en from './en.json'
import es from './es.json'

type Bundle = Record<string, unknown>

function isPlainObject(value: unknown): value is Bundle {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Flattens the nested bundle into dot-notation leaf paths, e.g. `admin.queue.title`. */
function flatten(value: unknown, prefix = ''): string[] {
  if (!isPlainObject(value)) return prefix ? [prefix] : []
  return Object.entries(value).flatMap(([key, child]) =>
    flatten(child, prefix ? `${prefix}.${key}` : key),
  )
}

function leafValues(value: unknown, prefix = ''): Array<[string, unknown]> {
  if (!isPlainObject(value)) return prefix ? [[prefix, value]] : []
  return Object.entries(value).flatMap(([key, child]) =>
    leafValues(child, prefix ? `${prefix}.${key}` : key),
  )
}

/** Every `{{name}}` slot in a translation, sorted and deduped. */
function placeholders(value: unknown): string[] {
  if (typeof value !== 'string') return []
  const found = value.matchAll(/\{\{\s*([^}\s,]+)[^}]*\}\}/gu)
  return [...new Set([...found].map((match) => match[1] ?? ''))].sort()
}

function describeMissing(label: string, keys: string[]): string {
  return `Missing from ${label} (${keys.length}):\n  ${keys.join('\n  ')}`
}

const esKeys = flatten(es)
const enKeys = flatten(en)

describe('i18n bundles', () => {
  it('ship a non-trivial number of keys', () => {
    // Guards against a bundle being replaced by `{}` and the parity test passing vacuously.
    expect(esKeys.length).toBeGreaterThan(100)
  })

  it('expose exactly the same key set in es and en', () => {
    const inEs = new Set(esKeys)
    const inEn = new Set(enKeys)

    const missingFromEn = esKeys.filter((key) => !inEn.has(key)).sort()
    const missingFromEs = enKeys.filter((key) => !inEs.has(key)).sort()

    const report: string[] = []
    if (missingFromEn.length > 0) report.push(describeMissing('en.json', missingFromEn))
    if (missingFromEs.length > 0) report.push(describeMissing('es.json', missingFromEs))

    expect(report.join('\n\n')).toBe('')
  })

  it('has no duplicate leaf paths', () => {
    expect(new Set(esKeys).size).toBe(esKeys.length)
    expect(new Set(enKeys).size).toBe(enKeys.length)
  })

  it('has a non-empty string at every leaf', () => {
    const offenders = [...leafValues(es), ...leafValues(en)]
      .filter(([, value]) => typeof value !== 'string' || value.trim() === '')
      .map(([key]) => key)
      .sort()

    expect(offenders.join('\n')).toBe('')
  })

  it('uses the same interpolation placeholders in both languages', () => {
    const enValues = new Map(leafValues(en))
    const mismatches = leafValues(es)
      .filter(([key, value]) => {
        if (!enValues.has(key)) return false // Already reported by the parity test.
        const left = placeholders(value).join(',')
        const right = placeholders(enValues.get(key)).join(',')
        return left !== right
      })
      .map(([key, value]) => {
        const right = placeholders(enValues.get(key)).join(', ')
        return `${key}: es{${placeholders(value).join(', ')}} en{${right}}`
      })
      .sort()

    expect(mismatches.join('\n')).toBe('')
  })

  it('keeps plural siblings paired across languages', () => {
    // i18next resolves `foo_other` off the base `foo`; an orphan `_other` never renders.
    const orphans = [...esKeys, ...enKeys]
      .filter((key) => key.endsWith('_other'))
      .filter((key) => {
        const base = key.slice(0, -'_other'.length)
        return !esKeys.includes(base) || !enKeys.includes(base)
      })
      .sort()

    expect(orphans.join('\n')).toBe('')
  })
})
