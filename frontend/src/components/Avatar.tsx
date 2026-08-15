import { useState, type CSSProperties } from 'react'

import { initialsOf } from '@/lib/format'

import { cx } from './cx'
import styles from './Avatar.module.css'

export type AvatarSize = 'sm' | 'md' | 'lg' | 'xl'

export interface AvatarProps {
  /** Display name; falls back to the email when it is empty. */
  name: string
  email: string
  /** Uploaded picture, or null for the initials treatment. */
  src?: string | null
  /** The hue the server assigned this account, used only by the fallback. */
  color?: string
  size?: AvatarSize
  className?: string
}

/**
 * A member's face: their picture, or their initials on the colour the server gave them.
 *
 * The fallback is not a placeholder waiting to be replaced — most households will never upload
 * a picture, and initials-on-colour is the finished design for that case. So both branches get
 * the same circle, the same size scale and the same border, and swapping one for the other
 * never moves the layout by a pixel.
 *
 * The image is decorative in every place this is used (a name is always rendered beside it, or
 * the control carries its own label), so `alt` is empty on purpose: announcing "photo of Ana"
 * next to the text "Ana" is noise for a screen reader, not information.
 *
 * A picture that fails to load — cache cleared under the app's feet, a half-written file —
 * falls back to the initials rather than to a broken-image glyph.
 */
export function Avatar({ name, email, src = null, color, size = 'md', className }: AvatarProps) {
  const [failed, setFailed] = useState<string | null>(null)
  const usable = src !== null && src !== '' && failed !== src

  const style = color ? ({ '--avatar-color': color } as CSSProperties) : undefined

  return (
    <span className={cx(styles.avatar, styles[size], className)} style={style}>
      {usable ? (
        <img
          className={styles.image}
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => {
            setFailed(src)
          }}
        />
      ) : (
        <span className={styles.initials} aria-hidden="true">
          {initialsOf(name, email)}
        </span>
      )}
    </span>
  )
}

export default Avatar
