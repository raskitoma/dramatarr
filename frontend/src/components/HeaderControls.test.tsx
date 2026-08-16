// @vitest-environment jsdom
/**
 * The two controls in the top-right corner, after the words came off them.
 *
 * Both were asked to become glyph-only, and that request has one way of going wrong that no
 * screenshot catches: deleting the label instead of hiding it. A button whose text is gone
 * from the DOM is a button a screen reader announces as nothing at all, and the theme picker
 * would become three unnamed circles. So what is asserted here is the pair — **no visible
 * words, every control still named** — because either half alone is the bug.
 *
 * The language picker is a radio group and not a `<select>` for a reason worth keeping: a
 * native `<option>` renders text only, so flags there could only have been emoji, and Windows
 * ships no glyphs for the flag range. That is why these assertions look for buttons.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { setupI18n } from '@/i18n'

const patchUser = vi.fn()
const setTheme = vi.fn()

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    user: null, // Signed out: the local switch still has to work, without a PATCH.
    isAdmin: false,
    isLoading: false,
    patchUser: (...args: unknown[]) => patchUser(...args),
  }),
}))

vi.mock('@/hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'light', setTheme: (...a: unknown[]) => setTheme(...a) }),
}))

const { ThemeToggle } = await import('./ThemeToggle')
const { LocaleSelect } = await import('./LocaleSelect')

function renderIn(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

/** Text a sighted user would actually read, ignoring the visually-hidden recipe. */
function visibleText(root: HTMLElement): string {
  return Array.from(root.querySelectorAll('*'))
    .filter((node) => !node.className.toString().includes('actionLabel'))
    .map((node) => (node.children.length === 0 ? (node.textContent ?? '') : ''))
    .join('')
    .trim()
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(cleanup)

describe('ThemeToggle', () => {
  it('shows no words and still names all three options', () => {
    const { container } = renderIn(<ThemeToggle />)

    expect(visibleText(container.firstElementChild as HTMLElement)).toBe('')
    // Named for a screen reader, and the same string in the tooltip for a mouse.
    for (const name of ['Automático', 'Claro', 'Oscuro']) {
      const option = screen.getByRole('radio', { name })
      expect(option.getAttribute('title')).toBe(name)
    }
    expect(screen.getByRole('radio', { name: 'Claro' }).getAttribute('aria-checked')).toBe('true')
  })
})

describe('LocaleSelect', () => {
  it('draws a flag per language and keeps the language name as its name', () => {
    const { container } = renderIn(<LocaleSelect />)

    expect(visibleText(container.firstElementChild as HTMLElement)).toBe('')
    // Real graphics, not emoji: emoji flags are blank on Windows.
    expect(container.querySelectorAll('svg').length).toBe(2)

    for (const name of ['Español', 'English']) {
      const option = screen.getByRole('radio', { name })
      expect(option.getAttribute('title')).toBe(name)
    }
  })

  it('switches the language when a flag is pressed', () => {
    renderIn(<LocaleSelect />)

    fireEvent.click(screen.getByRole('radio', { name: 'English' }))

    // No session here, so the local switch happens and no profile write is attempted.
    expect(patchUser).not.toHaveBeenCalled()
  })

  it('keeps the group to one tab stop', () => {
    renderIn(<LocaleSelect />)

    const stops = screen
      .getAllByRole('radio')
      .filter((option) => option.getAttribute('tabindex') === '0')
    expect(stops.length).toBe(1)
  })
})
