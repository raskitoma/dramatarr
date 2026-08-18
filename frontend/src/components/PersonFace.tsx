/** A performer's face: the indexer's photograph, or their initials on a tint.
 *
 * Both call sites already own their own shape and paint — a 2:3 portrait on `/people/:id`, a
 * 5rem disc in the series cast strip — so this component owns only the *mechanism*: which of
 * the two renders, and what happens when the picture does not arrive. The frame comes in
 * through `className`.
 *
 * It is deliberately not `Avatar`. That one is a household member's circle, with a 22%
 * color-mix wash and a border, across six call sites; neither person frame is that shape or
 * that paint. What is copied from it is the part worth copying: the failure key is the `src`
 * itself, so a later photo is attempted rather than suppressed by an earlier one's failure.
 *
 * `alt` is empty on purpose. The name is always rendered right beside this, and announcing
 * "photo of Addison Bowman" next to the words "Addison Bowman" is noise, not information.
 */

import { useState } from 'react'

import { initialsOf } from '@/lib/format'

import styles from './PersonFace.module.css'

export interface PersonFaceProps {
  name: string
  /** This app's proxy path. Null for the roughly one performer in four the indexer has no
   *  picture of — which is a finished state, not a pending one. */
  photoUrl?: string | null
  className?: string
  /** Six tints keyed off the person id, so one actor keeps one colour everywhere. */
  tint?: number
}

export function PersonFace({ name, photoUrl = null, className, tint }: PersonFaceProps) {
  const [failed, setFailed] = useState<string | null>(null)
  const usable = photoUrl != null && photoUrl !== '' && failed !== photoUrl

  return (
    <span className={className} aria-hidden="true" data-tint={tint}>
      {usable ? (
        <img
          className={styles.image}
          src={photoUrl}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => {
            setFailed(photoUrl)
          }}
        />
      ) : (
        initialsOf(name, '?')
      )}
    </span>
  )
}

export default PersonFace
