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

function mouseEnter(node: HTMLElement): void {
  fireEvent.pointerEnter(node, { pointerType: 'mouse' })
}

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  dramadexPerson.mockReset()
  dramadexPerson.mockResolvedValue({
    id: 43,
    name: 'Addison Bowman',
    bio: BIO,
    photoUrl: '/api/library/dramadex/people/43/photo',
    credits: [{ id: 1 }, { id: 2 }],
  })
})

afterEach(cleanup)

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
