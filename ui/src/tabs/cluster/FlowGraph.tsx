import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react'
import type { DeploymentDTO, NodeStateDTO, RoutingConfig, Topology } from '../../api/types'
import { useSelection } from '../../state/selection'
import { useMetrics } from '../../state/metrics'
import { nodeLive } from '../../state/live'
import { fmt } from '../../format'
import { layoutCluster, type ClusterBox } from './layout'
import { useParticleField } from './particles'

const ZOOM_MIN = 0.4
const ZOOM_MAX = 4

export interface FlowGraphHandle {
  reset: () => void
}

interface Props {
  deployments: DeploymentDTO[]
  topology: Topology
  /** Cluster's own node rows, used only by the live overlay's stale-fallback
   *  (`nodeLive`) -- never read by the memoized scene below. */
  nodes: NodeStateDTO[]
  routing: RoutingConfig[]
  zoomLabelRef: RefObject<HTMLSpanElement>
}

/** The flow graph: a memoized, static SVG scene from `layoutCluster` that
 *  re-renders on exactly five things (topology, deployments, routing,
 *  selection, width), plus two things composited on top that must NOT force
 *  it to re-render -- the pan/zoom transform (a ref-written DOM attribute,
 *  never React state) and live telemetry (`LiveOverlay` and `ParticleField`
 *  below subscribe to the metrics stream themselves, as their own small
 *  components, specifically so a frame tick re-renders only them). */
