// @vitest-environment jsdom
/**
 * Which menu item lights up, and where.
 *
 * `ownsLocation` is exported for exactly this: the rule is a table of route prefixes, and the
 * two ways it goes wrong are both silent. Give `/` an `owns` entry and a naive prefix test pins
 * Inicio lit on every page in the app; drop the trailing slash from the prefix comparison and
 * `/serieskkk` starts lighting Explorar. Neither shows up in a screenshot of the happy path.
 */

import { describe, expect, it } from 'vitest'

import { NAV_ITEMS, ownsLocation } from './AppHeader'

/** The item a location should light, or '' for the pages this menu does not own. */
function lit(location: string): string {
  const match = NAV_ITEMS.find((item) => ownsLocation(item, location))
  return match ? match.href : ''
}

describe('header nav ownership', () => {
  it('lights the destination you are actually on', () => {
    expect(lit('/')).toBe('/')
    expect(lit('/browse')).toBe('/browse')
    expect(lit('/search')).toBe('/search')
    expect(lit('/requests')).toBe('/requests')
  })

  it('keeps the catalogue lit on a series and on an actor', () => {
    // The complaint that started this: the menu went dark on the page people open most.
    expect(lit('/series/american-sniper-the-kid-of-guns')).toBe('/browse')
    expect(lit('/people/273')).toBe('/browse')
  })

  it('does not let home swallow every route', () => {
    // `/` is a prefix of literally everything, so it must match exactly and own nothing.
    for (const location of ['/browse', '/search', '/requests', '/series/x', '/account']) {
      expect(lit(location), `${location} must not light home`).not.toBe('/')
    }
  })

  it('matches whole path segments, not string prefixes', () => {
    expect(lit('/serieskkk')).toBe('')
    expect(lit('/peoplewatching')).toBe('')
  })

  it('lights nothing on the pages this menu is not a way into', () => {
    // The avatar menu owns the account; AdminNav owns its own sections. Lighting one of these
    // four would point at a door that does not lead where the user is.
    expect(lit('/account')).toBe('')
    expect(lit('/admin')).toBe('')
    expect(lit('/admin/settings')).toBe('')
    expect(lit('/nope')).toBe('')
  })
})
