/** A small mark standing for one vertical-drama platform.
 *
 * Drawn here rather than fetched, and that is a decision rather than a shortcut. The obvious
 * move is each platform's own favicon, and it is the wrong one twice over: loading it means
 * every member's browser announces to ReelShort and DramaBox which titles this household is
 * looking at, which is exactly what `cache_request_poster` exists to prevent on the server side;
 * and shipping their artwork would put their logo in this app's interface without asking.
 *
 * So each platform gets its initial on its own colour — recognisable at a glance, consistent
 * across the app, no request leaving the page, and not pretending to be anybody's logo. Every
 * tint is checked to carry white text at 4.5:1 or better.
 */

import styles from './PlatformMark.module.css'

interface PlatformMarkProps {
  /** The indexer's studio key: `reelshort`, `dramabox`, `goodshort`, … */
  studioKey: string
  /** Its display name, which is where the letter comes from. */
  name: string
  className?: string
}

/**
 * Tints this build knows, keyed by studio.
 *
 * A platform missing from here is not a problem — it falls back to the app's accent, and the
 * letter still tells the two apart. The alternative, deriving a colour by hashing the key,
 * gives a stable colour and no control over whether white is legible on it.
 */
const TINTS: Readonly<Record<string, string>> = {
  reelshort: '#d92b36',
  dramabox: '#b8480a',
  goodshort: '#c2249a',
  netshort: '#1f7a8c',
  shortmax: '#5b3fd6',
  flextv: '#0f7a8a',
  kalostv: '#8a5a00',
}

export function PlatformMark({ studioKey, name, className }: PlatformMarkProps) {
  const letter = (name.trim()[0] ?? studioKey.trim()[0] ?? '?').toLocaleUpperCase()
  const tint = TINTS[studioKey.toLowerCase()]

  return (
    <span
      className={className ? `${styles.mark} ${className}` : styles.mark}
      // Decorative: the platform's name is always rendered beside it.
      aria-hidden="true"
      style={tint ? { background: tint } : undefined}
    >
      {letter}
    </span>
  )
}

export default PlatformMark
