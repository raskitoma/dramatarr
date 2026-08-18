// @vitest-environment jsdom
/**
 * The face: photograph or initials, and what happens in between.
 *
 * The case worth trapping is the fallback. The series cast strip asks for a photo by id
 * without knowing whether one exists — roughly one performer in four has none — so the 404
 * path is not an edge case there, it is a quarter of every cast list.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { PersonFace } from './PersonFace'

afterEach(cleanup)

describe('PersonFace', () => {
  it('draws the photograph when there is one', () => {
    render(<PersonFace name="Addison Bowman" photoUrl="/api/library/dramadex/people/43/photo" />)

    const img = document.querySelector('img')
    expect(img?.getAttribute('src')).toBe('/api/library/dramadex/people/43/photo')
    // Decorative: the name is always rendered beside it, so announcing it twice is noise.
    expect(img?.getAttribute('alt')).toBe('')
    expect(screen.queryByText('AB')).toBeNull()
  })

  it('draws initials when the indexer has no picture', () => {
    render(<PersonFace name="Addison Bowman" photoUrl={null} />)

    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('AB')).toBeTruthy()
  })

  it('falls back to initials when the photo 404s', () => {
    render(<PersonFace name="Addison Bowman" photoUrl="/api/library/dramadex/people/43/photo" />)

    const img = document.querySelector('img')
    expect(img).toBeTruthy()
    fireEvent.error(img as HTMLImageElement)

    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('AB')).toBeTruthy()
  })

  it('tries again when the address changes', () => {
    /* The failure is keyed to the src, not to a bare boolean — otherwise one actor's missing
       photo would suppress the next one's in a re-rendered list. */
    const { rerender } = render(<PersonFace name="A B" photoUrl="/photo/1" />)
    fireEvent.error(document.querySelector('img') as HTMLImageElement)
    expect(document.querySelector('img')).toBeNull()

    rerender(<PersonFace name="A B" photoUrl="/photo/2" />)

    expect(document.querySelector('img')?.getAttribute('src')).toBe('/photo/2')
  })

  it('keeps the caller\'s frame and tint', () => {
    render(<PersonFace name="A B" photoUrl={null} className="frame" tint={3} />)

    const face = document.querySelector('.frame')
    expect(face?.getAttribute('data-tint')).toBe('3')
    expect(face?.getAttribute('aria-hidden')).toBe('true')
  })
})
