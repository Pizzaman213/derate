// Tab containment for the two modals: shell/Sheet.tsx and
// components/RevealModal.tsx. One copy, because the boundary rules below were
// found by getting them wrong once, and a second spelling would get them wrong
// again independently.

export const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'

export function focusables(card: HTMLElement | null): HTMLElement[] {
  return card ? Array.from(card.querySelectorAll<HTMLElement>(FOCUSABLE)) : []
}

/** Keeps a Tab keydown inside `card`. Returns without touching the event for
 *  any other key. */
export function containTab(e: KeyboardEvent, card: HTMLElement | null): void {
  if (e.key !== 'Tab') return
  const items = focusables(card)
  if (items.length === 0) {
    e.preventDefault()
    card?.focus()
    return
  }
  const first = items[0]!
  const last = items[items.length - 1]!
  const active = document.activeElement
  // Full containment, not just the two ends: if focus is anywhere outside
  // the card at all -- it escaped via a removed element handing focus
  // back to <body>, a stray portal, anything -- Tab snaps it back inside
  // instead of only catching the boundary case. The card itself is also
  // a valid Shift+Tab boundary: it's focusable (tabIndex={-1}, focused
  // by a click on its own padding, or as the fallback target when the
  // card has no focusable children at all), and `card.contains(card)`
  // is true for it, so without this clause `atBoundary` would be false
  // and a Shift+Tab from the card would fall through to the browser's
  // default -- moving focus to whatever is previous in document order
  // outside the portal, since the card lives under document.body.
  const inside = active != null && card != null && card.contains(active)
  const atBoundary = e.shiftKey ? active === first || active === card : active === last
  if (!inside || atBoundary) {
    e.preventDefault()
    ;(e.shiftKey ? last : first).focus()
  }
}
