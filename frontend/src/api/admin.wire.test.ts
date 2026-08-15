/**
 * The wire parsers, against payloads shaped the way the server actually answers.
 *
 * The component test for this screen mocks `discoverSeries`
 * themselves, which is right for asking what the UI renders and blind to what the *parsing*
 * does — so a server that nests its identifiers one level deeper than the parser looks, or
 * names a field `excerpt` where the parser reads `extract`, passes every existing test and
 * fails completely in a browser. Both of those happened. These fixtures are copied from
 * responses captured off the running backend, so they fail if either side moves again.
 *
 * The failure mode is what makes it worth a test: neither break throws. Discovery renders six
 * results with every "Link" button gone —
 * indistinguishable from the web simply not having found the show.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return { ...actual, api: { ...actual.api, get: (...args: unknown[]) => get(...args) } }
})

const { discoverSeries } = await import('@/api/admin')

/** GET /api/library/series/{id}/discover, verbatim from the server. */
const DISCOVER = {
  query: 'Ruta de Cristal 2024',
  provider: 'searxng',
  seriesId: null,
  results: [
    {
      title: 'Ruta de Cristal (TV Series 2024) — The Movie Database',
      url: 'https://www.themoviedb.org/tv/234567-ruta-de-cristal',
      snippet: 'Una repartidora de Shanghái descubre que su ruta diaria...',
      engine: 'bing',
      sourceDomain: 'themoviedb.org',
      ids: { tmdbId: 234567, tmdbKind: 'tv', imdbId: null, mydramalistId: null },
      resolvable: true,
    },
    {
      title: 'Ruta de Cristal (TV Series 2024– ) - IMDb',
      url: 'https://www.imdb.com/title/tt12345678/',
      snippet: 'Ruta de Cristal: With Lucía Márquez.',
      engine: 'bing',
      sourceDomain: 'imdb.com',
      ids: { tmdbId: null, tmdbKind: null, imdbId: 'tt12345678', mydramalistId: null },
      resolvable: true,
    },
    {
      title: 'Ruta de Cristal (2024) - MyDramaList',
      url: 'https://mydramalist.com/71234-ruta-de-cristal',
      snippet: 'Native Title: 玻璃之路.',
      engine: 'duckduckgo',
      sourceDomain: 'mydramalist.com',
      ids: { tmdbId: null, tmdbKind: null, imdbId: null, mydramalistId: '71234' },
      resolvable: false,
    },
  ],
  resolvableCount: 2,
}


describe('discovery results', () => {
  beforeEach(() => get.mockReset())

  it('keeps the identifiers the server groups under `ids`', async () => {
    get.mockResolvedValue(DISCOVER)
    const { results, configured } = await discoverSeries('series-1', 'Ruta de Cristal')

    expect(configured).toBe(true)
    expect(results.map((row) => row.tmdbId)).toEqual([234567, null, null])
    expect(results.map((row) => row.imdbId)).toEqual([null, 'tt12345678', null])
  })

  it('reads the domain the server sends rather than re-deriving it', async () => {
    get.mockResolvedValue(DISCOVER)
    const { results } = await discoverSeries('series-1', 'q')

    expect(results.map((row) => row.domain)).toEqual([
      'themoviedb.org',
      'imdb.com',
      'mydramalist.com',
    ])
    expect(results[0]?.snippet).not.toBe('')
  })

  it('still drops a result whose URL could not be safely linked', async () => {
    get.mockResolvedValue({
      ...DISCOVER,
      results: [
        { title: 'x', url: `java${''}script:alert(1)`, ids: { tmdbId: 99 } },
        ...DISCOVER.results,
      ],
    })
    const { results } = await discoverSeries('series-1', 'q')

    expect(results).toHaveLength(3)
    expect(results.some((row) => row.url.startsWith('javascript:'))).toBe(false)
  })
})

