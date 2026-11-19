import { createContext, useCallback, useContext } from 'react'
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Edge, type EdgeProps } from '@xyflow/react'
import { Icon } from '../../components/Icon'
import { EditorContext } from './blocks/editor'
import type { Wire } from './doc'
import type { PathLike } from './particles'
import { FAULT_COLOUR, SOCKET_TYPES, WARN_COLOUR, WIRE_STROKE } from './sockets'

export type WireEdgeType = Edge<{ wire: Wire }, 'wire'>

/** Where each drawn wire registers its `<path>`, so particles can walk it.
 *  A plain object the canvas owns; edges write into it as they mount. */
export const PathRegistry = createContext<Record<string, PathLike> | null>(null)

/** One wire: a bezier in its socket type's colour. The wires you own stand
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
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition })
  if (!data) return null
  const w = data.wire
  const base = w.editable ? WIRE_STROKE.editable : WIRE_STROKE.fixed
  let width: number = base.width
  let opacity: number = base.opacity
  let dash: string | undefined
  if (w.link) {
    width = w.link.measured ? 1.2 + 4.5 * Math.min(1, (w.link.gbps ?? 0) / 40) : 1
    opacity = w.link.measured ? 0.9 : 0.45
    dash = w.link.measured ? undefined : '5 4'
  }
  const colour = w.tone === 'fault' ? FAULT_COLOUR : w.tone === 'warn' ? WARN_COLOUR : SOCKET_TYPES[w.type].colour
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        // Only a wire you can select gets the wide hit area: a fixed wire
        // lying over an editable one must not swallow the click meant for it.
        interactionWidth={w.editable || w.link ? 14 : 0}
        style={{
          stroke: colour,
          // A selected wire you own stands out fully: it is the one Delete
          // or the × will cut.
          strokeWidth: selected ? width + (w.editable ? 1.5 : 1) : width,
          opacity: w.tone || (selected && w.editable) ? 1 : opacity,
          strokeDasharray: dash,
        }}
      />
      {/* An invisible twin that particles walk: BaseEdge keeps its own path
          element private, and a flight has to follow exactly this curve. */}
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
