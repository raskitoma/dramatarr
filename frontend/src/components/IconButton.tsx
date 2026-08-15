import { forwardRef } from 'react'

import { Button, type ButtonProps } from './Button'

export interface IconButtonProps extends Omit<ButtonProps, 'iconOnly'> {
  /** Required. Becomes both the accessible name and the native tooltip. */
  label: string
}

/**
 * A square Button holding one icon. Defaults to `ghost` because icon buttons almost always
 * sit inside toolbars, cards and dialog headers where a bordered control would be noise;
 * pass `variant` explicitly wherever that is not what you want.
 */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, variant = 'ghost', ...rest },
  ref,
) {
  return <Button {...rest} ref={ref} variant={variant} iconOnly aria-label={label} title={label} />
})

export default IconButton
