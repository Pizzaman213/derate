import type { RefObject } from 'react'
import type { FlowGraphHandle } from './FlowGraph'

interface Props {
  graphRef: RefObject<FlowGraphHandle>
  zoomLabelRef: RefObject<HTMLSpanElement>
}

/** The one line of chrome above the graph. The zoom percentage is written
 *  directly into `zoomLabelRef` by FlowGraph on every pan/zoom -- never React
 *  state -- so this component itself never re-renders while someone is
 *  scrolling or dragging; Reset is the only thing here that calls back in. */
export function GraphToolbar({ graphRef, zoomLabelRef }: Props) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
      <span className="unit">
        Drag to pan · scroll to zoom · click a node or a name to select · double-click to inspect
      </span>
      <span
        ref={zoomLabelRef}
        className="unit mono"
        style={{ marginLeft: 'auto', minWidth: 38, textAlign: 'right' }}
      >
        100%
      </span>
      <button onClick={() => graphRef.current?.reset()} title="Reset pan and zoom (0)">
        Reset view
      </button>
    </div>
  )
}
