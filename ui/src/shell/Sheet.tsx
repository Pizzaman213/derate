import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useSelection } from '../state/selection'

// The one modal host (mockups-next: a single `#sheet`/`#cardBody` pair reused
// by both the node inspector and the deployment inspector). What renders
// inside it -- the actual node/deployment detail -- is a later package's job
// (mockups-next/js/inspectors.js: "sidebar/SelectionCard"); this file owns
// only the mechanics every use of the sheet needs regardless of content.

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'

export function Sheet() {
  const { sheet, closeSheet } = useSelection()
  const open = sheet !== null
  const cardRef = useRef<HTMLDivElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) return

    // Focus restore: remember what had focus before the sheet opened, since
    // that is where it belongs once the sheet is gone, not <body>.
    restoreFocusRef.current = document.activeElement as HTMLElement | null

    // Scroll lock: the page behind a modal does not scroll.
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const focusable = () => {
      const card = cardRef.current
      return card ? Array.from(card.querySelectorAll<HTMLElement>(FOCUSABLE)) : []
    }
    ;(focusable()[0] ?? cardRef.current)?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        closeSheet()
        return
      }
      if (e.key !== 'Tab') return
      // Focus trap: Tab and Shift+Tab cycle within the card. Nothing outside
      // it is reachable by keyboard while the sheet is open.
      const items = focusable()
      if (items.length === 0) {
        e.preventDefault()
        return
      }
      const first = items[0]!
      const last = items[items.length - 1]!
      const active = document.activeElement
      const atStart = active === first || active === cardRef.current
      const atEnd = active === last
      if (e.shiftKey ? atStart : atEnd) {
        e.preventDefault()
        ;(e.shiftKey ? last : first).focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)

    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = prevOverflow
      restoreFocusRef.current?.focus()
    }
  }, [open, closeSheet])

  if (!sheet) return null

  const wide = sheet.kind === 'dep'

  return createPortal(
    <div
      className="sheet on"
      onClick={(e) => {
        // Scrim click closes; a click that started inside the card and
        // released here (a drag-selection) still targets the card, not this
        // element, so this only fires for an actual scrim click.
        if (e.target === e.currentTarget) closeSheet()
      }}
    >
      <div
        ref={cardRef}
        className={wide ? 'card wide' : 'card'}
        role="dialog"
        aria-modal="true"
        aria-label={sheet.kind === 'node' ? `${sheet.id}` : `${sheet.id} deployment`}
        tabIndex={-1}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            gap: 10,
          }}
        >
          <span className="label mono" style={{ fontSize: wide ? 17 : 16 }}>
            {sheet.id}
          </span>
          <button onClick={closeSheet}>Close</button>
        </div>
        <p className="unit" style={{ marginTop: 8 }}>
          No detail view yet.
        </p>
      </div>
    </div>,
    document.body,
  )
}
