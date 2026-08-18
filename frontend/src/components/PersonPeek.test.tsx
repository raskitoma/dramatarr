// @vitest-environment jsdom
/**
 * The performer card: when it opens, when it must not, and how it survives the trip.
 *
 * Two of these trap things that are invisible until somebody uses the app the way it is used.
 * A touch tap fires `pointerenter` in every browser that supports pointer events, so without
 * the `pointerType` check the card would flash open under a finger on the way to a link. And
 * the card is portaled out of the strip, so the pointer leaves the trigger before it reaches
 * the card — without the grace period it would close in the gap, every time.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { setupI18n } from '@/i18n'

const dramadexPerson = vi.fn()
vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return { ...actual, dramadexPerson: (id: number) => dramadexPerson(id) }
})

const { PersonPeek } = await import('./PersonPeek')

const BIO = 'Addison Bowman is a Los Angeles-based actor and model.'

function draw(personId = 43) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <PersonPeek personId={personId} name="Addison Bowman" tint={1} className="peek">
        <a href="/people/43">Addison Bowman</a>
      </PersonPeek>
    </QueryClientProvider>,
  )
}

/** The element the handlers are on — the outer wrapper, not the describedby span inside it. */
function trigger(): HTMLElement {
  return document.querySelector('.peek') as HTMLElement
}

const CARD_W = 352
const CARD_H = 180

/**
 * jsdom measures everything as zero, and the component refuses to place a card it cannot
 * measure — so without this the card never becomes visible and none of these tests mean
 * anything. The anchor is where the face is; the card is the size the CSS gives it.
 */
function stubGeometry(anchor: { left: number; top: number }, viewport = { w: 1200, h: 800 }): void {
  Object.defineProperty(window, 'innerWidth', { value: viewport.w, configurable: true })
  Object.defineProperty(window, 'innerHeight', { value: viewport.h, configurable: true })
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: Element,
  ) {
    const isCard = this.getAttribute('role') === 'tooltip'
    const box = isCard
      ? { width: CARD_W, height: CARD_H, left: 0, top: 0 }
      : { width: 96, height: 96, left: anchor.left, top: anchor.top }
    return {
      ...box,
      right: box.left + box.width,
      bottom: box.top + box.height,
      x: box.left,
      y: box.top,
      toJSON: () => box,
    } as DOMRect
  })
}

function positionOf(node: HTMLElement): { left: number; top: number } {
  return { left: Number.parseFloat(node.style.left), top: Number.parseFloat(node.style.top) }
}

