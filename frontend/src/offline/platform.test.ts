/**
 * Which fact matters on which platform.
 *
 * The owner's iPhone reported "Almacenamiento protegido: no" while the same download on the
 * laptop reported yes. Both were accurate and the iPhone one was the wrong question: WebKit
 * grants `persist()` on heuristics and defaults to refusing, so an iPhone says no even when the
 * storage is perfectly safe. What keeps it safe there is being a Home Screen web app.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

async function load() {
  vi.resetModules()
  return import('./register')
}

function stubPlatform(userAgent: string, extras: Record<string, unknown> = {}) {
  vi.stubGlobal('navigator', { userAgent, maxTouchPoints: 0, ...extras })
  vi.stubGlobal('window', {
    matchMedia: (query: string) => ({ matches: extras.displayMode === query }),
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('isIOS', () => {
  it('knows an iPhone', async () => {
    stubPlatform('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15')
    expect((await load()).isIOS()).toBe(true)
  })

  it('knows an iPad even though it claims to be a Mac', async () => {
    /* iPadOS 13+ reports a Macintosh user agent. The touch points are what give it away, and
       without this an iPad would be shown the desktop advice, which does not apply to it. */
    stubPlatform('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15', {
      maxTouchPoints: 5,
    })
    expect((await load()).isIOS()).toBe(true)
  })

  it('does not mistake a real Mac for one', async () => {
    stubPlatform('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/151')
    expect((await load()).isIOS()).toBe(false)
  })
})

describe('installedAsApp', () => {
  it('sees the iOS Home Screen app through its own flag', async () => {
    /* `navigator.standalone` is iOS-only and predates the standard signal; an iOS Home Screen
       app does not report `display-mode: standalone` reliably. */
    stubPlatform('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)', { standalone: true })
    expect((await load()).installedAsApp()).toBe(true)
  })

  it('sees an installed app everywhere else through the media query', async () => {
    stubPlatform('Mozilla/5.0 (Windows NT 10.0) Chrome/151', {
      displayMode: '(display-mode: standalone)',
    })
    expect((await load()).installedAsApp()).toBe(true)
  })

  it('says no in an ordinary tab', async () => {
    stubPlatform('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)')
    expect((await load()).installedAsApp()).toBe(false)
  })
})