export const FlowGraph = forwardRef<FlowGraphHandle, Props>(function FlowGraph(
  { deployments, topology, nodes, routing, zoomLabelRef },
  ref,
) {
  const { selDep, selNode, selLink, selectDep, selectNode, selectLink, openSheet } = useSelection()

  const hostRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const sceneRef = useRef<SVGGElement>(null)
  const particleLayerRef = useRef<SVGGElement>(null)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const el = hostRef.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.floor(entry.contentRect.width))
    })
    ro.observe(el)
    setWidth(Math.floor(el.getBoundingClientRect().width))
    return () => ro.disconnect()
  }, [])

  const layout = useMemo(
    () =>
      layoutCluster({
        deployments,
        nodes: topology.nodes,
        links: topology.edges,
        routing,
        selection: { selDep, selNode, selLink },
        width,
      }),
    [topology, deployments, routing, selDep, selNode, selLink, width],
  )

  // ── Pan and zoom: a ref-written transform, never React state ──────────────
  const view = useRef({ k: 1, tx: 0, ty: 0 })
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null)

  const applyView = () => {
    sceneRef.current?.setAttribute(
      'transform',
      `translate(${view.current.tx.toFixed(1)} ${view.current.ty.toFixed(1)}) scale(${view.current.k.toFixed(3)})`,
    )
    if (zoomLabelRef.current) {
      zoomLabelRef.current.textContent = `${Math.round(view.current.k * 100)}%`
    }
  }

  useImperativeHandle(ref, () => ({
    reset: () => {
      view.current = { k: 1, tx: 0, ty: 0 }
      applyView()
    },
  }))

  useEffect(() => {
    applyView()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- applyView only touches refs
  }, [layout.width, layout.height])

  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return

    const toVB = (e: { clientX: number; clientY: number }) => {
      const r = svg.getBoundingClientRect()
      const vb = svg.viewBox.baseVal
      return {
        x: ((e.clientX - r.left) / r.width) * (vb.width || layout.width),
        y: ((e.clientY - r.top) / r.height) * (vb.height || layout.height),
        sx: (vb.width || layout.width) / r.width,
        sy: (vb.height || layout.height) / r.height,
      }
    }

    const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v))
    const zoomAt = (px: number, py: number, f: number) => {
      const nk = clamp(view.current.k * f, ZOOM_MIN, ZOOM_MAX)
      const r = nk / view.current.k
      view.current.tx = px - (px - view.current.tx) * r
      view.current.ty = py - (py - view.current.ty) * r
      view.current.k = nk
      applyView()
    }

    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const p = toVB(e)
      zoomAt(p.x, p.y, Math.exp(-e.deltaY * 0.0015))
    }
    const onPointerDown = (e: PointerEvent) => {
      if ((e.target as Element).closest?.('.node,.name')) return
      drag.current = { x: e.clientX, y: e.clientY, tx: view.current.tx, ty: view.current.ty }
      svg.setPointerCapture(e.pointerId)
      svg.style.cursor = 'grabbing'
    }
    const onPointerMove = (e: PointerEvent) => {
      if (!drag.current) return
      const p = toVB(e)
      view.current.tx = drag.current.tx + (e.clientX - drag.current.x) * p.sx
      view.current.ty = drag.current.ty + (e.clientY - drag.current.y) * p.sy
      applyView()
    }
    const onPointerEnd = () => {
      drag.current = null
      svg.style.cursor = 'grab'
    }
    const onKeyDown = (e: KeyboardEvent) => {
      const step = e.shiftKey ? 60 : 20
      const vb = svg.viewBox.baseVal
      const c = { x: (vb.width || layout.width) / 2, y: (vb.height || layout.height) / 2 }
      const arrows: Record<string, [number, number]> = {
        ArrowLeft: [step, 0],
        ArrowRight: [-step, 0],
        ArrowUp: [0, step],
        ArrowDown: [0, -step],
      }
      if (arrows[e.key]) {
        e.preventDefault()
        view.current.tx += arrows[e.key]![0]
        view.current.ty += arrows[e.key]![1]
        applyView()
      } else if (e.key === '+' || e.key === '=') {
        e.preventDefault()
        zoomAt(c.x, c.y, 1.2)
      } else if (e.key === '-' || e.key === '_') {
        e.preventDefault()
        zoomAt(c.x, c.y, 1 / 1.2)
      } else if (e.key === '0') {
        e.preventDefault()
        view.current = { k: 1, tx: 0, ty: 0 }
        applyView()
      } else if (e.key === '[' || e.key === ']') {
        e.preventDefault()
        const edges = topology.edges
        if (edges.length === 0) return
        const keyOf = (a: string, b: string) => [a, b].sort().join('~')
        const cur = selLink == null ? -1 : edges.findIndex((edge) => keyOf(edge.src, edge.dst) === selLink)
        const n = edges.length
        const next = edges[(((e.key === ']' ? cur + 1 : cur - 1) % n) + n) % n]!
        selectLink(next.src, next.dst)
      }
    }

    svg.style.cursor = 'grab'
    svg.style.touchAction = 'none'
    svg.addEventListener('wheel', onWheel, { passive: false })
    svg.addEventListener('pointerdown', onPointerDown)
    svg.addEventListener('pointermove', onPointerMove)
    svg.addEventListener('pointerup', onPointerEnd)
    svg.addEventListener('pointercancel', onPointerEnd)
    svg.addEventListener('keydown', onKeyDown)
    return () => {
      svg.removeEventListener('wheel', onWheel)
      svg.removeEventListener('pointerdown', onPointerDown)
      svg.removeEventListener('pointermove', onPointerMove)
      svg.removeEventListener('pointerup', onPointerEnd)
      svg.removeEventListener('pointercancel', onPointerEnd)
      svg.removeEventListener('keydown', onKeyDown)
    }
  }, [layout.width, layout.height, topology.edges, selLink, selectLink])

  return (
    <div ref={hostRef} style={{ width: '100%' }}>
      {width > 0 ? (
        <svg
          ref={svgRef}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          style={{ width: '100%', display: 'block' }}
          tabIndex={0}
          aria-label="Request flow graph. Drag to pan, scroll to zoom. Arrow keys pan, plus and minus zoom, 0 resets."
        >
          <g ref={sceneRef} id="scene">
            {layout.conns.map((c) => (
              <path
                key={c.id}
                d={c.d}
                fill="none"
                stroke="var(--ink)"
                strokeWidth={c.weight}
                strokeDasharray={c.dashed ? '5 4' : undefined}
                strokeLinecap="square"
                opacity={c.opacity}
              />
            ))}
            <g ref={particleLayerRef} id="particles" />
            {/* The boundary rule sits BEFORE the labels that may plate over it --
                paint order matters here: a label with a `plate` knocks out
                whatever is already on the canvas, then draws its text on top
                of that. Rendering the rule after the labels (as SVG document
                order, not CSS z-index) would paint it back over the glyphs. */}
            <line x1={0} y1={layout.boundaryY} x2={layout.width} y2={layout.boundaryY} stroke="var(--rule)" />
            {layout.labels.map((l, i) => (
              <g key={i}>
                {l.plate ? (
                  <rect x={l.plate.x} y={l.plate.y} width={l.plate.w} height={l.plate.h} rx={2} fill="var(--panel)" />
                ) : null}
                <text x={l.x} y={l.y} fontSize={l.size ?? 11} fill="var(--ink-muted)">
                  {l.text}
                </text>
              </g>
            ))}
            {layout.boxes.map((b, i) => (
              <Box
                key={i}
                box={b}
                onSelectDep={selectDep}
                onSelectNode={selectNode}
                onSelectLink={selectLink}
                onOpenNode={(id) => openSheet({ kind: 'node', id })}
                onOpenDep={(name) => openSheet({ kind: 'dep', id: name })}
              />
            ))}
            <LiveOverlay boxes={layout.boxes} nodes={nodes} />
          </g>
          <ParticleField
            layerRef={particleLayerRef}
            deployments={deployments}
            routing={routing}
            paths={layout.paths}
          />
        </svg>
      ) : (
        <div style={{ height: 200 }} />
      )}
    </div>
  )
})

