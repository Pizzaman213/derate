import { useEffect, useReducer } from 'react'
import type { QuantTable } from '../../api/types'
import { subscribeDominant } from './dominant'
import { ModelCard } from './ModelCard'
import { subscribeAvatars } from './owner'
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
  loading,
  error,
  emptyNote,
  onOpen,
}: {
  groups: Group[]
  table: QuantTable | null
  loading: boolean
  error: Error | null
  emptyNote: string
  onOpen: (row: ModelRow) => void
}) {
  const [, bump] = useReducer((n: number) => n + 1, 0)
  useEffect(() => {
    const off = [subscribeAvatars(bump), subscribeDominant(bump)]
    return () => off.forEach((fn) => fn())
  }, [])

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
          <div
            className="sub"
            style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}
          >
            <span>{g.title}</span>
            <span className="unit">{g.rows.length}</span>
          </div>
          <div className="mgrid">
            {g.rows.map((row) => (
              <ModelCard key={row.key} row={row} table={table} onOpen={onOpen} />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
