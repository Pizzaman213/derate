import { createContext, useCallback, useContext } from 'react'
import {
  BaseEdge,
  EdgeLabelRenderer,
  getSmoothStepPath,
  type ConnectionLineComponentProps,
  type Edge,
  type EdgeProps,
} from '@xyflow/react'
import { Icon } from '../../components/Icon'
import { EditorContext } from './blocks/editor'
import type { Wire } from './doc'
import type { PathLike } from './particles'
import { FAULT_COLOUR, SOCKET_TYPES, WARN_COLOUR, WIRE_STEP_OFFSET, WIRE_STROKE, WIRE_WEIGHT } from './sockets'

export type WireEdgeType = Edge<{ wire: Wire }, 'wire'>

/** Where each drawn wire registers its `<path>`, so particles can walk it.
 *  A plain object the canvas owns; edges write into it as they mount. */
export const PathRegistry = createContext<Record<string, PathLike> | null>(null)

/** Square-cornered orthogonal steps (issue #5): horizontal and vertical runs,
 *  no curves and no rounded bends. One function for the drawn wire and the
 *  drag preview, so the two cannot disagree. */
export function wirePath(p: {
  sourceX: number
  sourceY: number
  sourcePosition: EdgeProps['sourcePosition']
  targetX: number
  targetY: number
  targetPosition: EdgeProps['targetPosition']
}): [path: string, labelX: number, labelY: number] {
  const [stepped, labelX, labelY] = getSmoothStepPath({ ...p, borderRadius: 0, offset: WIRE_STEP_OFFSET })
  return [squareCorners(stepped), labelX, labelY]
}

/** At `borderRadius: 0` React Flow still writes each corner as a zero-size
 *  quadratic, `L x,y Q x,y x,y`, which draws square but is a curve command all
 *  the same. The `L` before it already ends on that point, so dropping it
 *  leaves a path of `M`/`L` only -- straight segments in fact, not in effect. */
export function squareCorners(d: string): string {
  return d.replace(/Q\s*(-?[\d.e]+),(-?[\d.e]+)\s+\1,\2/g, '').replace(/\s+/g, ' ').trim()
}

/** The wire drawn while you drag from a socket, or drag a wire's end to
 *  reconnect it: the same steps as a placed wire, in React Flow's own
 *  connection-line styling. */
export function WireConnectionLine({ fromX, fromY, fromPosition, toX, toY, toPosition, connectionLineStyle }: ConnectionLineComponentProps) {
  const [path] = wirePath({
    sourceX: fromX,
    sourceY: fromY,
    sourcePosition: fromPosition,
    targetX: toX,
    targetY: toY,
    targetPosition: toPosition,
  })
  return <path d={path} fill="none" className="react-flow__connection-path" style={connectionLineStyle} />
}

/** One wire: orthogonal steps in its socket type's colour. The wires you own stand
 *  forward; the server's recede. Red when an issue names it. A measured link
 *  thickens with its GB/s against the 40 GB/s tensor-parallel threshold, and
 *  one never measured is dashed. */
export function WireEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  selected,
}: EdgeProps<WireEdgeType>) {
  const registry = useContext(PathRegistry)
  const register = useCallback(
    (el: SVGPathElement | null) => {
      if (!registry) return
      if (el) registry[id] = el
      else delete registry[id]
    },
    [registry, id],
  )
  const editor = useContext(EditorContext)
  const [path, labelX, labelY] = wirePath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition })
  if (!data) return null
  const w = data.wire
  const base = w.editable ? WIRE_STROKE.editable : WIRE_STROKE.fixed
  let width: number = base.width
  let opacity: number = base.opacity
  let dash: string | undefined
  if (w.link) {
    // A link reaches full wire weight at the 40 GB/s tensor-parallel
    // threshold, so a link as heavy as a request wire is one TP can use.
    width = w.link.measured ? 2 + (WIRE_WEIGHT - 2) * Math.min(1, (w.link.gbps ?? 0) / 40) : 2
    opacity = w.link.measured ? 0.9 : 0.45
    dash = w.link.measured ? undefined : '8 6'
  } else if (selected && w.editable) {
    opacity = WIRE_STROKE.selected.opacity
  }
  const colour = w.tone === 'fault' ? FAULT_COLOUR : w.tone === 'warn' ? WARN_COLOUR : SOCKET_TYPES[w.type].colour
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        // Only a wire you can select gets the wide hit area: a fixed wire
        // lying over an editable one must not swallow the click meant for it.
        interactionWidth={w.editable || w.link ? WIRE_WEIGHT + 8 : 0}
        style={{
          stroke: colour,
          // A selected link thickens; a selected wire you own darkens instead
          // (it is already at full weight): it is the one Delete or the × cuts.
          strokeWidth: selected && w.link ? width + 1.5 : width,
          opacity: w.tone ? 1 : opacity,
          strokeDasharray: dash,
          // Square like the mark: butt ends stay on the socket, mitred
          // corners stay corners at this weight (issue #5).
          strokeLinecap: 'butt',
          strokeLinejoin: 'miter',
        }}
      />
      {/* An invisible twin that particles walk: BaseEdge keeps its own path
          element private, and a flight has to follow exactly this path. */}
      <path ref={register} d={path} fill="none" stroke="none" />
      {w.label ? (
        <EdgeLabelRenderer>
          <span
            className={`wlabel${w.link ? ' wlabel-link' : ''}`}
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              color: w.tone === 'fault' ? 'var(--fault)' : w.tone === 'warn' ? 'var(--warn)' : undefined,
            }}
          >
            {w.label}
          </span>
        </EdgeLabelRenderer>
      ) : null}
      {selected && w.editable && editor ? (
        <EdgeLabelRenderer>
          <button
            type="button"
            className={`ghost wcut nodrag nopan${w.label ? ' wcut-below' : ''}`}
            aria-label="Cut this wire"
            title="Cut this wire"
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
            onClick={() => editor.cut(id)}
          >
            <Icon name="x-circle" size={14} decorative />
          </button>
        </EdgeLabelRenderer>
      ) : null}
    </>
  )
}