function Box({
  box,
  onSelectDep,
  onSelectNode,
  onSelectLink,
  onOpenNode,
  onOpenDep,
}: {
  box: ClusterBox
  onSelectDep: (name: string) => void
  onSelectNode: (id: string) => void
  onSelectLink: (a: string, b: string) => void
  onOpenNode: (id: string) => void
  onOpenDep: (name: string) => void
}) {
  if (box.kind === 'entry') {
    return (
      <g>
        <rect x={box.x} y={box.y} width={box.w} height={box.h} rx={4} fill="var(--fill)" />
        <text x={box.x + 12} y={box.y + 20} className="m" fontSize={10} fill="var(--on-fill)">
          POST /v1/chat/
        </text>
        <text x={box.x + 12} y={box.y + 35} className="m" fontSize={10} fill="var(--on-fill)">
          completions
        </text>
      </g>
    )
  }

  if (box.kind === 'name') {
    return (
      <g
        className="name"
        role="button"
        tabIndex={0}
        aria-label={`${box.servedName}, deployment`}
        onClick={() => onSelectDep(box.servedName)}
        onDoubleClick={() => onOpenDep(box.servedName)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onSelectDep(box.servedName)
          }
        }}
      >
        {box.selected ? (
          <rect
            x={box.x - 3}
            y={box.y - 3}
            width={box.w + 6}
            height={box.h + 6}
            rx={5}
            fill="none"
            stroke="var(--select-on-panel)"
            strokeWidth={2}
          />
        ) : null}
        <rect x={box.x} y={box.y} width={box.w} height={box.h} rx={3} fill="var(--fill)" />
        <text x={box.x + 11} y={box.y + box.h / 2 + 4} className="m" fontSize={10} fill="var(--on-fill)">
          {box.servedName}
        </text>
      </g>
    )
  }

  if (box.kind === 'node') {
    return (
      <g
        className="node"
        role="button"
        tabIndex={0}
        aria-label={`${box.nodeId}, node`}
        onClick={() => onSelectNode(box.nodeId)}
        onDoubleClick={() => onOpenNode(box.nodeId)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onSelectNode(box.nodeId)
          }
        }}
      >
        {box.selected ? (
          <rect
            x={box.x - 3}
            y={box.y - 3}
            width={box.w + 6}
            height={box.h + 6}
            rx={6}
            fill="none"
            stroke="var(--select-on-panel)"
            strokeWidth={2}
          />
        ) : null}
        <rect x={box.x} y={box.y} width={box.w} height={box.h} rx={4} fill="var(--fill)" />
        <text x={box.x + 11} y={box.y + 15} className="m" fontSize={9} fill="var(--on-fill)">
          {box.nodeId}
        </text>
      </g>
    )
  }

  if (box.kind === 'bracket') {
    const barY = box.y + 5
    return (
      <g
        className="name"
        role="button"
        tabIndex={0}
        aria-label={box.measured ? `Link ${box.note}, ${box.caption}` : `Link ${box.note}, never measured`}
        onClick={() => {
          const [a, b] = box.linkKey.split('~') as [string, string]
          onSelectLink(a, b)
        }}
      >
        <rect
          x={box.x}
          y={barY}
          width={box.w}
          height={7}
          fill="var(--ink)"
          opacity={0.16}
          stroke={box.measured ? undefined : 'var(--ink)'}
          strokeWidth={box.measured ? undefined : 1}
          strokeDasharray={box.measured ? undefined : '3 3'}
        />
        {box.fraction > 0 ? (
          <rect x={box.x} y={barY} width={box.w * box.fraction} height={7} fill="var(--ink)" />
        ) : null}
        <CenteredLabel cx={box.x + box.w / 2} y={box.y - 8} text={box.caption} />
      </g>
    )
  }

  if (box.kind === 'provider') {
    return (
      <g>
        <rect
          x={box.x + 0.5}
          y={box.y + 0.5}
          width={box.w - 1}
          height={box.h}
          rx={3}
          fill="none"
          stroke="var(--ink)"
          strokeWidth={1}
          opacity={box.active ? 1 : 0.55}
        />
        <text x={box.x + 11} y={box.y + 15} className="m" fontSize={9} fill={box.active ? 'var(--ink)' : 'var(--ink-muted)'}>
          {box.label}
        </text>
        <text x={box.x + 11} y={box.y + 30} className="m" fontSize={9} fill="var(--ink-muted)">
          {box.sublabel}
        </text>
      </g>
    )
  }

  return <circle cx={box.x} cy={box.y} r={box.r} fill="var(--ink)" opacity={box.opacity} />
}

