/** Copy a short string — a title, an id — to the clipboard.
 *
 * It exists because the app sets `user-select: none`: this reads as an application rather
 * than a document, and dragging across a poster grid used to leave a smear of half-selected
 * labels. Taking a title out of here is a deliberate act now, so there has to be a control
 * for it.
 *
 * **Two ways to copy, and the fallback is not paranoia.** `navigator.clipboard` only exists in
 * a secure context, and this app is reached both at `https://dramatarr.…` and directly at
 * `http://<nas-ip>:8090` on the LAN — where the modern API is simply undefined and the button
 * would fail silently. The textarea-and-`execCommand` path is deprecated and still the only
 * thing that works there.
 *
 * Confirmation is on the button itself rather than a toast: the feedback belongs where the
 * click was, and a toast for something this small is a notification about nothing.
 */

import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { IconButton, type IconButtonProps } from '@/components/IconButton'
import { CheckIcon, CopyIcon } from '@/components/icons'

/** Long enough to be read, short enough that the button is ready again before it is wanted. */
const CONFIRM_MS = 1600

export interface CopyButtonProps extends Omit<IconButtonProps, 'label' | 'onClick' | 'children'> {
  value: string
  /** Overrides the default "Copy «x»". */
  label?: string
}

async function writeToClipboard(value: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value)
      return true
    } catch {
      /* Denied by permissions policy, or the document was not focused. Fall through. */
    }
  }
  try {
    const field = document.createElement('textarea')
    field.value = value
    /* Off-screen rather than hidden: `display: none` and `visibility: hidden` are not
       selectable, and the copy would silently take nothing. */
    field.setAttribute('readonly', '')
    field.style.position = 'fixed'
    field.style.top = '-1000px'
    field.style.opacity = '0'
    document.body.appendChild(field)
    field.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(field)
    return ok
  } catch {
    return false
  }
}

export function CopyButton({ value, label, ...rest }: CopyButtonProps) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const name = copied ? t('common.copied') : (label ?? t('common.copyValue', { value }))

  return (
    <IconButton
      {...rest}
      label={name}
      onClick={async (event) => {
        /* The button is often inside a link or a card that navigates. */
        event.preventDefault()
        event.stopPropagation()
        if (!(await writeToClipboard(value))) return
        setCopied(true)
        window.clearTimeout(timer.current)
        timer.current = window.setTimeout(() => setCopied(false), CONFIRM_MS)
      }}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
    </IconButton>
  )
}

export default CopyButton
