import type { RefObject } from 'react'
import type { ClusterGraphHandle } from './ClusterGraph'

interface Props {
  graphRef: RefObject<ClusterGraphHandle>
  zoomLabelRef: RefObject<HTMLSpanElement>
  /** Only offered when there is something to reset -- a control that does
   *  nothing is worse than no control. True once a machine has been dealt a
   *  different slot OR dragged off the one it has. */
  rearranged: boolean
  onResetLayout: () => void
}

/** The one line of chrome above the graph. The zoom percentage is written
 *  directly into `zoomLabelRef` by ClusterGraph on every pan/zoom -- never
 *  React state -- so this component itself never re-renders while someone is
 *  scrolling or dragging.
 *
 *  There is no separate "fit" control because the resting view IS the fit:
 *  the viewBox is the graph element's own box, and ClusterGraph scales the
 *  drawing up until it fills it. The percentage is reported relative to that
 *  framing, so every machine is already framed at 100% and "Reset view" is the
 *  way back to it. */
export function GraphToolbar({ graphRef, zoomLabelRef, rearranged, onResetLayout }: Props) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
      <span className="unit">
        Drag a machine anywhere — it stays where you put it · drag the background to pan · scroll to
        zoom · double-click to inspect
      </span>
      <span
        ref={zoomLabelRef}
        className="unit mono"
        style={{ marginLeft: 'auto', minWidth: 38, textAlign: 'right' }}
      >
        100%
      </span>
      {rearranged ? (
        <button onClick={onResetLayout} title="Put every machine back where it started">
          Reset layout
        </button>
      ) : null}
      <button onClick={() => graphRef.current?.reset()} title="Reset pan and zoom (0)">
        Reset view
      </button>
    </div>
  )
}