/** A text label with its own knockout plate, for a caption wider than the gap
 *  it sits in -- otherwise it would sit ink-on-ink over the node boxes it
 *  crosses. */
function CenteredLabel({ cx, y, text }: { cx: number; y: number; text: string }) {
  const w = text.length * 5.4 + 10
  return (
    <>
      <rect x={cx - w / 2} y={y - 9} width={w} height={13} rx={2} fill="var(--panel)" />
      <text x={cx} y={y} className="m" fontSize={9} fill="var(--ink)" textAnchor="middle">
        {text}
      </text>
    </>
  )
}

/** The live telemetry text drawn over EVERY node box, not only a selected
 *  one -- mockups-next/js/cluster.js draws its `data-nf` line the same way
 *  ("Always, not only on tall PP boxes: the same node used to show
 *  telemetry as half a pipeline and hide it when solo") at a fixed `y+47`
 *  that fits inside every box height this layout produces (54 through 100).
 *  That is a DIFFERENT line from the mockup's selected-node-only detail
 *  block at `y+61`/`y+74` (temp/GPU/bandwidth, slot occupancy) -- this port
 *  correctly drops that second block instead, since bandwidth and per-slot
 *  occupancy are not on the wire. Its own component so a stream tick
 *  re-renders only this, never the boxes it sits on top of. */
function LiveOverlay({ boxes, nodes }: { boxes: ClusterBox[]; nodes: NodeStateDTO[] }) {
  const { frame, stale } = useMetrics()
  const nodeBoxes = boxes.filter(
    (b): b is Extract<ClusterBox, { kind: 'node' }> => b.kind === 'node',
  )
  if (nodeBoxes.length === 0) return null

  return (
    <>
      {nodeBoxes.map((b) => {
        const node = nodes.find((n) => n.profile.node_id === b.nodeId)
        if (!node) return null
        const live = nodeLive(node, frame, stale)
        const grey = stale || !live.fresh
        return (
          <text
            key={`${b.servedName}-${b.nodeId}`}
            x={b.x + 11}
            y={b.y + 47}
            className="m"
            fontSize={9}
            fill={grey ? 'var(--on-fill-dim)' : 'var(--on-fill)'}
          >
            {fmt(live.power_w, 0)} W · {fmt(live.temp_c, 0)} °C · {fmt(live.util_pct, 0)}% util
          </text>
        )
      })}
    </>
  )
}

/** The particle emitter for the selected deployment, gated on real traffic
 *  (frame tokens/sec or wire-reported outstanding requests). Its own
 *  component for the same reason as `LiveOverlay`: it subscribes to the
 *  metrics stream, and nothing else in the graph should re-render because it
 *  did. Renders nothing itself -- `useParticleField` writes into the ref'd
 *  `<g>` the parent already placed in the scene. */
function ParticleField({
  layerRef,
  deployments,
  routing,
  paths,
}: {
  layerRef: RefObject<SVGGElement>
  deployments: DeploymentDTO[]
  routing: RoutingConfig[]
  paths: ReturnType<typeof layoutCluster>['paths']
}) {
  const { selDep } = useSelection()
  const { frame, stale } = useMetrics()

  const cfg = routing.find((c) => c.served_name === selDep) ?? null
  const depId = deployments.find((d) => d.served_name === selDep)?.deployment_id
  const depFrame = frame?.deployments.find((d) => d.deployment_id === depId)
  const outstanding = cfg?.targets.reduce((a, t) => a + (t.outstanding || 0), 0) ?? 0
  const hasTraffic = !stale && ((depFrame?.tokens_per_sec ?? 0) > 0 || outstanding > 0)

  useParticleField({ layerRef, hasTraffic, servedName: selDep, cfg, paths })
  return null
}