function mouseEnter(node: HTMLElement): void {
  fireEvent.pointerEnter(node, { pointerType: 'mouse' })
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  stubGeometry({ left: 500, top: 300 })
  dramadexPerson.mockReset()
  dramadexPerson.mockResolvedValue({
    id: 43,
    name: 'Addison Bowman',
    bio: BIO,
    photoUrl: '/api/library/dramadex/people/43/photo',
    credits: [{ id: 1 }, { id: 2 }],
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  cleanup()
})

describe('PersonPeek', () => {
  it('asks for nothing until somebody hovers', () => {
    draw()

    expect(dramadexPerson).not.toHaveBeenCalled()
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('shows the bio after a deliberate hover', async () => {
    draw()
    mouseEnter(trigger())

    expect(await screen.findByRole('tooltip')).toBeTruthy()
    expect(await screen.findByText(BIO)).toBeTruthy()
    expect(dramadexPerson).toHaveBeenCalledWith(43)
  })

  it('stays shut for a finger', async () => {
    /* A tap fires pointerenter too. Opening under it would cover the link being tapped. */
    draw()
    fireEvent.pointerEnter(trigger(), { pointerType: 'touch' })

    await new Promise((resolve) => setTimeout(resolve, 400))

    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(dramadexPerson).not.toHaveBeenCalled()
  })

  it('opens on keyboard focus, not only on hover', async () => {
    draw()
    fireEvent.focus(trigger())

    expect(await screen.findByText(BIO)).toBeTruthy()
  })

  it('closes on Escape', async () => {
    draw()
    mouseEnter(trigger())
    await screen.findByRole('tooltip')

    fireEvent.keyDown(trigger(), { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull())
  })

  it('survives the pointer crossing the gap into it', async () => {
    /* The card is portaled to the body, so leaving the trigger is part of reaching the card.
       WCAG 1.4.13 calls this "hoverable"; without the grace period it is unreachable. */
    draw()
    mouseEnter(trigger())
    const card = await screen.findByRole('tooltip')

    fireEvent.pointerLeave(trigger(), { pointerType: 'mouse' })
    fireEvent.pointerEnter(card)

    await new Promise((resolve) => setTimeout(resolve, 300))
    expect(screen.queryByRole('tooltip')).toBeTruthy()
  })

  it('closes once the pointer leaves the card too', async () => {
    draw()
    mouseEnter(trigger())
    const card = await screen.findByRole('tooltip')

    fireEvent.pointerLeave(trigger(), { pointerType: 'mouse' })
    fireEvent.pointerEnter(card)
    fireEvent.pointerLeave(card)

    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull())
  })

  it('says so when the indexer has no biography', async () => {
    dramadexPerson.mockResolvedValue({
      id: 379,
      name: 'Anna Stadler',
      bio: null,
      photoUrl: null,
      credits: [],
    })
    draw(379)
    mouseEnter(trigger())

    expect(await screen.findByText('El indexador no tiene biografía de esta persona.')).toBeTruthy()
  })

  it('shows no card at all when the person cannot be resolved', async () => {
    dramadexPerson.mockRejectedValue(new Error('404'))
    draw()
    mouseEnter(trigger())

    await waitFor(() => expect(dramadexPerson).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull())
  })
})

describe('PersonPeek — staying on screen', () => {
  /* The complaint that produced these: at the ends of a scrolling cast strip the face is near
     the viewport edge, so a card centred on it hangs off and the bio cannot be read. The width
     cap in CSS bounds the card; it says nothing about where the card is put. */

  it('sits centred under the face when there is room', async () => {
    stubGeometry({ left: 500, top: 300 })
    draw()
    mouseEnter(trigger())
    const node = (await screen.findByRole('tooltip')) as HTMLElement

    const { left, top } = positionOf(node)
    expect(left).toBe(500 + 96 / 2 - CARD_W / 2)
    expect(top).toBe(300 + 96 + 8)
  })

  it('does not hang off the right edge', async () => {
    stubGeometry({ left: 1150, top: 300 }, { w: 1200, h: 800 })
    draw()
    mouseEnter(trigger())
    const node = (await screen.findByRole('tooltip')) as HTMLElement

    const { left } = positionOf(node)
    expect(left + CARD_W).toBeLessThanOrEqual(1200 - 12)
    expect(left).toBeGreaterThanOrEqual(12)
  })

  it('does not hang off the left edge', async () => {
    stubGeometry({ left: 4, top: 300 }, { w: 1200, h: 800 })
    draw()
    mouseEnter(trigger())
    const node = (await screen.findByRole('tooltip')) as HTMLElement

    expect(positionOf(node).left).toBeGreaterThanOrEqual(12)
  })

  it('flips above the face when there is no room below', async () => {
    /* A cast strip near the bottom of a short window: below would put the biography under the
       fold, where it is not readable at all. */
    stubGeometry({ left: 500, top: 600 }, { w: 1200, h: 760 })
    draw()
    mouseEnter(trigger())
    const node = (await screen.findByRole('tooltip')) as HTMLElement

    const { top } = positionOf(node)
    expect(top + CARD_H).toBeLessThanOrEqual(600)
    expect(top).toBeGreaterThanOrEqual(12)
  })

  it('stays inside a window too short for either side', async () => {
    stubGeometry({ left: 500, top: 90 }, { w: 1200, h: 300 })
    draw()
    mouseEnter(trigger())
    const node = (await screen.findByRole('tooltip')) as HTMLElement

    const { top } = positionOf(node)
    expect(top).toBeGreaterThanOrEqual(12)
    expect(top + CARD_H).toBeLessThanOrEqual(300 - 12 + 1)
  })
})
