/** The worker's rules, which are the half that can be tested.
 *
 * The cases here are the ones that would be silent: a watch-progress POST answered from a cache
 * would throw away three weeks of viewing, and artwork classified as media would be served
 * cache-first and never refresh.
 */

import { describe, expect, it } from 'vitest'

import { classify, type RequestFacts } from './route'

function facts(overrides: Partial<RequestFacts> = {}): RequestFacts {
  return {
    url: 'https://dramatarr.example.com/api/library/series/x-ray',
    method: 'GET',
    mode: 'cors',
    sameOrigin: true,
    ...overrides,
  }
}

describe('classify', () => {
  it('serves an episode from the cache first', () => {
    expect(classify(facts({ url: 'https://d.test/api/stream/ep123' }))).toBe('media')
  })

  it('treats artwork as ordinary API traffic, not as media', () => {
    /* Both live under /api/stream. Only one of them is gigabytes. */
    expect(classify(facts({ url: 'https://d.test/api/stream/images/abc.webp' }))).toBe('api')
  })

  it('caches the session, without which the app bounces to the login page offline', () => {
    expect(classify(facts({ url: 'https://d.test/api/auth/session' }))).toBe('api')
  })

  it('never touches a write', () => {
    for (const method of ['POST', 'PATCH', 'DELETE', 'PUT']) {
      expect(classify(facts({ method, url: 'https://d.test/api/watch/ep1/progress' }))).toBe(
        'passthrough',
      )
    }
  })

  it('never caches watch state, even on a GET', () => {
    /* Progress written on the plane lives in IndexedDB. A cached copy of the server's older
       answer would overwrite it on the way back. */
    expect(classify(facts({ url: 'https://d.test/api/watch/ep1' }))).toBe('passthrough')
  })

  it('leaves the admin API alone', () => {
    expect(classify(facts({ url: 'https://d.test/api/admin/jobs' }))).toBe('passthrough')
  })

  it('recognises a cold page load', () => {
    expect(classify(facts({ mode: 'navigate', url: 'https://d.test/series/x-ray' }))).toBe(
      'navigate',
    )
  })

  it('ignores another origin entirely', () => {
    expect(classify(facts({ sameOrigin: false, url: 'https://fonts.example/x.woff2' }))).toBe(
      'passthrough',
    )
  })

  it('does not choke on a URL it cannot parse', () => {
    expect(classify(facts({ url: 'not a url' }))).toBe('passthrough')
  })
})
