import { useReactFlow, useViewport } from '@xyflow/react'
import { Icon } from '../../components/Icon'

interface Props {
  onAddModel: () => void
  /** Absent when this gateway has no per-name limits to set. */
  onAddLimit: (() => void) | null
  /** Clears every dragged position: each block takes its dealt,
   *  overlap-free place. Always offered. */
  onArrange: () => void
  /** Shown only when the draft differs from the server. */
  onDiscard: (() => void) | null
  /** Ops plus launches. */
  changes: number
  /** Blocking errors; Save stays put while there are any. */
  errors: number
  saving: boolean
  onSave: () => void
}

/** The palette (a draft model, a rate limit), the view controls, and Save. */
export function GraphToolbar({ onAddModel, onAddLimit, onArrange, onDiscard, changes, errors, saving, onSave }: Props) {
  const flow = useReactFlow()
  const { zoom } = useViewport()
  const blocked = saving || changes === 0 || errors > 0
  const why = saving
    ? 'Saving…'
    : errors > 0
      ? `${errors} ${errors === 1 ? 'error' : 'errors'} to fix first — see Checks`
      : changes === 0
        ? 'Nothing to save'
        : 'Apply the wiring, then launch'
  return (
    <div className="wtoolbar">
      <button type="button" onClick={onAddModel}>
        <Icon name="plus" size={12} decorative /> Add model
      </button>
      {onAddLimit ? (
        <button type="button" onClick={onAddLimit}>
          <Icon name="gauge" size={12} decorative /> Add rate limit
        </button>
      ) : null}
      <span className="wtoolbar-gap" />
      <button type="button" className="ghost" aria-label="Zoom out" title="Zoom out" onClick={() => void flow.zoomOut({ duration: 150 })}>
        <Icon name="zoom-out" size={14} decorative />
      </button>
      <span className="unit mono wzoom" aria-live="off">
        {Math.round(zoom * 100)}%
      </span>
      <button type="button" className="ghost" aria-label="Zoom in" title="Zoom in" onClick={() => void flow.zoomIn({ duration: 150 })}>
        <Icon name="zoom-in" size={14} decorative />
      </button>
      <button type="button" className="ghost" aria-label="Fit to view" title="Fit to view" onClick={() => void flow.fitView({ duration: 200, padding: 0.08 })}>
        <Icon name="fit-view" size={14} decorative />
      </button>
      <button type="button" onClick={onArrange} title="Put every block in its column, nothing overlapping">
        Arrange
      </button>
      {onDiscard ? (
        <button type="button" onClick={onDiscard} title="Throw the draft away and show the server's state">
          Discard
        </button>
      ) : null}
      <button
        type="button"
        className="btn"
        aria-disabled={blocked}
        title={why}
        onClick={() => {
          if (!blocked) onSave()
        }}
      >
        {saving ? 'Saving…' : `Save ${changes} ${changes === 1 ? 'change' : 'changes'}`}
      </button>
    </div>
  )
}
