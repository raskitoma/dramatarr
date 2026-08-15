// @vitest-environment jsdom
/**
 * The avatar's fallback is a finished design, not a placeholder.
 *
 * Most households will never upload a picture, so initials-on-colour is what the header, the
 * account screen and the users table render most of the time — and a picture that fails to
 * load (cache cleared under the app's feet, a half-written file) has to land on that same
 * treatment rather than on a broken-image glyph in the middle of the header.
 *
 * The initials themselves are sliced by code point, which is what keeps a CJK or emoji name
 * from rendering as half a character; `initialsOf` owns that rule and is tested with it.
 */

import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'es' } }),
}))

const { Avatar } = await import('./Avatar')

afterEach(cleanup)

describe('Avatar', () => {
  it('renders the picture when there is one', () => {
    const { container } = render(
      <Avatar name="Ana Lopez" email="ana@casa.example" src="/pic.webp" color="#6c8cff" />,
    )
    const image = container.querySelector('img')
    expect(image?.getAttribute('src')).toBe('/pic.webp')
    // Decorative: a name is always rendered beside it, so announcing it twice is noise.
    expect(image?.getAttribute('alt')).toBe('')
    expect(container.textContent).toBe('')
  })

  it('falls back to initials when there is no picture', () => {
    const { container } = render(
      <Avatar name="Ana Lopez" email="ana@casa.example" src={null} color="#6c8cff" />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toBe('AL')
  })

  it('falls back to initials when the picture fails to load', () => {
    const { container } = render(
      <Avatar name="Ana Lopez" email="ana@casa.example" src="/gone.webp" />,
    )
    const image = container.querySelector('img')
    expect(image).toBeTruthy()

    fireEvent.error(image as HTMLImageElement)

    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toBe('AL')
  })

  it('uses the email when the display name is empty', () => {
    const { container } = render(<Avatar name="   " email="beto@casa.example" src={null} />)
    expect(container.textContent).toBe('B')
  })
})
