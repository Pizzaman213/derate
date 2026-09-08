// The heading a band draws, and the one band that draws collapsed.
//
// Shared by the row list and the card grid rather than written twice, for the
// reason `state/runtime.ts` gives about its own list: two copies of "which band
// hides itself" is one copy too many, and the two views must not disagree about
// whether four hundred models are on the screen.

import { useState } from 'react'

import { bandSubtitle, isCollapsibleBand } from './rows'
import type { Band, Group } from './rows'

/** Which collapsible bands are open.
 *
 *  `expandAll` is the search: a needle that matched inside a collapsed band has
 *  to show what it matched, or the box would silently find nothing. It forces
 *  the band open without touching what the person clicked, so clearing the
 *  search puts the band back the way they left it. */
export function useBandCollapse(expandAll: boolean) {
  const [open, setOpen] = useState<ReadonlySet<Band>>(() => new Set<Band>())
  return {
    collapsible: (band: Band) => isCollapsibleBand(band) && !expandAll,
    isOpen: (band: Band) => !isCollapsibleBand(band) || expandAll || open.has(band),
    toggle: (band: Band) =>
      setOpen((prev) => {
        const next = new Set(prev)
        if (next.has(band)) next.delete(band)
        else next.add(band)
        return next
      }),
  }
}

/** A band's heading: a plain line, or a disclosure when the band collapses. */
export function BandHeading({
  group,
  collapsible,
  open,
  onToggle,
}: {
  group: Group
  collapsible: boolean
  open: boolean
  onToggle: () => void
}) {
  const subtitle = bandSubtitle(group)
  const count = group.rows.length.toLocaleString()

  if (!collapsible) {
    return (
      <div
        className="sub"
        style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}
      >
        <span>{group.title}</span>
        <span className="unit">{count}</span>
      </div>
    )
  }

  return (
    <button
      type="button"
      className="sub"
      aria-expanded={open}
      onClick={onToggle}
      style={{
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        width: '100%',
        gap: 'var(--s-2)',
        textAlign: 'left',
      }}
    >
      <span>
        <span aria-hidden>{open ? '▾ ' : '▸ '}</span>
        {group.title}
        {subtitle ? <span className="unit"> · {subtitle}</span> : null}
      </span>
      <span className="unit">{count}</span>
    </button>
  )
}
