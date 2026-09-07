import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

interface Props {
  /** Names the panel for a screen reader. The trigger names itself. */
  label: string
  /** What shows on the button -- a live count, so it re-reads on every change. */
  trigger: ReactNode
  /** Receives `close` so a row can dismiss the panel when that is the whole
   *  gesture. Machine ticks deliberately do not: you are usually choosing
   *  several, and closing after each one would make that four gestures. */
  children: (close: () => void) => ReactNode
}

/** A field-sized disclosure: a button, and a panel under it.
 *
 *  Deliberately NOT `shell/Sheet.tsx`. That is a portalled modal with a scrim,
 *  a scroll lock and a focus trap, all of which are right for a detail surface
 *  and wrong for a dropdown in a field row -- a trap would make Tab loop inside
 *  a five-row list you are trying to leave.
 *
 *  Deliberately NOT a listbox either. The rows hold real checkboxes, so
 *  checked state is announced, Space toggles, and Tab navigates, all for free
 *  and all correct. There are no arrow keys and no roving tabindex on purpose:
 *  arrow keys are a menu affordance and this is not a menu. Adding them would
 *  mean re-implementing what the native control already does properly.
 *
 *  Not portalled: `main` is `overflow: hidden`, so the panel caps its height
 *  and anchors left rather than escaping the layout. If a cluster ever grows
 *  wide enough for that to bite, the fix is `createPortal` plus a measured
 *  rect -- which then owes reposition-on-scroll and on-resize, so it is not
 *  built until something needs it.
 */
export function Popover({ label, trigger, children }: Props) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const panelId = useId()

  const close = () => setOpen(false)

  // `pointerdown` rather than `click`: a press that lands on another control
  // should both dismiss this panel and reach that control, which a click
  // listener firing after the fact cannot do.
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [open])

  return (
    <div
      className="popwrap"
      ref={wrap}
      // Escape is handled here rather than on `document` so an open Sheet's own
      // Escape handler is never shadowed by this one.
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          e.preventDefault()
          e.stopPropagation()
          setOpen(false)
          button.current?.focus()
        }
      }}
      // Tabbing past the last row leaves the panel, which is also the gesture
      // that should close it. Focus moving *within* the wrapper is not.
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false)
      }}
    >
      <button
        ref={button}
        type="button"
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        {trigger}
      </button>
      {open ? (
        <div className="pop" id={panelId} role="group" aria-label={label}>
          {children(close)}
        </div>
      ) : null}
    </div>
  )
}
