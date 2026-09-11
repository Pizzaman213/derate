import { useEffect } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, FocusEvent as ReactFocusEvent, RefObject } from 'react'

/** The pointerdown/Escape/blur trio that closes a field-sized floating panel,
 *  lifted out of `Popover.tsx` so `Select.tsx` and `Combobox.tsx` share the
 *  exact same already-correct behavior rather than re-deriving it twice more.
 *  This is deduplication of proven code, not a speculative abstraction: all
 *  three consumers need this unmodified, and a bug found in one (the
 *  `pointerdown`-vs-`click` distinction below already needed a careful pass
 *  once) is fixed for all three at once rather than found three times.
 *
 *  `pointerdown` rather than `click`: a press that lands on another control
 *  should both dismiss this panel and reach that control, which a click
 *  listener firing after the fact cannot do.
 *
 *  `Escape` is returned as a keydown handler for the caller to attach to its
 *  own wrapper, rather than listened for on `document` here, so an open
 *  `shell/Sheet.tsx`'s own Escape handler is never shadowed by this one.
 *
 *  `onBlur` closes only when focus leaves the wrapper entirely -- focus
 *  moving *within* it (e.g. from a trigger button into its own listbox) is
 *  not a dismissal. */
export function useDismiss(
  open: boolean,
  close: () => void,
  wrapRef: RefObject<HTMLElement | null>,
  triggerRef: RefObject<HTMLElement | null>,
): {
  onKeyDown: (e: ReactKeyboardEvent) => void
  onBlur: (e: ReactFocusEvent) => void
} {
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) close()
    }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [open, close, wrapRef])

  return {
    onKeyDown: (e) => {
      if (e.key === 'Escape' && open) {
        e.preventDefault()
        e.stopPropagation()
        close()
        triggerRef.current?.focus()
      }
    },
    onBlur: (e) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node | null)) close()
    },
  }
}
