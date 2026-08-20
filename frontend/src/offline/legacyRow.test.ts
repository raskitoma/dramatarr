import { describe, expect, it } from 'vitest'

import type { DownloadRow } from './db'
import { normaliseRow } from './legacyRow'

function legacy(overrides: Record<string, unknown> = {}): DownloadRow {
  return {
    seriesId: 's1',
    slug: 'djinn',
    title: 'Djinn Under Contract',
    posterUrl: null,
    episodeIds: ['a', 'b', 'c'],
    storedIds: ['a', 'b'],
    bytesTotal: 942_000_000,
    bytesDone: 628_000_000,
    state: 'running',
    error: null,
    updatedAt: 0,
    ...overrides,
  } as unknown as DownloadRow
}

describe('normaliseRow', () => {
  it('turns an old row into the new shape rather than throwing on it', () => {
    const { row, changed } = normaliseRow(legacy())

    expect(changed).toBe(true)
    expect(row.episodes.map((episode) => episode.id)).toEqual(['a', 'b', 'c'])
  })

  it('keeps what was already downloaded, so nothing is fetched twice', () => {
    const { row } = normaliseRow(legacy())

    expect(row.storedIds).toEqual(['a', 'b'])
  })

  it('parks an interrupted one rather than leaving it claiming to run', () => {
    expect(normaliseRow(legacy()).row.state).toBe('paused')
  })

  it('leaves a finished one finished', () => {
    expect(normaliseRow(legacy({ state: 'done' })).row.state).toBe('done')
  })

  it('survives a row with no episode list at all', () => {
    const { row } = normaliseRow(legacy({ episodeIds: undefined }))

    expect(row.episodes).toEqual([])
  })

  it('leaves a current row untouched', () => {
    const current = {
      ...legacy(),
      episodes: [{ id: 'a', label: 'T01E01', sizeBytes: 10 }],
    } as unknown as DownloadRow

    const { row, changed } = normaliseRow(current)

    expect(changed).toBe(false)
    expect(row).toBe(current)
  })
})
