import { useEffect, type RefObject } from 'react'

/**
 * Closes a popup when a pointer lands outside it, or when focus moves out of it.
 *
 * Lives here rather than in `AppHeader` because both the avatar menu and the header search need
 * it, and `AppHeader` imports the search. A component importing back into its own parent is a
 * cycle that Vite can resolve to `undefined` in a production build while the dev server is
 * perfectly happy — a failure that appears only after deploy.
 *
 * Listening on `pointerdown` rather than `click` is deliberate: a `click` handler fires after
 * the browser has already moved focus, so a popup that closed on click swallowed the very first
 * interaction with whatever was underneath it.
 */
export function useDismissOnOutside(
  open: boolean,
  close: () => void,
  container: RefObject<HTMLElement | null>,
): void {
  useEffect(() => {
    if (!open) return undefined

    function isOutside(target: EventTarget | null): boolean {
      const node = container.current
      return node !== null && target instanceof Node && !node.contains(target)
    }

    function handlePointerDown(event: PointerEvent): void {
      if (isOutside(event.target)) close()
    }

    function handleFocusIn(event: FocusEvent): void {
      if (isOutside(event.target)) close()
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('focusin', handleFocusIn)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('focusin', handleFocusIn)
    }
  }, [open, close, container])
}

export default useDismissOnOutside
