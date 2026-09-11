import { useEffect, useReducer } from 'react'
import type { QuantTable } from '../../api/types'
import { BandHeading, useBandCollapse } from './BandSection'
import { subscribeDominant } from './dominant'
import { ModelCard } from './ModelCard'
import { useAvatars } from './OwnerMark'
import type { Group, ModelRow } from './rows'

/** The card grid.
 *
 *  One subscription for the whole grid, not one per card. Avatars and their
 *  extracted colours resolve asynchronously in batches of six; if each card
 *  held its own state, a hundred-card grid would re-render a hundred times as
 *  they landed. Here a batch landing is one render of the grid, which React
 *  then reconciles down to the cards that actually changed.
 *
 *  Sections keep the fit banding the row view uses, because the band is the
 *  answer and a wall of cards without one is the thing this screen was
 *  supposed to stop being. */
export function CardGrid({
  groups,
  table,
  canPull = false,
  loading,
  error,
  emptyNote,
  onOpen,
  selectedId,
  split,
  expandAll = false,
}: {
  groups: Group[]
  table: QuantTable | null
  /** Whether a GGUF row has anywhere to go. See `classifySupport`. */
  canPull?: boolean
  loading: boolean
  error: Error | null
  emptyNote: string
  onOpen: (row: ModelRow) => void
  /** The model open in the detail pane, marked `aria-current` in the list. */
  selectedId?: string | null
  /** Master-pane layout: one card per row. */
  split?: boolean
  /** Force every collapsible band open, the way the row list does it. */
  expandAll?: boolean
}) {
  const collapse = useBandCollapse(expandAll)
  // Two stores, one re-render each. The avatar half is shared with every other
  // list that draws a publisher's mark; the dominant-colour half is this
  // grid's alone -- nothing else reads an avatar's pixels back.
  const [, bump] = useReducer((n: number) => n + 1, 0)
  useAvatars()
  useEffect(() => subscribeDominant(bump), [])

  if (error) {
    return (
      <p
        className="label"
        style={{ fontWeight: 400, color: 'var(--fault)', whiteSpace: 'pre-wrap' }}
      >
        {error.message}
      </p>
    )
  }
  if (!groups.length) {
    return <p className="unit">{loading ? 'Loading…' : emptyNote}</p>
  }

  return (
    <div>
      {groups.map((g) => (
        <section key={`${g.band}::${g.title}`}>
          <BandHeading
            group={g}
            collapsible={collapse.collapsible(g.band)}
            open={collapse.isOpen(g.band)}
            onToggle={() => collapse.toggle(g.band)}
          />
          {collapse.isOpen(g.band) ? (
            <div className={split ? 'mgrid split' : 'mgrid'}>
              {g.rows.map((row) => (
                <ModelCard
                  key={row.key}
                  row={row}
                  table={table}
                  canPull={canPull}
                  onOpen={onOpen}
                  current={row.model_id === selectedId}
                />
              ))}
            </div>
          ) : null}
        </section>
      ))}
    </div>
  )
}
